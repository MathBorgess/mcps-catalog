import asyncio
from types import SimpleNamespace

import pytest
from laya_computer.driver import CuaDriver, DriverError, _normalize_result


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


def test_normalization_prefers_structured_payload():
    assert _normalize_result({"structuredContent": {"snapshot_id": "s1", "elements": []},
                              "content": [{"type": "text", "text": "redundant"}]})["snapshot_id"] == "s1"


def test_structured_refusal_is_not_a_successful_action():
    with pytest.raises(DriverError) as exc:
        _normalize_result({"structuredContent": {"status": "refused", "refusal": {
            "code": "permission_denied", "message": "Not authorized"
        }}})
    assert exc.value.code == "permission_denied"
    assert not exc.value.uncertain
    with pytest.raises(DriverError) as exc:
        _normalize_result({"effect": "refused", "code": "element_outside_target_window", "reason": "outside"})
    assert exc.value.code == "element_outside_target_window"
    with pytest.raises(DriverError) as exc:
        _normalize_result({"structuredContent": {"result": {
            "effect": "refused", "code": "permission_denied", "reason": "outside"
        }}})
    assert exc.value.code == "permission_denied"


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
