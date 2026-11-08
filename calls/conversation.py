"""The call loop: caller audio -> endpointing -> STT -> agent -> sentence TTS -> playback,
with barge-in. Providers are injected, so the loop is testable without audio models."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Protocol

import numpy as np
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack

from .audio import (
    STT_RATE,
    Downsampler,
    Endpointer,
    PlaybackTrack,
    SpeechHeld,
    Utterance,
    ack_tone,
)
from .speech import sentences, split_hangup

OOPS = "Sorry, something went wrong. Please say that again."
FILLER = "One moment."
GAP_S = 0.1  # no RTP for this long: the clock, not packets, advances endpointing


class Speech(Protocol):
    async def transcribe(self, pcm: np.ndarray) -> str: ...
    async def synthesize(self, text: str) -> np.ndarray: ...


class Turn:
    """What was said in the current reply and how much of it has been queued as audio."""

    def __init__(self, parts: list[str], start: int) -> None:
        self.parts, self.start = parts, start
        self.samples: list[int] = []  # audio length per synthesized part, in order

    def heard_fraction(self, played: int) -> float:
        remaining, heard = played - self.start, 0.0
        for text, length in zip(self.parts, self.samples, strict=False):
            heard += len(text) * min(1.0, max(0.0, remaining / max(length, 1)))
            remaining -= length
        return heard / max(sum(len(p) for p in self.parts), 1)


class Conversation:
    def __init__(
        self,
        speech: Speech,
        respond: Callable[[str], Awaitable[str]],
        on_hangup: Callable[[], Awaitable[None]],
        filler_after: float = 3.0,
        endpointer: Endpointer | None = None,
        ack_tone_enabled: bool = True,
        fallback_text: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.speech, self.respond, self.on_hangup = speech, respond, on_hangup
        self.fallback_text = fallback_text  # gets the reply as text when speaking it fails
        self._ack = ack_tone() if ack_tone_enabled else None
        self.filler_after = filler_after  # seconds of waiting for the agent before "One moment."
        self._filler_audio: np.ndarray | None = None
        self._cache: dict[
            str, asyncio.Future[np.ndarray]
        ] = {}  # sentence -> audio being made ahead
        self._protected = False  # speaking something the caller must not interrupt
        self._endpointer = endpointer or Endpointer()
        self._turn_pcm: np.ndarray | None = None  # caller audio of a turn with no reply yet
        self.playback = PlaybackTrack()
        self._task: asyncio.Task[None] | None = None  # the running turn
        self._listener: asyncio.Task[None] | None = None
        self._turn: Turn | None = None
        self._note = ""

    def attach(self, incoming: MediaStreamTrack) -> MediaStreamTrack:
        """CallMedia hook: start listening to the caller, return the track we speak on."""
        self._listener = asyncio.create_task(self._listen(incoming))
        return self.playback

    def prepare(self, text: str) -> None:
        """Start synthesizing `text` now (e.g. while the phone rings) so `say()` has no gap."""
        for part in sentences(text):
            if part not in self._cache:
                self._cache[part] = asyncio.ensure_future(self.speech.synthesize(part))

    def remember(self, spoken: str) -> None:
        """Tell the agent, on its next turn, what it said before the caller spoke."""
        self._note = f"[You placed this call and began by saying: {spoken}] "

    async def say(self, text: str, *, protected: bool = False) -> None:
        """Speak without a turn (greeting, outgoing-call opening). With `protected`, the caller
        cannot interrupt: their speech is ignored until `text` has been played in full."""
        self._protected = protected
        try:
            await self._speak(Turn(sentences(text), self.playback.played))
            if protected:
                await self.playback.wait_idle()
        finally:
            if protected:
                self._protected = False
                self._endpointer.reset()  # whatever they said meanwhile was not for us

    def caller_muted(self) -> None:
        """Jingle `mute` from the caller is a "stopped talking" signal: do not wait for silence."""
        if self._protected:
            return
        for utterance in self._endpointer.flush():
            self._start_turn(utterance.pcm)

    def stop(self) -> None:
        for task in (self._listener, self._task, *self._cache.values()):
            if task and task is not asyncio.current_task():  # hang-up runs inside the turn task
                task.cancel()

    async def _listen(self, track: MediaStreamTrack) -> None:
        down, endpointer = Downsampler(), self._endpointer
        pending = asyncio.ensure_future(track.recv())
        try:
            while True:
                done, _ = await asyncio.wait({pending}, timeout=GAP_S)
                if done:
                    pcm = down.feed(pending.result())  # type: ignore[arg-type]
                    pending = asyncio.ensure_future(track.recv())
                else:
                    # Clients stop sending RTP when muted; feed silence so utterances still end.
                    pcm = np.zeros(int(STT_RATE * GAP_S), np.int16)
                if self._protected:
                    continue  # speaking a briefing: the caller's audio is read and dropped
                for event in endpointer.feed(pcm):
                    if isinstance(event, SpeechHeld):
                        self._barge_in()
                    elif isinstance(event, Utterance):
                        logging.info("utterance %.1fs", len(event.pcm) / STT_RATE)
                        if (
                            self._ack is not None
                        ):  # "I heard you finish": tells the caller silence was detected
                            self.playback.play(self._ack)
                        self._start_turn(event.pcm)
        except MediaStreamError:
            return
        except Exception:
            logging.exception("call listener crashed")
        finally:
            pending.cancel()

    def _barge_in(self) -> None:
        """Caller spoke over us: stop audio, drop pending synthesis, remember what was heard."""
        if not (self.playback.busy or (self._task and not self._task.done())):
            return
        if self._turn and self.playback.busy:
            percent = round(self._turn.heard_fraction(self.playback.played) * 10) * 10
            self._note = (
                f"[The caller interrupted; they heard about {percent}% of your last reply.] "
            )
        self.playback.flush()
        if self._task:
            self._task.cancel()

    def _start_turn(self, pcm: np.ndarray) -> None:
        """Run a turn for `pcm`. If the previous utterance got no reply yet (the caller only
        paused mid-thought), it is replaced by both utterances joined, not forgotten."""
        if self._turn_pcm is not None:
            pcm = np.concatenate([self._turn_pcm, np.zeros(STT_RATE // 3, np.int16), pcm])
        self._turn_pcm = pcm
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = asyncio.create_task(self._run_turn(pcm))

    async def _run_turn(self, pcm: np.ndarray) -> None:
        try:
            t0 = time.monotonic()
            text = await self.speech.transcribe(pcm)
            if not text:
                self._turn_pcm = None
                return
            t1 = time.monotonic()
            logging.info("caller: %s", text)
            reply, hangup = split_hangup(await self._answer(self._note + text))
            self._note = ""
            self._turn_pcm = None  # answered: what the caller says next is a new turn
            t2 = time.monotonic()
            logging.info("agent: %s", reply)
            try:
                await self._speak(Turn(sentences(reply), self.playback.played))
            except Exception:
                logging.exception("speaking the reply failed")
                if self.fallback_text is not None:
                    await self.fallback_text(reply)
                return
            logging.info(
                "timing stt=%.1fs agent=%.1fs first-audio=%.1fs",
                t1 - t0,
                t2 - t1,
                time.monotonic() - t2,
            )
            await self.playback.wait_idle()
            if hangup:
                await self.on_hangup()
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("turn failed")
            self._turn_pcm = None
            await self._apologize()

    async def _answer(self, text: str) -> str:
        """Ask the agent; if it is slow, say a short filler so the line is not dead."""
        reply = asyncio.ensure_future(self.respond(text))
        try:
            done, _ = await asyncio.wait({reply}, timeout=self.filler_after)
            if not done:
                if self._filler_audio is None:
                    self._filler_audio = await self.speech.synthesize(FILLER)
                self.playback.play(self._filler_audio)
            return await reply
        finally:
            reply.cancel()  # no-op when finished; stops the agent call on barge-in

    async def _speak(self, turn: Turn) -> None:
        self._turn = turn
        for part in turn.parts:
            ahead = self._cache.pop(part, None)
            audio = await ahead if ahead is not None else await self.speech.synthesize(part)
            turn.samples.append(len(audio))
            self.playback.play(audio)

    async def _apologize(self) -> None:
        try:
            self.playback.play(await self.speech.synthesize(OOPS))
        except Exception:
            logging.exception("TTS also failed; staying silent")
