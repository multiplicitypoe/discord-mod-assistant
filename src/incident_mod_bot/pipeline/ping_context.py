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
CALLOUT_ABOVE = 4
CALLOUT_TEXT_MAX = 100
_CALLOUT_RE = re.compile(
    r"\b(?:bots?|botted|sus|sussy|suspicious|scams?|scammers?|scammy|spam|spams|spammer|spammers|"
    r"spamming|spammy|fake|catfish|phish\w*|chat ?gpt|gpt|ai|prompt|hacked|compromised)\b",
    re.IGNORECASE,
)
# A bystander calling for a moderator themselves. Only a short message that is
# nothing but the call: "mods" on its own in PoE chat usually means item mods.
# On 2026-11-10 a referral link drew "modssssssss" and "nuke this mf" before
# the bare ping, and neither was seen.
_MOD_CALL_RE = re.compile(
    r"\s*(?:@?mod+s*|@?moderators?|mods? (?:pls|please|where|help|\?+)|"
    r"(?:nuke|ban+|kick|delete|remove) (?:this|him|her|them|that|it|pls|please)\b.*)"
    r"[\s!?.]*",
    re.IGNORECASE,
)
MOD_CALL_MAX_LEN = 40
# The verbs collect_recent_mod_actions writes. "Banned spam_user_d · by mod_b".
_ENFORCEMENT_RE = re.compile(
    r"^(?:Banned|Kicked|Timed out|Deleted \d+ messages? from|Deleted a message from)\s+(.+?)"
    r"(?:\s+for\s+\S+.*?)?(?:\s+\([^)]*\))?\s+·\s+by\s+",
)
# How close before a bare ping a message has to be for a moderator's action on
# its author to be what the ping was about.
AUDIT_POINTER_WITHIN_MS = 5 * 60 * 1000
# One member posting the same thing again and again in a few seconds.
REPEAT_WITHIN_MS = 30 * 1000
REPEAT_MIN = 3
REPEAT_MAX_LISTED = 3
_MENTION_ONLY_RE = re.compile(r"(?:@[\w\-]+(?: [\w\-]+)*\s*)+")
_URL_ONLY_RE = re.compile(r"(?:https?://\S+\s*)+")
_GIF_RE = re.compile(r"tenor\.com|giphy\.com|/gifs?/|\.gif\b", re.IGNORECASE)


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
            "repeat_posts",
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


def _gist(message: dict[str, Any]) -> str:
    """What a message looks like in a list: its words, or that it was an image."""
    text = str(message.get("content") or "").strip()
    if text:
        return _clip(text, CALLOUT_TEXT_MAX)
    return "[attachment]" if message.get("attachments") else "[empty]"


def _is_mod_call(text: str) -> bool:
    text = text.strip()
    return len(text) <= MOD_CALL_MAX_LEN and bool(_MOD_CALL_RE.fullmatch(text))


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
    grouped: dict[Any, dict[str, Any]] = {}
    for i, m in enumerate(ordered):
        if m.get("author_id") == reporter_id:
            continue
        text = str(m.get("content") or "")
        is_call = _is_mod_call(text)
        if not is_call and not _CALLOUT_RE.search(text):
            continue
        about = None
        ref = m.get("reply_to")
        if isinstance(ref, int) and ref in by_id and by_id[ref].get("author_id") != m.get("author_id"):
            about = by_id[ref]
        elif not is_call:
            # Not a reply, so it could be about any of the last few messages,
            # including an image with no words ("stop spamming it" after three
            # screenshots). Say what was above it rather than pick one.
            above = [
                {"id": prev["id"], "author": prev.get("author_name"), "text": _gist(prev)}
                for prev in reversed(ordered[:i])
                if prev.get("author_id") != m.get("author_id") and not _is_mod_call(str(prev.get("content") or ""))
            ][:CALLOUT_ABOVE]
            if above:
                grouped[("nr", m["id"])] = {
                    "said": [{"by": m.get("author_name"), "text": _clip(text, CALLOUT_TEXT_MAX)}],
                    "not_a_reply_so_about_one_of": above,
                }
            continue
        else:
            for prev in reversed(ordered[:i]):
                prev_text = str(prev.get("content") or "")
                # Another call ("modsss" right above "nuke this mf") is not
                # the subject, and neither is the reporter asking for help.
                if _is_mod_call(prev_text) or (is_call and prev.get("author_id") == reporter_id):
                    continue
                if prev.get("author_id") == m.get("author_id"):
                    continue
                # Link-only posts are usually reaction GIFs, so a suspicion
                # callout skips them. A bare call for mods right after a
                # link is about the link, though: a link on its own is the
                # commonest spam there is.
                if _has_words(prev) or (
                    is_call and prev_text.strip() and not _GIF_RE.search(prev_text)
                ):
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


def _attachment_key(message: dict[str, Any]) -> str | None:
    raw = message.get("attachments")
    if not raw:
        return None
    items = raw if isinstance(raw, list) else [raw]
    return "|".join(sorted(str(i) for i in items))


