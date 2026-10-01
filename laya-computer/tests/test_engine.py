import asyncio

import pytest
from laya_computer.engine import Run
from laya_computer.plan import Plan


def plan(*, max_actions=60, max_seconds=300, max_rescues=1, version=1, steps=None, start_step=None):
    return Plan(
        version=version,
        goal="Open the details panel",
        app_bundle_id="com.example.Photos",
        steps=steps or [
            {
                "id": "open",
                "instruction": "Open the Details button",
                "action": "click",
                "target": {"role": "button", "label_contains": "Details"},
                "success": [{"kind": "exists", "role": "window", "label_contains": "Information"}],
            }
        ],
        start_step=start_step,
        max_actions=max_actions,
        max_seconds=max_seconds,
        max_rescues=max_rescues,
    )


def snapshot(*, details=False, truncated=False):
    elements = [{"id": "details-button", "role": "button", "label": "Details", "enabled": True, "actions": ["click"], "token": "private-driver-token"}]
    if details:
        elements.append({"id": "details-window", "role": "window", "label": "Information"})
    return {"snapshot_id": "synthetic", "title": "Photos", "app_bundle_id": "com.example.Photos", "elements": elements, "truncated": truncated}


class FakeDriver:
    def __init__(self, observations, actions=None):
        self.observations = list(observations)
        self.action_results = list(actions or [{"status": "accepted"}])
        self.action_calls = []
        self.observe_calls = 0

    async def observe(self, window_title=None):
        self.observe_calls += 1
        if len(self.observations) > 1:
            return self.observations.pop(0)
        return self.observations[0]

    async def act(self, action, candidate, step, current_snapshot):
        self.action_calls.append((action, candidate, current_snapshot))
        value = self.action_results.pop(0) if self.action_results else {"status": "accepted"}
        if isinstance(value, Exception):
            raise value
        return value


class FirstDecider:
    def __init__(self, chosen=None):
        self.chosen = chosen
        self.calls = []

    async def choose(self, instruction, candidates, current_snapshot):
        self.calls.append((instruction, candidates, current_snapshot))
        return self.chosen or candidates[0]["id"]


class DriverRefusal(Exception):
    code = "target_not_actionable"
    uncertain = False


def test_partial_opt_in_proves_only_positive_observed_predicates():
    p = plan(steps=[{"id": "known", "instruction": "Verify the observed details window",
                     "action": "verify", "allow_partial_observation": True,
                     "success": [{"kind": "exists", "label_contains": "Information"}]}])
    r = Run(p, FakeDriver([snapshot(details=True, truncated=True)]), FirstDecider())
    result = asyncio.run(r.execute())
    assert result["status"] == "completed"
    assert result["evidence"][0]["scope"] == "observed_controls"
    r = Run(p, FakeDriver([snapshot(truncated=True)]), FirstDecider())
    assert asyncio.run(r.execute())["verification"] == "unknown"


def test_partial_opt_in_cannot_claim_ordinal_extreme():
    with pytest.raises(ValueError, match="ordinal"):
        plan(steps=[{"id": "oldest", "instruction": "Choose first photo", "action": "click",
                     "allow_partial_observation": True,
                     "target": {"role": "AXImage", "index": "first"},
                     "success": [{"kind": "exists", "label_contains": "Info"}]}])


def test_execute_cannot_silently_replay_an_uncertain_action_after_rescue():
    driver = FakeDriver([snapshot()], actions=[TimeoutError("possibly applied")])
    r = Run(plan(), driver, FirstDecider())
    first = asyncio.run(r.execute())
    second = asyncio.run(r.execute())
    assert first["status"] == second["status"] == "rescue_needed"
    assert len(driver.action_calls) == 1


def test_action_acceptance_is_followed_by_fresh_predicate_verification():
    before = snapshot()
    after = snapshot(details=True)
    driver = FakeDriver([before, after])
    run = Run(plan(), driver, FirstDecider())

    result = asyncio.run(run.execute())

    assert result["status"] == "completed"
    assert result["verification"] == "satisfied"
    assert result["completed_steps"] == ["open"]
    assert result["metrics"]["actions"] == 1
    assert driver.observe_calls == 2
    assert driver.action_calls[0][1]["id"] == "details-button"
    assert result["evidence"][0]["observed"] == [{
        "kind": "exists", "role": "window", "label": "Information"
    }]


