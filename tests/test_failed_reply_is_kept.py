"""A garbled model reply used to leave only its first and last 200 characters in
the log, which made it impossible to see what the model had actually done. The
whole reply is now kept whenever parsing fails, newest 20, owner-readable only.
"""
import os
import stat

import pytest

from incident_mod_bot.openai_client import _parse_json

GARBLED = '{"headline": "x", "summary": "' + "d7a27cc8" * 400


def test_the_whole_failed_reply_is_saved_without_debug_logging(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_DEBUG_DUMP_DIR", str(tmp_path))

    with pytest.raises(ValueError) as caught:
        _parse_json(GARBLED, purpose="analyze_incident")

    [saved] = list(tmp_path.glob("analyze_incident_*.txt"))
    assert saved.read_text() == GARBLED
    assert f"dump={saved}" in str(caught.value)
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600


def test_only_the_newest_twenty_are_kept(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_DEBUG_DUMP_DIR", str(tmp_path))
    for i in range(22):
        old = tmp_path / f"analyze_incident_{i}.txt"
        old.write_text("old")
        os.utime(old, ns=(1_000_000_000 + i, 1_000_000_000 + i))

    with pytest.raises(ValueError) as caught:
        _parse_json(GARBLED, purpose="analyze_incident")

    kept = {f.name for f in tmp_path.glob("*.txt")}
    assert len(kept) == 20
    assert os.path.basename(str(caught.value).split("dump=")[1]) in kept
    assert "analyze_incident_0.txt" not in kept and "analyze_incident_2.txt" not in kept
    assert "analyze_incident_21.txt" in kept


def test_valid_and_empty_replies_write_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_DEBUG_DUMP_DIR", str(tmp_path))
    assert _parse_json('{"a": 1}', purpose="x") == {"a": 1}
    with pytest.raises(ValueError, match="empty"):
        _parse_json("  ", purpose="x")
    assert list(tmp_path.iterdir()) == []
