"""Reasoning models reject temperature outright (400), so a model switch
that misses this fails every brief. gpt-6-luna was confirmed to reject it
on 2026-09-27."""
import pytest

from incident_mod_bot.openai_client import _sampling


@pytest.mark.parametrize("model", ["gpt-5-mini", "gpt-5.6-luna", "gpt-6-luna", "gpt-6-sol", "o4-mini"])
def test_reasoning_models_get_no_temperature_and_extra_headroom(model: str) -> None:
    params = _sampling(model, temperature=0.0, max_output_tokens=900)
    assert "temperature" not in params
    assert params["reasoning"] == {"effort": "low"}
    assert params["max_output_tokens"] > 900


@pytest.mark.parametrize("model", ["gpt-4.1-mini", "gpt-4o-mini", "gpt-5-chat-latest"])
def test_other_models_keep_temperature(model: str) -> None:
    assert _sampling(model, temperature=0.0, max_output_tokens=900) == {
        "temperature": 0.0,
        "max_output_tokens": 900,
    }
