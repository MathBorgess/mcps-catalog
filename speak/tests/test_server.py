"""Tests for speak_mcp.server: the three MCP tools, validation and dispatch.

Fakes are in-process and instantaneous: no real audio playback, no network,
no models. `make_sink`/`make_synth` are monkeypatched on the server module so
tool bodies never build a real KokoroSynth/LocalSink/TelegramSink. The stdio
`initialize` handshake test is the one real subprocess/protocol exercise;
it never plays audio (no `--say`) and only checks the handshake response.
"""

import asyncio
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from speak_mcp import server
from speak_mcp import sinks as sinks_module
from speak_mcp.errors import ConfigError
from speak_mcp.sinks import ClipResult, SpeakResult
from speak_mcp.synth import MODEL, VOICES_FILE


class FakeSink:
    """Records every play()/stop() call; returns a canned SpeakResult."""

    def __init__(self, sink_name: str = "local"):
        self.sink_name = sink_name
        self.play_calls: list[tuple] = []
        self.stop_calls = 0

    def play(self, clips, voice, note=None):
        self.play_calls.append((clips, voice, note))
        results = [ClipResult(index=i, ok=True, seconds=0.1) for i in range(len(clips))]
        return SpeakResult(ok=True, sink=self.sink_name, queued=(self.sink_name == "local"),
                            clips=results)

    def stop(self):
        self.stop_calls += 1
        return SpeakResult(ok=True, sink=self.sink_name, message="stopped")


class FakeSynth:
    """Exposes just enough of KokoroSynth's surface for voice validation
    (lang_for is a free function in synth.py, exercised directly)."""

    def __init__(self, voices=("pf_dora", "af_heart", "bf_alice")):
        self._voices = list(voices)

    def voices(self):
        return self._voices


DEFAULT_SYNTH = FakeSynth()


def _call(tool_name: str, args: dict):
    return asyncio.run(server.srv.call_tool(tool_name, args))


# -- tool listing: names and schemas -----------------------------------------


def test_tool_names():
    tools = asyncio.run(server.srv.list_tools())
    assert {t.name for t in tools} == {"speak", "speak_clips", "speak_stop"}


def test_tool_schemas_never_expose_a_destination():
    tools = asyncio.run(server.srv.list_tools())
    forbidden = {"chat_id", "destination", "url", "path", "to", "recipient"}
    for tool in tools:
        props = set(tool.input_schema.get("properties", {}))
        assert not (props & forbidden), f"{tool.name} exposes forbidden field(s): {props & forbidden}"


def test_speak_schema_fields():
    tools = asyncio.run(server.srv.list_tools())
    speak_tool = next(t for t in tools if t.name == "speak")
    assert set(speak_tool.input_schema["properties"]) == {"text", "title", "voice"}
    assert speak_tool.input_schema["required"] == ["text"]


def test_speak_clips_schema_fields():
    tools = asyncio.run(server.srv.list_tools())
    tool = next(t for t in tools if t.name == "speak_clips")
    assert set(tool.input_schema["properties"]) == {"clips", "note", "voice"}
    assert tool.input_schema["required"] == ["clips"]


def test_speak_stop_schema_has_no_fields():
    tools = asyncio.run(server.srv.list_tools())
    tool = next(t for t in tools if t.name == "speak_stop")
    assert tool.input_schema.get("properties", {}) == {}


# -- speak: happy path --------------------------------------------------------


def test_speak_happy_path(monkeypatch):
    fake = FakeSink()
    monkeypatch.setattr(server, "make_sink", lambda: fake)
    monkeypatch.setattr(server, "make_synth", lambda: DEFAULT_SYNTH)
    res = _call("speak", {"text": "ola mundo", "title": "titulo", "voice": "pf_dora"})
    assert res.is_error is False
    assert res.structured_content["ok"] is True
    assert res.structured_content["sink"] == "local"
    assert len(fake.play_calls) == 1
    clips, voice, note = fake.play_calls[0]
    assert [c.text for c in clips] == ["ola mundo"]
    assert clips[0].caption == "titulo"
    assert voice == "pf_dora"
    assert note is None


def test_speak_defaults_title_and_voice_to_none(monkeypatch):
    fake = FakeSink()
    monkeypatch.setattr(server, "make_sink", lambda: fake)
    _call("speak", {"text": "ola"})
    clips, voice, note = fake.play_calls[0]
    assert clips[0].caption is None
    assert voice is None


# -- speak_clips: happy path ---------------------------------------------------


