"""Audio plumbing for calls: endpointing (VAD), playback track, WAV helpers. No network."""

import asyncio
import fractions
import io
import time
import wave
from dataclasses import dataclass

import av
import numpy as np
from aiortc.mediastreams import MediaStreamTrack

STT_RATE = 16000
OUT_RATE = 48000
FRAME_MS = 20
STT_FRAME = STT_RATE * FRAME_MS // 1000
OUT_FRAME = OUT_RATE * FRAME_MS // 1000


@dataclass(frozen=True)
class SpeechHeld:
    """Caller has been voiced for `hold_ms`: long enough to be speech, not a click."""


@dataclass(frozen=True)
class Utterance:
    pcm: np.ndarray  # int16 mono at STT_RATE


class Endpointer:
    """Energy-based endpointing over 16 kHz mono int16.

    shortcut: energy threshold with an adaptive noise floor, not a trained VAD. Upgrade to
    Silero/WebRTC VAD if a noisy line causes false starts or cut-offs.
    """

    def __init__(
        self,
        *,
        silence_ms: int = 700,
        min_ms: int = 300,
        max_ms: int = 20000,
        hold_ms: int = 300,
        preroll_ms: int = 200,
        min_rms: float = 250.0,
    ) -> None:
        self.silence_ms, self.min_ms, self.max_ms = silence_ms, min_ms, max_ms
        self.hold_ms, self.min_rms = hold_ms, min_rms
        self._preroll = preroll_ms // FRAME_MS
        self._pending = np.zeros(0, np.int16)
        self._idle: list[np.ndarray] = []  # recent quiet frames kept as preroll
        self._frames: list[np.ndarray] = []
        self._open = False
        self._voiced = self._silence = 0
        self._held = False
        self._floor = 0.0

    def feed(self, pcm: np.ndarray) -> list[SpeechHeld | Utterance]:
        self._pending = np.concatenate([self._pending, pcm.astype(np.int16)])
        events: list[SpeechHeld | Utterance] = []
        while len(self._pending) >= STT_FRAME:
            frame, self._pending = self._pending[:STT_FRAME], self._pending[STT_FRAME:]
            events += self._frame(frame)
        return events

    def flush(self) -> list[Utterance]:
        """The caller says they stopped talking (client muted): end the open utterance now."""
        events: list[Utterance] = []
        if self._open and self._voiced >= self.min_ms:
            events.append(Utterance(np.concatenate(self._frames)))
        self._open, self._frames, self._idle = False, [], []
        return events

    def _frame(self, frame: np.ndarray) -> list[SpeechHeld | Utterance]:
        rms = float(np.sqrt(np.mean(frame.astype(np.float64) ** 2)))
        loud = rms > max(self.min_rms, 3 * self._floor)
        if not loud:
            self._floor = 0.95 * self._floor + 0.05 * rms
        events: list[SpeechHeld | Utterance] = []
        if not self._open:
            if not loud:
                self._idle = [*self._idle, frame][-self._preroll :]
                return events
            self._open, self._frames = True, [*self._idle, frame]
            self._idle, self._voiced, self._silence, self._held = [], FRAME_MS, 0, False
        else:
            self._frames.append(frame)
            if loud:
                self._voiced, self._silence = self._voiced + FRAME_MS, 0
            else:
                self._silence += FRAME_MS
        if not self._held and self._voiced >= self.hold_ms:
            self._held = True
            events.append(SpeechHeld())
        if self._silence >= self.silence_ms or len(self._frames) * FRAME_MS >= self.max_ms:
            if self._voiced >= self.min_ms:
                events.append(Utterance(np.concatenate(self._frames)))
            self._open, self._frames = False, []
        return events


class Downsampler:
    """Decoded call frames (any rate/layout) to 16 kHz mono int16 for the endpointer and STT."""

    def __init__(self) -> None:
        self._resampler = av.AudioResampler(format="s16", layout="mono", rate=STT_RATE)

    def feed(self, frame: av.AudioFrame) -> np.ndarray:
        chunks = [f.to_ndarray().reshape(-1) for f in self._resampler.resample(frame)]
        return np.concatenate(chunks) if chunks else np.zeros(0, np.int16)


def wav_bytes(pcm: np.ndarray, rate: int = STT_RATE) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(pcm.astype(np.int16).tobytes())
    return out.getvalue()


def decode_to_out_rate(path: str) -> np.ndarray:
    """Any audio file to 48 kHz mono int16 (what PlaybackTrack plays)."""
    resampler = av.AudioResampler(format="s16", layout="mono", rate=OUT_RATE)
    chunks: list[np.ndarray] = []
    with av.open(path) as container:
        for frame in container.decode(audio=0):
            chunks += [f.to_ndarray().reshape(-1) for f in resampler.resample(frame)]
    chunks += [f.to_ndarray().reshape(-1) for f in resampler.resample(None)]
    return np.concatenate(chunks) if chunks else np.zeros(0, np.int16)


class PlaybackTrack(MediaStreamTrack):
    """What the caller hears: queued PCM, silence when empty, paced at real time."""

    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self._buf = np.zeros(0, np.int16)
        self._start: float | None = None
        self._n = 0
        self.played = 0  # samples of real audio sent so far

    def play(self, pcm: np.ndarray) -> None:
        self._buf = np.concatenate([self._buf, pcm])

    def flush(self) -> None:
        self._buf = self._buf[:0]

    @property
    def busy(self) -> bool:
        return len(self._buf) > 0

    async def wait_idle(self) -> None:
        while self.busy:  # noqa: ASYNC110  recv() drains the buffer, no event to wait on
            await asyncio.sleep(FRAME_MS / 1000)

    async def recv(self) -> av.AudioFrame:
        if self._start is None:
            self._start = time.monotonic()
        await asyncio.sleep(max(0.0, self._start + self._n * FRAME_MS / 1000 - time.monotonic()))
        chunk, self._buf = self._buf[:OUT_FRAME], self._buf[OUT_FRAME:]
        self.played += len(chunk)
        frame = av.AudioFrame.from_ndarray(
            np.pad(chunk, (0, OUT_FRAME - len(chunk))).reshape(1, -1), format="s16", layout="mono"
        )
        frame.sample_rate, frame.pts = OUT_RATE, self._n * OUT_FRAME
        frame.time_base = fractions.Fraction(1, OUT_RATE)
        self._n += 1
        return frame
