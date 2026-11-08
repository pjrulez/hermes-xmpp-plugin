"""One call's media: an aiortc peer connection that answers a Jingle offer."""

import asyncio
import logging
from collections.abc import Callable

from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaRelay
from aiortc.mediastreams import MediaStreamTrack
from aiortc.sdp import candidate_from_sdp as rtc_candidate

from .jingle.model import Candidate, Content, Jingle
from .jingle.sdp import answer_to_content, candidate_to_sdp, offer_to_sdp

Transform = Callable[[MediaStreamTrack], MediaStreamTrack]


class CallMedia:
    """`make_outgoing` maps the caller's audio track to the track we send back (default: echo)."""

    def __init__(
        self, ice_servers: list[RTCIceServer], make_outgoing: Transform | None = None
    ) -> None:
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=ice_servers))
        relay = MediaRelay()
        self._make_outgoing = make_outgoing or relay.subscribe
        self._ready = asyncio.Event()
        self._mid = ""
        self.connected = asyncio.Event()
        self.pc.on("connectionstatechange", self._on_state)

    async def _on_state(self) -> None:
        logging.info("media state %s", self.pc.connectionState)
        if self.pc.connectionState == "connected":
            self.connected.set()

    async def answer(self, offer: Jingle) -> Content:
        """Apply the offer and return the content for `session-accept`."""
        text = offer_to_sdp(offer)  # raises SecurityError / UnsupportedError on refusals
        offered = next(c for c in offer.contents if c.media == "audio")
        self._mid = offered.name
        await self.pc.setRemoteDescription(RTCSessionDescription(text, "offer"))
        for transceiver in self.pc.getTransceivers():
            self.pc.addTrack(self._make_outgoing(transceiver.receiver.track))
        await self.pc.setLocalDescription(await self.pc.createAnswer())
        self._ready.set()
        return answer_to_content(self.pc.localDescription.sdp, offered)

    async def offer(self) -> Content:
        """Outgoing call: build our offer. The peer's `session-accept` goes to `accept()`."""
        transceiver = self.pc.addTransceiver("audio", "sendrecv")
        transceiver.sender.replaceTrack(self._make_outgoing(transceiver.receiver.track))
        await self.pc.setLocalDescription(await self.pc.createOffer())
        content = answer_to_content(self.pc.localDescription.sdp, Content("initiator", "0"))
        self._mid = content.name
        return content

    async def accept(self, accepted: Jingle) -> None:
        """Apply the peer's `session-accept` to our offer."""
        text = offer_to_sdp(accepted)  # same checks: DTLS and Opus required
        self._mid = next(c.name for c in accepted.contents if c.media == "audio")
        await self.pc.setRemoteDescription(RTCSessionDescription(text, "answer"))
        self._ready.set()

    async def add_candidate(self, candidate: Candidate) -> None:
        """Trickled remote candidate. Waits for the answer; ignores RTCP component 2 (rtcp-mux)."""
        await self._ready.wait()
        if candidate.component != 1:
            return
        parsed = rtc_candidate(candidate_to_sdp(candidate))
        parsed.sdpMid, parsed.sdpMLineIndex = self._mid, 0
        await self.pc.addIceCandidate(parsed)

    async def packets(self) -> tuple[int, int]:
        """(received, sent) RTP packets so far; for logs and tests."""
        report = await self.pc.getStats()
        received = sum(
            getattr(s, "packetsReceived", 0) for s in report.values() if s.type == "inbound-rtp"
        )
        sent = sum(
            getattr(s, "packetsSent", 0) for s in report.values() if s.type == "outbound-rtp"
        )
        return received, sent

    async def close(self) -> None:
        await self.pc.close()
