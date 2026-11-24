"""A rolling, full-detail log of every brief: what triggered it, everything
the model was given, what it answered, and every later look and rebuild.

Two bad briefs in November could only be explained after the fact by
replaying the audit log and the saved payload by hand, and the saved
payload is overwritten on every rebuild, so the first look was gone. This
keeps every step as one JSON line, newest last, in data/logs/briefs.jsonl,
rotated by size so it never grows without bound (BRIEF_LOG_MAX_MB per file,
BRIEF_LOG_FILES files kept; 0 files turns it off). The lines hold Discord
messages, so the files are owner-readable only.

Read it with jq, e.g. everything about one ping:
    jq -c 'select(.anchor_message_id == <anchor message id>)' data/logs/briefs.jsonl*
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

_logger = logging.getLogger("incident_mod_bot.brief_log")
_logger.propagate = False
_logger.setLevel(logging.INFO)
_configured = False


class _PrivateRotatingFileHandler(RotatingFileHandler):
    def _open(self):  # type: ignore[override]
        stream = super()._open()
        try:
            os.chmod(self.baseFilename, 0o600)
        except OSError:
            pass
        return stream


def configure(directory: str | None = None) -> str | None:
    """Start writing. Returns the file path, or None when turned off."""
    global _configured
    files = int(os.getenv("BRIEF_LOG_FILES", "10"))
    if files <= 0:
        return None
    max_mb = float(os.getenv("BRIEF_LOG_MAX_MB", "20"))
    base = Path(directory or os.getenv("BRIEF_LOG_DIR", "data/logs"))
    base.mkdir(parents=True, exist_ok=True)
    path = base / "briefs.jsonl"
    for handler in list(_logger.handlers):
        _logger.removeHandler(handler)
        handler.close()
    handler = _PrivateRotatingFileHandler(
        path, maxBytes=int(max_mb * 1024 * 1024), backupCount=files - 1, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(handler)
    _configured = True
    return str(path)


def _ms(snowflake: Any) -> int | None:
    try:
        return (int(snowflake) >> 22) + 1420070400000
    except (TypeError, ValueError):
        return None


def record(event: str, **fields: Any) -> None:
    """One line. Never raises: losing a log line must not lose a brief."""
    if not _configured:
        return
    try:
        now = time.time()
        line: dict[str, Any] = {
            "ts": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="milliseconds"),
            "event": event,
        }
        anchor_ms = _ms(fields.get("anchor_message_id"))
        if anchor_ms is not None:
            # Seconds since the ping, which is what every race comes down to.
            line["since_ping_s"] = round(now - anchor_ms / 1000, 1)
        line.update(fields)
        _logger.info(json.dumps(line, default=str, ensure_ascii=False))
    except Exception:
        logging.getLogger(__name__).exception("brief_log.record failed for %s", event)
