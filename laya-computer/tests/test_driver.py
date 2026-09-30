import asyncio
import json
from types import SimpleNamespace

import pytest
from laya_computer.driver import CuaDriver, DriverError, _normalize_tool_result, _SdkTransport


class Transport:
    def __init__(self):
        self.calls = []
        self.fail = None

    async def request(self, tool, args):
        self.calls.append((tool, args))
        if self.fail:
            raise self.fail
        return {"effect": "confirmed"}

    async def close(self):
        pass


def fixture():
    transport = Transport()
    driver = CuaDriver(transport=transport)
    snapshot = {"snapshot_id": "s1", "elements": [
        {"id": "1", "token": "s1:1", "snapshot_id": "s1", "element_index": 1}
    ]}
    return transport, driver, snapshot


def ToolResult(**kw):  # noqa: N802 - mirrors the ``cua_driver.ToolResult`` constructor
    """Shape of ``cua_driver.ToolResult`` (only the fields the adapter reads)."""
    defaults = dict(text="", structured_json=None, is_error=False, error_code=None, action=None, verification=None)
    return SimpleNamespace(**{**defaults, **kw})


def action(effect="CONFIRMED", route="ACCESSIBILITY", **extra):
    named = lambda n: SimpleNamespace(name=n)  # noqa: E731 - mimics an SDK enum member
    base = dict(effect=named(effect), route=named(route), delivery=None, evidence=None,
                escalation=None, summary=None, error=None)
    return SimpleNamespace(**{**base, **extra})


def test_structured_json_is_the_payload():
    data = _normalize_tool_result(ToolResult(structured_json='{"snapshot_id": "s1", "elements": []}', text="redundant"))
    assert data == {"snapshot_id": "s1", "elements": []}


def test_typed_action_effect_is_flattened_into_the_result():
    data = _normalize_tool_result(ToolResult(action=action("UNVERIFIABLE", "SYNTHETIC_EVENTS")))
    assert data["effect"] == "unverifiable" and data["route"] == "synthetic_events"
    data = _normalize_tool_result(ToolResult(
        structured_json='{"ok": true}',
        action=action("PARTIAL", delivery=SimpleNamespace(mode=SimpleNamespace(name="BACKGROUND"), delivered_count=1),
                      escalation=SimpleNamespace(target=SimpleNamespace(name="PIXEL"),
                                                 reason=SimpleNamespace(name="EFFECT_UNCONFIRMED")))))
    assert data["ok"] and data["delivery_mode"] == "background"
    assert data["escalation"] == {"target": "pixel", "reason": "effect_unconfirmed"}


def test_tool_errors_are_bounded_driver_errors_not_successes():
    body = '{"refusal": {"code": "invalid_element_token", "message": "bad token"}, "status": "refused"}'
    with pytest.raises(DriverError) as exc:
        _normalize_tool_result(ToolResult(is_error=True, error_code="invalid_element_token", text="bad token",
                                          structured_json=body))
    assert exc.value.code == "invalid_element_token" and not exc.value.uncertain
    with pytest.raises(DriverError) as exc:
        _normalize_tool_result(ToolResult(is_error=True, text="element_token_stale: snapshot superseded"))
    assert exc.value.code == "stale_snapshot"
    with pytest.raises(DriverError) as exc:
        _normalize_tool_result(ToolResult(is_error=True, text="cannot inject input"))
    assert exc.value.code == "driver_error"


def test_refused_effect_is_a_certain_failure_with_the_documented_error_shape():
    refused = action("REFUSED")
    refused.error = SimpleNamespace(code="ambiguous_window_target", hint="pass window_id")
    with pytest.raises(DriverError) as exc:
        _normalize_tool_result(ToolResult(action=refused))
    assert exc.value.code == "ambiguous_window_target" and "window_id" in str(exc.value)
    assert not exc.value.uncertain
    with pytest.raises(DriverError) as exc:
        _normalize_tool_result(ToolResult(structured_json='{"effect": "refused", "code": "element_outside_target_window",'
                                                           ' "reason": "outside"}'))
    assert exc.value.code == "element_outside_target_window"


