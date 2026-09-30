import pytest
from laya_computer.plan import Plan, Step
from pydantic import ValidationError


def click_step(**overrides):
    value = {
        "id": "open",
        "instruction": "Open the item",
        "action": "click",
        "target": {"role": "button", "label_contains": "Open"},
        "success": [{"kind": "exists", "role": "window", "label_contains": "Details"}],
    }
    value.update(overrides)
    return value


def test_plan_accepts_bounded_explicit_branching_and_cycles():
    plan = Plan(
        version=1,
        goal="Open details",
        app_bundle_id="com.example.App",
        start_step="open",
        steps=[
            click_step(next_step="check", on_failure="open"),
            {
                "id": "check",
                "instruction": "Confirm details appeared",
                "action": "verify",
                "success": [{"kind": "exists", "role": "window"}],
            },
        ],
    )
    assert plan.first_step_id == "open"


@pytest.mark.parametrize(
    "step",
    [
        {"id": "x", "instruction": "click", "action": "click", "success": [{"kind": "exists", "role": "button"}]},
        {"id": "x", "instruction": "type", "action": "set_value", "target": {"role": "text field"}, "success": [{"kind": "exists", "role": "text field"}]},
        {"id": "x", "instruction": "wait", "action": "wait", "success": [{"kind": "exists", "role": "button"}]},
        {"id": "x", "instruction": "verify", "action": "verify", "success": []},
    ],
)
def test_step_rejects_missing_or_inconsistent_action_arguments(step):
    with pytest.raises(ValidationError):
        Step.model_validate(step)


def test_plan_rejects_unknown_branch_and_unbounded_limits():
    with pytest.raises(ValidationError):
        Plan(version=1, goal="x", app_bundle_id="com.example.App", steps=[click_step(next_step="missing")])
    with pytest.raises(ValidationError):
        Plan(version=1, goal="x", app_bundle_id="com.example.App", steps=[click_step()], max_actions=61)
