from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from openai import OpenAI

logger = logging.getLogger("incident_mod_bot")


@dataclass(frozen=True)
class OpenAISettings:
    api_key: str
    model: str
    image_detail: str
    debug_logs: bool = False


def create_client(api_key: str) -> OpenAI:
    return OpenAI(api_key=api_key)


def _log_usage(settings: OpenAISettings, response: Any, purpose: str) -> None:
    if not settings.debug_logs:
        return

    response_id = getattr(response, "id", None)
    incomplete = getattr(response, "incomplete_details", None)
    if incomplete is not None:
        reason = getattr(incomplete, "reason", None)
        logger.info(
            "OpenAI response %s: response_id=%s incomplete_reason=%s",
            purpose,
            response_id,
            reason,
        )
    else:
        logger.info("OpenAI response %s: response_id=%s", purpose, response_id)

    usage = getattr(response, "usage", None)
    if usage is None:
        logger.info("OpenAI usage %s: (missing)", purpose)
        return
    input_tokens = getattr(usage, "input_tokens", None)
    output_tokens = getattr(usage, "output_tokens", None)
    total_tokens = getattr(usage, "total_tokens", None)
    cached_tokens = None
    reasoning_tokens = None
    details = getattr(usage, "input_tokens_details", None)
    if details is not None:
        cached_tokens = getattr(details, "cached_tokens", None)
    out_details = getattr(usage, "output_tokens_details", None)
    if out_details is not None:
        reasoning_tokens = getattr(out_details, "reasoning_tokens", None)
    logger.info(
        "OpenAI usage %s: input=%s output=%s total=%s cached=%s reasoning=%s",
        purpose,
        input_tokens,
        output_tokens,
        total_tokens,
        cached_tokens,
        reasoning_tokens,
    )


def _response_text(response: Any) -> str:
    err = getattr(response, "error", None)
    if err is not None:
        code = getattr(err, "code", "unknown")
        message = getattr(err, "message", "(no message)")
        raise RuntimeError(f"OpenAI response error: {code} {message}")

    # SDK convenience property.
    text = getattr(response, "output_text", "")
    if isinstance(text, str) and text.strip():
        return text

    output = getattr(response, "output", None)
    if output is None:
        return ""
    try:
        output_types = [getattr(item, "type", str(type(item))) for item in output]
        logger.info("OpenAI response had no output_text; output types=%s", output_types)
    except Exception:
        logger.info("OpenAI response had no output_text and output could not be inspected")
    return ""


def _dump_failure(text: str, purpose: str) -> str | None:
    base_dir = os.getenv("OPENAI_DEBUG_DUMP_DIR", "data/openai_debug")
    try:
        Path(base_dir).mkdir(parents=True, exist_ok=True)
        filename = f"{purpose}_{int(time.time())}_{uuid4().hex[:8]}.txt"
        path = Path(base_dir) / filename
        path.write_text(text, encoding="utf-8", errors="replace")
        return str(path)
    except Exception:
        return None


def _parse_json(text: str, purpose: str, *, debug_dump: bool) -> Any:
    if not text or not text.strip():
        raise ValueError(f"{purpose}: empty response text")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        head = text[:200].replace("\n", "\\n")
        tail = text[-200:].replace("\n", "\\n")
        dump_path = _dump_failure(text, purpose=purpose) if debug_dump else None
        extra = f" dump={dump_path}" if dump_path else ""
        raise ValueError(
            f"{purpose}: invalid JSON at line={exc.lineno} col={exc.colno} pos={exc.pos}"
            f" (first 200 chars={head!r} last 200 chars={tail!r}){extra}"
        ) from exc


