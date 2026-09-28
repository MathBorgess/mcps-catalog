import json
import logging
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


# -- I1: httpx/httpcore must never log the token at INFO -----------------------


def test_httpx_and_httpcore_loggers_are_silenced_on_import():
    """speak_mcp.telegram must raise httpx's and httpcore's own loggers to
    WARNING at import time -- httpx logs the full request URL (which embeds
    the bot token, .../bot<TOKEN>/sendMessage) at INFO, and the MCP server
    configures root/stderr logging at INFO (regression: I1)."""
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_token_never_appears_in_any_log_record_during_a_real_send(caplog):
    """Drives a real send through httpx's MockTransport (no network) with
    root logging captured at DEBUG -- the level httpx/httpcore would log
    the request line at if their own loggers were not silenced -- and
    asserts the token is absent from every record, not just the raised
    exception message."""
    caplog.set_level(logging.DEBUG)

    def handler(request):
        return httpx.Response(200, json={"ok": True, "result": {}})

    Telegram(TOKEN, "42", client=client_with(handler)).send_message("olá")

    for record in caplog.records:
        assert TOKEN not in record.getMessage()
        assert TOKEN not in caplog.text
