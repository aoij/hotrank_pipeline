from __future__ import annotations

import json
from pathlib import Path


_HISTORY_DIR = Path(__file__).resolve().parents[2] / "data" / "scheduler"
_HISTORY_PATH = _HISTORY_DIR / "daily_publish_history.jsonl"


def record_scheduler_history(entry: dict) -> None:
    _HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    with _HISTORY_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def read_scheduler_history(limit: int = 20) -> list[dict]:
    if not _HISTORY_PATH.exists():
        return []
    entries: list[dict] = []
    for raw in _HISTORY_PATH.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            entries.append(json.loads(raw))
        except Exception:
            continue
    return list(reversed(entries[-max(1, limit) :]))


def latest_scheduler_history() -> dict | None:
    items = read_scheduler_history(limit=1)
    return items[0] if items else None


def latest_scheduler_brief_entry(limit: int = 50) -> dict | None:
    for item in read_scheduler_history(limit=max(1, limit)):
        if str(item.get("brief_path") or "").strip():
            return item
    return None
