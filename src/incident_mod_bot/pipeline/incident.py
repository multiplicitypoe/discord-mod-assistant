from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field, field_validator


class Participant(BaseModel):
    user_id: int
    name: str
    role: str
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


def parse_incident_result(data: dict[str, Any]) -> IncidentResult:
    return IncidentResult.model_validate(data)
