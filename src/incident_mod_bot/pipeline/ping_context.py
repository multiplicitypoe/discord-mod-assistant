"""What the analysis needs to know about a mod ping beyond the message window:
which message the ping points at, the pinger's earlier pings, and which
images near the ping are worth showing the model.

Pure functions over the compressed message dicts (bot._compress_messages),
so the live trigger, the follow-up refresh and replay.py all agree.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

DISCORD_EPOCH_MS = 1420070400000

# How far back a pinger's earlier pings count toward a pattern.
PING_HISTORY_WINDOW_S = 7 * 86400
PING_HISTORY_RECENT_S = 24 * 3600
PING_HISTORY_MAX_LISTED = 5

# Messages either side of the ping whose images can reach the analysis pass.
IMAGE_NEIGHBOURS_BEFORE = 6
IMAGE_NEIGHBOURS_AFTER = 3

# The reporter's own words right after the ping are part of the report.
FOLLOWUP_MAX = 3
FOLLOWUP_WITHIN_MS = 10 * 60 * 1000
FOLLOWUP_WITHIN_MESSAGES = 25

# Other members voicing suspicion ("same prompt ahh bot"), with the message each is about.
CALLOUT_MAX = 5
CALLOUT_TEXT_MAX = 100
_CALLOUT_RE = re.compile(
    r"\b(?:bots?|botted|sus|sussy|suspicious|scams?|scammers?|scammy|spam|spams|spammer|spammers|"
    r"spamming|spammy|fake|catfish|phish\w*|chat ?gpt|gpt|ai|prompt|hacked|compromised)\b",
    re.IGNORECASE,
)
_MENTION_ONLY_RE = re.compile(r"(?:@[\w\-]+(?: [\w\-]+)*\s*)+")
_URL_ONLY_RE = re.compile(r"(?:https?://\S+\s*)+")


def snowflake_ms(snowflake: int) -> int:
    return (int(snowflake) >> 22) + DISCORD_EPOCH_MS


def snowflake_at_ms(ms: int) -> int:
    """The smallest snowflake created at or after this unix time in ms."""
    return max(int(ms) - DISCORD_EPOCH_MS, 0) << 22


def excerpt(message: dict[str, Any], *, max_len: int = 200) -> dict[str, Any]:
    out: dict[str, Any] = {"id": message.get("id")}
    for key in ("author_id", "author_name"):
        if message.get(key) is not None:
            out[key] = message[key]
    content = str(message.get("content") or "")
    out["content"] = content if len(content) <= max_len else content[: max_len - 1] + "…"
    if message.get("attachments"):
        out["attachments"] = message["attachments"]
    return out


def _reported_pointer(
    reporter: dict[str, Any], messages: list[dict[str, Any]], anchor_message_id: int | None
) -> dict[str, Any]:
    """The reporter dict with a pointer to the message the ping is about.

    A ping sent as a Discord reply points at exactly one message, so that is
    the reported message, whatever came between it and the ping. Before
    this, every bare ping was pointed at the last message from anyone else,
    and on 2026-10-04/05 three reply-pings in a row were judged on a
    neighbouring message instead of the one they replied to.

    A bare ping that is not a reply still gets the last message before it
    from someone else (the "mods, kill him" case). A ping with text and no reply
    gets no pointer: "@Chat Moderator worth an announcement?" went hunting
    for someone to blame when it had one.

    Pointers are always recomputed, never carried over from a stored payload.
    """
    out = {
        k: v
        for k, v in reporter.items()
        if k not in (
            "likely_reported_message_id",
            "likely_reported_message",
            "replied_to_message_id",
            "reported_message",
            "followup_text",
            "callouts",
        )
    }
    if anchor_message_id is None:
        return out
    by_id = {m["id"]: m for m in messages if isinstance(m.get("id"), int)}
    reporter_id = out.get("user_id")

    anchor = by_id.get(anchor_message_id) or {}
    snapshot = out.get("replied_to") if isinstance(out.get("replied_to"), dict) else {}
    reply_id = anchor.get("reply_to") or snapshot.get("id")
    if isinstance(reply_id, int):
        target = by_id.get(reply_id) or {**snapshot, "id": reply_id}
        # Replying to your own message points at nothing new.
        if target.get("author_id") != reporter_id:
            out["replied_to_message_id"] = reply_id
            out["reported_message"] = excerpt(target)
            return out

    if not out.get("bare_ping"):
        return out
    before = [
        m
        for m in by_id.values()
        if m["id"] < anchor_message_id and m.get("author_id") != reporter_id
    ]
    if before:
        last = max(before, key=lambda m: m["id"])
        # A guess, not a report: it must not carry the authority a reply target does
        # (reported_message), or the model defends it against the reporter's own words.
        out["likely_reported_message_id"] = last["id"]
        out["likely_reported_message"] = excerpt(last)
    return out


def _clip(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def reporter_followup_text(
    messages: list[dict[str, Any]], reporter_id: Any, anchor_message_id: int
) -> list[dict[str, Any]]:
    """What the reporter wrote right after the ping, e.g. "likely bot in chat".

    That text is part of the report and can name the subject, which a bare
    ping alone never does. Only their own messages, shortly after the ping.
    """
    if reporter_id is None:
        return []
    anchor_ms = snowflake_ms(anchor_message_id)
    after = sorted(
        (m for m in messages if isinstance(m.get("id"), int) and m["id"] > anchor_message_id),
        key=lambda m: m["id"],
    )[:FOLLOWUP_WITHIN_MESSAGES]
    out: list[dict[str, Any]] = []
    for m in after:
        if snowflake_ms(m["id"]) - anchor_ms > FOLLOWUP_WITHIN_MS:
            break
        if m.get("author_id") != reporter_id:
            continue
        text = str(m.get("content") or "").strip()
        if not text or _MENTION_ONLY_RE.fullmatch(text):
            continue
        out.append({"id": m["id"], "text": _clip(text, 200)})
        if len(out) >= FOLLOWUP_MAX:
            break
    return out


def _has_words(message: dict[str, Any]) -> bool:
    text = str(message.get("content") or "").strip()
    return bool(text) and not _URL_ONLY_RE.fullmatch(text)


def bystander_callouts(
    messages: list[dict[str, Any]], reporter_id: Any
) -> list[dict[str, Any]]:
    """Other members voicing suspicion, with the message each one is about.

    "same prompt ahh bot" is about the message above it, not the one nearest
    the mod ping 30 messages later. A reply names its target; otherwise it is
    the previous message with words in it from someone else. These are hints
    for the model, never facts.
    """
    ordered = sorted((m for m in messages if isinstance(m.get("id"), int)), key=lambda m: m["id"])
    by_id = {m["id"]: m for m in ordered}
    grouped: dict[int, dict[str, Any]] = {}
    for i, m in enumerate(ordered):
        if m.get("author_id") == reporter_id:
            continue
        text = str(m.get("content") or "")
        if not _CALLOUT_RE.search(text):
            continue
        about = None
        ref = m.get("reply_to")
        if isinstance(ref, int) and ref in by_id and by_id[ref].get("author_id") != m.get("author_id"):
            about = by_id[ref]
        else:
            for prev in reversed(ordered[:i]):
                if prev.get("author_id") != m.get("author_id") and _has_words(prev):
                    about = prev
                    break
        if about is None:
            continue
        entry = grouped.setdefault(
            about["id"],
            {
                "about_message_id": about["id"],
                "about_author": about.get("author_name"),
                "about_text": _clip(str(about.get("content") or ""), CALLOUT_TEXT_MAX),
                "said": [],
            },
        )
        if len(entry["said"]) < 3:
            entry["said"].append({"by": m.get("author_name"), "text": _clip(text, CALLOUT_TEXT_MAX)})
    return list(grouped.values())[-CALLOUT_MAX:]


def reported_message_pointer(
    reporter: dict[str, Any], messages: list[dict[str, Any]], anchor_message_id: int | None
) -> dict[str, Any]:
    """The reporter dict, with what the ping points at and what was said about it.

    A reply-ping points at its reply target (authoritative). A bare ping gets
    only a low-authority guess (the last message before it) plus the
    reporter's own follow-up text and any bystander callouts, so that
    "likely bot in chat" can move the model off the guess. Everything is
    recomputed, never carried over from a stored payload.
    """
    out = _reported_pointer(reporter, messages, anchor_message_id)
    if anchor_message_id is None:
        return out
    followup = reporter_followup_text(messages, out.get("user_id"), anchor_message_id)
    if followup:
        out["followup_text"] = followup
    callouts = bystander_callouts(messages, out.get("user_id"))
    if callouts:
        out["callouts"] = callouts
    return out


def _reported_author(payload: dict[str, Any]) -> str | None:
    """Who an earlier ping was aimed at, from that brief's own payload."""
    reporter = payload.get("reporter") or {}
    anchor_id = payload.get("anchor_message_id")
    messages = {m.get("id"): m for m in payload.get("messages") or [] if isinstance(m, dict)}
    target_id = reporter.get("replied_to_message_id")
    if target_id is None and anchor_id in messages:
        target_id = messages[anchor_id].get("reply_to")
    if target_id is None:
        target_id = reporter.get("likely_reported_message_id")
    target = messages.get(target_id) or {}
    name = target.get("author_name") or (reporter.get("replied_to") or {}).get("author_name")
    return str(name) if name else None


