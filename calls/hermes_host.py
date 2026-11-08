"""Host for the Hermes gateway: STT/TTS through Hermes' own provider functions and agent turns
through the platform adapter, so tools, memory, approvals and the configured providers apply
exactly as in a text chat. Gateway modules are imported lazily; this file is only used inside
a running Hermes."""

import asyncio
import json
import logging
import os
import tempfile
from typing import Any

import numpy as np

from .audio import STT_RATE, decode_to_out_rate, wav_bytes
from .speech import HANGUP

CALL_PROMPT = (
    "This message is spoken on a live phone call and your reply is read aloud. Answer in one to "
    "three short sentences of plain words: no markdown, lists, links or code, and no closing "
    f"notes about what you skipped or checked. If the caller says goodbye, end with {HANGUP}."
)
TURN_TIMEOUT = 180.0


class HermesSpeech:
    """Transcribe and synthesize with the providers configured in Hermes (`stt`, `tts`)."""

    async def transcribe(self, pcm: np.ndarray) -> str:
        from tools.transcription_tools import transcribe_audio  # type: ignore[import-not-found]

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "caller.wav")
            await asyncio.to_thread(self._write, path, wav_bytes(pcm, STT_RATE))
            result = await asyncio.to_thread(transcribe_audio, path, None, "gateway")
        if not result.get("success"):
            raise RuntimeError(f"STT failed: {result.get('error')}")
        return " ".join(str(result.get("transcript", "")).split())

    async def synthesize(self, text: str) -> np.ndarray:
        from tools.tts_tool import text_to_speech_tool  # type: ignore[import-not-found]

        result = json.loads(await asyncio.to_thread(text_to_speech_tool, text))
        paths = result.get("file_paths") or [result.get("file_path")]
        if not result.get("success") or not paths[0]:
            raise RuntimeError(f"TTS failed: {result.get('error')}")
        try:
            return np.concatenate([await asyncio.to_thread(decode_to_out_rate, p) for p in paths])
        finally:
            for path in paths:
                await asyncio.to_thread(self._remove, path)

    @staticmethod
    def _write(path: str, data: bytes) -> None:
        with open(path, "wb") as handle:
            handle.write(data)

    @staticmethod
    def _remove(path: str) -> None:
        try:
            os.remove(path)
        except OSError:
            logging.debug("calls: could not remove %s", path)


class _Turn:
    def __init__(self, message_id: str) -> None:
        self.message_id = message_id
        self.parts: list[str] = []
        self.done: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    def finish(self) -> None:
        if not self.done.done():
            self.done.set_result("\n".join(self.parts).strip())


class HermesCallHost:
    """Runs call turns as normal DM turns of the peer and captures the reply instead of sending it.

    Capture rule: a `send()` whose `reply_to` is the turn's own message id (or has none) while a
    turn is pending for that chat. Progress messages and anything else go to the chat as usual.
    """

    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter
        self.speech = HermesSpeech()
        self._pending: dict[str, _Turn] = {}
        self._count = 0

    def authorized(self, bare_jid: str) -> bool:
        return self.adapter._is_authorized(chat_type="dm", chat_id=bare_jid, user_jid=bare_jid)

    async def ask(self, peer: str, call_id: str, text: str) -> str:
        from gateway.platforms.base import (  # type: ignore[import-not-found]
            MessageEvent,
            MessageType,
        )

        self._count += 1
        turn = self._pending[peer] = _Turn(f"call-{call_id}-{self._count}")
        source = self.adapter.build_source(
            chat_id=peer, chat_type="dm", user_id=peer, user_name=None
        )
        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=turn.message_id,
            channel_prompt=CALL_PROMPT,
        )
        try:
            await self.adapter.handle_message(event)
            return await asyncio.wait_for(turn.done, TURN_TIMEOUT)
        finally:
            if self._pending.get(peer) is turn:
                del self._pending[peer]

    async def post_text(self, peer: str, text: str) -> None:
        await self.adapter.send(chat_id=peer, content=text)  # no turn is pending: not captured

    def capture(self, chat_id: str, content: str, reply_to: str | None) -> bool:
        """Called from the adapter's send(). True means the text was taken for speech."""
        turn = self._pending.get(chat_id)
        if turn is None or (reply_to and reply_to != turn.message_id):
            return False
        turn.parts.append(content)
        return True

    def complete(self, chat_id: str, message_id: str | None) -> None:
        """Called from the adapter's on_processing_complete: the turn's reply is whole."""
        turn = self._pending.get(chat_id)
        if turn is not None and message_id == turn.message_id:
            turn.finish()
