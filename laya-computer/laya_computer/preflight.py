"""Pure static assessment of candidate plans; never accesses the desktop."""

from pydantic import ValidationError

from .plan import Plan


def analyze_preflight_plan(plan_data: dict) -> dict:
    result = {
        "schema_version": 1,
        "status": "invalid",
        "recommendation": "repair",
        "issues": [],
        "risk_factors": [],
        "checks_run": ["plan_schema", "references", "reachability", "risk_rules"],
        "checks_not_run": ["live_accessibility", "runtime_effects"],
    }
    try:
        try:
            plan = Plan.model_validate(plan_data)
        except ValidationError as exc:
            # Pydantic's loc/input/context may contain user-supplied values or paths.
            errors = exc.errors()
            for error in errors:
                result["issues"].append({
                    "code": "PLAN_SCHEMA_INVALID", "severity": "error",
                    "message": str(error.get("msg", "Invalid plan schema"))[:300],
                })
            if not errors:
                result["issues"].append({"code": "PLAN_SCHEMA_INVALID", "severity": "error", "message": "Invalid plan schema"})
            return result

        result["status"] = "valid"
        reachable = set()
        by_id = {step.id: step for step in plan.steps}
        current = plan.first_step_id
        while current is not None and current not in reachable:
            reachable.add(current)
            current = by_id[current].next_step
        for step in plan.steps:
            if step.id not in reachable:
                result["issues"].append({"code": "UNREACHABLE_STEP", "severity": "error", "step_id": step.id})
        if any(s.id not in reachable for s in plan.steps):
            result["status"] = "invalid"
            result["recommendation"] = "repair"

        risks = set()
        for step in plan.steps:
            if step.action.value == "set_value":
                risks.add("writes_user_data")
                result["issues"].append({"code": "WRITES_USER_DATA", "severity": "review", "step_id": step.id})
            key = (step.key or "").casefold().replace(" ", "")
            dangerous = {"delete", "backspace", "ctrl+c", "ctrl+x", "ctrl+v", "ctrl+a", "ctrl+z", "command+q", "cmd+q", "alt+f4", "meta+q"}
            if key in dangerous or "command" in key or "terminal" in key:
                risks.add("destructive_keys")
                result["issues"].append({"code": "DESTRUCTIVE_KEY", "severity": "review", "step_id": step.id})
            if step.allow_partial_observation:
                risks.add("partial_observation")
                result["issues"].append({"code": "PARTIAL_OBSERVATION", "severity": "review", "step_id": step.id})
        result["risk_factors"] = sorted(risks)
        if "partial_observation" in risks or "destructive_keys" in risks or "writes_user_data" in risks:
            result["recommendation"] = "human_review"
        elif result["status"] == "valid":
            result["recommendation"] = "proceed_to_inspection"
        return result
    except Exception:
        return {
            "schema_version": 1, "status": "unverified", "recommendation": "human_review",
            "issues": [{"code": "ANALYSIS_ERROR", "severity": "error", "message": "Unexpected analysis failure"}],
            "risk_factors": [], "checks_run": ["plan_schema", "references", "reachability", "risk_rules"],
            "checks_not_run": ["live_accessibility", "runtime_effects"],
        }