def test_speak_clips_happy_path(monkeypatch):
    fake = FakeSink()
    monkeypatch.setattr(server, "make_sink", lambda: fake)
    monkeypatch.setattr(server, "make_synth", lambda: DEFAULT_SYNTH)
    res = _call("speak_clips", {
        "clips": [{"text": "um"}, {"text": "dois", "caption": "c2"}],
        "note": "nota",
        "voice": "af_heart",
    })
    assert res.structured_content["ok"] is True
    clips, voice, note = fake.play_calls[0]
    assert [c.text for c in clips] == ["um", "dois"]
    assert clips[1].caption == "c2"
    assert voice == "af_heart"
    assert note == "nota"


# -- speak: validation failures ------------------------------------------------


def test_speak_empty_text_is_validation_failure(monkeypatch):
    fake = FakeSink()
    monkeypatch.setattr(server, "make_sink", lambda: fake)
    res = _call("speak", {"text": ""})
    assert res.structured_content["ok"] is False
    assert "1-12000" in res.structured_content["message"]
    assert fake.play_calls == []  # never reached the sink


def test_speak_text_too_long_is_validation_failure(monkeypatch):
    fake = FakeSink()
    monkeypatch.setattr(server, "make_sink", lambda: fake)
    res = _call("speak", {"text": "x" * 12001})
    assert res.structured_content["ok"] is False
    assert fake.play_calls == []


def test_speak_title_with_link_is_validation_failure(monkeypatch):
    fake = FakeSink()
    monkeypatch.setattr(server, "make_sink", lambda: fake)
    res = _call("speak", {"text": "ola", "title": "veja www.example.com"})
    assert res.structured_content["ok"] is False
    assert "link" in res.structured_content["message"]
    assert fake.play_calls == []


def test_speak_title_with_control_char_is_validation_failure(monkeypatch):
    fake = FakeSink()
    monkeypatch.setattr(server, "make_sink", lambda: fake)
    res = _call("speak", {"text": "ola", "title": "linha\ncom quebra"})
    assert res.structured_content["ok"] is False
    assert fake.play_calls == []


# -- speak_clips: validation failures ------------------------------------------


def test_speak_clips_empty_list_is_validation_failure(monkeypatch):
    fake = FakeSink()
    monkeypatch.setattr(server, "make_sink", lambda: fake)
    res = _call("speak_clips", {"clips": []})
    assert res.structured_content["ok"] is False
    assert "1-20" in res.structured_content["message"]
    assert fake.play_calls == []


def test_speak_clips_too_many_is_validation_failure(monkeypatch):
    fake = FakeSink()
    monkeypatch.setattr(server, "make_sink", lambda: fake)
    res = _call("speak_clips", {"clips": [{"text": "x"}] * 21})
    assert res.structured_content["ok"] is False
    assert fake.play_calls == []


def test_speak_clips_clip_text_too_long_is_validation_failure(monkeypatch):
    fake = FakeSink()
    monkeypatch.setattr(server, "make_sink", lambda: fake)
    res = _call("speak_clips", {"clips": [{"text": "x" * 3001}]})
    assert res.structured_content["ok"] is False
    assert fake.play_calls == []


def test_speak_clips_caption_with_link_is_validation_failure(monkeypatch):
    fake = FakeSink()
    monkeypatch.setattr(server, "make_sink", lambda: fake)
    res = _call("speak_clips", {"clips": [{"text": "ola", "caption": "http://evil.example"}]})
    assert res.structured_content["ok"] is False
    assert fake.play_calls == []


def test_speak_clips_note_too_long_is_validation_failure(monkeypatch):
    fake = FakeSink()
    monkeypatch.setattr(server, "make_sink", lambda: fake)
    res = _call("speak_clips", {"clips": [{"text": "ola"}], "note": "x" * 3001})
    assert res.structured_content["ok"] is False
    assert fake.play_calls == []


def test_speak_clips_note_allows_newlines(monkeypatch):
    fake = FakeSink()
    monkeypatch.setattr(server, "make_sink", lambda: fake)
    res = _call("speak_clips", {"clips": [{"text": "ola"}], "note": "linha um\nlinha dois"})
    assert res.structured_content["ok"] is True


# -- config errors --------------------------------------------------------------


def test_speak_config_error_from_make_sink_is_ok_false(monkeypatch):
    def _raise():
        raise ConfigError("TELEGRAM_BOT_TOKEN is not set")
    monkeypatch.setattr(server, "make_sink", _raise)
    res = _call("speak", {"text": "ola"})
    assert res.is_error is False  # never an unhandled tool exception
    assert res.structured_content["ok"] is False
    assert res.structured_content["message"] == "config: TELEGRAM_BOT_TOKEN is not set"


