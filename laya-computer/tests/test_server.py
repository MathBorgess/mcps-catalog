import asyncio
import json

import pytest
from laya_computer.server import Manager


class Decider:
    def close(self):
        pass


class Driver:
    def __init__(self, observations=None):
        self.observations = list(observations or [])
        self.bundle = None
        self.observe_calls = 0
        self.closed = False

    async def start(self, bundle):
        self.bundle = bundle

    async def observe(self, window_title=None):
        self.observe_calls += 1
        if self.observations:
            state = self.observations.pop(0)
            if len(self.observations) == 0:
                self._last_state = state
        else:
            state = getattr(self, "_last_state", {
                "title": "Fixture", "elements": [{"id": "1", "role": "AXButton", "label": "Done"}]
            })
        state = dict(state)
        state.setdefault("app_bundle_id", self.bundle)
        return state

    async def act(self, action, candidate, step, snapshot):
        return {"status": "accepted", "effect": action}

    async def close(self):
        self.closed = True


def plan(app="test.app"):
    return {"version": 1, "goal": "Check fixture", "app_bundle_id": app, "steps": [
        {"id": "check", "instruction": "Check Done", "action": "verify",
         "success": [{"kind": "exists", "label_contains": "Done"}]}
    ]}


def test_background_run_and_polling_does_not_reobserve():
    async def run():
        m = Manager(Driver, Decider())
        started = await m.start(plan())
        result = await m.get(started["run_id"], 1)
        assert result["status"] == "completed"
        d = m.entries[started["run_id"]].run.driver
        count = d.observe_calls
        await m.get(started["run_id"])
        assert d.observe_calls == count
        await m.close()
    asyncio.run(run())


def test_concurrent_app_run_is_rejected_before_launch():
    class WaitingDriver(Driver):
        async def observe(self, window_title=None):
            await asyncio.Event().wait()

    async def run():
        made = []
        def factory():
            driver = WaitingDriver()
            made.append(driver)
            return driver
        m = Manager(factory, Decider())
        await m.start(plan())
        with pytest.raises(ValueError, match="active operation"):
            await m.start(plan())
        assert len(made) == 1
        await m.close()
    asyncio.run(run())


def test_initial_driver_failure_is_reported_without_success_and_releases_lease():
    class Broken(Driver):
        async def start(self, bundle):
            self.bundle = bundle
            raise RuntimeError("permissions_pending")
    async def run():
        m = Manager(Broken, Decider())
        started = await m.start(plan())
        result = await m.get(started["run_id"], 1)
        assert result["status"] == "blocked"
        assert result["verification"] == "unknown"
        assert not m.busy
        assert m.entries[started["run_id"]].run.driver.closed
        await m.close()
    asyncio.run(run())


def test_inspect_bounds_large_menu_tree_and_always_releases_lease():
    class MenuDriver(Driver):
        async def observe(self, window_title=None):
            self.observe_calls += 1
            return {
                "app_bundle_id": self.bundle,
                "title": "X" * 1000,
                "elements": [
                    {"role": "AXMenuItem", "label": f"Item {i}" + "x" * 1000,
                     "value": "v" * 2000, "actions": ["click"] * 50, "token": f"secret-{i}"}
                    for i in range(400)
                ],
            }
    async def run():
        driver = MenuDriver()
        m = Manager(lambda: driver, Decider())
        result = await m.inspect("test.app")
        assert len(result["elements"]) <= 120
        assert result["element_count"] == 400
        assert result["truncated"] is True
        assert len(json.dumps(result)) < 40_000
        assert "token" not in json.dumps(result)
        assert driver.closed
        assert not m.busy
        await m.close()
    asyncio.run(run())


def test_inspect_driver_factory_failure_does_not_leak_app_lease():
    def broken_factory():
        raise RuntimeError("cannot create driver")
    async def run():
        m = Manager(broken_factory, Decider())
        with pytest.raises(RuntimeError, match="cannot create driver"):
            await m.inspect("test.app")
        assert not m.busy
        await m.close()
    asyncio.run(run())