def build_ping_history(
    earlier: list[tuple[int, dict[str, Any]]],
    outcomes: dict[int, str],
    *,
    anchor_message_id: int,
) -> dict[str, Any] | None:
    """The pinger's earlier mod pings, for the payload and the card.

    earlier: (brief_message_id, payload) for each earlier brief this person
    triggered, any order. outcomes: brief_message_id -> what the brief said
    when a moderator marked it handled, only for briefs handled before this
    ping. Everything is measured at the anchor so a replay matches what the
    live brief saw.
    """
    if not earlier:
        return None
    anchor_ms = snowflake_ms(anchor_message_id)
    rows = []
    for brief_id, payload in sorted(earlier, key=lambda r: r[0], reverse=True):
        ping_id = payload.get("anchor_message_id") or brief_id
        hours = round((anchor_ms - snowflake_ms(ping_id)) / 3_600_000, 1)
        row: dict[str, Any] = {"hours_before": hours}
        target = _reported_author(payload)
        if target:
            row["replying_to"] = target
        row["brief"] = outcomes.get(brief_id) or "not marked handled"
        rows.append(row)
    recent = [r for r in rows if r["hours_before"] * 3600 <= PING_HISTORY_RECENT_S]
    return {
        "window_days": PING_HISTORY_WINDOW_S // 86400,
        "earlier_pings": len(rows),
        "earlier_pings_last_24h": len(recent),
        "oldest_hours_before": rows[-1]["hours_before"],
        "earlier": rows[:PING_HISTORY_MAX_LISTED],
    }


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _span(hours: float) -> str:
    if hours < 1:
        return "the last hour"
    if hours < 48:
        return f"~{round(hours)}h"
    return f"~{round(hours / 24)} days"


