from __future__ import annotations

import hashlib
import json
from pathlib import Path


_HISTORY_DIR = Path(__file__).resolve().parents[2] / "data" / "scheduler"
_HISTORY_PATH = _HISTORY_DIR / "daily_publish_history.jsonl"
_DATA_ROOT = Path(__file__).resolve().parents[2] / "data"


def _entry_history_id(raw: str) -> str:
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _normalize_entry(entry: dict, raw: str) -> dict:
    item = dict(entry)
    item["history_id"] = _entry_history_id(raw)
    return item


def _safe_delete_brief_file(brief_path: str | None) -> bool:
    raw_path = str(brief_path or "").strip()
    if not raw_path:
        return False
    try:
        candidate = Path(raw_path)
        resolved = candidate.resolve()
        data_root = _DATA_ROOT.resolve()
        resolved.relative_to(data_root)
    except Exception:
        return False
    try:
        candidate.unlink(missing_ok=True)
        return True
    except OSError:
        return False


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
            entry = json.loads(raw)
        except Exception:
            continue
        entries.append(_normalize_entry(entry, raw))
    return list(reversed(entries[-max(1, limit) :]))


def latest_scheduler_history() -> dict | None:
    items = read_scheduler_history(limit=1)
    return items[0] if items else None


def latest_scheduler_brief_entry(limit: int = 50) -> dict | None:
    for item in read_scheduler_history(limit=max(1, limit)):
        if str(item.get("brief_path") or "").strip():
            return item
    return None


def delete_scheduler_history_entry(history_id: str, *, delete_brief: bool = True) -> dict:
    target_id = (history_id or "").strip()
    if not target_id or not _HISTORY_PATH.exists():
        return {"deleted": 0, "brief_deleted": 0}

    kept_lines: list[str] = []
    deleted = 0
    brief_deleted = 0

    for raw in _HISTORY_PATH.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            entry = json.loads(stripped)
        except Exception:
            kept_lines.append(stripped)
            continue
        current_id = _entry_history_id(stripped)
        if current_id != target_id:
            kept_lines.append(stripped)
            continue
        deleted += 1
        if delete_brief and _safe_delete_brief_file(entry.get("brief_path")):
            brief_deleted += 1

    if deleted:
        if kept_lines:
            _HISTORY_PATH.write_text("\n".join(kept_lines) + "\n", encoding="utf-8")
        else:
            _HISTORY_PATH.unlink(missing_ok=True)
    return {"deleted": deleted, "brief_deleted": brief_deleted}


def clear_scheduler_history(*, delete_briefs: bool = True) -> dict:
    if not _HISTORY_PATH.exists():
        return {"deleted": 0, "brief_deleted": 0}

    deleted = 0
    brief_deleted = 0
    for raw in _HISTORY_PATH.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            entry = json.loads(stripped)
        except Exception:
            continue
        deleted += 1
        if delete_briefs and _safe_delete_brief_file(entry.get("brief_path")):
            brief_deleted += 1
    _HISTORY_PATH.unlink(missing_ok=True)
    return {"deleted": deleted, "brief_deleted": brief_deleted}
