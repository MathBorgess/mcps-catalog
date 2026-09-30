import asyncio

import pytest
from laya_computer.decider import LayaDecider


class FakeModel:
    def system_one(self, state, questions):
        assert "secret-token" not in state
        assert "secret-token" not in str(questions)
        return {"answers": {"target": {
            "choice": "1", "confidence": 0.8, "probabilities": {"1": 0.8, "2": 0.2}
        }}, "usage": {"input_tokens": 42}}


def test_choice_is_observed_id_and_tokens_stay_out_of_model():
    decider = LayaDecider(FakeModel())
    candidates = [{"id": "1", "label": "Search", "token": "secret-token"},
                  {"id": "2", "label": "Cancel"}]
    assert asyncio.run(decider.choose("Find search", candidates, {})) == "1"
    assert decider.metrics()["input_tokens"] == 42
    decider.close()


def test_invalid_probability_contract_is_rejected():
    decider = LayaDecider(FakeModel())
    with pytest.raises(ValueError, match="Invalid local decision"):
        asyncio.run(decider.choose("Pick", [{"id": "9"}], {}))
    decider.close()
