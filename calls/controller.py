"""Call controller: Jingle Message Initiation + Jingle signaling for one slixmpp client, with
one `Conversation` per call. Host-agnostic: the Hermes plugin and the dev bots both implement
`Host`. Nothing here may raise into the host; a failing call is torn down and logged."""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from xml.etree import ElementTree as ET

from . import turn
from .audio import Endpointer, webrtc_vad
from .conversation import Conversation, Speech
from .jingle import wire
from .jingle.model import JINGLE, RTP, Jingle, JingleError
from .jingle.session import IllegalTransition, Session
from .media import CallMedia
from .speech import clean, split_hangup

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


RECAP_PROMPT = (
    "[The call has ended. In one or two plain sentences for a chat message, say what the person "
    "asked for or wanted changed during the call and what you did about it. If they asked for "
    "nothing, reply exactly NONE.]"
)
MISSED_CALL = "I tried to call you but nobody picked up. What I wanted to say:\n\n{opening}"


class Host(Protocol):
    """What the controller needs from whoever embeds it."""

    @property
    def speech(self) -> Speech: ...

    def authorized(self, bare_jid: str) -> bool: ...

    async def ask(self, peer: str, call_id: str, text: str) -> str:
        """One agent turn for `text` said by `peer` (bare JID) on call `call_id`."""
        ...

    async def post_text(self, peer: str, text: str) -> None:
        """Send `text` to the peer's chat (a reply that could not be spoken)."""
        ...

    async def steer(self, peer: str, call_id: str, text: str) -> bool:
        """Give `text` to the agent turn already running for this call. False if not possible."""
        ...

    async def name_call(self, peer: str, call_id: str, title: str) -> None:
        """Give the call's agent session a recognizable title. Never raises."""
        ...