def summarize_rules(client: OpenAI, settings: OpenAISettings, rules_text: str) -> dict[str, Any]:
    prompt = (
        "Summarize these server rules into a compact JSON object with a 'rules' list. "
        "Each rule should have: id (short), title, summary, and enforcement (if stated). "
        "Output JSON only.\n\nRules:\n"
        f"{rules_text}"
    )
    responses = getattr(client, "responses")
    response = responses.create(
        model=settings.model,
        input=[{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
        temperature=0.2,
        text={"format": {"type": "json_object"}},
        max_output_tokens=800,
    )
    _log_usage(settings, response, purpose="summarize_rules")
    text = _response_text(response)
    data = _parse_json(text, purpose="summarize_rules", debug_dump=settings.debug_logs)
    if not isinstance(data, dict):
        raise ValueError("summarize_rules: expected a JSON object")
    return data


def summarize_images(
    client: OpenAI,
    settings: OpenAISettings,
    image_payloads: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [
        {
            "type": "input_text",
            "text": (
                "For each image, write a 1-sentence note focused on what matters for moderation OR understanding the back-and-forth. "
                "Output flags:\n"
                "- is_evidence=true if the image is likely relevant evidence for moderation action (e.g., it shows a scam attempt/spam solicitation, harassment/insults, slurs/threats, doxxing/personal info, NSFW/gore, or a screenshot/log/receipt that substantiates a report).\n"
                "- is_context=true only if the image materially changes how to interpret nearby messages (especially reply chains), even if it is not a rule violation.\n"
                "If the image is a screenshot shared as proof (e.g., DMs showing a scam attempt), treat it as evidence even if it is a screenshot of a conversation. Do not quote or transcribe personal info; summarize at a high level.\n"
                "Otherwise set both false. "
                "Return JSON only as an object: {\"images\": [{\"id\": string, \"note\": string, \"is_evidence\": boolean, \"is_context\": boolean}]}."
            ),
        }
    ]
    for payload in image_payloads:
        content.append({"type": "input_text", "text": f"Image id: {payload['id']}"})
        context = payload.get("context")
        if isinstance(context, str) and context.strip():
            content.append({"type": "input_text", "text": f"Context: {context.strip()}"})
        content.append(
            {
                "type": "input_image",
                "image_url": payload["data_url"],
                "detail": payload.get("detail") or settings.image_detail,
            }
        )
    responses = getattr(client, "responses")
    response = responses.create(
        model=settings.model,
        input=[{"role": "user", "content": content}],
        temperature=0.2,
        text={"format": {"type": "json_object"}},
        max_output_tokens=600,
    )
    _log_usage(settings, response, purpose="summarize_images")
    text = _response_text(response)
    data = _parse_json(text, purpose="summarize_images", debug_dump=settings.debug_logs)
    if not isinstance(data, dict):
        raise ValueError("summarize_images: expected a JSON object")
    images = data.get("images")
    if not isinstance(images, list):
        raise ValueError("summarize_images: missing 'images' list")
    out: list[dict[str, Any]] = []
    for item in images:
        if not isinstance(item, dict):
            continue
        if "id" not in item or "note" not in item:
            continue
        is_evidence = item.get("is_evidence", False)
        is_context = item.get("is_context", False)
        out.append(
            {
                "id": str(item["id"]),
                "note": str(item["note"]),
                "is_evidence": bool(is_evidence),
                "is_context": bool(is_context),
            }
        )
    return out


def analyze_incident(
    client: OpenAI,
    settings: OpenAISettings,
    payload: dict[str, Any],
) -> dict[str, Any]:
    prompt = (
        "You are a Discord moderation assistant. Your job is to help a busy mod catch up fast. "
        "Write like a competent human mod: terse, direct, no filler, no 'AI voice'. No emojis. "
        "Do not narrate obvious things unless it changes the decision. "
        "Use server_memory to match tone/expectations. "
        "Use user_memory only as cautious context (requires repeated observations). "
        "Never speculate; only cite what you can evidence from messages. "
        "If evidence is weak, say so plainly and suggest a neutral check-in.\n\n"
        "If payload includes anchor_message_id, focus the brief around that message "
        "(what led up to it and what happened right after).\n\n"
        "Return JSON only with keys: headline, summary, participants, signals, rule_refs, "
        "recommendations, draft_message, reply_targets, draft_replies, confidence, evidence_quotes, memory_suggestions.\n\n"
        "Hard limits (keep it tight):\n"
        "- headline: <= 70 chars. The one line a moderator needs: what happened, then "
        "the recommended action, separated by ' - '. e.g. 'RMT link - delete + warn', "
        "'Begging in LFG - warn', 'Heated argument - no action'. No names, no @mentions, "
        "no rule ids, no trailing period. This is the first and sometimes only thing read.\n"
        "- summary: 1-2 sentences, <= 220 chars. Prefer: 'Start: ... | Flashpoint: ... | Now: ...'.\n"
        "- participants: <= 4\n"
        "- signals: <= 4\n"
        "- rule_refs: <= 2\n"
        "- recommendations: <= 3. If none: [\\\"No action.\\\"].\n"
        "- evidence_quotes: <= 3; each quote <= 140 chars\n"
        "- draft_message: if recommendations is exactly [\\\"No action.\\\"], set draft_message to empty string and reply_targets to [].\n"
        "- Otherwise, draft_message <= 350 chars, 1-3 sentences; sound like a mod, not a chatbot.\n"
        "  Avoid corporate/mod-bot phrasing like: 'to maintain a positive environment', 'friendly reminder', 'we appreciate', 'come across as', 'toxic'.\n"
        "  Use 'please' and keep it calm/empathetic.\n"
        "  Use conditional consequences when relevant: 'If you keep doing X, you will be timed out.'\n"
        "  Do not cite rules by default; only cite if it's genuinely helpful.\n"
        "  If you do cite, use the rule_refs id (e.g. 'Rule: respect').\n"
        "  Output ASCII only. No emojis or special symbols.\n"
        "  Do not include @mentions in draft_message or draft_replies (we will add pings).\n"
        "- reply_targets: set 1-3 targets only if draft_message is non-empty. If exactly one target, set message_id to a relevant message to reply to. If >1 targets, set message_id to null.\n"
        "- draft_replies: optional; use only if you want different copy per target (<= 3 lines).\n"
        "- confidence: number between 0 and 1\n"
        "- memory_suggestions: only when moderation-relevant; if no action, keep both lists empty\n\n"
        "If recommendations is exactly [\\\"No action.\\\"], be extra compact:\n"
        "- participants: <= 3\n"
        "- signals: <= 3\n"
        "- evidence_quotes: <= 2\n"
        "- memory_suggestions.server_notes: []\n"
        "- memory_suggestions.user_notes: []\n\n"
        "Participants entries: {user_id, name, role, notes} (notes optional, <= 60 chars).\n"
        "Reply targets: [{user_id, message_id}].\n"
        "Draft replies: [{user_id, text}] where text is what comes after the ping.\n"
        "Evidence quotes entries: {quote, message_id} where message_id is from payload.messages[].id. Do not output URLs.\n"
        "Rule refs entries: {id, reason}.\n"
        "Memory suggestions: {server_notes: [str], user_notes: [{user_id, label, evidence_message_id}]}.\n"
        "Only suggest user_notes if the behavior appears more than once or is clearly habitual.\n"
        "Use the provided user_id values.\n"
        "IDs can be long. Treat user_id and message_id as opaque identifiers and copy digits exactly from the payload; never guess or alter IDs. If you cannot provide a valid ID, use null or omit that field.\n\n"
        "Payload:\n"
        + json.dumps(payload, ensure_ascii=True)
    )
    responses = getattr(client, "responses")
    response = responses.create(
        model=settings.model,
        input=[{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
        temperature=0.0,
        text={"format": {"type": "json_object"}},
        max_output_tokens=900,
    )
    _log_usage(settings, response, purpose="analyze_incident")
    text = _response_text(response)
    data = _parse_json(text, purpose="analyze_incident", debug_dump=settings.debug_logs)
    if not isinstance(data, dict):
        raise ValueError("analyze_incident: expected a JSON object")
    return data


def refine_incident_with_images(
    client: OpenAI,
    settings: OpenAISettings,
    base_result: dict[str, Any],
    image_notes: list[dict[str, Any]],
) -> dict[str, Any]:
    prompt = (
        "You are updating a Discord mod brief. "
        "You already generated a JSON result from text messages. Now you have image notes from the same message window. "
        "Use them to correct the story if needed. "
        "Only mention images explicitly if they matter to moderation or the key back-and-forth. "
        "If an image note indicates a screenshot/log offered as proof (e.g., DMs showing a scam attempt), acknowledge that in summary/signals and avoid saying 'no evidence' unless no such proof was provided. If the screenshot is not conclusive, say so plainly. "
        "Write like a competent human mod: terse, direct, no filler, no 'AI voice'. No emojis.\n"
        "Output ASCII only. No emojis or special symbols.\n\n"
        "Return JSON only with keys: headline, summary, participants, signals, rule_refs, "
        "recommendations, draft_message, reply_targets, draft_replies, confidence, evidence_quotes, memory_suggestions.\n\n"
        "Hard limits (keep it tight):\n"
        "- headline: <= 70 chars. The one line a moderator needs: what happened, then "
        "the recommended action, separated by ' - '. e.g. 'RMT link - delete + warn', "
        "'Begging in LFG - warn', 'Heated argument - no action'. No names, no @mentions, "
        "no rule ids, no trailing period. This is the first and sometimes only thing read.\n"
        "- summary: 1-2 sentences, <= 220 chars. Prefer: 'Start: ... | Flashpoint: ... | Now: ...'.\n"
        "- participants: <= 4\n"
        "- signals: <= 4\n"
        "- rule_refs: <= 2\n"
        "- recommendations: <= 3. If none: [\\\"No action.\\\"].\n"
        "- evidence_quotes: <= 3; each quote <= 140 chars\n"
        "- draft_message: if recommendations is exactly [\\\"No action.\\\"], set draft_message to empty string and reply_targets to [].\n"
        "- reply_targets: <= 3; if you want a public reply, set 1-3 targets.\n"
        "- draft_replies: optional; use only if you want different copy per target (<= 3 lines).\n"
        "- draft_message otherwise: <= 350 chars, 1-3 sentences; sound like a mod, not a chatbot.\n"
        "  Avoid corporate/mod-bot phrasing like: 'to maintain a positive environment', 'friendly reminder', 'we appreciate', 'come across as', 'toxic'.\n"
        "  Use 'please' and keep it calm/empathetic.\n"
        "  Use conditional consequences: 'If you keep doing X, you will be timed out.'\n"
        "  Do not cite rules by default; only cite if it's genuinely helpful.\n"
        "  If you do cite, use the rule_refs id (e.g. 'Rule: respect').\n"
        "  Do not include @mentions in draft_message or draft_replies (we will add pings).\n"
        "- confidence: number between 0 and 1\n\n"

        "If recommendations is exactly [\\\"No action.\\\"], be extra compact:\n"
        "- participants: <= 3\n"
        "- signals: <= 3\n"
        "- evidence_quotes: <= 2\n"
        "- memory_suggestions.server_notes: []\n"
        "- memory_suggestions.user_notes: []\n\n"
        "Reply targets: [{user_id, message_id}]. If exactly one target, set message_id to the specific message to reply to. If >1 targets, set message_id to null.\n"
        "Draft replies: [{user_id, text}] where text is what comes after the ping.\n"
        "Evidence quotes entries: {quote, message_id} where message_id is from the provided image notes or payload messages.\n"
        "Image notes entries include: image_id, message_id, author_name, note, is_evidence, is_context.\n"
        "IDs can be long. Treat user_id and message_id as opaque identifiers and copy digits exactly from the base result or image notes; never guess or alter IDs. If you cannot provide a valid ID, use null or omit that field.\n\n"
        "Base result (from text):\n"
        + json.dumps(base_result, ensure_ascii=True)
        + "\n\nImage notes:\n"
        + json.dumps(image_notes, ensure_ascii=True)
    )
    responses = getattr(client, "responses")
    response = responses.create(
        model=settings.model,
        input=[{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
        temperature=0.0,
        text={"format": {"type": "json_object"}},
        # Must exceed analyze_incident's budget (900): this step consumes that
        # result and returns a superset of it. At 650 it truncated mid-JSON
        # ("Unterminated string" at ~2300 chars) and the refinement was lost.
        max_output_tokens=1200,
    )
    _log_usage(settings, response, purpose="refine_incident_with_images")
    text = _response_text(response)
    data = _parse_json(text, purpose="refine_incident_with_images", debug_dump=settings.debug_logs)
    if not isinstance(data, dict):
        raise ValueError("refine_incident_with_images: expected a JSON object")
    return data
