"""What the analysis needs to know about a mod ping beyond the message window:
which message the ping points at, the pinger's earlier pings, and which
images near the ping are worth showing the model.

Pure functions over the compressed message dicts (bot._compress_messages),
so the live trigger, the follow-up refresh and replay.py all agree.
"""
from __future__ import annotations

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


def reported_message_pointer(
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
        if k not in ("likely_reported_message_id", "replied_to_message_id", "reported_message")
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
        out["likely_reported_message_id"] = last["id"]
        out["reported_message"] = excerpt(last)
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
