"""spam_account's "Dms open" post was read as off-topic chat; the account was 22
hours old, which is what makes it a scam bot. The model only knows that if
the payload says so."""
from datetime import timedelta
from types import SimpleNamespace

from incident_mod_bot.bot import _author_facts, _with_relative_times
from incident_mod_bot.discord_ui.incident_view import _snowflake_created_at

PING = 1553881801282863418
SPAM_ACCOUNT = 1553553023374098910
REPORTER_W = 880074703830444381
PING_TIME = _snowflake_created_at(PING)


def _msg(author_id, *, joined_days=None, spammer=False, bot=False):
    author = SimpleNamespace(
        id=author_id,
        bot=bot,
        created_at=_snowflake_created_at(author_id),
        joined_at=PING_TIME - timedelta(days=joined_days) if joined_days is not None else None,
        public_flags=SimpleNamespace(spammer=spammer),
    )
    return SimpleNamespace(author=author)


def test_a_day_old_account_is_measured_at_the_ping() -> None:
    facts = _author_facts([_msg(SPAM_ACCOUNT, joined_days=0.2)], [], anchor_message_id=PING, mod_role_id=None)
    assert facts[str(SPAM_ACCOUNT)]["account_age_days"] == 0.9
    assert facts[str(SPAM_ACCOUNT)]["joined_server_days"] == 0.2


def test_old_accounts_round_to_whole_days() -> None:
    facts = _author_facts([_msg(REPORTER_W)], [], anchor_message_id=PING, mod_role_id=None)
    assert isinstance(facts[str(REPORTER_W)]["account_age_days"], int)
    assert "joined_server_days" not in facts[str(REPORTER_W)]


def test_discords_own_spammer_flag_is_carried() -> None:
    facts = _author_facts([_msg(SPAM_ACCOUNT, spammer=True)], [], anchor_message_id=PING, mod_role_id=None)
    assert facts[str(SPAM_ACCOUNT)]["discord_spammer_flag"] is True
    clean = _author_facts([_msg(REPORTER_W)], [], anchor_message_id=PING, mod_role_id=None)
    assert "discord_spammer_flag" not in clean[str(REPORTER_W)]


def test_bots_are_left_out() -> None:
    assert _author_facts([_msg(SPAM_ACCOUNT, bot=True)], [], anchor_message_id=PING, mod_role_id=None) == {}


def test_authors_only_in_a_persisted_payload_still_get_account_age() -> None:
    facts = _author_facts([], [{"id": 1, "author_id": SPAM_ACCOUNT}], anchor_message_id=PING, mod_role_id=None)
    assert facts[str(SPAM_ACCOUNT)] == {"account_age_days": 0.9}


def test_prior_facts_survive_a_refresh_without_the_live_member() -> None:
    prior = {str(SPAM_ACCOUNT): {"account_age_days": 0.9, "joined_server_days": 0.2, "discord_spammer_flag": True}}
    facts = _author_facts(
        [], [{"id": 1, "author_id": SPAM_ACCOUNT}], anchor_message_id=PING, mod_role_id=None, prior=prior
    )
    assert facts[str(SPAM_ACCOUNT)]["joined_server_days"] == 0.2
    assert facts[str(SPAM_ACCOUNT)]["discord_spammer_flag"] is True


def test_relative_times_count_from_the_anchor() -> None:
    spam_account_post = 1553881461879083813
    kill_him = 1553881822390983333
    out = _with_relative_times([{"id": spam_account_post}, {"id": PING}, {"id": kill_him}], PING)
    assert [m["t"] for m in out] == [-81, 0, 5]


def test_no_anchor_means_no_relative_times() -> None:
    assert _with_relative_times([{"id": PING}], None) == [{"id": PING}]


def test_one_person_gets_one_name() -> None:
    # user_s (nickname) and user_s_full (username) are the same account; the model
    # read them as two people.
    from incident_mod_bot.bot import _consistent_author_names

    out = _consistent_author_names(
        [
            {"id": 1, "author_id": 181747481567178809, "author_name": "user_s_full"},
            {"id": 2, "author_id": 7, "author_name": "spammer_p"},
            {"id": 3, "author_id": 181747481567178809, "author_name": "user_s"},
        ]
    )
    assert [m["author_name"] for m in out] == ["user_s", "spammer_p", "user_s"]
