"""Jingle audio content to SDP and back.

Scope is one audio m-line, Opus only, DTLS-SRTP only, rtcp-mux. The legacy SDES
`<encryption>` block, RTCP component 2 candidates and every codec but Opus are dropped
(Dino offers speex/PCMU/PCMA and SDES; Conversations offers red and telephone-event).
"""

from .model import (
    Candidate,
    Content,
    Fingerprint,
    Jingle,
    JingleError,
    PayloadType,
    SecurityError,
    Transport,
    UnsupportedError,
)


def offer_to_sdp(offer: Jingle) -> str:
    """Build the SDP offer to hand to the WebRTC stack from a Jingle `session-initiate`."""
    content = next((c for c in offer.contents if c.media == "audio"), None)
    if content is None:
        raise UnsupportedError("no audio content in offer")
    transport = content.transport
    if transport is None or transport.fingerprint is None:
        raise SecurityError("offer has no DTLS fingerprint; unencrypted calls are refused")
    opus = next((p for p in content.payloads if p.name.lower() == "opus"), None)
    if opus is None:
        raise UnsupportedError("offer has no Opus payload")
    lines = ["v=0", "o=- 1 2 IN IP4 127.0.0.1", "s=-", "t=0 0"]
    if offer.group:
        lines.append("a=group:BUNDLE " + " ".join(offer.group))
    lines += [
        f"m=audio 9 UDP/TLS/RTP/SAVPF {opus.id}",
        "c=IN IP4 0.0.0.0",
        "a=rtcp:9 IN IP4 0.0.0.0",
        f"a=ice-ufrag:{transport.ufrag}",
        f"a=ice-pwd:{transport.pwd}",
        f"a=fingerprint:{transport.fingerprint.hash} {transport.fingerprint.value}",
        f"a=setup:{transport.fingerprint.setup}",
        f"a=mid:{content.name}",
        "a=sendrecv",
        "a=rtcp-mux",
        f"a=rtpmap:{opus.id} opus/{opus.clockrate}/{opus.channels}",
    ]
    if opus.parameters:
        lines.append(f"a=fmtp:{opus.id} " + ";".join(f"{k}={v}" for k, v in opus.parameters))
    lines += [
        f"a=candidate:{candidate_to_sdp(c)}" for c in transport.candidates if c.component == 1
    ]
    return "\r\n".join(lines) + "\r\n"


def candidate_to_sdp(c: Candidate) -> str:
    """The value after `a=candidate:` (also the string for a trickled candidate)."""
    line = f"{c.foundation} {c.component} {c.protocol} {c.priority} {c.ip} {c.port} typ {c.type}"
    if c.rel_addr is not None and c.rel_port is not None:
        line += f" raddr {c.rel_addr} rport {c.rel_port}"
    return line


def candidate_from_sdp(value: str, generation: int = 0) -> Candidate:
    """Parse `foundation component proto priority ip port typ type [raddr x rport y]`."""
    try:
        return _candidate_from_sdp(value, generation)
    except (ValueError, IndexError) as error:
        raise JingleError(f"bad candidate: {value[:80]!r}") from error


def _candidate_from_sdp(value: str, generation: int) -> Candidate:
    parts = value.removeprefix("candidate:").split()
    if len(parts) < 8 or parts[6] != "typ":
        raise JingleError(f"bad candidate: {value[:80]!r}")
    extra = dict(zip(parts[8::2], parts[9::2], strict=False))
    return Candidate(
        foundation=parts[0],
        component=int(parts[1]),
        protocol=parts[2].lower(),
        priority=int(parts[3]),
        ip=parts[4],
        port=int(parts[5]),
        type=parts[7],
        generation=generation,
        id=f"{parts[0]}-{parts[1]}-{parts[4]}-{parts[5]}",
        rel_addr=extra.get("raddr"),
        rel_port=int(extra["rport"]) if "rport" in extra else None,
    )


def answer_to_content(sdp: str, offered: Content) -> Content:
    """Turn the stack's SDP answer into the Jingle content for `session-accept`.

    Mirrors the offer's `name`/`creator` so the content matches (Conversations uses name "0").
    """
    attrs: dict[str, str] = {}
    candidates: list[Candidate] = []
    opus: PayloadType | None = None
    fmtp: dict[int, tuple[tuple[str, str], ...]] = {}
    rtpmap: dict[int, tuple[str, int, int]] = {}
    for raw in sdp.replace("\r\n", "\n").split("\n"):
        if not raw.startswith("a="):
            continue
        key, _, value = raw[2:].partition(":")
        if key == "candidate":
            c = candidate_from_sdp(value)
            if c.component == 1:
                candidates.append(c)
        elif key == "rtpmap":
            number, _, spec = value.partition(" ")
            name, _, rest = spec.partition("/")
            rate, _, channels = rest.partition("/")
            rtpmap[int(number)] = (name, int(rate), int(channels or 1))
        elif key == "fmtp":
            number, _, spec = value.partition(" ")
            fmtp[int(number)] = tuple(
                (k, v) for k, _, v in (item.strip().partition("=") for item in spec.split(";")) if k
            )
        else:
            attrs.setdefault(key, value)
    for number, (name, rate, channels) in rtpmap.items():
        if name.lower() == "opus":
            opus = PayloadType(number, "opus", rate, channels, fmtp.get(number, ()))
    if opus is None:
        raise UnsupportedError("answer has no Opus payload")
    fingerprint_hash, _, fingerprint_value = attrs.get("fingerprint", "").partition(" ")
    if not fingerprint_value:
        raise SecurityError("answer has no DTLS fingerprint")
    return Content(
        creator=offered.creator,
        name=offered.name,
        senders="both",
        media="audio",
        payloads=(opus,),
        rtcp_mux=True,
        transport=Transport(
            ufrag=attrs.get("ice-ufrag", ""),
            pwd=attrs.get("ice-pwd", ""),
            fingerprint=Fingerprint(
                fingerprint_hash, attrs.get("setup", "active"), fingerprint_value
            ),
            candidates=tuple(candidates),
        ),
    )
