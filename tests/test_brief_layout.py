"""The brief must lead with the verdict and the action.

Mods described their real workflow: read the synopsis, press Action Taken.
Today the recommended action is 5th of 8 sections and the draft reply is 8th —
about 2.5 mobile screens down, so they hand-write their own TLDR.

Fixture mirrors the real RMT incident from the mod channel screenshots.
"""
import pytest

from incident_mod_bot.pipeline.incident import (
    EvidenceQuote,
    IncidentResult,
    MemorySuggestions,
    Participant,
    RuleRef,
)


def rmt_incident(**kw) -> IncidentResult:
    base = dict(
        headline="RMT link - delete + warn",
        summary=(
            "Start: General PoE build talk | Flashpoint: User rmt_seller1 advertises "
            "real-money trading link | Now: Potential rule violation flagged."
        ),
        participants=[
            Participant(user_id=42002, name="rmt_seller1", role="offender",
                        notes="Posted RMT link"),
            Participant(user_id=2, name="helpfuluser1", role="reporter", notes="Called mod attention"),
            # bystanders: present in the window, did nothing
            Participant(user_id=3, name="bystander1", role="member"),
            Participant(user_id=4, name="bystander2", role="member"),
        ],
        signals=["Real-money trading link posted", "User helpfuluser1 calls for mod attention"],
        rule_refs=[
            RuleRef(id="spam", reason="Advertising and non-participatory content not allowed"),
            RuleRef(id="notrading", reason="No trading or begging outside designated channel"),
        ],
        recommendations=["Delete rmt_seller1's message with the link",
                         "Warn rmt_seller1 about RMT and server rules"],
        draft_message="Posting real-money trading links is not allowed here. Please stop.",
        confidence=0.95,
        evidence_quotes=[
            EvidenceQuote(quote="i have big news for all guys big offers for orb",
                          message_id=10, link="https://discord.com/x/10"),
            EvidenceQuote(quote="https://www.g2g.com/r/8edf", message_id=11,
                          link="https://discord.com/x/11"),
        ],
        memory_suggestions=MemorySuggestions(),
    )
    base.update(kw)
    return IncidentResult(**base)


@pytest.fixture
def bot():
    from incident_mod_bot.bot import IncidentBot
    return IncidentBot.__new__(IncidentBot)  # embed builder needs no live client


def field_names(embed):
    return [f.name for f in embed.fields]


def test_the_verdict_is_the_title(bot):
    embed = bot._build_incident_embed(rmt_incident())
    assert "RMT link" in embed.title
    assert "delete" in embed.title.lower()


def test_the_draft_reply_comes_before_the_evidence(bot):
    names = field_names(bot._build_incident_embed(rmt_incident()))
    assert "Draft reply" in names and "Evidence" in names
    assert names.index("Draft reply") < names.index("Evidence"), (
        "the actionable artifact must precede the audit trail"
    )


def test_what_happened_is_dropped_when_it_only_restates_the_summary(bot):
    names = field_names(bot._build_incident_embed(rmt_incident()))
    assert "What happened" not in names


def test_what_happened_survives_for_genuinely_complex_incidents(bot):
    """Three or more people with real roles is where the narrative earns its place."""
    result = rmt_incident(participants=[
        Participant(user_id=1, name="a", role="offender", notes="x"),
        Participant(user_id=2, name="b", role="reporter", notes="y"),
        Participant(user_id=3, name="c", role="escalated", notes="z"),
    ])
    assert "What happened" in field_names(bot._build_incident_embed(result))


def test_bystanders_are_not_listed_as_involved(bot):
    embed = bot._build_incident_embed(rmt_incident())
    involved = next((f.value for f in embed.fields if f.name == "Involved"), "")
    assert "rmt_seller1" in involved
    assert "bystander1" not in involved and "bystander2" not in involved


def test_rules_are_internal_only_never_shown_on_the_card(bot):
    """Rule ids inform the model, they don't belong in front of a moderator."""
    embed = bot._build_incident_embed(rmt_incident())
    assert "Rules" not in field_names(embed)
    assert "spam" not in embed.description and "notrading" not in embed.description


def test_high_confidence_is_not_shown(bot):
    embed = bot._build_incident_embed(rmt_incident(confidence=0.95))
    assert "0.95" not in (embed.footer.text or "")


def test_low_confidence_is_called_out(bot):
    embed = bot._build_incident_embed(rmt_incident(confidence=0.4))
    assert "verify" in (embed.footer.text or "").lower()


def test_the_brief_is_materially_shorter(bot):
    embed = bot._build_incident_embed(rmt_incident())
    assert len(embed.fields) <= 4, f"expected <=4 sections, got {field_names(embed)}"


def test_it_still_works_without_a_headline(bot):
    """Fallback when the model omits it - must never render an empty title."""
    embed = bot._build_incident_embed(rmt_incident(headline=""))
    assert embed.title and embed.title.strip()
