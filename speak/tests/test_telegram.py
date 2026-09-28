import json
import traceback

import httpx
import pytest

from speak_mcp.errors import ConfigError, SendError
from speak_mcp.telegram import Telegram

TOKEN = "123456:SECRET-token"


def client_with(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def ok(request):
    return httpx.Response(200, json={"ok": True, "result": {}})


def test_send_message_posts_plain_text_to_fixed_chat():
    seen = []

    def handler(request):
        seen.append(request)
        return ok(request)

    Telegram(TOKEN, "42", client=client_with(handler)).send_message("olá")
    req = seen[0]
    assert req.url.path == f"/bot{TOKEN}/sendMessage"
    body = json.loads(req.content)
    assert body == {"chat_id": "42", "text": "olá",
                     "link_preview_options": {"is_disabled": True}}  # no parse_mode, ever


def test_send_voice_is_multipart_ogg():
    seen = []

    def handler(request):
        seen.append(request)
        return ok(request)

    Telegram(TOKEN, "42", client=client_with(handler)).send_voice(b"OggS-bytes", "1. X", 17)
    req = seen[0]
    assert req.url.path == f"/bot{TOKEN}/sendVoice"
    raw = req.read()
    assert b'name="voice"; filename="radar.ogg"' in raw
    assert b"audio/ogg" in raw and b"OggS-bytes" in raw
    assert b'name="chat_id"' in raw and b"42" in raw
    assert b'name="duration"' in raw and b"17" in raw
    assert b"parse_mode" not in raw


def test_api_error_raises_without_token():
    def handler(request):
        return httpx.Response(400, json={"ok": False, "description": "Bad Request: chat not found"})

    with pytest.raises(SendError) as exc:
        Telegram(TOKEN, "42", client=client_with(handler)).send_message("x")
    assert "chat not found" in str(exc.value)
    assert TOKEN not in str(exc.value) and "SECRET" not in str(exc.value)


def test_network_error_redacts_token():
    def handler(request):
        raise httpx.ConnectError(f"boom {request.url}", request=request)

    with pytest.raises(SendError) as exc:
        Telegram(TOKEN, "42", client=client_with(handler)).send_message("x")
    assert "SECRET" not in str(exc.value) and "<token>" in str(exc.value)


def test_network_error_no_context_chain():
    def handler(request):
        raise httpx.ConnectError(f"boom {request.url}", request=request)

    with pytest.raises(SendError) as exc:
        Telegram(TOKEN, "42", client=client_with(handler)).send_message("x")
    assert exc.value.__context__ is None
    assert exc.value.__cause__ is None
    full_trace = "".join(traceback.format_exception(type(exc.value), exc.value, exc.value.__traceback__))
    assert "SECRET" not in full_trace


def test_from_env_requires_both_vars():
    with pytest.raises(ConfigError) as exc:
        Telegram.from_env({"TELEGRAM_CHAT_ID": "42"})
    assert "TELEGRAM_BOT_TOKEN" in str(exc.value)
    with pytest.raises(ConfigError) as exc:
        Telegram.from_env({"TELEGRAM_BOT_TOKEN": TOKEN})
    assert "TELEGRAM_CHAT_ID" in str(exc.value) and "SECRET" not in str(exc.value)


def test_from_env_builds_client():
    t = Telegram.from_env({"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "42"},
                           client=client_with(ok))
    t.send_message("x")


def test_from_env_strips_whitespace():
    seen = []

    def handler(request):
        seen.append(request)
        return ok(request)

    t = Telegram.from_env({"TELEGRAM_BOT_TOKEN": f" {TOKEN} \n", "TELEGRAM_CHAT_ID": " 42 "},
                           client=client_with(handler))
    t.send_message("x")
    req = seen[0]
    assert req.url.path == f"/bot{TOKEN}/sendMessage"
    assert json.loads(req.content)["chat_id"] == "42"


def test_from_env_rejects_whitespace_only_values():
    with pytest.raises(ConfigError) as exc:
        Telegram.from_env({"TELEGRAM_BOT_TOKEN": "   ", "TELEGRAM_CHAT_ID": "42"})
    assert "TELEGRAM_BOT_TOKEN" in str(exc.value)


def test_call_converts_unexpected_exception_to_redacted_send_error():
    def handler(request):
        raise httpx.InvalidURL("boom, cannot parse")

    with pytest.raises(SendError) as exc:
        Telegram(TOKEN, "42", client=client_with(handler)).send_message("x")
    assert "SECRET" not in str(exc.value)
    assert exc.value.__context__ is None
    assert exc.value.__cause__ is None
