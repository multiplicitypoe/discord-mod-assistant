"""A ping with no text of its own is the case that produced a wrong brief:
".noreasons" bare-pinged @Chat Moderator, then explained who and why in a
separate message 57 seconds later - which the scan window, looking only
backward from the ping, never saw. Detecting this is what lets
_maybe_update_brief_with_followup decide whether it's worth waiting for
that explanation at all.
"""
from incident_mod_bot.bot import _is_bare_ping


class Msg:
    def __init__(self, content: str) -> None:
        self.content = content


def test_a_role_mention_alone_is_bare() -> None:
    assert _is_bare_ping(Msg("<@&174997701513969665>"))


def test_a_role_mention_with_surrounding_whitespace_is_bare() -> None:
    assert _is_bare_ping(Msg("  <@&174997701513969665>  "))


def test_a_user_mention_alone_is_bare() -> None:
    assert _is_bare_ping(Msg("<@590765760092307456>"))


def test_a_nickname_mention_alone_is_bare() -> None:
    assert _is_bare_ping(Msg("<@!590765760092307456>"))


def test_a_ping_with_a_question_is_not_bare() -> None:
    assert not _is_bare_ping(Msg("<@&174997701513969665> worth an announcement?"))


def test_a_ping_with_a_reason_is_not_bare() -> None:
    assert not _is_bare_ping(Msg("Hazzy harassing people <@&174997701513969665>"))