def test_empty_or_truncated_evidence_never_completes_a_verify_step():
    verify_plan = plan(steps=[{
        "id": "check",
        "instruction": "Check details",
        "action": "verify",
        "success": [{"kind": "window_title_contains", "value": "Photos"}],
    }])
    for bad_snapshot in (
        {"title": "Photos", "elements": []},
        {**snapshot(details=True), "truncated": True},
    ):
        run = Run(verify_plan, FakeDriver([bad_snapshot]), FirstDecider())
        result = asyncio.run(run.execute())
        assert result["status"] == "rescue_needed"
        assert result["verification"] == "unknown"
        assert result["completed_steps"] == []


def test_stale_snapshot_reobserves_and_reselects_before_retrying():
    driver = FakeDriver([snapshot(), snapshot(), snapshot(details=True)], actions=[{"status": "stale_snapshot"}, {"status": "accepted"}])
    run = Run(plan(), driver, FirstDecider())
    result = asyncio.run(run.execute())
    assert result["status"] == "completed"
    assert result["metrics"]["actions"] == 1
    assert result["metrics"]["stale_reobservations"] == 1
    assert len(driver.action_calls) == 2
    assert driver.action_calls[0][2]["snapshot_id"] != "synthetic" or len(driver.action_calls) == 2


def test_stale_snapshot_rechecks_preconditions_before_retrying_mutation():
    class Stale(Exception):
        code = "stale_snapshot"
        uncertain = False

    initial = {**snapshot(), "elements": snapshot()["elements"] + [
        {"id": "scope", "role": "window", "label": "Photos"}
    ]}
    changed = snapshot()
    step = {
        "id": "open",
        "instruction": "Open Details in the Photos library",
        "action": "click",
        "target": {"role": "button", "label_contains": "Details"},
        "preconditions": [{"kind": "exists", "role": "window", "label_contains": "Photos"}],
        "success": [{"kind": "exists", "role": "window", "label_contains": "Information"}],
    }
    driver = FakeDriver([initial, changed], actions=[Stale("expired")])
    result = asyncio.run(Run(plan(steps=[step]), driver, FirstDecider()).execute())
    assert result["status"] == "rescue_needed"
    assert result["reason"] == "preconditions_not_satisfied"
    assert len(driver.action_calls) == 1


def test_slow_initial_observation_cannot_complete_verify_only_after_time_limit():
    class SlowObserve(FakeDriver):
        async def observe(self, window_title=None):
            await asyncio.sleep(0.02)
            return await super().observe(window_title)

    verify_step = {
        "id": "check",
        "instruction": "Verify Photos is open",
        "action": "verify",
        "success": [{"kind": "window_title_contains", "value": "Photos"}],
    }
    result = asyncio.run(Run(
        plan(steps=[verify_step], max_seconds=0.005),
        SlowObserve([snapshot()]),
        FirstDecider(),
    ).execute())
    assert result["status"] == "blocked"
    assert result["reason"] == "time_limit"
    assert result["verification"] == "unknown"
    assert result["completed_steps"] == []


def test_uncertain_mutation_is_not_retried_and_returns_compact_rescue_context():
    driver = FakeDriver([snapshot()], actions=[TimeoutError("possibly applied")])
    run = Run(plan(), driver, FirstDecider())
    result = asyncio.run(run.execute())
    assert result["status"] == "rescue_needed"
    assert result["reason"] == "action_effect_unknown"
    assert len(driver.action_calls) == 1
    assert result["rescue_context"]["goal"] == "Open the details panel"
    assert result["rescue_context"]["app_bundle_id"] == "com.example.Photos"
    assert "token" not in str(result["rescue_context"])
    assert result["metrics"]["remote_tokens"] is None


def test_explicit_no_effect_refusal_is_not_misclassified_as_uncertain_or_retried():
    driver = FakeDriver([snapshot()], actions=[DriverRefusal("Cua refused before execution")])
    run = Run(plan(), driver, FirstDecider())
    result = asyncio.run(run.execute())
    assert result["status"] == "rescue_needed"
    assert result["reason"] == "action_rejected"
    assert len(driver.action_calls) == 1
    assert result["metrics"]["actions"] == 1
    assert result["rescue_context"]["recent_effects"][-1]["effect"] == "no_effect"


