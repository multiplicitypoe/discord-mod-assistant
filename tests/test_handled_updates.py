"""Background updates (image refinement, bare-ping follow-up) must be able to
land on a brief that's already been marked handled - a mod pressing a button
doesn't mean the conversation is over, and the periodic audit-log summary
already keeps writing to a handled card. But the content must still read as
resolved afterward, not quietly reopen it.
"""
import discord

from incident_mod_bot.bot import IncidentBot


class FakeMessage:
    def __init__(self, footer_text: str | None) -> None:
        embed = discord.Embed(title="x")
        if footer_text is not None:
            embed.set_footer(text=footer_text)
        self.embeds = [embed]


def bot() -> IncidentBot:
    return IncidentBot.__new__(IncidentBot)


def test_handled_look_turns_the_embed_green():
    embed = discord.Embed(title="x", color=discord.Color.orange())
    bot()._apply_handled_look(embed, FakeMessage("Marked Handled by mod_d"))
    assert embed.color == discord.Color.green()


def test_handled_look_drops_the_draft_reply_field():
    embed = discord.Embed(title="x")
    embed.add_field(name="Draft reply", value="please stop", inline=False)
    embed.add_field(name="Evidence", value="quote", inline=False)
    bot()._apply_handled_look(embed, FakeMessage("Marked Handled by mod_d"))
    names = [f.name for f in embed.fields]
    assert "Draft reply" not in names
    assert "Evidence" in names


def test_handled_look_carries_the_attribution_forward():
    embed = discord.Embed(title="x")
    bot()._apply_handled_look(embed, FakeMessage("Marked Handled by mod_d"))
    assert embed.footer.text == "Marked Handled by mod_d"


def test_handled_look_keeps_a_low_confidence_note_alongside_the_attribution():
    embed = discord.Embed(title="x")
    embed.set_footer(text="Low confidence - please verify before acting")
    bot()._apply_handled_look(embed, FakeMessage("Marked Handled by mod_d"))
    assert "Low confidence" in embed.footer.text
    assert "Marked Handled by mod_d" in embed.footer.text


def test_handled_look_is_a_noop_on_a_message_with_no_prior_footer():
    """Shouldn't happen in practice - this is only ever called when
    view.payload.handled is True, which only gets set after Mark Handled
    stamps a footer - but must not crash if it somehow did."""
    embed = discord.Embed(title="x")
    bot()._apply_handled_look(embed, FakeMessage(None))
    assert embed.footer.text is None


# The stale-view case from 2026-09-27: each rebuild attaches a new view, a
# moderator presses the button on that one (which saves the record), and the
# follow-up poll is still holding the first view object.
from types import SimpleNamespace  # noqa: E402

import asyncio  # noqa: E402


class FakeViewStore:
    def __init__(self, handled: bool | None) -> None:
        self.handled = handled

    async def load_view(self, message_id: int):
        if self.handled is None:
            return None
        return SimpleNamespace(payload={"handled": self.handled})


def _stale_view(handled: bool):
    return SimpleNamespace(payload=SimpleNamespace(handled=handled))


def _is_handled(store, view) -> bool:
    b = bot()
    b.view_store = store
    return asyncio.run(b._brief_is_handled(1553881826733830247, view))


def test_the_stored_record_wins_over_a_stale_view_object():
    assert _is_handled(FakeViewStore(handled=True), _stale_view(False))


def test_an_open_brief_stays_open():
    assert not _is_handled(FakeViewStore(handled=False), _stale_view(False))


def test_a_handled_view_needs_no_lookup():
    assert _is_handled(None, _stale_view(True))


def test_no_stored_record_means_not_handled():
    assert not _is_handled(FakeViewStore(handled=None), _stale_view(False))
