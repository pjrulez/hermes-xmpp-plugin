"""Call controller: Jingle Message Initiation + Jingle signaling for one slixmpp client, with
one `Conversation` per call. Host-agnostic: the Hermes plugin and the dev bots both implement
`Host`. Nothing here may raise into the host; a failing call is torn down and logged."""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Protocol
from xml.etree import ElementTree as ET

from . import turn
from .conversation import Conversation, Speech
from .jingle import wire
from .jingle.model import JINGLE, RTP, Jingle, JingleError
from .jingle.session import IllegalTransition, Session
from .media import CallMedia

JMI = "urn:xmpp:jingle-message:0"
RTP_INFO = "urn:xmpp:jingle:apps:rtp:info:1"
HINTS = "urn:xmpp:hints"
CALL_FEATURES = (
    JINGLE,
    "urn:xmpp:jingle:transports:ice-udp:1",
    "urn:xmpp:jingle:apps:rtp:1",
    "urn:xmpp:jingle:apps:dtls:0",
    "urn:xmpp:jingle:apps:rtp:audio",
    JMI,
)


class Host(Protocol):
    """What the controller needs from whoever embeds it."""

    @property
    def speech(self) -> Speech: ...

    def authorized(self, bare_jid: str) -> bool: ...

    async def ask(self, peer: str, call_id: str, text: str) -> str:
        """One agent turn for `text` said by `peer` (bare JID) on call `call_id`."""
        ...


@dataclass(frozen=True)
class CallConfig:
    max_seconds: int = 600
    greeting: str = "Hi, I'm here. What can I do for you?"
    filler_after: float = 3.0
    max_calls: int = 1
    ring_timeout: int = 45  # outgoing calls: seconds to wait for an answer


class Call:
    def __init__(self, peer: str, session: Session, media: CallMedia, convo: Conversation) -> None:
        self.peer, self.session, self.media, self.convo = peer, session, media, convo
        self.tasks: list[asyncio.Task[None]] = []
        self.accepted = asyncio.Event()  # outgoing: the peer sent session-accept


class Ringing:
    """An outgoing call proposed and not yet answered."""

    def __init__(self, peer: str) -> None:
        self.peer = peer
        self.answer: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()


