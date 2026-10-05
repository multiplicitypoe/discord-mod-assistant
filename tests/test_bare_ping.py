"""A ping with no text of its own is the case that produced a wrong brief:
"reporter_n" bare-pinged @Chat Moderator, then explained who and why in a
separate message 57 seconds later - which the scan window, looking only
backward from the ping, never saw. Detecting this is what lets
_maybe_update_brief_with_followup decide whether it's worth waiting for
that explanation at all.
"""
from incident_mod_bot.bot import _is_bare_ping, _ping_reporter, _with_likely_reported


class Msg:
    def __init__(self, content: str) -> None:
        self.content = content


def test_a_role_mention_alone_is_bare() -> None:
    assert _is_bare_ping(Msg("<@&174997701513969665>"))


def test_a_role_mention_with_surrounding_whitespace_is_bare() -> None:
    assert _is_bare_ping(Msg("  <@&174997701513969665>  "))


def test_a_user_mention_alone_is_bare() -> None:
    assert _is_bare_ping(Msg("<@590765760092319801>"))


def test_a_nickname_mention_alone_is_bare() -> None:
    assert _is_bare_ping(Msg("<@!590765760092319801>"))


def test_a_ping_with_a_question_is_not_bare() -> None:
    assert not _is_bare_ping(Msg("<@&174997701513969665> worth an announcement?"))


def test_a_ping_with_a_reason_is_not_bare() -> None:
    assert not _is_bare_ping(Msg("user_h harassing people <@&174997701513969665>"))


class Author:
    id = 880074703830444381
    name = "reporter_w"
    display_name = "reporter_w"
    global_name = None
    nick = None


class Ping:
    author = Author()

    def __init__(self, content: str = "<@&174997701513969665>") -> None:
        self.content = content


def test_the_pinger_is_named_as_the_reporter() -> None:
    reporter = _ping_reporter(Ping())
    assert reporter["user_id"] == 880074703830444381
    assert reporter["name"] == "reporter_w"
    assert reporter["bare_ping"] is True


# The real window: reporter_w split "mods, kill him" around a bare ping, and
# spam_account's post just before it is what was being reported.
REPORTER_W = 880074703830444381
SPAM_ACCOUNT = 1553553023374098910
PING = 1553881801282863418
WINDOW = [
    {"id": 1553881194312235053, "author_id": REPORTER_W, "content": "you actually asked about esl yourself damn"},
    {"id": 1553881461879083813, "author_id": SPAM_ACCOUNT, "content": "Hi wyd guys I'm bored ... Dms open"},
    {"id": 1553881708074827293, "author_id": REPORTER_W, "content": "mods"},
    {"id": PING, "author_id": REPORTER_W, "content": "@Chat Moderator"},
    {"id": 1553881819545536346, "author_id": 191187737065794026, "content": "https://klipy.com/gifs/lowtiergod-meme-mods-1"},
    {"id": 1553881822390983333, "author_id": REPORTER_W, "content": "kill him"},
]


def test_a_bare_ping_points_past_the_reporters_own_lines() -> None:
    reporter = _with_likely_reported(_ping_reporter(Ping()), WINDOW, PING)
    assert reporter["likely_reported_message_id"] == 1553881461879083813


def test_a_ping_with_text_gets_no_pointer() -> None:
    # "@Chat Moderator worth an announcement?" reports no one; with a pointer
    # the model went looking for someone to blame.
    reporter = _with_likely_reported(
        _ping_reporter(Ping("<@&174997701513969665> worth an announcement?")), WINDOW, PING
    )
    assert "likely_reported_message_id" not in reporter


def test_a_stored_pointer_is_recomputed_not_carried_over() -> None:
    stale = {**_ping_reporter(Ping()), "likely_reported_message_id": 1}
    assert _with_likely_reported(stale, WINDOW, PING)["likely_reported_message_id"] != 1
