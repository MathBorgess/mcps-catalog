"""Adapter for the Cua Driver SDK (``cua_driver``).

The module deliberately has no platform-native imports.  It can therefore be
imported on Linux and in portable tests, while the native runtime is only
loaded when a driver instance is started.  By default the SDK runs the driver
*embedded* in this process; set ``CUA_DRIVER_SOCKET`` to talk to an already
running ``cua-driver serve`` daemon instead (useful on macOS, where the
Accessibility and Screen Recording grants belong to the process that hosts the
driver).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
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


class _SdkTransport:
    """Call Cua tools through the typed SDK; tool errors arrive as ``ToolResult``s."""

    def __init__(self, *, socket_path: str | None = None, driver: Any = None):
        self.socket_path = socket_path
        self._driver = driver

    async def _ensure_driver(self) -> Any:
        if self._driver is None:
            try:
                # Imported lazily: the SDK bundles a native library, and the
                # portable unit tests never need it.
                import cua_driver

                self._driver = await asyncio.to_thread(
                    cua_driver.CuaDriver.connect if self.socket_path else cua_driver.CuaDriver.create,
                    *([self.socket_path] if self.socket_path else []),
                )
            except Exception as exc:  # noqa: BLE001 - surfaced as a bounded driver error
                raise _transport_error(exc) from exc
        return self._driver

    async def request(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        driver = await self._ensure_driver()
        try:
            result = await driver.call_tool(tool, json.dumps(arguments))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - infrastructure failure, not a tool result
            raise _transport_error(exc) from exc
        return _normalize_tool_result(result)

    async def close(self) -> None:
        driver, self._driver = self._driver, None
        if driver is not None:
            await driver.shutdown()


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


def _enum_name(value: Any) -> str:
    """``ActionEffect.CONFIRMED`` -> ``confirmed`` (works for any SDK enum)."""
    return str(getattr(value, "name", value)).lower()


def _action_fields(action: Any) -> dict[str, Any]:
    """Flatten the SDK's typed ``ActionResult`` into compact, JSON-safe fields."""
    fields: dict[str, Any] = {"effect": _enum_name(action.effect), "route": _enum_name(action.route)}
    delivery = getattr(action, "delivery", None)
    if delivery is not None:
        fields["delivery_mode"] = _enum_name(delivery.mode)
    escalation = getattr(action, "escalation", None)
    if escalation is not None:
        fields["escalation"] = {"target": _enum_name(escalation.target), "reason": _enum_name(escalation.reason)}
    evidence = getattr(action, "evidence", None)
    if evidence:
        fields["evidence"] = [{"kind": _enum_name(item.kind), "detail": str(item.detail or "")[:120]}
                              for item in evidence[:4]]
    error = getattr(action, "error", None)
    if error is not None:
        fields["error"] = {"code": str(error.code), "hint": str(error.hint or "")}
    if getattr(action, "summary", None):
        fields["summary"] = str(action.summary)[:200]
    return fields


def _normalize_tool_result(result: Any) -> dict[str, Any]:
    """Turn an SDK ``ToolResult`` into a plain dict, or raise a bounded ``DriverError``.

    ``call_tool`` does not raise for tool-level failures: they come back with
    ``is_error`` set, an optional machine ``error_code`` and, for refusals, a
    structured ``{"refusal": {"code", "message"}}`` body.
    """
    structured: Any = None
    raw = getattr(result, "structured_json", None)
    if raw:
        try:
            structured = json.loads(raw)
        except ValueError:
            structured = None
    text = str(getattr(result, "text", "") or "")

    if getattr(result, "is_error", False):
        code = getattr(result, "error_code", None)
        message = text
        refusal = structured.get("refusal") if isinstance(structured, dict) else None
        if isinstance(refusal, dict):
            code = code or refusal.get("code")
            message = str(refusal.get("message") or message)
        stale = code is None and bool(_STALE_RE.search(message))
        raise DriverError(
            "stale_snapshot" if stale or code == "element_token_stale" else str(code or "driver_error"),
            message or "Cua Driver returned an error",
            uncertain=False,
        )

    data: dict[str, Any]
    if isinstance(structured, dict):
        data = dict(structured)
    elif structured is not None:
        data = {"result": structured}
    else:
        data = {"text": text}
    action = getattr(result, "action", None)
    if action is not None:
        data.update(_action_fields(action))
    return _unwrap_payload(data)


def _unwrap_payload(data: dict[str, Any]) -> dict[str, Any]:
    # A refused action never reached an actuator, so it is certain, not uncertain.
    if data.get("status") == "refused" or data.get("effect") == "refused" or data.get("refusal"):
        refusal = data.get("refusal") or data.get("error") or {}
        raise DriverError(
            str(refusal.get("code", data.get("code", "refused"))),
            str(refusal.get("message") or refusal.get("hint") or data.get("reason") or "Cua refused before execution"),
        )
    nested = data.get("result")
    if isinstance(nested, dict) and len(data) <= 3:
        return _unwrap_payload({**nested, **{k: v for k, v in data.items() if k != "result"}})
    return data


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
    """Bind and operate one application window through the Cua Driver SDK."""

    def __init__(self, *, socket_path: str | None = None, timeout: float = 30.0,
                 transport: _Transport | None = None):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.socket_path = socket_path or os.environ.get("CUA_DRIVER_SOCKET") or None
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
        self._transport = self._injected_transport or _SdkTransport(socket_path=self.socket_path)

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