def test_speak_clips_config_error_from_make_sink_is_ok_false(monkeypatch):
    def _raise():
        raise ConfigError("model file missing")
    monkeypatch.setattr(server, "make_sink", _raise)
    res = _call("speak_clips", {"clips": [{"text": "ola"}]})
    assert res.structured_content["ok"] is False
    assert res.structured_content["message"] == "config: model file missing"


def test_speak_config_error_never_leaks_as_exception(monkeypatch):
    def _raise():
        raise ConfigError("boom")
    monkeypatch.setattr(server, "make_sink", _raise)
    res = _call("speak", {"text": "ola"})
    assert res.is_error is False


# -- speak_stop -----------------------------------------------------------------


def test_speak_stop_routes_to_sink(monkeypatch):
    fake = FakeSink()
    monkeypatch.setattr(server, "make_sink", lambda: fake)
    res = _call("speak_stop", {})
    assert fake.stop_calls == 1
    assert res.structured_content["ok"] is True
    assert res.structured_content["message"] == "stopped"


def test_speak_stop_config_error_is_ok_false(monkeypatch):
    def _raise():
        raise ConfigError("no audio player found")
    monkeypatch.setattr(server, "make_sink", _raise)
    res = _call("speak_stop", {})
    assert res.structured_content["ok"] is False
    assert res.structured_content["message"] == "config: no audio player found"


# -- make_synth / make_sink: lazy singleton wiring -----------------------------


def test_make_sink_builds_local_sink_via_make_synth(monkeypatch):
    built = []

    class FakeSynth:
        pass

    def fake_make_synth():
        s = FakeSynth()
        built.append(s)
        return s

    monkeypatch.setattr(server, "_sink", None)
    monkeypatch.setattr(server, "make_synth", fake_make_synth)
    monkeypatch.setattr(server, "choose_sink", lambda: "local")
    sink = server.make_sink()
    assert isinstance(sink, server.LocalSink)
    assert len(built) == 1

    # cached: a second call does not build another synth
    sink2 = server.make_sink()
    assert sink2 is sink
    assert len(built) == 1


def test_make_sink_does_not_deadlock_on_first_real_call(monkeypatch):
    """Regression for F1: make_sink() used to acquire `_lock` and then call
    make_synth() -- which acquires the *same* plain (non-reentrant) Lock --
    from inside that `with` block. Same thread, same lock, twice: deadlock,
    on the very first real tool call of every fresh process.

    This exercises the real make_synth()/make_sink() (not monkeypatched
    stand-ins for them), stubbing only the leaves they call out to: model
    settings/download/init, and the local player class (so nothing can ever
    actually play). Runs the call on a background thread with a timeout: if
    the deadlock regresses, that thread is still alive after 5s and the
    assertion fails instead of the test hanging forever.
    """
    monkeypatch.setattr(server, "_synth", None)
    monkeypatch.setattr(server, "_sink", None)
    monkeypatch.setattr(server, "choose_sink", lambda: "local")

    class FakeSettings:
        pass

    monkeypatch.setattr(server, "Settings",
                         type("FakeSettingsCls", (), {"from_env": classmethod(lambda cls: FakeSettings())}))
    monkeypatch.setattr(server, "ensure_models", lambda settings: None)

    class FakeKokoroSynth:
        def __init__(self, settings):
            self._settings = settings

        def voices(self):
            return []

    monkeypatch.setattr(server, "KokoroSynth", FakeKokoroSynth)

    class NoOpPlayer:
        """LocalSink's default player, replaced so nothing can ever play."""

        def play_file(self, path):
            raise AssertionError("must never play audio during this test")

        def stop(self):
            pass

    monkeypatch.setattr(sinks_module, "SubprocessPlayer", NoOpPlayer)

    outcome: dict = {}

    def _call_make_sink():
        try:
            outcome["sink"] = server.make_sink()
        except Exception as exc:  # captured, not raised, so the thread always exits cleanly
            outcome["error"] = exc

    worker = threading.Thread(target=_call_make_sink, daemon=True)
    worker.start()
    worker.join(timeout=5.0)

    assert not worker.is_alive(), "make_sink() deadlocked (F1 regression)"
    assert "error" not in outcome, f"make_sink() raised: {outcome.get('error')!r}"
    assert isinstance(outcome["sink"], server.LocalSink)