class CallController:
    def __init__(self, client: Any, host: Host, config: CallConfig | None = None) -> None:
        self.client, self.host, self.config = client, host, config or CallConfig()
        self.calls: dict[str, Call] = {}
        self._ringing: dict[str, Ringing] = {}

    # ---- wiring -------------------------------------------------------------------------

    def install(self) -> None:
        """Register stanza handlers. Call once, before the client connects."""
        from slixmpp.xmlstream.handler import CoroutineCallback
        from slixmpp.xmlstream.matcher import MatchXPath

        ns = self.client.default_ns
        for verb in ("propose", "retract", "proceed", "reject"):
            path = f"{{{ns}}}message/{{{JMI}}}{verb}"
            self.client.register_handler(
                CoroutineCallback(f"calls-{verb}", MatchXPath(path), self._on_jmi)
            )
        path = f"{{{ns}}}iq/{{{JINGLE}}}jingle"
        self.client.register_handler(
            CoroutineCallback("calls-jingle", MatchXPath(path), self._on_jingle)
        )

    async def advertise(self) -> None:
        """Publish call features. After session bind: slixmpp keys features by the current JID."""
        disco = self.client.plugin["xep_0030"]
        for feature in CALL_FEATURES:
            disco.add_feature(feature)
        if "xep_0115" in self.client.plugin:
            await self.client.plugin["xep_0115"].update_caps()

    async def close(self) -> None:
        for sid in list(self.calls):
            await self.end(sid, "success", notify=True)

    # ---- Jingle Message Initiation --------------------------------------------------------

    async def _on_jmi(self, stanza: Any) -> None:
        try:
            child = next((c for c in stanza.xml if c.tag.startswith(f"{{{JMI}}}")), None)
            peer = stanza["from"]
            if child is None or not self.host.authorized(peer.bare):
                return
            verb, proposal_id = child.tag.rsplit("}", 1)[1], child.get("id", "")
            if verb == "propose":
                self._answer_proposal(peer, proposal_id)
            elif verb in ("proceed", "reject"):
                self._callee_replied(peer, verb, proposal_id)
        except Exception:
            logging.exception("calls: JMI handling failed")

    def _answer_proposal(self, peer: Any, proposal_id: str) -> None:
        if self._busy():
            self._jmi(peer.bare, "reject", proposal_id)
            return
        # accept to ourselves stops our other devices ringing; proceed tells the caller to offer.
        self._jmi(self.client.boundjid.bare, "accept", proposal_id)
        self._jmi(peer, "proceed", proposal_id)

    def _callee_replied(self, peer: Any, verb: str, proposal_id: str) -> None:
        ringing = self._ringing.get(proposal_id)
        if ringing is None or ringing.peer != peer.bare or ringing.answer.done():
            return
        ringing.answer.set_result(str(peer) if verb == "proceed" else None)

    def _busy(self) -> bool:
        return len(self.calls) + len(self._ringing) >= self.config.max_calls

    def _jmi(self, to: Any, verb: str, proposal_id: str, *, propose: bool = False) -> None:
        message = self.client.make_message(mto=to, mtype="chat")
        element = ET.SubElement(message.xml, f"{{{JMI}}}{verb}", id=proposal_id)
        if propose:
            ET.SubElement(element, f"{{{RTP}}}description", media="audio")
            ET.SubElement(message.xml, f"{{{HINTS}}}store")  # offline devices still get the call
        message.send()

    # ---- Jingle ------------------------------------------------------------------------------

    async def _on_jingle(self, stanza: Any) -> None:
        element = stanza.xml.find(f"{{{JINGLE}}}jingle")
        if (
            element is None
            or stanza["type"] != "set"
            or not self.host.authorized(stanza["from"].bare)
        ):
            return
        stanza.reply().send()
        sid = element.get("sid", "")
        try:
            await self.handle(str(stanza["from"]), wire.parse(element))
        except JingleError as error:
            logging.warning("calls: refusing call %s: %s", sid, error)
            await self.end(sid, error.reason, notify=str(stanza["from"]))
        except IllegalTransition as error:
            logging.warning("calls: illegal transition on %s: %s", sid, error)
        except Exception:
            logging.exception("calls: call %s failed", sid)
            await self.end(sid, "general-error", notify=str(stanza["from"]))

    async def handle(self, peer: str, jingle: Jingle) -> None:
        if jingle.action == "session-initiate":
            await self._incoming(peer, jingle)
            return
        call = self.calls.get(jingle.sid)
        if call is None:
            return
        if jingle.action == "session-info":
            return self._session_info(call, jingle)
        call.session.apply(jingle.action, sent_by_us=False)
        if jingle.action == "session-accept":
            await call.media.accept(jingle)
            call.accepted.set()
        elif jingle.action == "transport-info":
            for content in jingle.contents:
                for candidate in content.transport.candidates if content.transport else ():
                    await call.media.add_candidate(candidate)
        elif jingle.action == "session-terminate":
            await self.end(jingle.sid, None)

    def _session_info(self, call: Call, jingle: Jingle) -> None:
        if jingle.info == "mute":
            call.convo.caller_muted()

    async def _incoming(self, peer: str, jingle: Jingle) -> None:
        if self._busy():
            await self.end(jingle.sid, "busy", notify=peer)
            return
        session = Session(jingle.sid, we_initiated=False)
        session.apply("session-initiate", sent_by_us=False)
        bare = peer.split("/", 1)[0]
        convo = Conversation(
            self.host.speech,
            lambda text: self.host.ask(bare, jingle.sid, text),
            lambda: self.end(jingle.sid, "success", notify=True),
            filler_after=self.config.filler_after,
        )
        media = CallMedia(await turn.fetch(self.client), make_outgoing=convo.attach)
        call = self.calls[jingle.sid] = Call(peer, session, media, convo)
        content = await media.answer(jingle)
        session.apply("session-accept", sent_by_us=True)
        accept = Jingle("session-accept", jingle.sid, contents=(content,), group=jingle.group)
        self.client.make_iq_set(sub=wire.build(accept), ito=peer).send()
        call.tasks += [
            asyncio.create_task(self._limit(jingle.sid)),
            asyncio.create_task(self._greet(call)),
        ]

    # ---- outgoing calls ------------------------------------------------------------------

    async def place_call(self, peer: str, opening: str) -> str:
        """Call `peer` (bare JID) and, when answered, say `opening`. Returns a status sentence.

        Never raises. The conversation then runs on its own; this returns once the call is up
        or has failed, so an agent tool can report the outcome.
        """
        if not self.host.authorized(peer):
            return f"refused: {peer} is not allowed to be called"
        if self._busy():
            return "busy: another call is in progress"
        proposal = str(uuid.uuid4())
        ringing = self._ringing[proposal] = Ringing(peer)
        try:
            self._jmi(peer, "propose", proposal, propose=True)
            try:
                full = await asyncio.wait_for(ringing.answer, self.config.ring_timeout)
            except TimeoutError:
                self._jmi(peer, "retract", proposal)
                return "no answer: nobody picked up in time"
            if full is None:
                return "declined: the person rejected the call"
            return await self._offer(full, proposal, opening)
        except Exception as error:
            logging.exception("calls: outgoing call to %s failed", peer)
            await self.end(proposal, "general-error", notify=True)
            return f"failed: {error}"
        finally:
            self._ringing.pop(proposal, None)

    async def _offer(self, full: str, sid: str, opening: str) -> str:
        session = Session(sid, we_initiated=True)
        convo = Conversation(
            self.host.speech,
            lambda text: self.host.ask(full.split("/", 1)[0], sid, text),
            lambda: self.end(sid, "success", notify=True),
            filler_after=self.config.filler_after,
        )
        convo.remember(opening)
        media = CallMedia(await turn.fetch(self.client), make_outgoing=convo.attach)
        call = self.calls[sid] = Call(full, session, media, convo)
        call.tasks.append(asyncio.create_task(convo.prepare(opening)))  # ready before they answer
        content = await media.offer()
        session.apply("session-initiate", sent_by_us=True)
        initiate = Jingle(
            "session-initiate",
            sid,
            initiator=str(self.client.boundjid),
            contents=(content,),
            group=(content.name,),
        )
        self.client.make_iq_set(sub=wire.build(initiate), ito=full).send()
        try:
            await asyncio.wait_for(call.accepted.wait(), self.config.ring_timeout)
        except TimeoutError:
            await self.end(sid, "timeout", notify=True)
            return "no answer: the device never accepted the call"
        try:
            await asyncio.wait_for(media.connected.wait(), self.config.ring_timeout)
        except TimeoutError:
            await self.end(sid, "connectivity-error", notify=True)
            return "failed: the call was accepted but audio never connected"
        call.tasks.append(asyncio.create_task(self._limit(sid)))
        call.tasks.append(asyncio.create_task(convo.say(opening)))
        return "connected: the call is up and the opening is being spoken"

    async def _greet(self, call: Call) -> None:
        await call.media.connected.wait()
        if self.config.greeting:
            await call.convo.say(self.config.greeting)

    async def _limit(self, sid: str) -> None:
        await asyncio.sleep(self.config.max_seconds)
        await self.end(sid, "success", notify=True)

    async def end(self, sid: str, reason: str | None, notify: str | bool = False) -> None:
        """Close one call; with `reason`, tell the peer (`notify`: its JID, or True)."""
        call = self.calls.pop(sid, None)
        peer = notify if isinstance(notify, str) else (call.peer if call and notify else None)
        if call is not None:
            call.convo.stop()
            for task in call.tasks:
                if task is not asyncio.current_task():
                    task.cancel()
            try:
                await call.media.close()
            except Exception:
                logging.exception("calls: closing media of %s failed", sid)
        if reason and peer:
            terminate = wire.build(Jingle("session-terminate", sid, reason=reason))
            self.client.make_iq_set(sub=terminate, ito=peer).send()
