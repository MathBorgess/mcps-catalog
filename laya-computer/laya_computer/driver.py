"""Persistent stdio adapter for the installed Cua Driver MCP server.

The module deliberately has no platform-native imports.  It can therefore be
imported on Linux, while the native Cua Driver process is only started when a
driver instance is started.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Protocol


class DriverError(RuntimeError):
    """A bounded adapter or Cua Driver failure."""

    def __init__(self, code: str, message: str, *, uncertain: bool = False):
        super().__init__(message)
        self.code = code
        self.uncertain = uncertain


class _Transport(Protocol):
    async def request(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]: ...
    async def close(self) -> None: ...


@dataclass
class _Request:
    tool: str
    arguments: dict[str, Any]
    future: asyncio.Future[dict[str, Any]]


class _StdioTransport:
    """Own the MCP AnyIO context in one long-lived asyncio task."""

    def __init__(self, command: str, *, timeout: float):
        self.command = command
        self.timeout = timeout
        self._queue: asyncio.Queue[_Request | None] = asyncio.Queue()
        self._ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._worker = asyncio.create_task(self._run(), name="laya-cua-mcp-stdio")

    async def request(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        await asyncio.shield(self._ready)
        if self._worker.done():
            raise DriverError("transport_closed", "Cua MCP transport has ended")
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        await self._queue.put(_Request(tool, arguments, future))
        return await future

    async def close(self) -> None:
        if self._worker.done():
            return
        await self._queue.put(None)
        try:
            await asyncio.wait_for(asyncio.shield(self._worker), self.timeout + 2)
        except asyncio.TimeoutError:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)

    async def _run(self) -> None:
        try:
            # Import lazily so importing this package does not require MCP or a
            # native Cua installation (for example in Linux-only unit tests).
            from mcp import ClientSession
            from mcp.client.stdio import StdioServerParameters, stdio_client

            params = StdioServerParameters(command=self.command, args=["mcp"])
            async with stdio_client(params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await asyncio.wait_for(session.initialize(), self.timeout)
                    if not self._ready.done():
                        self._ready.set_result(None)
                    while True:
                        request = await self._queue.get()
                        if request is None:
                            break
                        if request.future.cancelled():
                            continue
                        try:
                            result = await asyncio.wait_for(
                                session.call_tool(request.tool, request.arguments),
                                timeout=self.timeout,
                            )
                            if not request.future.done():
                                request.future.set_result(_normalize_result(result))
                        except asyncio.TimeoutError:
                            if not request.future.done():
                                request.future.set_exception(
                                    DriverError("timeout", f"Cua Driver {request.tool} timed out")
                                )
                        except Exception as exc:
                            if not request.future.done():
                                request.future.set_exception(_transport_error(exc))
        except Exception as exc:
            error = _transport_error(exc)
            if not self._ready.done():
                self._ready.set_exception(error)
            while not self._queue.empty():
                request = self._queue.get_nowait()
                if request is not None and not request.future.done():
                    request.future.set_exception(error)
        finally:
            if not self._ready.done():
                self._ready.set_exception(
                    DriverError("transport_closed", "Cua Driver MCP transport closed")
                )


_MUTATING_TOOLS = {"click", "double_click", "set_value", "press_key", "scroll", "invoke_menu"}
_STALE_RE = re.compile(r"stale|snapshot.{0,20}(expired|supersed|invalid)|element_token_stale", re.I)
_PRE_ACTION_RE = re.compile(
    r"permission|not permitted|not authorized|window_id_not_found|window_owner_pid_mismatch|"
    r"element_token_stale|stale snapshot|invalid element", re.I
)


def _transport_error(exc: BaseException) -> DriverError:
    if isinstance(exc, DriverError):
        return exc
    return DriverError("transport_error", str(exc) or type(exc).__name__)


def _plain(value: Any) -> Any:
    """Convert MCP/Pydantic response objects into ordinary Python values."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if hasattr(value, "model_dump"):
        return _plain(value.model_dump(by_alias=True, exclude_none=True))
    if hasattr(value, "__dict__"):
        return _plain(vars(value))
    return value


