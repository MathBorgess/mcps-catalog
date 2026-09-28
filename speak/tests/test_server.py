"""Tests for speak_mcp.server: the three MCP tools, validation and dispatch.

Fakes are in-process and instantaneous: no real audio playback, no network,
no models. `make_sink`/`make_synth` are monkeypatched on the server module so
tool bodies never build a real KokoroSynth/LocalSink/TelegramSink. The stdio
`initialize` handshake test is the one real subprocess/protocol exercise;
it never plays audio (no `--say`) and only checks the handshake response.
"""

import asyncio
import json
import subprocess
import sys

import pytest

from speak_mcp import server
from speak_mcp.errors import ConfigError
from speak_mcp.sinks import ClipResult, SpeakResult


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