@dataclass(frozen=True)
class CallConfig:
    max_seconds: int = 600
    greeting: str = "Hi, I'm here. What can I do for you?"
    filler_after: float = 3.0
    max_calls: int = 1
    ring_timeout: int = 45  # outgoing calls: seconds to wait for an answer
    silence_ms: int = 800  # this much non-speech ends the caller's turn
    vad_level: int = (
        2  # WebRTC VAD aggressiveness 0-3: higher rejects more noise, misses soft speech
    )
    ack_tone: bool = True  # short beep when an utterance is captured
    hangup_grace: float = 5.0  # warning beeps, then this long to speak before a planned hang-up
    busy_wait: float = 90.0  # outgoing calls wait this long for the current call to end
    recap: bool = True  # after a call with talk in it, text the person what was asked and done
    barge_in: str = (
        "queue"  # "queue": talk any time, it becomes the next turn; "stop": cut the agent off
    )


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
        self._background: set[asyncio.Task[None]] = set()
        self._missed: dict[str, str] = {}  # peer -> text sent instead of a call nobody answered

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
        convo = self._conversation(jingle.sid, bare)
        if bare in self._missed:
            convo.remember_missed(self._missed.pop(bare))
        self._name(jingle.sid, bare, "from")
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

    def _conversation(self, sid: str, bare: str) -> Conversation:
        config = self.config
        return Conversation(
            self.host.speech,
            lambda text: self.host.ask(bare, sid, text),
            lambda: self.end(sid, "success", notify=True),
            filler_after=config.filler_after,
            endpointer=Endpointer(
                silence_ms=config.silence_ms, is_speech=webrtc_vad(config.vad_level)
            ),
            ack_tone_enabled=config.ack_tone,
            fallback_text=lambda text: self.host.post_text(bare, text),
            barge_in=config.barge_in,
            hangup_grace=config.hangup_grace,
            steer=lambda text: self.host.steer(bare, sid, text),
        )

    def _name(self, sid: str, bare: str, direction: str, topic: str = "") -> None:
        """Title the call's session: "Call from|to <person> <YYYY-MM-DD HH:MM> [<topic>]"."""
        when = datetime.now().strftime("%Y-%m-%d %H:%M")
        title = f"Call {direction} {bare.split('@', 1)[0]} {when}" + (f" {topic}" if topic else "")
        task = asyncio.create_task(self.host.name_call(bare, sid, title))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _recap(self, bare: str, sid: str) -> None:
        """After the call: ask the call's own agent session what was asked, and text the answer."""
        try:
            reply = clean(split_hangup(await self.host.ask(bare, sid, RECAP_PROMPT))[0]).strip()
            if reply and reply.strip(" .").upper() != "NONE":
                await self.host.post_text(bare, f"Call recap: {reply}")
        except Exception:
            logging.exception("calls: the call recap failed")

    async def _missed_call(self, peer: str, opening: str) -> None:
        """Nobody answered: send the message as text and remember it for a call back."""
        bare = peer.split("/", 1)[0]
        self._missed[bare] = opening
        try:
            await self.host.post_text(bare, MISSED_CALL.format(opening=opening))
        except Exception:
            logging.exception("calls: sending the missed-call text failed")

    # ---- outgoing calls ------------------------------------------------------------------

    async def place_call(
        self,
        peer: str,
        opening: str,
        *,
        uninterruptible: bool = False,
        hang_up_after: bool = False,
        topic: str = "",
        context: str = "",
    ) -> str:
        """Call `peer` (bare JID) and, when answered, say `opening`. Returns a status sentence.

        `uninterruptible`: the opening is delivered whole whatever the person says meanwhile.
        `hang_up_after`: end the call when the opening is done instead of taking questions.

        Never raises. The conversation then runs on its own; this returns once the call is up
        or has failed, so an agent tool can report the outcome.
        """
        if not self.host.authorized(peer):
            return f"refused: {peer} is not allowed to be called"
        deadline = asyncio.get_running_loop().time() + self.config.busy_wait
        # A result can arrive just as the previous call ends: queue behind it instead of failing.
        while self._busy() and asyncio.get_running_loop().time() < deadline:  # noqa: ASYNC110
            await asyncio.sleep(0.25)
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
                await self._missed_call(peer, opening)
                return "no answer: nobody picked up in time; the message was sent as text instead"
            if full is None:
                await self._missed_call(peer, opening)
                return (
                    "declined: the person rejected the call; the message was sent as text instead"
                )
            return await self._offer(
                full, proposal, opening, uninterruptible, hang_up_after, topic, context
            )
        except Exception as error:
            logging.exception("calls: outgoing call to %s failed", peer)
            await self.end(proposal, "general-error", notify=True)
            return f"failed: {error}"
        finally:
            self._ringing.pop(proposal, None)

    async def _offer(
        self,
        full: str,
        sid: str,
        opening: str,
        uninterruptible: bool,
        hang_up_after: bool,
        topic: str = "",
        context: str = "",
    ) -> str:
        session = Session(sid, we_initiated=True)
        convo = self._conversation(sid, full.split("/", 1)[0])
        convo.remember(opening, context)
        media = CallMedia(await turn.fetch(self.client), make_outgoing=convo.attach)
        call = self.calls[sid] = Call(full, session, media, convo)
        convo.prepare(opening)  # synthesized while the phone rings
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
            await self._missed_call(full, opening)
            return "no answer: the device never accepted the call; message sent as text instead"
        try:
            await asyncio.wait_for(media.connected.wait(), self.config.ring_timeout)
        except TimeoutError:
            await self.end(sid, "connectivity-error", notify=True)
            return "failed: the call was accepted but audio never connected"
        self._name(sid, full.split("/", 1)[0], "to", topic)
        call.tasks.append(asyncio.create_task(self._limit(sid)))
        call.tasks.append(
            asyncio.create_task(self._deliver(sid, convo, opening, uninterruptible, hang_up_after))
        )
        return "connected: the call is up and the opening is being spoken"

    async def _deliver(
        self,
        sid: str,
        convo: Conversation,
        opening: str,
        uninterruptible: bool,
        hang_up_after: bool,
    ) -> None:
        try:
            await convo.say(opening, protected=uninterruptible)
        except Exception:
            logging.exception("calls: delivering the opening of %s failed", sid)
            return
        if hang_up_after:
            await convo.playback.wait_idle()
            if not await convo.grace(self.config.hangup_grace):
                await self.end(sid, "success", notify=True)

    async def _greet(self, call: Call) -> None:
        await call.media.connected.wait()
        if self.config.greeting:
            await call.convo.say(self.config.greeting)

    async def _limit(self, sid: str) -> None:
        """Enforce the call time limit; meanwhile log RTP counts every 10 s (is audio flowing?)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.config.max_seconds
        while (left := deadline - loop.time()) > 0:
            await asyncio.sleep(min(10, left))
            call = self.calls.get(sid)
            if call is not None:
                logging.info("calls: %s rtp received/sent %s", sid[:8], await call.media.packets())
        await self.end(sid, "success", notify=True)

    async def end(self, sid: str, reason: str | None, notify: str | bool = False) -> None:
        """Close one call; with `reason`, tell the peer (`notify`: its JID, or True)."""
        call = self.calls.pop(sid, None)
        peer = notify if isinstance(notify, str) else (call.peer if call and notify else None)
        if call is not None:
            if self.config.recap and call.convo.turns:
                task = asyncio.create_task(self._recap(call.peer.split("/", 1)[0], sid))
                self._background.add(task)
                task.add_done_callback(self._background.discard)
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