def test_invalid_resume_repair_is_rejected_without_poisoning_paused_run():
    async def run():
        before = {"title": "Fixture", "elements": [
            {"id": "open", "role": "button", "label": "Details", "enabled": True}
        ]}
        details = {"title": "Fixture", "elements": [
            {"id": "open", "role": "button", "label": "Details", "enabled": True},
            {"id": "window", "role": "window", "label": "Information"},
        ]}
        with_date = {"title": "Fixture", "elements": details["elements"] + [
            {"id": "date", "role": "text", "label": "Date", "value": "1901"}
        ]}
        driver = Driver([before, details, details, with_date])
        m = Manager(lambda: driver, Decider())
        initial = {
            "version": 1,
            "goal": "Open and read details",
            "app_bundle_id": "test.app",
            "steps": [
                {"id": "open", "instruction": "Open details", "action": "click",
                 "target": {"role": "button", "label_contains": "Details"},
                 "success": [{"kind": "exists", "role": "window", "label_contains": "Information"}],
                 "next_step": "read"},
                {"id": "read", "instruction": "Read date", "action": "verify",
                 "success": [{"kind": "value_contains", "role": "text", "label_contains": "Date", "value": "1900"}]},
            ],
        }
        started = await m.start(initial)
        paused = await m.get(started["run_id"], 1)
        assert paused["status"] == "rescue_needed"
        changed_completed = [dict(initial["steps"][0], instruction="Altered finished step"),
                             dict(initial["steps"][1], success=[{"kind": "value_contains", "role": "text", "label_contains": "Date", "value": "1901"}])]
        with pytest.raises(ValueError, match="completed step"):
            await m.resume(started["run_id"], 1, {
                **initial, "version": 2, "start_step": "read", "steps": changed_completed
            })
        assert m.entries[started["run_id"]].error is None
        assert m.entries[started["run_id"]].run.status == "rescue_needed"
        repaired = [initial["steps"][0], dict(initial["steps"][1], success=[
            {"kind": "value_contains", "role": "text", "label_contains": "Date", "value": "1901"}
        ])]
        resumed = await m.resume(started["run_id"], 1, {
            **initial, "version": 2, "start_step": "read", "steps": repaired
        })
        completed = await m.get(started["run_id"], 1)
        assert resumed["run_id"] == completed["run_id"]
        assert completed["status"] == "completed"
        assert completed["completed_steps"] == ["open", "read"]
        await m.close()
    asyncio.run(run())


def test_stop_paused_run_closes_driver_and_marks_stopped():
    async def run():
        class NeverDone(Driver):
            async def observe(self, window_title=None):
                self.observe_calls += 1
                return {"app_bundle_id": self.bundle, "title": "Fixture", "elements": []}
        driver = NeverDone()
        m = Manager(lambda: driver, Decider())
        started = await m.start(plan())
        paused = await m.get(started["run_id"], 1)
        assert paused["status"] == "rescue_needed"
        result = await m.stop(started["run_id"])
        assert result["status"] == "stopped"
        assert driver.closed
        assert not m.busy
        await m.close()
    asyncio.run(run())


def test_stop_during_observation_prevents_the_next_action():
    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()

        class BlockingObserve(Driver):
            def __init__(self):
                super().__init__()
                self.action_calls = 0

            async def observe(self, window_title=None):
                entered.set()
                await release.wait()
                return {"app_bundle_id": self.bundle, "title": "Fixture", "elements": [
                    {"id": "1", "role": "button", "label": "Done"}
                ]}

            async def act(self, action, candidate, step, snapshot):
                self.action_calls += 1
                return {"status": "accepted"}

        driver = BlockingObserve()
        click_plan = {"version": 1, "goal": "Click Done", "app_bundle_id": "test.app", "steps": [
            {"id": "click", "instruction": "Click Done", "action": "click",
             "target": {"role": "button", "label_contains": "Done"},
             "success": [{"kind": "exists", "role": "window", "label_contains": "Done"}]}
        ]}
        m = Manager(lambda: driver, Decider())
        started = await m.start(click_plan)
        await entered.wait()
        stopping = asyncio.create_task(m.stop(started["run_id"]))
        await asyncio.sleep(0)
        release.set()
        result = await stopping
        assert result["status"] == "stopped"
        assert driver.action_calls == 0
        assert driver.closed
        await m.close()
    asyncio.run(run())


def test_cancelled_run_closes_driver_and_releases_lease():
    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()
        class Blocking(Driver):
            async def observe(self, window_title=None):
                entered.set()
                await release.wait()
                return {"app_bundle_id": self.bundle, "title": "Fixture", "elements": [
                    {"id": "1", "role": "button", "label": "Done"}
                ]}
        driver = Blocking()
        m = Manager(lambda: driver, Decider())
        started = await m.start(plan())
        task = m.entries[started["run_id"]].task
        await entered.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        result = await m.get(started["run_id"])
        assert result["status"] == "blocked"
        assert "CancelledError" in result["error"]
        assert driver.closed
        assert not m.busy
        release.set()
        await m.close()
    asyncio.run(run())


def test_finished_entries_are_evicted_at_capacity_but_active_entries_are_not():
    async def run():
        m = Manager(Driver, Decider())
        ids = []
        for index in range(32):
            started = await m.start(plan(f"test.app.{index}"))
            ids.append(started["run_id"])
            result = await m.get(started["run_id"], 1)
            assert result["status"] == "completed"
        next_started = await m.start(plan("test.app.32"))
        await m.get(next_started["run_id"], 1)
        assert len(m.entries) == 32
        assert ids[0] not in m.entries
        await m.close()
    asyncio.run(run())


def test_failed_entries_are_evicted_instead_of_exhausting_session_capacity():
    class Broken(Driver):
        async def start(self, bundle):
            self.bundle = bundle
            raise RuntimeError("synthetic startup failure")

    async def run():
        m = Manager(Broken, Decider())
        run_ids = []
        for index in range(33):
            started = await m.start(plan(f"failure.app.{index}"))
            run_ids.append(started["run_id"])
            result = await m.get(started["run_id"], 1)
            assert result["status"] == "blocked"
        assert len(m.entries) == 32
        assert run_ids[0] not in m.entries
        assert not m.busy
        await m.close()

    asyncio.run(run())
