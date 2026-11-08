"""Audio-call glue: config, controller wiring, reply capture, Hermes-backed speech.

The call engine itself (Jingle, media, conversation) is tested in the calls library; this file
covers only what the adapter adds. Requires aiortc/av/numpy (requirements-calls.txt).

Run: python -m pytest tests/test_calls_glue.py -v
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
import wave
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("aiortc")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from conftest import evict_adapter_modules, restore_sys_modules, snapshot_sys_modules

_MODULES_SNAPSHOT = snapshot_sys_modules()
evict_adapter_modules()

for name in ("gateway", "gateway.config", "gateway.platforms", "gateway.platforms.base",
             "gateway.platforms.models", "gateway.util", "tools", "tools.clarify_gateway"):
    sys.modules[name] = MagicMock()
sys.modules["gateway.config"].Platform = type("Platform", (), {"__init__": lambda self, *a, **k: None})
sys.modules["gateway.config"].PlatformConfig = type("PlatformConfig", (), {"__init__": lambda self, *a, **k: None})
gw_base = sys.modules["gateway.platforms.base"]
gw_base.MessageEvent = MagicMock
gw_base.MessageType = type("MessageType", (), {"TEXT": "text", "VOICE": "voice"})
gw_base.ProcessingOutcome = type("ProcessingOutcome", (), {"SUCCESS": 0, "FAILURE": 1, "CANCELLED": 2})
gw_base.SendResult = type("SendResult", (), {"__init__": lambda s, **kw: s.__dict__.update(kw) or None})
gw_base.BasePlatformAdapter = type("BasePlatformAdapter", (), {
    "__init__": lambda self, *a, **k: None,
    "build_source": lambda s, **kw: types.SimpleNamespace(**kw),
    "_release_platform_lock": lambda self: None,
    "_mark_disconnected": lambda self: None,
})

import adapter  # noqa: E402

restore_sys_modules(_MODULES_SNAPSHOT)


def make_adapter(**extra):
    cfg = MagicMock()
    cfg.extra = {"jid": "hermes@example.org", "password": "test-only",
                 "allowed_users": "user@example.org", **extra}
    a = adapter.XmppAdapter(cfg)
    a._self_bare = "hermes@example.org"
    return a


# ---------------------------------------------------------------- settings

def test_calls_are_off_by_default():
    assert make_adapter()._calls_cfg["enabled"] is False


def test_settings_from_config():
    a = make_adapter(calls={"enabled": True, "max_seconds": "120", "greeting": "Hello!", "filler_after": 2})
    assert a._calls_cfg == {
        "enabled": True, "max_seconds": 120, "greeting": "Hello!", "filler_after": 2.0, "ring_timeout": 45,
        "silence_ms": 800, "vad_level": 2, "ack_tone": True, "barge_in": "queue",
    }


def test_vad_level_is_clamped_and_ack_tone_can_be_off():
    cfg = adapter._calls_settings({"calls": {"vad_level": 9, "ack_tone": False, "silence_ms": 1200}})
    assert (cfg["vad_level"], cfg["ack_tone"], cfg["silence_ms"]) == (3, False, 1200)


def test_bad_numbers_fall_back():
    cfg = adapter._calls_settings({"calls": {"max_seconds": "soon", "filler_after": None}})
    assert (cfg["max_seconds"], cfg["filler_after"]) == (600, 3.0)


def test_env_enables_calls(monkeypatch):
    monkeypatch.setenv("XMPP_CALLS_ENABLED", "true")
    assert adapter._calls_settings({})["enabled"] is True


def test_yaml_passes_calls_block_through():
    extra = adapter._apply_yaml_config({}, {"jid": "a@b", "password": "x", "calls": {"enabled": True}})
    assert extra["calls"] == {"enabled": True}


# ---------------------------------------------------------------- wiring

def test_disabled_builds_no_controller():
    a = make_adapter()
    assert a._build_calls(MagicMock()) is None
    assert a._calls_host is None


def test_enabled_builds_controller_and_installs_handlers():
    a = make_adapter(calls={"enabled": True})
    client = MagicMock()
    controller = a._build_calls(client)
    assert controller is not None
    assert a._calls_host is not None
    assert client.register_handler.call_count == 5  # propose, retract, proceed, reject, jingle
    client.register_plugin.assert_called_with("xep_0115")


def test_missing_dependencies_keep_chat_alive(monkeypatch):
    monkeypatch.setitem(sys.modules, "calls.hermes_host", None)  # import raises ImportError
    a = make_adapter(calls={"enabled": True})
    assert a._build_calls(MagicMock()) is None
    assert a._calls_host is None


def test_authorization_follows_the_allow_list():
    a = make_adapter(calls={"enabled": True})
    a._build_calls(MagicMock())
    assert a._calls_host.authorized("user@example.org")
    assert not a._calls_host.authorized("stranger@example.org")


# ---------------------------------------------------------------- reply capture

def test_send_is_captured_only_for_the_pending_call_turn():
    a = make_adapter(calls={"enabled": True})
    a._build_calls(MagicMock())
    a.client = MagicMock()

    async def scenario():
        host = a._calls_host
        turn = host._pending["user@example.org"] = type("T", (), {})()
        turn.message_id, turn.parts = "call-1-1", []
        taken = await a.send("user@example.org/phone", "Fine, thanks.", reply_to="call-1-1")
        other = host.capture("user@example.org", "unrelated", "some-other-id")
        return taken, other, turn.parts

    taken, other, parts = asyncio.run(scenario())
    assert taken.success is True
    assert other is False
    assert parts == ["Fine, thanks."]


def test_processing_complete_resolves_the_turn(monkeypatch):
    a = make_adapter(calls={"enabled": True})
    a._build_calls(MagicMock())
    for key, module in (("gateway", MagicMock()), ("gateway.platforms", MagicMock()),
                        ("gateway.platforms.base", gw_base)):
        monkeypatch.setitem(sys.modules, key, module)  # hermes_host imports these lazily

    async def scenario():
        host = a._calls_host
        seen = {}

        async def fake_handle(event):
            seen["event"] = event
            host.capture("user@example.org", "First part.", event.message_id)
            host.capture("user@example.org", "Second part.", event.message_id)
            await a.on_processing_complete(event, 0)

        a.handle_message = fake_handle
        a._reactions_allowed = lambda event: False
        reply = await host.ask("user@example.org", "sid", "what time is it")
        return reply, seen["event"]

    reply, event = asyncio.run(scenario())
    assert reply == "First part.\nSecond part."
    assert event.message_id.startswith("call-sid-")
    assert "phone call" in event.channel_prompt


def test_disconnect_hangs_up_calls():
    a = make_adapter(calls={"enabled": True})
    closed = []

    async def close():
        closed.append(True)

    a._calls = types.SimpleNamespace(close=close)
    a.client = None
    asyncio.run(a.disconnect())
    assert closed == [True]
    assert a._calls is None


# ---------------------------------------------------------------- Hermes-backed speech

def test_speech_uses_hermes_stt_and_tts(tmp_path, monkeypatch):
    import numpy as np
    from calls.hermes_host import HermesSpeech

    wav_path = tmp_path / "speech.wav"
    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(np.zeros(24000, np.int16).tobytes())

    tools = types.ModuleType("tools")
    stt = types.ModuleType("tools.transcription_tools")
    stt.transcribe_audio = lambda path, model=None, source=None: {"success": True, "transcript": " hello  there "}
    tts = types.ModuleType("tools.tts_tool")
    tts.text_to_speech_tool = lambda text: json.dumps({"success": True, "file_path": str(wav_path)})
    monkeypatch.setitem(sys.modules, "tools", tools)
    monkeypatch.setitem(sys.modules, "tools.transcription_tools", stt)
    monkeypatch.setitem(sys.modules, "tools.tts_tool", tts)

    speech = HermesSpeech()
    assert asyncio.run(speech.transcribe(np.zeros(16000, np.int16))) == "hello there"
    audio = asyncio.run(speech.synthesize("Hi."))
    assert 47000 <= len(audio) <= 49000  # 1 s at 48 kHz
    assert not wav_path.exists()  # cleaned up after decoding


# ---------------------------------------------------------------- xmpp_call tool

def test_tool_reports_disabled_when_no_adapter_is_live(monkeypatch):
    monkeypatch.setattr(adapter, "_ACTIVE_ADAPTER", None)
    assert adapter._calls_available() is False
    out = json.loads(adapter._xmpp_call_tool({"opening": "hi"}))
    assert out["success"] is False and "not enabled" in out["status"]


def test_tool_places_the_call_on_the_adapter_loop(monkeypatch):
    import threading

    a = make_adapter(calls={"enabled": True})
    loop = asyncio.new_event_loop()
    threading.Thread(target=loop.run_forever, daemon=True).start()
    seen = []

    async def place_call(peer, opening, **options):
        seen.append((peer, opening, threading.current_thread() is not threading.main_thread(), options))
        return "connected: the call is up"

    a._calls = types.SimpleNamespace(place_call=place_call)
    a._calls_loop = loop
    monkeypatch.setattr(adapter, "_ACTIVE_ADAPTER", a)
    try:
        out = json.loads(adapter._xmpp_call_tool(
            {"opening": "Your backup finished.", "uninterruptible": True, "hang_up_after": "false"}))
    finally:
        loop.call_soon_threadsafe(loop.stop)
    assert out == {"success": True, "status": "connected: the call is up"}
    assert seen == [("user@example.org", "Your backup finished.", True,
                     {"uninterruptible": True, "hang_up_after": False})]
    assert adapter._calls_available() is True


def test_tool_needs_a_target_when_several_users_are_allowed(monkeypatch):
    a = make_adapter(calls={"enabled": True}, allowed_users="a@example.org,b@example.org")
    a._calls, a._calls_loop = object(), object()
    monkeypatch.setattr(adapter, "_ACTIVE_ADAPTER", a)
    out = json.loads(adapter._xmpp_call_tool({"opening": "hi"}))
    assert out["success"] is False and "who to call" in out["status"]


def test_register_adds_the_tool_and_survives_old_hermes():
    ctx = MagicMock()
    adapter.register(ctx)
    assert ctx.register_tool.call_args.kwargs["name"] == "xmpp_call"
    assert ctx.register_platform.called

    old = MagicMock()
    old.register_tool.side_effect = AttributeError
    adapter.register(old)
    assert old.register_platform.called


def test_barge_in_setting():
    assert adapter._calls_settings({"calls": {"barge_in": "STOP"}})["barge_in"] == "stop"
    assert adapter._calls_settings({"calls": {"barge_in": "whatever"}})["barge_in"] == "queue"


def test_every_call_gets_its_own_session_thread():
    a = make_adapter(calls={"enabled": True})
    a._build_calls(MagicMock())
    source = a._calls_host._source("user@example.org", "sid42")
    assert (source.chat_id, source.chat_type, source.thread_id) == ("user@example.org", "dm", "call-sid42")


def test_steer_sends_a_steer_command_for_the_pending_turn(monkeypatch):
    a = make_adapter(calls={"enabled": True})
    a._build_calls(MagicMock())
    monkeypatch.setitem(sys.modules, "gateway", MagicMock())
    monkeypatch.setitem(sys.modules, "gateway.platforms", MagicMock())
    monkeypatch.setitem(sys.modules, "gateway.platforms.base", gw_base)
    sent = []

    async def handle(event):
        sent.append(event)

    a.handle_message = handle
    host = a._calls_host

    async def scenario():
        refused = await host.steer("user@example.org", "sid", "also check email")
        host._pending["user@example.org"] = types.SimpleNamespace(message_id="call-sid-1", parts=[])
        return refused, await host.steer("user@example.org", "sid", "also check email")

    refused, accepted = asyncio.run(scenario())
    assert refused is False and accepted is True
    assert sent[0].text == "/steer also check email"
    assert sent[0].message_id.startswith("call-steer-")
    assert sent[0].source.thread_id == "call-sid"
    assert host.capture("user@example.org", "ack", sent[0].message_id) is False
