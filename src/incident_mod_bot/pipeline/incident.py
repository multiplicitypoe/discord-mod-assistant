from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, field_validator


class Participant(BaseModel):
    user_id: int
    name: str
    role: str = "member"
    notes: str | None = None


class RuleRef(BaseModel):
    id: str
    reason: str


class ReplyTarget(BaseModel):
    user_id: int
    # If exactly one user is targeted, set message_id to the specific message to reply to.
    message_id: int | None = None


class DraftReplyLine(BaseModel):
    user_id: int
    text: str


class EvidenceQuote(BaseModel):
    quote: str
    message_id: int | None = None
    link: str | None = None


class ImageNote(BaseModel):
    note: str
    link: str


class UserMemorySuggestion(BaseModel):
    user_id: int
    label: str
    evidence_message_id: int | None = None
    evidence_link: str | None = None


class MemorySuggestions(BaseModel):
    server_notes: list[str] = Field(default_factory=list)
    user_notes: list[UserMemorySuggestion] = Field(default_factory=list)


class IncidentResult(BaseModel):
    # One-line verdict: what happened + what to do. Leads the brief, because
    # moderators were writing this line by hand from the sections below.
    headline: str = ""
    # How many enforcement observations informed this brief. Rendered so the
    # ledger's contribution is visible rather than silent.
    informed_by: int = 0
    summary: str
    participants: list[Participant] = Field(default_factory=list)
    signals: list[str] = Field(default_factory=list)
    rule_refs: list[RuleRef] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    draft_message: str
    reply_targets: list[ReplyTarget] = Field(default_factory=list)
    draft_replies: list[DraftReplyLine] = Field(default_factory=list)
    confidence: float = 0.0
    evidence_quotes: list[EvidenceQuote] = Field(default_factory=list)
    image_notes: list[ImageNote] = Field(default_factory=list)
    memory_suggestions: MemorySuggestions = Field(default_factory=MemorySuggestions)
    # The pinger's own conduct (baiting, mocking the mods, a repeat pattern),
    # from the model. Empty when there is nothing worth a moderator's time.
    reporter_note: str = ""
    # The pinger's earlier pings, rendered from stored briefs, never by the
    # model (ping_context.format_ping_history).
    ping_history: str = Field(default="", exclude=True)
    # Media keys ("att:<id>", "url:<url>") the analysis pass already looked
    # at, so the background image pass doesn't pay to look again.
    analysed_media: list[str] = Field(default_factory=list, exclude=True)

    @field_validator("confidence", mode="before")
    @classmethod
    def _coerce_confidence(cls, v: Any) -> float:
        """Coerce LLM-friendly confidence values into a 0..1 float.

        The model sometimes returns strings like "high" instead of a number.
        """

        if v is None:
            return 0.0

        if isinstance(v, (int, float)):
            out = float(v)
            return max(0.0, min(1.0, out))

        if isinstance(v, str):
            s = v.strip().lower()
            s = s.replace("_", " ")
            s = s.strip(" \t\r\n.,!?")
            if not s:
                return 0.0
            try:
                if s.endswith("%"):
                    out = float(s[:-1].strip()) / 100.0
                else:
                    out = float(s)
                return max(0.0, min(1.0, out))
            except ValueError:
                mapping = {
                    "very high": 0.9,
                    "high": 0.8,
                    "medium": 0.55,
                    "low": 0.3,
                    "very low": 0.15,
                }
                if s in mapping:
                    return mapping[s]

        return 0.0


@dataclass(frozen=True)
class IncidentPayload:
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return self.payload


def _as_id(value: Any, known_users: Mapping[str, int]) -> int | None:
    """An id the model returned, or None if it isn't one.

    The prompt asks for ids copied from the payload, but audit_findings names
    people ("Banned spam_account · by mod_b") without ids, and the model then
    sometimes puts the name where the id goes. Resolve it when the name is
    someone in the window rather than failing the whole brief over it.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        s = value.strip().lstrip("@")
        if s.isdigit():
            return int(s)
        return known_users.get(s.casefold())
    return None


def _sanitize_ids(data: dict[str, Any], known_users: Mapping[str, int]) -> dict[str, Any]:
    """Drop entries whose required user_id can't be resolved, and null out
    optional message ids that aren't ids. One bad id used to fail validation
    for the entire result, which throws away a whole analysis - on
    2026-09-27 that was the refresh that had seen the ban and would have
    corrected a wrong brief."""
    out = dict(data)

    def _clean_list(items: Any, *, optional_ids: tuple[str, ...] = ()) -> Any:
        if not isinstance(items, list):
            return items
        kept = []
        for item in items:
            if not isinstance(item, dict):
                kept.append(item)
                continue
            item = dict(item)
            user_id = _as_id(item.get("user_id"), known_users)
            if user_id is None:
                continue
            item["user_id"] = user_id
            for key in optional_ids:
                if item.get(key) is not None:
                    item[key] = _as_id(item[key], {})
            kept.append(item)
        return kept

    for key, optional_ids in (
        ("participants", ()),
        ("reply_targets", ("message_id",)),
        ("draft_replies", ()),
    ):
        if out.get(key) is None:
            out.pop(key, None)
        else:
            out[key] = _clean_list(out[key], optional_ids=optional_ids)

    # Seen from the gpt-5.4 models: a rule ref as a bare id string, and
    # signals as objects or a single string.
    rule_refs = out.get("rule_refs")
    if isinstance(rule_refs, list):
        out["rule_refs"] = [
            {"id": r, "reason": ""} if isinstance(r, str) else r for r in rule_refs
        ]
    signals = out.get("signals")
    if isinstance(signals, str):
        out["signals"] = [signals]
    elif isinstance(signals, list):
        out["signals"] = [
            s if isinstance(s, str) else json.dumps(s, ensure_ascii=True) for s in signals
        ]

    quotes = out.get("evidence_quotes")
    if isinstance(quotes, list):
        out["evidence_quotes"] = [
            {**q, "message_id": _as_id(q.get("message_id"), {})} if isinstance(q, dict) else q
            for q in quotes
        ]

    memory = out.get("memory_suggestions")
    if isinstance(memory, dict) and "user_notes" in memory:
        out["memory_suggestions"] = {
            **memory,
            "user_notes": _clean_list(memory["user_notes"], optional_ids=("evidence_message_id",)),
        }
    return out


def known_users_from_payload(payload: Mapping[str, Any]) -> dict[str, int]:
    """author_name -> author_id for everyone in an analysis payload's window."""
    out: dict[str, int] = {}
    for m in payload.get("messages") or []:
        name, user_id = m.get("author_name"), m.get("author_id")
        if isinstance(name, str) and isinstance(user_id, int):
            out.setdefault(name.casefold(), user_id)
    return out


_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _strip_control_chars(data: dict[str, Any]) -> dict[str, Any]:
    """The payload goes out ASCII-escaped, and the model sometimes copies a
    name like "user (\u2642)" back with a raw control byte in place of the
    symbol, which Discord shows as a box or nothing."""
    out = dict(data)
    for key in ("headline", "summary", "reporter_note"):
        if isinstance(out.get(key), str):
            out[key] = _CONTROL_CHARS.sub("", out[key])
    return out


def parse_incident_result(
    data: dict[str, Any], known_users: Mapping[str, int] | None = None
) -> IncidentResult:
    return IncidentResult.model_validate(
        _strip_control_chars(_sanitize_ids(data, known_users or {}))
    )