def _normalize_result(result: Any) -> dict[str, Any]:
    """Unwrap the MCP result envelope, including JSON text fallbacks."""
    plain = _plain(result)
    if isinstance(plain, dict) and plain.get("isError"):
        message = _error_text(plain)
        stale = bool(_STALE_RE.search(message))
        raise DriverError(
            "stale_snapshot" if stale else "driver_error",
            message or "Cua Driver returned an error",
            uncertain=False,
        )

    if isinstance(plain, dict):
        structured = plain.get("structuredContent", plain.get("structured_content"))
        if structured is not None:
            data = _plain(structured)
            if isinstance(data, dict):
                return _unwrap_payload(data)
            return {"result": data}
        content = plain.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text", "")
                    try:
                        parsed = json.loads(text)
                    except (TypeError, json.JSONDecodeError):
                        return {"text": text}
                    if isinstance(parsed, dict):
                        return _unwrap_payload(parsed)
                    return {"result": parsed}
            return {"content": content}
        return _unwrap_payload(plain)
    return {"result": plain}


def _unwrap_payload(data: dict[str, Any]) -> dict[str, Any]:
    # Some Cua builds expose a conventional {result: {...}} wrapper in
    # structuredContent. Preserve metadata while making the actual payload
    # straightforward to consume.
    if data.get("status") == "refused" or data.get("effect") == "refused" or data.get("refusal"):
        refusal = data.get("refusal") or {}
        raise DriverError(str(refusal.get("code", data.get("code", "refused"))),
                          str(refusal.get("message", data.get("reason", "Cua refused before execution"))))
    nested = data.get("result")
    if isinstance(nested, dict) and len(data) <= 3:
        return _unwrap_payload({**nested, **{k: v for k, v in data.items() if k != "result"}})
    return data


def _error_text(payload: dict[str, Any]) -> str:
    messages: list[str] = []
    for block in payload.get("content", []) or []:
        block = _plain(block)
        if isinstance(block, dict) and block.get("text"):
            messages.append(str(block["text"]))
    structured = payload.get("structuredContent") or payload.get("structured_content")
    if isinstance(structured, dict):
        messages.extend(str(structured[k]) for k in ("message", "error", "code") if structured.get(k))
    return "; ".join(messages) or str(payload.get("error", ""))


def _without_images(data: dict[str, Any]) -> dict[str, Any]:
    """Keep the response compact and never return screenshot bytes."""
    return {
        key: value
        for key, value in data.items()
        if key not in {"screenshot", "image", "image_data", "screenshot_base64"}
    }


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _step_value(step: Any, name: str, default: Any = None) -> Any:
    value = _field(step, name, default)
    if value is None:
        return default
    return value


def _element_label(element: dict[str, Any]) -> str:
    return str(element.get("label") or element.get("title") or element.get("name") or "")


