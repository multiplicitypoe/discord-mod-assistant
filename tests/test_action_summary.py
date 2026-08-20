"""The action summary renders a timeout as a duration, not a timestamp.

Moderators think in "1w", not "until 2026-08-24T15:18:26Z". The audit log only
gives the absolute end time, so this converts it back.
"""
from datetime import datetime, timedelta, timezone

from incident_mod_bot.discord_ui.incident_view import _humanise_until

START = datetime(2026, 8, 17, 15, 18, tzinfo=timezone.utc)


def _u(**kw) -> str:
    return _humanise_until(START, START + timedelta(**kw))


def test_a_week_long_timeout_reads_as_days() -> None:
    # the shape moderators actually type: a 1-week timeout, not an end timestamp
    assert _u(days=7) == "for 7d"


def test_hours_and_minutes() -> None:
    assert _u(hours=6) == "for 6h"
    assert _u(minutes=10) == "for 10m"
    assert _u(seconds=30) == "for 30s"


def test_the_discord_maximum() -> None:
    assert _u(days=28) == "for 28d"


def test_a_timeout_being_lifted_is_not_negative() -> None:
    assert _u(seconds=-500) == "for 0s"


def test_rubbish_input_does_not_raise() -> None:
    assert _humanise_until(START, None) == "for a while"
