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
    bot()._apply_handled_look(embed, FakeMessage("Marked Handled by Demolition"))
    assert embed.color == discord.Color.green()


def test_handled_look_drops_the_draft_reply_field():
    embed = discord.Embed(title="x")
    embed.add_field(name="Draft reply", value="please stop", inline=False)
    embed.add_field(name="Evidence", value="quote", inline=False)
    bot()._apply_handled_look(embed, FakeMessage("Marked Handled by Demolition"))
    names = [f.name for f in embed.fields]
    assert "Draft reply" not in names
    assert "Evidence" in names


def test_handled_look_carries_the_attribution_forward():
    embed = discord.Embed(title="x")
    bot()._apply_handled_look(embed, FakeMessage("Marked Handled by Demolition"))
    assert embed.footer.text == "Marked Handled by Demolition"


def test_handled_look_keeps_a_low_confidence_note_alongside_the_attribution():
    embed = discord.Embed(title="x")
    embed.set_footer(text="Low confidence - please verify before acting")
    bot()._apply_handled_look(embed, FakeMessage("Marked Handled by Demolition"))
    assert "Low confidence" in embed.footer.text
    assert "Marked Handled by Demolition" in embed.footer.text


def test_handled_look_is_a_noop_on_a_message_with_no_prior_footer():
    """Shouldn't happen in practice - this is only ever called when
    view.payload.handled is True, which only gets set after Mark Handled
    stamps a footer - but must not crash if it somehow did."""
    embed = discord.Embed(title="x")
    bot()._apply_handled_look(embed, FakeMessage(None))
    assert embed.footer.text is None