class CuaDriver:
    """Bind and operate one application window through a persistent MCP client."""

    def __init__(self, *, command: str | None = None, timeout: float = 30.0,
                 transport: _Transport | None = None):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.command = command or os.environ.get("CUA_DRIVER_COMMAND", "cua-driver")
        self.timeout = timeout
        self._injected_transport = transport
        self._transport: _Transport | None = None
        self.bundle_id: str | None = None
        self.pid: int | None = None
        self.window_id: int | None = None
        self.window_title: str | None = None

    async def start(self, bundle_id: str, pid: int | None = None,
                    window_id: int | None = None) -> "CuaDriver":
        if not bundle_id:
            raise DriverError("invalid_app", "bundle_id is required")
        if self._transport is not None:
            raise DriverError("already_started", "Cua driver is already started")
        self.bundle_id = bundle_id
        self._transport = self._injected_transport or _StdioTransport(self.command, timeout=self.timeout)

        if pid is None:
            launched = await self._call("launch_app", {"bundle_id": bundle_id})
            pid = launched.get("pid")
            launched_windows = launched.get("windows") or []
            if window_id is None and len(launched_windows) == 1:
                window_id = launched_windows[0].get("window_id")
                self.window_title = launched_windows[0].get("title")
            if pid is None:
                windows = launched.get("windows") or []
                matching = [w for w in windows if isinstance(w, dict) and w.get("bundle_id") == bundle_id]
                if matching:
                    pid = matching[0].get("pid")
        if pid is None:
            raise DriverError("app_not_running", f"Cua Driver did not return a pid for {bundle_id}")
        self.pid = int(pid)
        self.window_id = int(window_id) if window_id is not None else None
        return self

    async def close(self) -> None:
        transport, self._transport = self._transport, None
        if transport is not None:
            await transport.close()

    async def observe(self, window_title: str | None = None) -> dict[str, Any]:
        self._require_started()
        if window_title and window_title != self.window_title:
            self.window_id = None
            self.window_title = window_title
        if self.window_id is None:
            windows_result = await self._call("list_windows", {"pid": self.pid})
            windows = windows_result.get("windows", windows_result.get("items", []))
            if not isinstance(windows, list):
                windows = []
            window = self._select_window(windows, self.window_title)
            self.window_id = int(window["window_id"])
            self.window_title = str(window.get("title") or "")

        raw = await self._call("get_window_state", {
            "pid": self.pid,
            "window_id": self.window_id,
            "include_screenshot": False,
        })
        data = _without_images(raw)
        elements = data.get("elements") or []
        if not isinstance(elements, list):
            elements = []
        snapshot_id = data.get("snapshot_id")
        normalized_elements = []
        for raw_element in elements:
            if not isinstance(raw_element, dict):
                continue
            element = dict(raw_element)
            token = element.get("element_token", element.get("token"))
            index = element.get("element_index")
            normalized = {
                "id": str(index if index is not None else len(normalized_elements)),
                "role": element.get("role"),
                "label": _element_label(element),
                "value": element.get("value"),
                "enabled": element.get("enabled"),
                "selected": element.get("selected"),
                "actions": element.get("actions", []),
                "token": token,
                "element_index": index,
                "snapshot_id": snapshot_id,
            }
            for key, value in element.items():
                if key not in {"element_token", "token", "element_index", "role", "label", "title", "name",
                               "value", "enabled", "selected", "actions"}:
                    normalized[key] = value
            normalized_elements.append(normalized)
        by_index = {e["element_index"]: e for e in normalized_elements}
        for element in normalized_elements:
            if element["role"] != "AXMenuItem":
                continue
            path, cursor, seen = [], element, set()
            while cursor and cursor.get("element_index") not in seen:
                seen.add(cursor.get("element_index"))
                if cursor["role"] in {"AXMenuItem", "AXMenuBarItem"}:
                    path.insert(0, cursor["label"])
                if cursor["role"] == "AXMenuBarItem":
                    if path and all(path):
                        element["menu_path"] = path
                    break
                cursor = by_index.get(cursor.get("parent_index"))
        title = data.get("title") or next(
            (e["label"] for e in normalized_elements if e["role"] == "AXWindow"),
            self.window_title or "",
        )
        return {
            "snapshot_id": snapshot_id,
            "title": str(title),
            "pid": self.pid,
            "window_id": self.window_id,
            "elements": normalized_elements,
            "truncated": bool(data.get("truncated", False)) or data.get("elements_complete") is False,
            "app_bundle_id": self.bundle_id,
        }

    async def act(self, action: str, candidate: Any, step: Any, snapshot: dict[str, Any]) -> dict[str, Any]:
        self._require_started()
        action = str(action).lower()
        if action == "wait":
            seconds = _step_value(step, "duration_seconds", 0.5)
            try:
                seconds = float(seconds)
            except (TypeError, ValueError):
                raise DriverError("invalid_step", "wait duration must be numeric") from None
            if not 0 <= seconds <= 10:
                raise DriverError("invalid_step", "wait duration must be between 0 and 10 seconds")
            await asyncio.sleep(seconds)
            return {"effect": "waited", "seconds": seconds}
        if action == "verify":
            return await self.observe(_step_value(step, "window_title"))

        tool: str
        args: dict[str, Any] = {"pid": self.pid, "window_id": self.window_id}
        if action in {"click", "double_click", "set_value"}:
            element = self._resolve_candidate(candidate, snapshot)
            token = element.get("token")
            index = element.get("element_index")
            if token:
                args["element_token"] = token
            elif index is not None and snapshot.get("snapshot_id"):
                args["element_index"] = index
                args["snapshot_id"] = snapshot["snapshot_id"]
            else:
                raise DriverError("invalid_candidate", "candidate has no current element token or index")
            if action == "click":
                tool = "click"
            elif action == "double_click":
                tool = "double_click"
            else:
                tool = "set_value"
                text = _step_value(step, "text")
                if text is None:
                    raise DriverError("invalid_step", "set_value requires explicit step.text")
                args["value"] = str(text)
            if action == "click" and element.get("menu_path"):
                # Native menus are app-level surfaces, not ordinary window
                # elements. Cua re-resolves this observed exact path live.
                tool = "invoke_menu"
                args = {"pid": self.pid, "window_id": self.window_id, "path": element["menu_path"]}
        elif action == "press_key":
            tool = "press_key"
            key = _step_value(step, "key")
            if not key:
                raise DriverError("invalid_step", "press_key requires step.key")
            parts = str(key).lower().split("+")
            if len(parts) > 1:
                if any(p not in {"cmd", "shift", "option", "alt", "ctrl", "fn"} for p in parts[:-1]):
                    raise DriverError("invalid_step", "Unknown key modifier")
                args["modifiers"] = parts[:-1]
            args["key"] = parts[-1]
        elif action == "scroll":
            tool = "scroll"
            direction = _step_value(step, "direction")
            if direction not in {"up", "down", "left", "right"}:
                raise DriverError("invalid_step", "scroll direction must be up, down, left, or right")
            args["direction"] = direction
            element = self._resolve_candidate(candidate, snapshot)
            if element.get("token"):
                args["element_token"] = element["token"]
            else:
                args.update(element_index=element["element_index"], snapshot_id=snapshot["snapshot_id"])
        else:
            raise DriverError("unsupported_action", f"unsupported Cua action: {action}")

        try:
            result = await asyncio.wait_for(self._transport.request(tool, args), timeout=self.timeout)
        except asyncio.TimeoutError:
            raise DriverError("timeout", f"Cua Driver {tool} timed out", uncertain=True) from None
        except DriverError as exc:
            if tool in _MUTATING_TOOLS and exc.code in {"timeout", "transport_error", "driver_error", "transport_closed"}:
                if not _PRE_ACTION_RE.search(str(exc)):
                    exc.uncertain = True
            raise
        result = _without_images(result)
        receipt = result.get("action_receipt") or result.get("receipt") or {}
        if "effect" not in result and isinstance(receipt, dict):
            result["effect"] = receipt.get("effect", "unverifiable")
        return result

    def _resolve_candidate(self, candidate: Any, snapshot: dict[str, Any]) -> dict[str, Any]:
        if isinstance(candidate, str):
            candidate = next((e for e in snapshot.get("elements", []) if e.get("id") == candidate), None)
        if not isinstance(candidate, dict):
            raise DriverError("invalid_candidate", "candidate must come from the supplied snapshot")
        snapshot_id = snapshot.get("snapshot_id")
        candidate_snapshot_id = candidate.get("snapshot_id")
        if not snapshot_id or (candidate_snapshot_id and candidate_snapshot_id != snapshot_id):
            raise DriverError("stale_snapshot", "candidate is not bound to the supplied current snapshot")
        if candidate not in snapshot.get("elements", []):
            # Permit a caller-created view of a returned candidate only when
            # its authentic token/index and snapshot id still match an entry.
            same = any(
                (candidate.get("token") and e.get("token") == candidate.get("token"))
                or (candidate.get("element_index") is not None
                    and e.get("element_index") == candidate.get("element_index")
                    and e.get("snapshot_id") == snapshot_id)
                for e in snapshot.get("elements", [])
            )
            if not same:
                raise DriverError("invalid_candidate", "candidate was not present in the supplied snapshot")
        if candidate.get("token") and candidate_snapshot_id not in (None, snapshot_id):
            raise DriverError("stale_snapshot", "candidate token belongs to another snapshot")
        return candidate

    def _select_window(self, windows: list[Any], title: str | None) -> dict[str, Any]:
        valid = [w for w in windows if isinstance(w, dict) and w.get("window_id") is not None]
        if not valid:
            raise DriverError("window_not_found", f"no window is available for pid {self.pid}")
        if title:
            exact = [w for w in valid if str(w.get("title", "")) == title]
            matches = exact or [w for w in valid if title.casefold() in str(w.get("title", "")).casefold()]
            if not matches:
                raise DriverError("window_not_found", f"no window title matches {title!r}")
            valid = matches
        if len(valid) > 1 and not title:
            onscreen = [w for w in valid if w.get("is_on_screen") is True]
            if len(onscreen) == 1:
                valid = onscreen
            else:
                raise DriverError("ambiguous_window", "multiple app windows require an explicit window_title")
        return valid[0]

    async def _call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        self._require_started(allow_transport_initializing=True)
        try:
            return await asyncio.wait_for(self._transport.request(tool, args), timeout=self.timeout)
        except asyncio.TimeoutError:
            raise DriverError("timeout", f"Cua Driver {tool} timed out", uncertain=tool in _MUTATING_TOOLS) from None
        except DriverError as exc:
            if tool in _MUTATING_TOOLS and exc.code != "stale_snapshot" and not _PRE_ACTION_RE.search(str(exc)):
                exc.uncertain = True
            raise

    def _require_started(self, *, allow_transport_initializing: bool = False) -> None:
        if self._transport is None or self.pid is None and not allow_transport_initializing:
            raise DriverError("not_started", "call start(bundle_id, ...) before using the driver")


__all__ = ["CuaDriver", "DriverError"]
