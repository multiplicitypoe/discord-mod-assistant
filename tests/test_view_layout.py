"""Buttons should match how moderators actually use the brief.

Observed: they read it and press Action Taken. Today Action Taken is the 4th
button, sitting directly under a row containing Ban - a mis-tap risk on mobile,
for a destructive action, next to the one they actually want.
"""
import pytest

from incident_mod_bot.discord_ui.incident_view import IncidentView, IncidentViewPayload


def payload(**kw):
    base = dict(
        draft_message="please stop",
        memory_suggestions={},
        mod_role_id=None,
        participants=[{"user_id": 1, "name": "a"}],
        evidence_quotes=[{"message_id": 2, "quote": "x"}],
        reply_targets=[{"user_id": 1}],
        recommendations=["Delete"],
        rule_ids=["spam"],
    )
    base.update(kw)
    return IncidentViewPayload(**base)


def labels(view):
    return [getattr(i, "label", None) for i in view.children if getattr(i, "label", None)]


async def test_destructive_actions_are_not_on_the_default_view():
    view = IncidentView(payload(), memory_store=None, view_store=None)
    shown = " ".join(labels(view)).lower()
    assert "ban" not in shown
    assert "kick" not in shown


async def test_the_button_moderators_actually_press_is_present_by_default():
    assert any("action taken" in (l or "").lower() for l in labels(IncidentView(payload(), None, None)))


async def test_the_default_view_is_small():
    view = IncidentView(payload(), memory_store=None, view_store=None)
    assert len(view.children) <= 4, f"too many components: {labels(view)}"


async def test_moderate_expands_to_the_full_toolset():
    view = IncidentView(payload(expanded=True), memory_store=None, view_store=None)
    shown = " ".join(labels(view)).lower()
    assert "ban" in shown and "kick" in shown and "timeout" in shown


async def test_expanding_is_offered_when_actions_are_allowed():
    assert any("moderate" in (l or "").lower() for l in labels(IncidentView(payload(), None, None)))


async def test_a_handled_brief_carries_no_buttons():
    """Pressing Action Taken strips the controls, so a resolved brief reads as a
    short record. Restoring one after a restart rebuilt them disabled instead,
    which put two rows of dead buttons back under every closed card."""
    view = IncidentView(payload(handled=True), memory_store=None, view_store=None)
    assert view.children == [], f"handled brief still carries {labels(view)}"