def test_speak_invalid_speak_sink_never_builds_synth(monkeypatch):
    """Regression (fix round 2): make_sink() must check choose_sink() --
    cheap, env-only -- *before* calling make_synth() -- a full model
    download/ONNX init. Otherwise a typo'd SPEAK_SINK pays for a model build
    first and its ConfigError would surface as a confusing model error
    instead of the actual SPEAK_SINK message. Uses the real choose_sink()
    (via SPEAK_SINK in the environment) and the real make_sink(), with
    make_synth stubbed to blow up if it is ever reached."""
    monkeypatch.setattr(server, "_sink", None)
    monkeypatch.setattr(server, "_synth", None)
    monkeypatch.setenv("SPEAK_SINK", "carrier-pigeon")

    def _boom():
        raise AssertionError("make_synth() must not be called for an invalid SPEAK_SINK")

    monkeypatch.setattr(server, "make_synth", _boom)

    res = _call("speak", {"text": "ola"})
    assert res.structured_content["ok"] is False
    assert "SPEAK_SINK" in res.structured_content["message"]


# -- voice validation (F3): unknown voice must never reach the sink -----------


def test_speak_unknown_voice_prefix_is_validation_failure(monkeypatch):
    fake = FakeSink()
    monkeypatch.setattr(server, "make_sink", lambda: fake)
    monkeypatch.setattr(server, "make_synth", lambda: DEFAULT_SYNTH)
    res = _call("speak", {"text": "ola", "voice": "qq_bogus"})
    assert res.structured_content["ok"] is False
    assert "voice" in res.structured_content["message"]
    assert fake.play_calls == []


def test_speak_voice_not_in_loaded_voices_is_validation_failure(monkeypatch):
    fake = FakeSink()
    monkeypatch.setattr(server, "make_sink", lambda: fake)
    monkeypatch.setattr(server, "make_synth", lambda: DEFAULT_SYNTH)  # "pz_unknown" isn't in it
    res = _call("speak", {"text": "ola", "voice": "pz_unknown"})
    assert res.structured_content["ok"] is False
    assert "unknown voice" in res.structured_content["message"]
    assert fake.play_calls == []


def test_speak_clips_unknown_voice_is_validation_failure(monkeypatch):
    fake = FakeSink()
    monkeypatch.setattr(server, "make_sink", lambda: fake)
    monkeypatch.setattr(server, "make_synth", lambda: DEFAULT_SYNTH)
    res = _call("speak_clips", {"clips": [{"text": "ola"}], "voice": "zz_unknown"})
    assert res.structured_content["ok"] is False
    assert fake.play_calls == []


def test_speak_known_voice_passes_validation(monkeypatch):
    fake = FakeSink()
    monkeypatch.setattr(server, "make_sink", lambda: fake)
    monkeypatch.setattr(server, "make_synth", lambda: DEFAULT_SYNTH)
    res = _call("speak", {"text": "ola", "voice": "af_heart"})
    assert res.structured_content["ok"] is True
    assert len(fake.play_calls) == 1


def test_speak_no_explicit_voice_skips_validation_entirely(monkeypatch):
    """When the caller doesn't override the voice, the server must not force
    a make_synth() build just to validate it (that would mean every call
    pays for model loading, defeating the point of lazy construction)."""
    fake = FakeSink()

    def _boom():
        raise AssertionError("make_synth() must not be called when voice is None")

    monkeypatch.setattr(server, "make_sink", lambda: fake)
    monkeypatch.setattr(server, "make_synth", _boom)
    res = _call("speak", {"text": "ola"})
    assert res.structured_content["ok"] is True


# -- stdio initialize handshake (real subprocess, real protocol) --------------


