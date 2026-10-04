from laya_computer.preflight import analyze_preflight_plan


def plan():
    return {"version": 1, "goal": "test", "app_bundle_id": "x", "steps": [
        {"id": "a", "instruction": "verify", "action": "verify", "success": [{"kind": "window_title_contains", "value": "x"}]}
    ]}


def test_minimum_valid():
    assert analyze_preflight_plan(plan())["status"] == "valid"


def test_invalid_schema():
    result = analyze_preflight_plan({"goal": "broken"})
    assert result["status"] == "invalid" and result["recommendation"] == "repair"
    assert result["issues"]
    assert all("loc" not in issue for issue in result["issues"])


def test_unreachable():
    data = plan()
    data["steps"].append({"id": "b", "instruction": "verify", "action": "verify", "success": [{"kind": "window_title_contains", "value": "x"}]})
    result = analyze_preflight_plan(data)
    assert any(i["code"] == "UNREACHABLE_STEP" and i["step_id"] == "b" for i in result["issues"])


def test_set_value_and_dangerous_key():
    data = plan()
    data["steps"][0] = {"id": "a", "instruction": "set", "action": "set_value", "target": {"role": "textbox"}, "text": "value", "success": [{"kind": "exists", "role": "textbox"}], "next_step": "b"}
    data["steps"].append({"id": "b", "instruction": "key", "action": "press_key", "key": "ctrl+c", "success": [{"kind": "window_title_contains", "value": "x"}]})
    result = analyze_preflight_plan(data)
    assert result["recommendation"] == "human_review"
    assert {"writes_user_data", "destructive_keys"} <= set(result["risk_factors"])


def test_partial_observation():
    data = plan()
    data["steps"][0]["allow_partial_observation"] = True
    result = analyze_preflight_plan(data)
    assert "partial_observation" in result["risk_factors"]
    assert result["recommendation"] == "human_review"


def test_unexpected_exception(monkeypatch):
    import laya_computer.preflight as preflight
    monkeypatch.setattr(preflight.Plan, "model_validate", lambda _: (_ for _ in ()).throw(RuntimeError()))
    result = preflight.analyze_preflight_plan({})
    assert result["status"] == "unverified"
    assert result["recommendation"] == "human_review"
