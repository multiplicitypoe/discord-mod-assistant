from __future__ import annotations

from incident_mod_bot.bot import forwarded_content, media_carriers


class Snap:
    def __init__(self, content="", attachments=(), embeds=(), stickers=()):
        self.content = content
        self.attachments = list(attachments)
        self.embeds = list(embeds)
        self.stickers = list(stickers)


class Msg:
    def __init__(self, content="", snapshots=(), attachments=(), embeds=()):
        self.content = content
        self.clean_content = content
        self.message_snapshots = list(snapshots)
        self.attachments = list(attachments)
        self.embeds = list(embeds)


def test_a_forward_carries_its_text_in_the_snapshot() -> None:
    """Discord leaves the outer content empty on a forward, so a message that
    reads as blank can still be the whole incident."""
    m = Msg(content="", snapshots=[Snap(content="free elden ring keys click here")])
    assert forwarded_content(m) == "free elden ring keys click here"


def test_a_plain_message_has_no_forwarded_text() -> None:
    assert forwarded_content(Msg(content="hello")) == ""


def test_several_snapshots_are_all_kept() -> None:
    m = Msg(snapshots=[Snap(content="one"), Snap(content="two")])
    assert forwarded_content(m) == "one\ntwo"


def test_blank_snapshots_are_skipped() -> None:
    m = Msg(snapshots=[Snap(content="   "), Snap(content="real")])
    assert forwarded_content(m) == "real"


def test_media_carriers_includes_the_snapshots() -> None:
    """Attachments and embeds on a forward hang off the snapshot, not the
    message, so anything scanning message.attachments alone finds nothing."""
    snap = Snap(attachments=["a"], embeds=["e"])
    m = Msg(snapshots=[snap])
    assert media_carriers(m) == [m, snap]


def test_media_carriers_is_just_the_message_normally() -> None:
    m = Msg(content="hi", attachments=["a"])
    assert media_carriers(m) == [m]


def test_missing_attribute_is_tolerated() -> None:
    class Bare:
        content = "x"
        clean_content = "x"
    b = Bare()
    assert forwarded_content(b) == ""
    assert media_carriers(b) == [b]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
