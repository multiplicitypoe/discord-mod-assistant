"""The model sometimes puts a name where an id goes - on 2026-09-27 it wrote
participants[1].user_id = "mod_b", lifted from the audit line "Banned
spam_account · by mod_b". That failed validation for the whole result, and the
refresh that had just seen the ban (and would have corrected a wrong brief)
was thrown away.
"""
from incident_mod_bot.pipeline.incident import known_users_from_payload, parse_incident_result

MOD_B = 143093548282032185
SPAM_ACCOUNT = 1553553023374098910


def _raw(**overrides):
    data = {
        "headline": "Solicitation post from a new account",
        "summary": "Posted a 'dms open' solicitation.",
        "draft_message": "",
        "recommendations": ["Already handled."],
    }
    data.update(overrides)
    return data


def test_a_name_in_place_of_a_user_id_resolves_from_the_window() -> None:
    known = known_users_from_payload(
        {"messages": [{"author_name": "mod_b", "author_id": MOD_B, "content": "gif"}]}
    )
    result = parse_incident_result(
        _raw(
            participants=[
                {"user_id": SPAM_ACCOUNT, "name": "spam_account", "role": "member"},
                {"user_id": "mod_b", "name": "mod_b", "role": "mod"},
            ]
        ),
        known,
    )
    assert [p.user_id for p in result.participants] == [SPAM_ACCOUNT, MOD_B]


def test_an_unresolvable_user_id_drops_that_entry_not_the_brief() -> None:
    result = parse_incident_result(
        _raw(
            participants=[
                {"user_id": SPAM_ACCOUNT, "name": "spam_account", "role": "member"},
                {"user_id": "mod_b", "name": "mod_b", "role": "mod"},
            ],
            reply_targets=[{"user_id": "someone", "message_id": None}],
            draft_replies=[{"user_id": "someone", "text": "hi"}],
        )
    )
    assert [p.user_id for p in result.participants] == [SPAM_ACCOUNT]
    assert result.reply_targets == []
    assert result.draft_replies == []


def test_digit_strings_are_still_ids() -> None:
    result = parse_incident_result(
        _raw(participants=[{"user_id": str(SPAM_ACCOUNT), "name": "spam_account", "role": "member"}])
    )
    assert result.participants[0].user_id == SPAM_ACCOUNT


def test_a_bad_optional_message_id_is_nulled_not_fatal() -> None:
    result = parse_incident_result(
        _raw(
            evidence_quotes=[{"quote": "Dms open", "message_id": "the spam_account post"}],
            reply_targets=[{"user_id": SPAM_ACCOUNT, "message_id": "n/a"}],
            memory_suggestions={
                "server_notes": [],
                "user_notes": [{"user_id": SPAM_ACCOUNT, "label": "x", "evidence_message_id": "?"}],
            },
        )
    )
    assert result.evidence_quotes[0].message_id is None
    assert result.reply_targets[0].message_id is None
    assert result.memory_suggestions.user_notes[0].evidence_message_id is None


def test_explicit_nulls_for_lists_fall_back_to_empty() -> None:
    result = parse_incident_result(_raw(participants=None, reply_targets=None))
    assert result.participants == []
    assert result.reply_targets == []


def test_the_input_dict_is_not_mutated() -> None:
    raw = _raw(participants=[{"user_id": "mod_b", "name": "mod_b", "role": "mod"}])
    parse_incident_result(raw)
    assert raw["participants"][0]["user_id"] == "mod_b"


def test_shapes_the_gpt_5_4_models_return_still_parse() -> None:
    # Observed from gpt-5.4-mini / -nano on real payloads: no participant
    # role, a rule ref as a bare id, signals as objects.
    result = parse_incident_result(
        _raw(
            participants=[{"user_id": SPAM_ACCOUNT, "name": "spam_account"}],
            rule_refs=["spam"],
            signals=[{"type": "new_account"}],
        )
    )
    assert result.participants[0].role == "member"
    assert result.rule_refs[0].id == "spam"
    assert result.signals == ['{"type": "new_account"}']
