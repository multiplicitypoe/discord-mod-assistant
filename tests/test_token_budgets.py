"""The refine step must not be given a smaller budget than the analysis it refines.

Three production failures, all `refine_incident_with_images`, all truncating at
~2300 chars (~650 tokens) with `JSONDecodeError: Unterminated string`. The refine
step consumes analyze_incident's result and returns a superset of it, so a
smaller cap guarantees truncation on any incident near the analysis ceiling.
"""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "incident_mod_bot" / "openai_client.py"


def _budgets() -> dict[str, int]:
    """Map each LLM-calling function to its max_output_tokens."""
    text = SRC.read_text(encoding="utf-8")
    budgets: dict[str, int] = {}
    current = None
    for line in text.splitlines():
        m = re.match(r"^def (\w+)\(", line)
        if m:
            current = m.group(1)
        m = re.search(r"max_output_tokens=(\d+)", line)
        if m and current:
            budgets[current] = int(m.group(1))
    return budgets


def test_refine_has_at_least_as_much_budget_as_the_analysis_it_refines():
    b = _budgets()
    assert "analyze_incident" in b and "refine_incident_with_images" in b
    assert b["refine_incident_with_images"] >= b["analyze_incident"], (
        f"refine={b['refine_incident_with_images']} < analyze={b['analyze_incident']}: "
        "the refine step returns a superset of the analysis, so it will truncate"
    )


def test_no_call_is_budgeted_below_the_observed_truncation_point():
    """Truncation was observed at ~650 tokens of JSON; anything at/below that is at risk."""
    risky = {fn: n for fn, n in _budgets().items() if n <= 650 and fn != "summarize_images"}
    assert not risky, f"these produce full incident JSON on an unsafe budget: {risky}"