def test_stdio_initialize_handshake():
    """Spawns `python -m speak_mcp` for real and sends a raw JSON-RPC
    `initialize` request over stdio. Never sends --say/--sample, so no
    audio is played and no models are touched."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "speak_mcp"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    try:
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2026-07-28",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "0.0.1"},
            },
        }
        proc.stdin.write(json.dumps(request) + "\n")
        proc.stdin.flush()

        import select
        readable, _, _ = select.select([proc.stdout], [], [], 10)
        assert readable, "server never responded to initialize within 10s"
        line = proc.stdout.readline()
        response = json.loads(line)
        assert response["jsonrpc"] == "2.0"
        assert response["id"] == 1
        assert "result" in response, response
        assert response["result"]["serverInfo"]["name"] == "speak"
        assert "protocolVersion" in response["result"]
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def test_stdio_server_writes_only_protocol_to_stdout():
    """No stray prints, banners or logging should ever hit stdout: it is the
    MCP protocol channel. Everything up to the initialize response must be
    valid JSON-RPC on its own line."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "speak_mcp"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    try:
        request = {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2026-07-28", "capabilities": {},
                       "clientInfo": {"name": "t", "version": "0"}},
        }
        proc.stdin.write(json.dumps(request) + "\n")
        proc.stdin.flush()

        import select
        readable, _, _ = select.select([proc.stdout], [], [], 10)
        assert readable, "server never responded"
        line = proc.stdout.readline()
        json.loads(line)  # must parse cleanly as the only thing on stdout
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


# -- F4: real model load must never leak a stray print onto stdout ------------


REAL_SPEAK_HOME = os.environ.get("SPEAK_HOME", "/opt/speak")


def _jsonrpc_send(stdin, obj: dict) -> None:
    stdin.write(json.dumps(obj) + "\n")
    stdin.flush()


def _jsonrpc_read_until_id(stdout, target_id: int, timeout: float) -> list[dict]:
    """Reads lines until one has `"id" == target_id`, or times out. Every
    line read must parse as JSON-RPC (`jsonrpc` key present) -- a stray print
    from a C-extension library during model load would break that
    immediately, with the offending raw line in the assertion message."""
    import select
    import time

    messages = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = max(0.0, deadline - time.monotonic())
        readable, _, _ = select.select([stdout], [], [], remaining)
        if not readable:
            break
        raw = stdout.readline()
        if not raw:
            break
        parsed = json.loads(raw)  # AssertionError-equivalent: raises on a stray non-JSON print
        assert parsed.get("jsonrpc") == "2.0", f"non-protocol line on stdout: {raw!r}"
        messages.append(parsed)
        if parsed.get("id") == target_id:
            return messages
    raise AssertionError(f"never saw a response with id={target_id} within {timeout}s; "
                          f"got: {messages}")


@pytest.mark.skipif(
    not (Path(REAL_SPEAK_HOME, "models", MODEL).exists()
         and Path(REAL_SPEAK_HOME, "models", VOICES_FILE).exists()),
    reason="Kokoro model not installed under SPEAK_HOME",
)
def test_stdio_stays_protocol_clean_through_real_model_load_telegram_sink():
    """F4: starts the real stdio server with SPEAK_SINK=telegram and bogus
    TELEGRAM_* credentials, completes initialize, then calls speak_stop.
    speak_stop routes through the real make_sink() -> make_synth() ->
    KokoroSynth(settings), so this loads the real ONNX model -- exactly the
    scenario where a C-extension (onnxruntime/kokoro) could print a stray
    line onto stdout and corrupt the MCP protocol stream. TelegramSink.stop()
    itself is a pure no-op (never calls the Telegram API), so this never
    touches the network, and nothing here ever plays audio.
    """
    env = dict(os.environ)
    env["SPEAK_HOME"] = REAL_SPEAK_HOME
    env["SPEAK_SINK"] = "telegram"
    env["TELEGRAM_BOT_TOKEN"] = "bogus-token-never-used"
    env["TELEGRAM_CHAT_ID"] = "bogus-chat-never-used"

    proc = subprocess.Popen(
        [sys.executable, "-m", "speak_mcp"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1, env=env,
    )
    try:
        _jsonrpc_send(proc.stdin, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2026-07-28", "capabilities": {},
                       "clientInfo": {"name": "f4-test", "version": "0.0.1"}},
        })
        init_messages = _jsonrpc_read_until_id(proc.stdout, target_id=1, timeout=15.0)
        assert "result" in init_messages[-1], init_messages[-1]

        _jsonrpc_send(proc.stdin, {"jsonrpc": "2.0", "method": "notifications/initialized",
                                    "params": {}})

        _jsonrpc_send(proc.stdin, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "speak_stop", "arguments": {}},
        })
        # Generous timeout: this is a real model load (ONNX session init), not a fake.
        call_messages = _jsonrpc_read_until_id(proc.stdout, target_id=2, timeout=60.0)
        call_response = call_messages[-1]
        assert "result" in call_response, call_response
        structured = call_response["result"].get("structuredContent")
        assert structured is not None, call_response
        assert structured["ok"] is True
        assert structured["sink"] == "telegram"
        assert structured["message"] == "nothing to stop on telegram"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