def test_decider_can_only_choose_an_observed_matching_candidate():
    two = {**snapshot(), "elements": [
        {"id": "a", "role": "button", "label": "Details A"},
        {"id": "b", "role": "button", "label": "Details B"},
    ]}
    decider = FirstDecider(chosen="invented-id")
    driver = FakeDriver([two])
    result = asyncio.run(Run(plan(), driver, decider).execute())
    assert result["status"] == "rescue_needed"
    assert result["reason"] == "decider_returned_unobserved_candidate"
    assert driver.action_calls == []
    assert "token" not in str(decider.calls)


def test_resume_requires_next_version_and_preserves_goal_app_completed_work_and_budget():
    first_steps = [
        {
            "id": "open",
            "instruction": "Open Details",
            "action": "click",
            "target": {"role": "button", "label_contains": "Details"},
            "success": [{"kind": "exists", "role": "window", "label_contains": "Information"}],
            "next_step": "read",
        },
        {
            "id": "read",
            "instruction": "Read date",
            "action": "verify",
            "success": [{"kind": "value_contains", "role": "text", "label_contains": "Date", "value": "1900"}],
        },
    ]
    with_date = {**snapshot(details=True), "elements": snapshot(details=True)["elements"] + [
        {"id": "date", "role": "text", "label": "Date", "value": "1901"}
    ]}
    driver = FakeDriver([snapshot(), snapshot(details=True), snapshot(details=True), with_date])
    run = Run(plan(steps=first_steps, max_actions=3), driver, FirstDecider())
    result = asyncio.run(run.execute())
    assert result["status"] == "rescue_needed"
    assert result["completed_steps"] == ["open"]
    repaired_steps = [first_steps[0], {**first_steps[1], "success": [{"kind": "value_contains", "role": "text", "label_contains": "Date", "value": "1901"}]}]
    with pytest.raises(ValueError, match="expected_version"):
        asyncio.run(run.resume(plan(steps=repaired_steps, version=3), expected_version=1))
    with pytest.raises(ValueError, match="completed step"):
        changed_open = {**first_steps[0], "instruction": "Change completed step"}
        asyncio.run(run.resume(plan(steps=[changed_open, repaired_steps[1]], version=2), expected_version=1))
    with pytest.raises(ValueError, match="preserve"):
        asyncio.run(run.resume(Plan(version=2, goal="different", app_bundle_id="com.example.Photos", steps=repaired_steps), expected_version=1))

    result = asyncio.run(run.resume(plan(steps=repaired_steps, version=2, start_step="read", max_actions=60), expected_version=1))
    assert result["status"] == "completed"
    assert result["completed_steps"] == ["open", "read"]
    assert result["plan_version"] == 2
    assert result["metrics"]["actions"] == 1


def test_action_budget_blocks_instead_of_reporting_success():
    two_steps = [
        {
            "id": "one",
            "instruction": "Open details",
            "action": "click",
            "target": {"role": "button", "label_contains": "Details"},
            "success": [{"kind": "exists", "role": "window", "label_contains": "Information"}],
            "next_step": "two",
        },
        {
            "id": "two",
            "instruction": "Open another details panel",
            "action": "click",
            "target": {"role": "button", "label_contains": "Details"},
            "success": [{"kind": "exists", "role": "window", "label_contains": "Information"}],
        },
    ]
    driver = FakeDriver([snapshot(), snapshot(details=True)])
    result = asyncio.run(Run(plan(steps=two_steps, max_actions=1), driver, FirstDecider()).execute())
    assert result["status"] == "blocked"
    assert result["reason"] == "action_limit"
    assert result["verification"] == "unknown"


def test_stop_requested_during_target_decision_prevents_the_pending_action():
    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()

        class SlowDecider(FirstDecider):
            async def choose(self, instruction, candidates, current_snapshot):
                entered.set()
                await release.wait()
                return candidates[0]["id"]

        ambiguous = {**snapshot(), "elements": [
            {"id": "details-1", "role": "button", "label": "Details One"},
            {"id": "details-2", "role": "button", "label": "Details Two"},
        ]}
        driver = FakeDriver([ambiguous])
        controller = Run(plan(), driver, SlowDecider())
        task = asyncio.create_task(controller.execute())
        await entered.wait()
        controller.request_stop()
        release.set()
        result = await task
        assert result["status"] == "stopped"
        assert driver.action_calls == []

    asyncio.run(run())
