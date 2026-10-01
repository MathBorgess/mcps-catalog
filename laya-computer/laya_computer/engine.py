"""Async local plan controller. It never invokes a planning or rescue LLM."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Protocol

from .plan import Action, Plan, Predicate, PredicateKind, Selector, Step


class Driver(Protocol):
    async def observe(self, window_title: str | None = None) -> dict[str, Any]: ...

    async def act(
        self, action: str, candidate: dict[str, Any] | None, step: Step, snapshot: dict[str, Any]
    ) -> dict[str, Any]: ...


class Decider(Protocol):
    async def choose(
        self, instruction: str, candidates: list[dict[str, Any]], snapshot: dict[str, Any]
    ) -> str: ...


class Run:
    """Execute a validated plan against a caller-owned driver and bounded decider.

    Result statuses are ``running``, ``completed``, ``rescue_needed``, ``blocked``,
    and ``stopped``. Verification is reported separately. A resume must advance
    the plan version and cannot change the goal, app, or already completed steps.
    """

    NO_PROGRESS_LIMIT = 2
    STALE_REOBSERVE_LIMIT = 2

    def __init__(self, plan: Plan, driver: Driver, decider: Decider):
        self.plan = plan
        self.driver = driver
        self.decider = decider
        self.current_step_id: str | None = plan.first_step_id
        self.completed_steps: list[str] = []
        self._completed_specs: dict[str, dict[str, Any]] = {}
        self._active_elapsed = 0.0
        self._active_started: float | None = None
        self._action_limit = plan.max_actions
        self._time_limit = plan.max_seconds
        self._rescue_limit = plan.max_rescues
        self._actions = 0
        self._decisions = 0
        self._stale_reobservations = 0
        self._rescues = 0
        self._transitions = 0
        self._no_progress = 0
        self._history: list[dict[str, Any]] = []
        self._evidence: list[dict[str, Any]] = []
        self._stop_requested = False
        self._status = "running"
        self._verification = "unknown"
        self._lock = asyncio.Lock()

    def request_stop(self) -> None:
        """Request cooperative stop; the current awaited driver call may finish."""
        self._stop_requested = True

    @property
    def status(self) -> str:
        return self._status

    def result(self) -> dict[str, Any]:
        """Return the current compact in-memory state without observing the app."""
        return self._result()

    def snapshot(self) -> dict[str, Any]:
        """Alias for result(), suitable for polling by a session manager."""
        return self._result()

    async def execute(self) -> dict[str, Any]:
        async with self._lock:
            if self._status in {"completed", "blocked", "stopped"}:
                return self._result()
            if self._status == "rescue_needed" and not self._stop_requested:
                return self._result()
            self._status = "running"
            self._begin_active_time()
            try:
                return await self._drive()
            finally:
                self._end_active_time()

    async def resume(self, new_plan: Plan, expected_version: int) -> dict[str, Any]:
        async with self._lock:
            if self._status != "rescue_needed":
                raise ValueError("Only rescue_needed runs can resume")
            if expected_version != self.plan.version:
                raise ValueError("expected_version does not match the current plan version")
            if new_plan.version != expected_version + 1:
                raise ValueError("resumed plan version must be expected_version + 1")
            if new_plan.goal != self.plan.goal or new_plan.app_bundle_id != self.plan.app_bundle_id:
                raise ValueError("resume must preserve the original goal and app bundle id")
            updated = {step.id: step.model_dump(mode="json") for step in new_plan.steps}
            for step_id, original in self._completed_specs.items():
                if updated.get(step_id) != original:
                    raise ValueError(f"resume must preserve completed step {step_id!r}")
            if self.current_step_id not in updated and new_plan.start_step is None:
                raise ValueError("resume must keep the current step or provide start_step")
            if new_plan.start_step in self.completed_steps:
                raise ValueError("resume cannot replay a completed step")

            self._action_limit = min(self._action_limit, new_plan.max_actions)
            self._time_limit = min(self._time_limit, new_plan.max_seconds)
            self._rescue_limit = min(self._rescue_limit, new_plan.max_rescues)
            self.current_step_id = new_plan.start_step or self.current_step_id
            self.plan = new_plan
            self._stop_requested = False
            self._status = "running"
            self._verification = "unknown"
            self._begin_active_time()
            try:
                return await self._drive()
            finally:
                self._end_active_time()

    async def _drive(self) -> dict[str, Any]:
        while self.current_step_id is not None:
            if self._stop_requested:
                self._status = "stopped"
                return self._result()
            limit = self._limit_reason()
            if limit:
                self._status = "blocked"
                self._verification = "unknown"
                self._last_reason = limit
                return self._result()
            step = self._step(self.current_step_id)
            self._transitions += 1
            if self._transitions > max(20, self._action_limit * 3):
                self._status = "blocked"
                self._last_reason = "transition_limit"
                return self._result()

            snapshot, stale_count = await self._observe_fresh(step)
            self._stale_reobservations += stale_count
            limit = self._limit_reason()
            if limit:
                self._status = "blocked"
                self._verification = "unknown"
                self._last_reason = limit
                return self._result()
            if snapshot is None:
                return self._need_rescue("observation_failed", step, None)
            if self._stop_requested:
                self._status = "stopped"
                return self._result()
            evidence_error = self._evidence_error(snapshot, step.allow_partial_observation)
            if evidence_error:
                return self._need_rescue(evidence_error, step, snapshot)
            bundle_id = snapshot.get("app_bundle_id")
            if bundle_id != self.plan.app_bundle_id:
                return self._need_rescue("wrong_app", step, snapshot)

            prereq = self._evaluate(step.preconditions, snapshot, step.allow_partial_observation)
            if prereq != "satisfied":
                return self._need_rescue(f"preconditions_{prereq}", step, snapshot)

            if step.action == Action.VERIFY:
                outcome = self._evaluate(step.success, snapshot, step.allow_partial_observation)
            else:
                chosen, choice_error = await self._choose_target(step, snapshot)
                if choice_error:
                    return self._need_rescue(choice_error, step, snapshot)
                if self._stop_requested:
                    self._status = "stopped"
                    return self._result()
                result, stale_count, act_error = await self._act_fresh(step, chosen, snapshot)
                self._stale_reobservations += stale_count
                if act_error:
                    if act_error == "stopped":
                        self._status = "stopped"
                        return self._result()
                    if act_error in {"action_limit", "time_limit"}:
                        self._status = "blocked"
                        self._verification = "unknown"
                        self._last_reason = act_error
                        return self._result()
                    # The controller cannot know whether a timed-out or failed call mutated UI.
                    return self._need_rescue(act_error, step, snapshot)
                if result is not None and result.get("status") in {"rejected", "failed_no_effect"}:
                    self._history_add(step, "no_effect", str(result.get("reason", "driver_rejected")))
                    self._no_progress += 1
                    if step.on_failure:
                        self.current_step_id = step.on_failure
                        continue
                    if self._no_progress >= self.NO_PROGRESS_LIMIT:
                        return self._need_rescue("no_progress", step, snapshot)
                    return self._need_rescue("action_rejected", step, snapshot)
                # Even an accepted action is not evidence of success. Observe again.
                after, stale_count = await self._observe_fresh(step)
                self._stale_reobservations += stale_count
                if after is None:
                    return self._need_rescue("post_action_observation_failed", step, snapshot)
                evidence_error = self._evidence_error(after, step.allow_partial_observation)
                if evidence_error:
                    return self._need_rescue(evidence_error, step, after)
                outcome = self._evaluate(step.success, after, step.allow_partial_observation)
                snapshot = after

            if self._elapsed_active_seconds() >= self._time_limit:
                self._status = "blocked"
                self._verification = "unknown"
                self._last_reason = "time_limit"
                return self._result()
            if outcome == "satisfied":
                self._no_progress = 0
                self._verification = "satisfied"
                if step.id not in self.completed_steps:
                    self.completed_steps.append(step.id)
                    self._completed_specs[step.id] = step.model_dump(mode="json")
                self._evidence.append({"step_id": step.id, "window_title": str(snapshot.get("title", ""))[:200],
                                       "predicates": [p.model_dump(mode="json") for p in step.success],
                                       "observed": self._witnesses(step.success, snapshot),
                                       "scope": "observed_controls" if snapshot.get("truncated") else "snapshot"})
                self._evidence = self._evidence[-12:]
                self._history_add(step, "verified", "success predicates satisfied")
                self.current_step_id = step.next_step
                if self.current_step_id is None:
                    self._status = "completed"
                    return self._result()
                continue

            self._verification = "not_satisfied" if outcome == "not_satisfied" else "unknown"
            self._history_add(step, "unverified", outcome)
            self._no_progress += 1
            if outcome == "not_satisfied" and step.on_failure:
                self.current_step_id = step.on_failure
                continue
            if self._no_progress >= self.NO_PROGRESS_LIMIT or step.action == Action.VERIFY:
                return self._need_rescue("success_predicates_not_verified", step, snapshot)
            # One known but unverified effect is not blindly repeated.
            return self._need_rescue("success_predicates_not_verified", step, snapshot)

        self._status = "completed"
        self._verification = "satisfied"
        return self._result()

    async def _observe_fresh(self, step: Step) -> tuple[dict[str, Any] | None, int]:
        stale_count = 0
        for attempt in range(self.STALE_REOBSERVE_LIMIT + 1):
            try:
                result = await self.driver.observe(window_title=step.window_title)
            except Exception:  # noqa: BLE001 - driver failures are returned as explicit rescue states.
                return None, stale_count
            if not isinstance(result, dict):
                return None, stale_count
            if result.get("status") == "stale_snapshot":
                if attempt == self.STALE_REOBSERVE_LIMIT:
                    return None, stale_count
                stale_count += 1
                continue
            return result, stale_count
        return None, stale_count

    async def _act_fresh(
        self, step: Step, candidate: dict[str, Any] | None, snapshot: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, int, str | None]:
        stale_count = 0
        for attempt in range(self.STALE_REOBSERVE_LIMIT + 1):
            if self._stop_requested:
                return None, stale_count, "stopped"
            if self._limit_reason():
                return None, stale_count, self._limit_reason()
            self._actions += 1
            try:
                result = await self.driver.act(step.action.value, candidate, step, snapshot)
            except Exception as exc:  # noqa: BLE001 - driver errors carry stale/uncertain semantics.
                if getattr(exc, "code", None) == "stale_snapshot" and not getattr(exc, "uncertain", False):
                    self._actions -= 1
                    if attempt == self.STALE_REOBSERVE_LIMIT:
                        return None, stale_count, "stale_snapshot_limit"
                    fresh, used = await self._observe_fresh(step)
                    stale_count += used + 1
                    if fresh is None or self._evidence_error(fresh, step.allow_partial_observation):
                        return None, stale_count, "stale_snapshot_reobserve_failed"
                    validation_error = self._revalidation_error(step, fresh)
                    if validation_error:
                        return None, stale_count, validation_error
                    snapshot = fresh
                    candidate, error = await self._choose_target(step, snapshot)
                    if error:
                        return None, stale_count, error
                    continue
                if hasattr(exc, "uncertain") and getattr(exc, "uncertain") is False:
                    code = str(getattr(exc, "code", "action_refused"))
                    reason = str(exc)[:160] or code
                    self._history_add(step, "no_effect", reason)
                    return {"status": "failed_no_effect", "reason": code}, stale_count, None
                self._history_add(step, "effect_unknown", "driver call raised")
                return None, stale_count, "action_effect_unknown"
            if not isinstance(result, dict):
                self._history_add(step, "effect_unknown", "driver returned invalid result")
                return None, stale_count, "action_effect_unknown"
            if result.get("status") == "stale_snapshot":
                # Explicit stale rejection means this operation was not applied.
                self._actions -= 1
                if attempt == self.STALE_REOBSERVE_LIMIT:
                    return None, stale_count, "stale_snapshot_limit"
                fresh, used = await self._observe_fresh(step)
                stale_count += used + 1
                if fresh is None or self._evidence_error(fresh, step.allow_partial_observation):
                    return None, stale_count, "stale_snapshot_reobserve_failed"
                validation_error = self._revalidation_error(step, fresh)
                if validation_error:
                    return None, stale_count, validation_error
                snapshot = fresh
                candidate, error = await self._choose_target(step, snapshot)
                if error:
                    return None, stale_count, error
                continue
            if result.get("status") in {"unknown", "timeout", "error"}:
                self._history_add(step, "effect_unknown", str(result.get("status")))
                return None, stale_count, "action_effect_unknown"
            self._history_add(step, "action", "driver returned a non-stale result")
            return result, stale_count, None
        return None, stale_count, "stale_snapshot_limit"

    async def _choose_target(
        self, step: Step, snapshot: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str | None]:
        if step.target is None:
            return None, None
        elements = snapshot.get("elements")
        if not isinstance(elements, list):
            return None, "invalid_observation_elements"
        matches = [e for e in elements if isinstance(e, dict) and self._matches_selector(e, step.target) and e.get("enabled") is not False]
        selector = step.target
        if isinstance(selector.index, int):
            matches = matches[selector.index : selector.index + 1]
        elif selector.index == "first":
            matches = matches[:1]
        elif selector.index == "last":
            matches = matches[-1:]
        if not matches:
            return None, "target_not_found"
        if len(matches) == 1:
            return matches[0], None
        candidates = [self._public_candidate(item) for item in matches]
        self._decisions += 1
        try:
            chosen_id = await self.decider.choose(step.instruction, candidates, self._public_snapshot(snapshot))
        except Exception:  # noqa: BLE001 - local decider failure must pause the plan.
            return None, "target_decision_failed"
        for item in matches:
            if str(item.get("id")) == str(chosen_id):
                return item, None
        return None, "decider_returned_unobserved_candidate"

    @staticmethod
    def _matches_selector(element: dict[str, Any], selector: Selector) -> bool:
        if selector.role and element.get("role") != selector.role:
            return False
        label = str(element.get("label", ""))
        description = str(element.get("description", ""))
        if selector.label_contains and selector.label_contains.casefold() not in label.casefold():
            return False
        return not (selector.description_contains and selector.description_contains.casefold() not in description.casefold())

    @staticmethod
    def _public_candidate(item: dict[str, Any]) -> dict[str, Any]:
        return {key: item[key] for key in ("id", "role", "label", "description", "value", "enabled", "selected", "actions") if key in item}

    @classmethod
    def _public_snapshot(cls, snapshot: dict[str, Any]) -> dict[str, Any]:
        return {
            key: snapshot[key]
            for key in ("snapshot_id", "title", "app_bundle_id", "truncated", "complete")
            if key in snapshot
        } | {"elements": [cls._public_candidate(e) for e in snapshot.get("elements", []) if isinstance(e, dict)]}

    @staticmethod
    def _evidence_error(snapshot: dict[str, Any], allow_partial: bool = False) -> str | None:
        if not allow_partial and (snapshot.get("truncated") is True or snapshot.get("complete") is False):
            return "observation_truncated"
        elements = snapshot.get("elements")
        if not isinstance(elements, list):
            return "invalid_observation_elements"
        if not elements:
            return "empty_observation"
        return None

    def _revalidation_error(self, step: Step, snapshot: dict[str, Any]) -> str | None:
        error = self._evidence_error(snapshot, step.allow_partial_observation)
        if error:
            return error
        if snapshot.get("app_bundle_id") != self.plan.app_bundle_id:
            return "wrong_app"
        preconditions = self._evaluate(step.preconditions, snapshot, step.allow_partial_observation)
        if preconditions != "satisfied":
            return f"preconditions_{preconditions}"
        return None

    @staticmethod
    def _witnesses(predicates: list[Predicate], snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        """Return compact, token-free observations that positively satisfy predicates."""
        witnesses: list[dict[str, Any]] = []
        elements = [item for item in snapshot.get("elements", []) if isinstance(item, dict)]
        for predicate in predicates:
            if predicate.kind == PredicateKind.WINDOW_TITLE_CONTAINS:
                title = snapshot.get("title")
                if isinstance(title, str) and predicate.value.casefold() in title.casefold():
                    witnesses.append({"kind": predicate.kind.value, "observed": title[:200]})
                continue
            matches = [
                item for item in elements
                if (not predicate.role or item.get("role") == predicate.role)
                and (not predicate.label_contains or predicate.label_contains.casefold() in str(item.get("label", "")).casefold())
            ]
            if predicate.kind == PredicateKind.VALUE_CONTAINS:
                matches = [item for item in matches if isinstance(item.get("value"), str)
                           and predicate.value.casefold() in item["value"].casefold()]
            elif predicate.kind == PredicateKind.SELECTED:
                matches = [item for item in matches if item.get("selected") is True]
            if matches:
                item = matches[0]
                witness = {"kind": predicate.kind.value}
                for key in ("role", "label", "value", "selected"):
                    value = item.get(key)
                    if value is not None:
                        witness[key] = value[:200] if isinstance(value, str) else value
                witnesses.append(witness)
        return witnesses

    @staticmethod
    def _evaluate(predicates: list[Predicate], snapshot: dict[str, Any], allow_partial: bool = False) -> str:
        if Run._evidence_error(snapshot, allow_partial):
            return "unknown"
        unknown = False
        partial = snapshot.get("truncated") is True or snapshot.get("complete") is False
        missing = "unknown" if partial else "not_satisfied"
        elements = [e for e in snapshot["elements"] if isinstance(e, dict)]
        for predicate in predicates:
            if predicate.kind == PredicateKind.WINDOW_TITLE_CONTAINS:
                title = snapshot.get("title")
                if not isinstance(title, str):
                    unknown = True
                elif predicate.value.casefold() not in title.casefold():
                    return "not_satisfied"
                continue
            candidates = [
                e for e in elements
                if (not predicate.role or e.get("role") == predicate.role)
                and (not predicate.label_contains or predicate.label_contains.casefold() in str(e.get("label", "")).casefold())
            ]
            if predicate.kind == PredicateKind.EXISTS:
                if not candidates:
                    return missing
            elif not candidates:
                return missing
            elif predicate.kind == PredicateKind.VALUE_CONTAINS:
                known_values = [e.get("value") for e in candidates if isinstance(e.get("value"), str)]
                if not known_values:
                    unknown = True
                elif not any(predicate.value.casefold() in value.casefold() for value in known_values):
                    return "not_satisfied"
            elif predicate.kind == PredicateKind.SELECTED:
                selected = [e.get("selected") for e in candidates if isinstance(e.get("selected"), bool)]
                if not selected:
                    unknown = True
                elif not any(selected):
                    return "not_satisfied"
        return "unknown" if unknown else "satisfied"

    def _need_rescue(self, reason: str, step: Step, snapshot: dict[str, Any] | None) -> dict[str, Any]:
        self._rescues += 1
        if self._rescues > self._rescue_limit:
            self._status = "blocked"
            self._last_reason = f"rescue_limit:{reason}"
        else:
            self._status = "rescue_needed"
            self._last_reason = reason
        self._verification = "unknown" if reason.endswith("unknown") or "observation" in reason else self._verification
        compact = self._public_snapshot(snapshot) if snapshot else None
        if compact:
            items = [e for e in compact["elements"] if e.get("label") or e.get("value")]
            if step.target:
                items.sort(key=lambda e: not self._matches_selector(e, step.target))
            compact["elements"] = [{k: (str(v)[:180] if isinstance(v, str) else v)
                                     for k, v in e.items() if k != "actions"} for e in items[:24]]
            compact["response_truncated"] = len(items) > 24
        self._rescue_context = {
            "reason": reason,
            "plan_version": self.plan.version,
            "goal": self.plan.goal,
            "app_bundle_id": self.plan.app_bundle_id,
            "current_step": step.model_dump(mode="json"),
            "observation": compact,
            "completed_steps": list(self.completed_steps),
            "recent_effects": self._history[-4:],
            "instruction": "Resolve the blocker with the smallest plan repair. Preserve goal, app, constraints, and completed steps.",
        }
        return self._result()

    def _limit_reason(self) -> str | None:
        if self._actions >= self._action_limit:
            return "action_limit"
        if self._elapsed_active_seconds() >= self._time_limit:
            return "time_limit"
        return None

    def _begin_active_time(self) -> None:
        if self._active_started is None:
            self._active_started = time.monotonic()

    def _end_active_time(self) -> None:
        if self._active_started is not None:
            self._active_elapsed += time.monotonic() - self._active_started
            self._active_started = None

    def _elapsed_active_seconds(self) -> float:
        current = time.monotonic() - self._active_started if self._active_started is not None else 0.0
        return self._active_elapsed + current

    def _history_add(self, step: Step, effect: str, note: str) -> None:
        self._history.append({"step_id": step.id, "effect": effect, "note": note[:160]})
        self._history = self._history[-12:]

    def _step(self, step_id: str) -> Step:
        return next(step for step in self.plan.steps if step.id == step_id)

    def _result(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": self._status,
            "verification": self._verification,
            "plan_version": self.plan.version,
            "current_step": self.current_step_id,
            "completed_steps": list(self.completed_steps),
            "evidence": list(self._evidence),
            "metrics": {
                "actions": self._actions,
                "decisions": self._decisions,
                "stale_reobservations": self._stale_reobservations,
                "rescues": self._rescues,
                "elapsed_seconds": round(self._elapsed_active_seconds(), 3),
                "remote_tokens": None,
            },
        }
        if hasattr(self, "_last_reason"):
            result["reason"] = self._last_reason
        if hasattr(self, "_rescue_context") and self._status in {"rescue_needed", "blocked"}:
            result["rescue_context"] = self._rescue_context
        return result
