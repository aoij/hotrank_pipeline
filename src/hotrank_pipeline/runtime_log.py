from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any


_LOG_LOCK = Lock()
_LOG_DIR = Path(__file__).resolve().parents[2] / "data" / "runtime_logs"
_LOG_PATH = _LOG_DIR / "web_runtime_log.jsonl"


def _ensure_log_dir() -> None:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)


def append_runtime_log(level: str, message: str, source: str = "web") -> dict[str, Any]:
    _ensure_log_dir()
    now = datetime.now()
    entry = {
        "time": now.strftime("%H:%M:%S"),
        "created_at": now.isoformat(timespec="seconds"),
        "level": (level or "info").lower(),
        "source": source,
        "message": message.strip(),
    }
    with _LOG_LOCK:
        with _LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def read_runtime_logs(limit: int = 120) -> list[dict[str, Any]]:
    if not _LOG_PATH.exists():
        return []

    entries: list[dict[str, Any]] = []
    for raw_line in _LOG_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries[-max(1, limit) :]


def clear_runtime_logs() -> int:
    if not _LOG_PATH.exists():
        return 0
    with _LOG_LOCK:
        try:
            count = len(_LOG_PATH.read_text(encoding="utf-8").splitlines())
        except OSError:
            count = 0
        _LOG_PATH.write_text("", encoding="utf-8")
    return count


def latest_notice(logs: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not logs:
        return None
    return logs[-1]