def test_sdk_transport_serializes_arguments_and_normalizes_results():
    class FakeSdkDriver:
        def __init__(self):
            self.calls, self.closed = [], False

        async def call_tool(self, name, arguments_json):
            self.calls.append((name, json.loads(arguments_json)))
            return ToolResult(action=action("CONFIRMED"))

        async def shutdown(self):
            self.closed = True

    async def run():
        sdk = FakeSdkDriver()
        transport = _SdkTransport(driver=sdk)
        result = await transport.request("click", {"pid": 3, "element_token": "s1:1"})
        assert sdk.calls == [("click", {"pid": 3, "element_token": "s1:1"})]
        assert result["effect"] == "confirmed"
        await transport.close()
        assert sdk.closed
    asyncio.run(run())


def test_sdk_infrastructure_failure_is_a_transport_error():
    class Broken:
        async def call_tool(self, name, arguments_json):
            raise OSError("socket closed")

    async def run():
        with pytest.raises(DriverError) as exc:
            await _SdkTransport(driver=Broken()).request("list_windows", {})
        assert exc.value.code == "transport_error"
    asyncio.run(run())


def test_real_sdk_refuses_a_malformed_element_token():
    """Runs the real (embedded) Cua SDK; skipped where it is not installed."""
    pytest.importorskip("cua_driver")

    async def run():
        transport = _SdkTransport()
        try:
            with pytest.raises(DriverError) as exc:
                await transport.request("click", {"pid": 1, "window_id": 1, "element_token": "bogus"})
            assert exc.value.code == "invalid_element_token" and not exc.value.uncertain
            assert (await transport.request("list_windows", {}))["windows"] is not None
        finally:
            await transport.close()
    asyncio.run(run())


def test_native_menu_uses_observed_ancestry_and_native_menu_route():
    class MenuTransport(Transport):
        async def request(self, tool, args):
            self.calls.append((tool, args))
            if tool != "get_window_state":
                return {"effect": "confirmed"}
            return {"snapshot_id": "s1", "elements": [
                {"element_index": 1, "role": "AXMenuBarItem", "label": "View"},
                {"element_index": 2, "role": "AXMenu", "parent_index": 1},
                {"element_index": 3, "role": "AXMenuItem", "label": "Sort", "parent_index": 2},
                {"element_index": 4, "role": "AXMenu", "parent_index": 3},
                {"element_index": 5, "element_token": "s1:5", "role": "AXMenuItem",
                 "label": "Oldest", "parent_index": 4}
            ]}
    async def run():
        t = MenuTransport()
        d = CuaDriver(transport=t)
        await d.start("test", pid=3, window_id=4)
        s = await d.observe()
        await d.act("click", s["elements"][-1], SimpleNamespace(), s)
        assert t.calls[-1] == ("invoke_menu", {"pid": 3, "window_id": 4, "path": ["View", "Sort", "Oldest"]})
    asyncio.run(run())


def test_stale_handle_is_rejected_before_transport():
    async def run():
        t, d, s = fixture()
        await d.start("test", pid=3, window_id=4)
        with pytest.raises(DriverError, match="not bound"):
            await d.act("click", {"token": "old:1", "snapshot_id": "old"}, SimpleNamespace(), s)
        assert not t.calls
    asyncio.run(run())


def test_keyboard_and_scroll_use_exact_window_and_observed_token():
    async def run():
        t, d, s = fixture()
        await d.start("test", pid=3, window_id=4)
        await d.act("press_key", None, SimpleNamespace(key="cmd+home"), s)
        assert t.calls[-1] == ("press_key", {"pid": 3, "window_id": 4, "key": "home", "modifiers": ["cmd"]})
        await d.act("scroll", s["elements"][0], SimpleNamespace(direction="up"), s)
        assert t.calls[-1][1]["element_token"] == "s1:1"
    asyncio.run(run())


def test_mutation_timeout_is_uncertain_and_not_retried():
    async def run():
        t, d, s = fixture()
        await d.start("test", pid=3, window_id=4)
        t.fail = asyncio.TimeoutError()
        with pytest.raises(DriverError) as error:
            await d.act("click", s["elements"][0], SimpleNamespace(), s)
        assert error.value.uncertain
        assert len(t.calls) == 1
    asyncio.run(run())


def test_token_never_becomes_model_candidate_id():
    class StateTransport(Transport):
        async def request(self, tool, args):
            return {"snapshot_id": "s123", "elements": [
                {"element_index": 7, "element_token": "s123:7", "role": "AXButton", "label": "Go"}
            ]}
    async def run():
        d = CuaDriver(transport=StateTransport())
        await d.start("test", pid=3, window_id=4)
        s = await d.observe()
        assert s["elements"][0]["id"] == "7"
        assert s["elements"][0]["token"] == "s123:7"
    asyncio.run(run())
