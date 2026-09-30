"""Obstacle policy with synthetic trees. They prove the controller's rules, not real Photos behavior."""

import asyncio

import pytest
from laya_computer.engine import Run
from laya_computer.obstacles import find_obstacle
from laya_computer.plan import Plan
from pydantic import ValidationError

APP = "com.example.Photos"


def plan(*, obstacles=None, target_label="Details"):
    body = {
        "version": 1, "goal": "Open the details panel", "app_bundle_id": APP,
        "steps": [{
            "id": "open", "instruction": f"Open the {target_label} button", "action": "click",
            "target": {"role": "AXButton", "label_contains": target_label},
            "success": [{"kind": "exists", "role": "AXWindow", "label_contains": "Information"}],
        }],
    }
    if obstacles is not None:
        body["obstacles"] = obstacles
    return Plan.model_validate(body)


class SheetDriver:
    """A window whose main controls are hidden while a sheet with ``buttons`` is open."""

    def __init__(self, buttons, *, sticky=False, then=()):
        self.buttons, self.sticky, self.then = list(buttons), sticky, [list(b) for b in then]
        self.sheet_open, self.details, self.clicked = True, False, []

    def _el(self, index, role, label, parent=None):
        return {"id": str(index), "element_index": index, "parent_index": parent, "role": role, "label": label,
                "enabled": True, "token": f"t{index}", "snapshot_id": "s"}

    async def observe(self, window_title=None):
        elements = [self._el(1, "AXWindow", "Photos")]
        if self.sheet_open:
            elements.append(self._el(10, "AXSheet", "Welcome", 1))
            elements += [self._el(11 + i, "AXButton", label, 10) for i, label in enumerate(self.buttons)]
        else:
            elements.append(self._el(2, "AXButton", "Details", 1))
        if self.details:
            elements.append(self._el(3, "AXWindow", "Information"))
        return {"snapshot_id": "s", "title": "Photos", "app_bundle_id": APP, "elements": elements, "truncated": False}

    async def act(self, action, candidate, step, snapshot):
        self.clicked.append(candidate["label"])
        if candidate["label"] == "Details":
            self.details = True
        elif not self.sticky:
            # A different sheet may follow the dismissed one (a new set of buttons).
            self.sheet_open = bool(self.then)
            if self.then:
                self.buttons = self.then.pop(0)
        return {"effect": "confirmed"}


class Decider:
    def __init__(self, pick):
        self.pick, self.calls = pick, []

    async def choose(self, instruction, candidates, snapshot):
        self.calls.append((instruction, [c["label"] for c in candidates]))
        return next(c["id"] for c in candidates if c["label"] == self.pick)


def run(p, driver, decider=None):
    return asyncio.run(Run(p, driver, decider or Decider("Not Now")).execute())


def test_single_allowed_button_is_pressed_without_laya_then_the_plan_continues():
    driver, decider = SheetDriver(["Not Now", "Delete"]), Decider("unused")
    result = run(plan(), driver, decider)
    assert result["status"] == "completed" and result["verification"] == "satisfied"
    assert driver.clicked == ["Not Now", "Details"] and not decider.calls
    assert result["metrics"]["obstacles_dismissed"] == 1 and result["metrics"]["actions"] == 2


def test_laya_only_ranks_among_allowed_buttons():
    driver, decider = SheetDriver(["Not Now", "Close", "Delete"]), Decider("Close")
    result = run(plan(), driver, decider)
    assert result["status"] == "completed" and driver.clicked == ["Close", "Details"]
    assert decider.calls == [("Which control closes this notice without confirming a change, granting access, or"
                              " touching user data?", ["Not Now", "Close"])]


def test_unlisted_buttons_are_never_pressed_and_go_to_the_caller():
    driver = SheetDriver(["Delete Photos", "Cancel"])
    result = run(plan(), driver)
    assert result["status"] == "rescue_needed" and result["reason"] == "obstacle_needs_decision"
    assert driver.clicked == [] and result["metrics"]["actions"] == 0
    assert result["rescue_context"]["obstacle"]["other_buttons"] == ["Delete Photos", "Cancel"]


def test_plan_can_extend_the_allow_list_but_not_with_destructive_labels():
    driver = SheetDriver(["Skip Tour", "Delete Photos"])
    assert run(plan(obstacles={"dismiss_labels": ["Skip Tour"]}), driver)["status"] == "completed"
    assert driver.clicked[0] == "Skip Tour"
    for label in ("Delete", "Don’t Allow", "Turn On Sync", " ", "x" * 61):
        with pytest.raises(ValidationError):
            plan(obstacles={"dismiss_labels": [label]})


def test_a_dismissal_must_be_verified_or_the_run_pauses():
    driver = SheetDriver(["Not Now"], sticky=True)
    result = run(plan(), driver)
    assert result["status"] == "rescue_needed" and result["reason"] == "obstacle_not_dismissed"
    assert driver.clicked == ["Not Now"] and result["metrics"]["obstacles_dismissed"] == 0


def test_dismissal_budget_is_bounded_and_counts_as_actions():
    driver = SheetDriver(["Not Now"], then=[["Later"]])
    result = run(plan(obstacles={"max_dismissals": 1}), driver)
    assert result["status"] == "rescue_needed" and result["reason"] == "obstacle_limit"
    assert driver.clicked == ["Not Now"] and result["metrics"]["actions"] == 1
    assert run(plan(), SheetDriver(["Not Now"], then=[["Later"]]))["status"] == "completed"


def test_disabled_policy_keeps_the_old_behavior():
    driver = SheetDriver(["Not Now"])
    result = run(plan(obstacles={"max_dismissals": 0}), driver)
    assert result["status"] == "rescue_needed" and result["reason"] == "target_not_found"
    assert driver.clicked == []


def test_a_button_the_plan_asks_for_is_a_step_not_an_obstacle():
    driver = SheetDriver(["Not Now"])
    p = plan(target_label="Not Now")
    p.steps[0].success[0].role, p.steps[0].success[0].label_contains = "AXWindow", "Photos"
    result = run(p, driver)
    assert result["status"] == "completed" and driver.clicked == ["Not Now"]
    assert result["metrics"]["obstacles_dismissed"] == 0


def test_buttons_outside_the_blocking_container_are_ignored():
    snap = {"elements": [
        {"element_index": 1, "role": "AXWindow", "label": "Photos"},
        {"element_index": 2, "role": "AXButton", "label": "Close", "parent_index": 1},
        {"element_index": 10, "role": "AXSheet", "label": "Welcome", "parent_index": 1},
        {"element_index": 11, "role": "AXButton", "label": "Not Now", "parent_index": 10},
    ]}
    obstacle = find_obstacle(snap)
    assert [b["label"] for b in obstacle.allowed] == ["Not Now"]
    assert find_obstacle({"elements": snap["elements"][:2]}) is None