def format_ping_history(history: dict[str, Any] | None) -> str:
    """One line for the card, e.g. "3rd mod ping in ~16h. Earlier briefs:
    No action x2". Built from data, not by the model, so it is never wrong
    about the count."""
    if not history or not history.get("earlier_pings"):
        return ""
    total = int(history["earlier_pings"]) + 1
    line = f"{_ordinal(total)} mod ping in {_span(float(history.get('oldest_hours_before') or 0))}"
    recent = int(history.get("earlier_pings_last_24h") or 0) + 1
    if 1 < recent < total:
        line += f" ({recent} in the last 24h)"
    counts = Counter(str(r.get("brief") or "") for r in history.get("earlier") or [])
    if counts:
        parts = [f"{label} x{n}" if n > 1 else label for label, n in counts.most_common()]
        line += ". Earlier briefs: " + ", ".join(parts)
    return line


def analysis_image_candidates(
    messages: list[dict[str, Any]],
    anchor_message_id: int | None,
    reporter: dict[str, Any] | None,
) -> list[int]:
    """Message ids whose images the analysis pass should see, best first.

    The reported message, then the ping itself, then the reported author's
    own messages next to the ping, then everything else next to it, nearest
    first. The case that prompted this needed exactly that: the reported line
    was text, and the image that explained it (a screenshot of a role) was
    posted by the same author four messages earlier.
    """
    if anchor_message_id is None:
        return []
    ordered = sorted(
        (m for m in messages if isinstance(m.get("id"), int)), key=lambda m: m["id"]
    )
    ids = [m["id"] for m in ordered]
    by_id = {m["id"]: m for m in ordered}
    reporter = reporter or {}
    reported_id = reporter.get("replied_to_message_id") or reporter.get(
        "likely_reported_message_id"
    )

    out: list[int] = []

    def add(mid: Any) -> None:
        if isinstance(mid, int) and mid not in out:
            out.append(mid)

    add(reported_id)
    add(anchor_message_id)

    if anchor_message_id in by_id:
        pos = ids.index(anchor_message_id)
    else:
        pos = len([i for i in ids if i < anchor_message_id])
    before = list(reversed(ids[max(pos - IMAGE_NEIGHBOURS_BEFORE, 0) : pos]))
    after = [i for i in ids[pos : pos + IMAGE_NEIGHBOURS_AFTER + 1] if i != anchor_message_id]
    neighbours = sorted(
        before + after, key=lambda i: abs(snowflake_ms(i) - snowflake_ms(anchor_message_id))
    )

    reported_author = (by_id.get(reported_id) or reporter.get("reported_message") or {}).get(
        "author_id"
    )
    if reported_author is not None:
        for mid in neighbours:
            if by_id[mid].get("author_id") == reported_author:
                add(mid)
    for mid in neighbours:
        add(mid)
    return out
