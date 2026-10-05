"""Evidence found across polling ticks must only ever grow.

A later audit-log scan can legitimately return FEWER lines than an earlier
one: Discord's audit log page is newest-first, so if enough unrelated
activity happens elsewhere in the guild between ticks, an earlier relevant
entry can get pushed off a limited page. Channel follow-up messages have the
same problem for a different reason - a moderator deleting the very message
that explains an incident is routine, and it's exactly the enforcement
action the brief should be crediting. Either way, whatever a fresh look no
longer surfaces must not overwrite what an earlier look already found.
"""
from incident_mod_bot.discord_ui.incident_view import merge_new_lines


def test_new_lines_are_folded_in():
    merged, changed = merge_new_lines([], ["Banned spam_user_j. · by mod_y"])
    assert merged == ["Banned spam_user_j. · by mod_y"]
    assert changed is True


def test_unchanged_evidence_reports_no_change():
    prev = ["Banned spam_user_j. · by mod_y"]
    merged, changed = merge_new_lines(prev, list(prev))
    assert merged == prev
    assert changed is False


def test_a_line_missing_from_a_later_look_is_not_dropped():
    prev = ["Banned spam_user_j. · by mod_y", "Deleted a message from spam_user_j. · by mod_y"]
    later_look = ["Banned spam_user_j. · by mod_y"]  # the delete line fell off the page
    merged, changed = merge_new_lines(prev, later_look)
    assert merged == prev
    assert changed is False


def test_a_genuinely_new_line_is_detected_even_alongside_a_dropped_one():
    prev = ["Banned spam_user_j. · by mod_y"]
    later_look = ["Timed out someoneelse · by mod_y"]  # new, but the old one didn't come back either
    merged, changed = merge_new_lines(prev, later_look)
    assert set(merged) == {"Banned spam_user_j. · by mod_y", "Timed out someoneelse · by mod_y"}
    assert changed is True


def test_order_is_preserved_with_new_items_appended():
    prev = ["a", "b"]
    merged, changed = merge_new_lines(prev, ["b", "c"])
    assert merged == ["a", "b", "c"]
    assert changed is True


def test_a_reason_arriving_later_replaces_the_plain_line_not_duplicates_it():
    """The real incident: the audit line format grew a (reason) suffix. A
    stale reason-less line already on a card must not sit next to the
    enriched one that later replaces it - same ban, told twice."""
    prev = ["Banned spam_user_j. · by mod_y"]
    later_look = ["Banned spam_user_j. (Suspicious or spam account) · by mod_y"]
    merged, changed = merge_new_lines(prev, later_look)
    assert merged == ["Banned spam_user_j. (Suspicious or spam account) · by mod_y"]
    assert changed is True


def test_a_shorter_later_line_does_not_regress_an_already_enriched_one():
    enriched = ["Banned spam_user_j. (Suspicious or spam account) · by mod_y"]
    plain_again = ["Banned spam_user_j. · by mod_y"]
    merged, changed = merge_new_lines(enriched, plain_again)
    assert merged == enriched
    assert changed is False