def repeat_posts(messages: list[dict[str, Any]], reporter_id: Any) -> list[dict[str, Any]]:
    """Members who posted the same thing several times within seconds.

    Three attachments of the same size and name in six seconds is one
    screenshot posted three times. The text pass cannot see the images, and
    an image-only message has no words to give it away, so this says so in
    plain data. Identical text counts too. A signal, not a verdict.
    """
    ordered = sorted((m for m in messages if isinstance(m.get("id"), int)), key=lambda m: m["id"])
    by_author: dict[Any, list[dict[str, Any]]] = {}
    for m in ordered:
        if m.get("author_id") is None or m.get("author_id") == reporter_id:
            continue
        by_author.setdefault(m["author_id"], []).append(m)
    found: list[dict[str, Any]] = []
    for author_id, posts in by_author.items():
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for m in posts:
            key = _attachment_key(m)
            if key:
                groups.setdefault(("attachment", key), []).append(m)
            text = str(m.get("content") or "").strip().lower()
            if len(text) >= 4:
                groups.setdefault(("text", text), []).append(m)
        for (kind, what), group in groups.items():
            if len(group) < REPEAT_MIN:
                continue
            # The tightest run of REPEAT_MIN or more inside the time limit.
            run: list[dict[str, Any]] = []
            for m in group:
                run = [r for r in run if snowflake_ms(m["id"]) - snowflake_ms(r["id"]) <= REPEAT_WITHIN_MS]
                run.append(m)
                if len(run) >= REPEAT_MIN:
                    break
            if len(run) < REPEAT_MIN:
                continue
            span_ms = snowflake_ms(run[-1]["id"]) - snowflake_ms(run[0]["id"])
            found.append(
                {
                    "author_id": author_id,
                    "author_name": run[0].get("author_name"),
                    "times": len(group),
                    "within_seconds": round(span_ms / 1000),
                    "same": ("the same attachment: " + what) if kind == "attachment" else "the same text: " + _clip(what, 60),
                    "message_ids": [r["id"] for r in group][:6],
                }
            )
    found.sort(key=lambda f: f["message_ids"][-1])
    return found[-REPEAT_MAX_LISTED:]


def enforced_names(audit_findings: list[str] | None) -> set[str]:
    """Who the audit lines say a moderator banned, kicked, timed out or
    deleted messages from, by the name the line uses."""
    names: set[str] = set()
    for line in audit_findings or []:
        match = _ENFORCEMENT_RE.match(str(line).lstrip("• ").strip())
        if match:
            names.add(match.group(1).strip().lower())
    return names


def _audit_pointer(
    out: dict[str, Any],
    messages: list[dict[str, Any]],
    anchor_message_id: int,
    audit_findings: list[str] | None,
) -> dict[str, Any]:
    """Point a bare ping at the person a moderator then acted on.

    The guess for a bare ping is the last message before it, and on
    2026-11-10 that was build talk while the referral link 24 seconds
    earlier was what the ping was about. mod_b banned its author 33
    seconds after the ping, the rebuild saw the ban, and the brief still
    said "No action" because it was judging the wrong message. An
    enforcement entry on someone who posted just before the ping outranks
    proximity.
    """
    if out.get("replied_to_message_id") is not None:
        return out
    names = enforced_names(audit_findings)
    if not names:
        return out
    reporter_id = out.get("user_id")
    anchor_ms = snowflake_ms(anchor_message_id)
    candidates = [
        m
        for m in messages
        if isinstance(m.get("id"), int)
        and m["id"] < anchor_message_id
        and m.get("author_id") != reporter_id
        and str(m.get("author_name") or "").strip().lower() in names
        and anchor_ms - snowflake_ms(m["id"]) <= AUDIT_POINTER_WITHIN_MS
    ]
    if not candidates:
        return out
    target = max(candidates, key=lambda m: m["id"])
    out["likely_reported_message_id"] = target["id"]
    out["likely_reported_message"] = excerpt(target)
    out["pointer_basis"] = "audit_findings shows a moderator acted on this author"
    return out


def reported_message_pointer(
    reporter: dict[str, Any],
    messages: list[dict[str, Any]],
    anchor_message_id: int | None,
    audit_findings: list[str] | None = None,
) -> dict[str, Any]:
    """The reporter dict, with what the ping points at and what was said about it.

    A reply-ping points at its reply target (authoritative). A bare ping gets
    only a low-authority guess (the last message before it) plus the
    reporter's own follow-up text and any bystander callouts, so that
    "likely bot in chat" can move the model off the guess. Everything is
    recomputed, never carried over from a stored payload.
    """
    out = _reported_pointer(
        {k: v for k, v in reporter.items() if k != "pointer_basis"}, messages, anchor_message_id
    )
    if anchor_message_id is None:
        return out
    if out.get("bare_ping"):
        out = _audit_pointer(out, messages, anchor_message_id, audit_findings)
    followup = reporter_followup_text(messages, out.get("user_id"), anchor_message_id)
    if followup:
        out["followup_text"] = followup
    callouts = bystander_callouts(messages, out.get("user_id"))
    if callouts:
        out["callouts"] = callouts
    if out.get("bare_ping"):
        repeats = repeat_posts(messages, out.get("user_id"))
        if repeats:
            out["repeat_posts"] = repeats
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


def enforcement_conflict(raw_result: dict[str, Any], payload: dict[str, Any]) -> list[str]:
    """Names a moderator acted on in this window when the analysis said
    there was nothing to act on.

    On 2026-11-10 a brief read "No action" directly above "Banned spam_user_d"
    because it had judged the wrong message. The prompt already says audit
    enforcement means Already handled; this is the check that it listened.
    Empty when there is no conflict.
    """
    recs = [str(r).strip().lower().rstrip(".") for r in raw_result.get("recommendations") or []]
    if recs != ["no action"]:
        return []
    names = enforced_names(payload.get("audit_findings"))
    if not names:
        return []
    reporter_id = (payload.get("reporter") or {}).get("user_id")
    authors = {
        str(m.get("author_name") or "").strip().lower(): m.get("author_name")
        for m in payload.get("messages") or []
        if m.get("author_id") != reporter_id
        and not (payload.get("authors") or {}).get(str(m.get("author_id")), {}).get("mod")
    }
    return sorted(str(authors[n]) for n in names if n in authors)
