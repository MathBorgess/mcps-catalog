"""Minimal Telegram Bot API client: plain-text message and OGG voice, to one fixed chat."""

import logging
import os
from collections.abc import Mapping

import httpx

from speak_mcp.errors import ConfigError, SendError

TIMEOUT = httpx.Timeout(60.0, connect=10.0)

# httpx logs the full request URL -- including .../bot<TOKEN>/sendMessage -- at INFO, and the
# MCP server configures root/stderr logging at INFO (see MCPServer(log_level=...) in server.py).
# Silence both httpx's and its transport library httpcore's loggers at import time so the bot
# token can never reach stderr through a log line (regression: I1).
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class Telegram:
    def __init__(self, token: str, chat_id: str, client: httpx.Client | None = None,
                 base_url: str = "https://api.telegram.org"):
        self._token = token
        self._chat_id = chat_id
        self._client = client or httpx.Client(timeout=TIMEOUT)
        self._base = f"{base_url}/bot{token}"

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ,
                 client: httpx.Client | None = None) -> "Telegram":
        values: dict[str, str] = {}
        for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
            raw = env.get(name)
            stripped = raw.strip() if raw else ""
            if not stripped:
                raise ConfigError(f"{name} is not set")
            values[name] = stripped
        return cls(values["TELEGRAM_BOT_TOKEN"], values["TELEGRAM_CHAT_ID"], client=client)

    def send_message(self, text: str) -> None:
        self._call("sendMessage", json={"chat_id": self._chat_id, "text": text,
                                        "link_preview_options": {"is_disabled": True}})

    def send_voice(self, ogg: bytes, caption: str, duration: int) -> None:
        self._call("sendVoice",
                   data={"chat_id": self._chat_id, "caption": caption, "duration": str(duration)},
                   files={"voice": ("radar.ogg", ogg, "audio/ogg")})

    def _call(self, method: str, **kwargs) -> None:
        err_to_raise = None
        try:
            resp = self._client.post(f"{self._base}/{method}", **kwargs)
        except Exception as exc:  # any client-side failure (httpx.HTTPError, InvalidURL, ...)
            err_to_raise = SendError(self._redact(f"{method}: {exc}"))
        if err_to_raise is not None:
            err_to_raise.__context__ = None
            raise err_to_raise from None
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code != 200 or not body.get("ok"):
            desc = body.get("description", f"HTTP {resp.status_code}")
            err_to_raise = SendError(self._redact(f"{method}: {desc}"))
            err_to_raise.__context__ = None
            raise err_to_raise from None

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "<token>")
