from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .config import Settings, load_runtime_config
from .daily_brief import create_daily_publish_brief
from .runtime_log import append_runtime_log
from .scheduler_history import read_scheduler_history, record_scheduler_history
from .services import run_daily_auto_publish


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SCHEDULER_DIR = _PROJECT_ROOT / "data" / "scheduler"
_DAILY_RUN_LOCK_PATH = _SCHEDULER_DIR / "daily_publish.lock"
_DAILY_RUN_LOCK_STALE_SECONDS = 6 * 60 * 60

_SCHEDULER_LOCK = threading.RLock()
_PROCESS_RUN_LOCK = threading.Lock()
_SCHEDULER_THREAD: threading.Thread | None = None
_SCHEDULER_STOP = threading.Event()
_SCHEDULER_STATE = {
    "enabled": False,
    "time": "07:00",
    "draft_limit": 10,
    "publish_limit": 4,
    "retry_count": 2,
    "enable_wechat": True,
    "enable_toutiao": True,
    "preference_keywords": [],
    "last_run_at": "",
    "last_status": "",
    "last_message": "",
    "active_run_started_at": "",
    "active_run_trigger": "",
}


class _DailyPublishFileLock:
    """跨进程每日发布锁，避免 Web 调度、手动按钮和 Windows 计划任务同时发布。"""

    def __init__(self, trigger: str, stale_seconds: int = _DAILY_RUN_LOCK_STALE_SECONDS) -> None:
        self.trigger = trigger
        self.stale_seconds = max(60, int(stale_seconds))
        self.path = _DAILY_RUN_LOCK_PATH
        self.fd: int | None = None
        self.existing_payload = ""
        self.acquired = False

    def _payload(self) -> str:
        payload = {
            "pid": os.getpid(),
            "trigger": self.trigger,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "cwd": str(Path.cwd()),
        }
        return json.dumps(payload, ensure_ascii=False)

    def _read_existing(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8")[:500]
        except Exception:
            return ""

    def _remove_stale_if_needed(self) -> None:
        try:
            age_seconds = time.time() - self.path.stat().st_mtime
        except FileNotFoundError:
            return
        except Exception:
            return
        if age_seconds < self.stale_seconds:
            return
        try:
            self.path.unlink()
            append_runtime_log(
                "warning",
                f"[自动任务] 已清理过期跨进程锁：path={self.path}｜age={int(age_seconds)}s",
                source="scheduler",
            )
        except FileNotFoundError:
            pass
        except Exception as exc:
            append_runtime_log(
                "warning",
                f"[自动任务] 清理过期跨进程锁失败：path={self.path}｜{exc}",
                source="scheduler",
            )

    def __enter__(self) -> "_DailyPublishFileLock":
        _SCHEDULER_DIR.mkdir(parents=True, exist_ok=True)
        self._remove_stale_if_needed()
        try:
            self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(self.fd, self._payload().encode("utf-8"))
            self.acquired = True
        except FileExistsError:
            self.existing_payload = self._read_existing()
            self.acquired = False
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except Exception:
                pass
            self.fd = None
        if self.acquired:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            except Exception:
                pass
        self.acquired = False


def _today_history_items() -> list[dict[str, Any]]:
    today = datetime.now().strftime("%Y-%m-%d")
    items: list[dict[str, Any]] = []
    try:
        for item in read_scheduler_history(limit=200):
            run_at = str(item.get("run_at") or "")
            if run_at.startswith(today):
                items.append(item)
    except Exception:
        pass
    return items


def _today_latest_history_status() -> tuple[str, str]:
    items = _today_history_items()
    if not items:
        return "", ""
    for item in items:
        if str(item.get("status") or "").lower() == "success":
            return "success", str(item.get("message") or "")
    latest = items[0]
    return str(latest.get("status") or ""), str(latest.get("message") or "")


def today_has_successful_daily_publish() -> bool:
    status, _message = _today_latest_history_status()
    return status == "success"


def _daily_publish_config(settings: Settings) -> dict:
    runtime = load_runtime_config(settings)
    return (runtime.get("automation") or {}).get("daily_publish") or {}


def _normalize_schedule_time(value: str) -> str:
    raw = (value or "07:00").strip()
    try:
        hour_text, minute_text = raw.split(":", 1)
        hour = max(0, min(int(hour_text), 23))
        minute = max(0, min(int(minute_text), 59))
    except Exception:
        hour = 7
        minute = 0
    return f"{hour:02d}:{minute:02d}"


def _refresh_state(settings: Settings) -> dict:
    config = _daily_publish_config(settings)
    with _SCHEDULER_LOCK:
        _SCHEDULER_STATE["enabled"] = bool(config.get("enabled", False))
        _SCHEDULER_STATE["time"] = _normalize_schedule_time(str(config.get("schedule_time") or "07:00"))
        _SCHEDULER_STATE["draft_limit"] = max(1, int(config.get("draft_limit") or 10))
        _SCHEDULER_STATE["publish_limit"] = max(1, int(config.get("publish_limit") or 4))
        _SCHEDULER_STATE["retry_count"] = max(1, int(config.get("retry_count") or 2))
        _SCHEDULER_STATE["enable_wechat"] = bool(config.get("enable_wechat", True))
        _SCHEDULER_STATE["enable_toutiao"] = bool(config.get("enable_toutiao", True))
        _SCHEDULER_STATE["preference_keywords"] = [
            str(item).strip()
            for item in (config.get("preference_keywords") or [])
            if str(item).strip()
        ]
        state = dict(_SCHEDULER_STATE)
        state["active_run"] = bool(_PROCESS_RUN_LOCK.locked() or _DAILY_RUN_LOCK_PATH.exists())
        state["file_lock_path"] = str(_DAILY_RUN_LOCK_PATH)
        state["today_has_success"] = today_has_successful_daily_publish()
        return state


def get_scheduler_state(settings: Settings) -> dict:
    state = _refresh_state(settings)
    with _SCHEDULER_LOCK:
        state["running"] = bool(_SCHEDULER_THREAD and _SCHEDULER_THREAD.is_alive() and not _SCHEDULER_STOP.is_set())
        return state


def _mark_scheduler_result(status: str, message: str) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _SCHEDULER_LOCK:
        _SCHEDULER_STATE["last_run_at"] = now
        _SCHEDULER_STATE["last_status"] = status
        _SCHEDULER_STATE["last_message"] = message


def _set_active_run(trigger: str) -> None:
    with _SCHEDULER_LOCK:
        _SCHEDULER_STATE["active_run_started_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _SCHEDULER_STATE["active_run_trigger"] = trigger


def _clear_active_run() -> None:
    with _SCHEDULER_LOCK:
        _SCHEDULER_STATE["active_run_started_at"] = ""
        _SCHEDULER_STATE["active_run_trigger"] = ""


def _running_skip_message(trigger: str, existing_payload: str = "") -> str:
    with _SCHEDULER_LOCK:
        started_at = _SCHEDULER_STATE.get("active_run_started_at") or "未知时间"
        active_trigger = _SCHEDULER_STATE.get("active_run_trigger") or "未知入口"
    message = f"自动任务已有运行中的实例，已跳过本次触发：trigger={trigger}｜active={active_trigger}｜started_at={started_at}"
    if existing_payload:
        message += f"｜lock={existing_payload}"
    return message


def _skip_payload(status: str, message: str, *, trigger: str, state: dict | None = None) -> dict[str, Any]:
    _mark_scheduler_result(status, message)
    append_runtime_log("warning" if status == "skipped" else "info", f"[自动任务] {message}", source="scheduler")
    return {
        "status": status,
        "ran": False,
        "trigger": trigger,
        "message": message,
        "state": state or {},
    }


def run_daily_publish_once(
    settings: Settings,
    *,
    trigger: str = "cli",
    force: bool = False,
    draft_limit: int | None = None,
    publish_limit: int | None = None,
    log_cb: Callable[[str, str], None] | None = None,
    process_lock_acquired: bool = False,
) -> dict[str, Any]:
    trigger = (trigger or "cli").strip() or "cli"
    if not process_lock_acquired:
        process_lock_acquired = _PROCESS_RUN_LOCK.acquire(blocking=False)
    if not process_lock_acquired:
        message = _running_skip_message(trigger)
        if log_cb:
            log_cb("warning", message)
        return _skip_payload("skipped", message, trigger=trigger)

    _set_active_run(trigger)
    state = dict(_SCHEDULER_STATE)

    def progress(level: str, message: str) -> None:
        append_runtime_log(level, f"[自动任务] {message}", source="scheduler")
        if log_cb:
            log_cb(level, message)

    try:
        with _DailyPublishFileLock(trigger) as file_lock:
            if not file_lock.acquired:
                message = _running_skip_message(trigger, file_lock.existing_payload)
                if log_cb:
                    log_cb("warning", message)
                return _skip_payload("skipped", message, trigger=trigger, state=state)

            state = _refresh_state(settings)
            if not force and not state.get("enabled"):
                message = "自动任务配置未启用，已跳过本次触发"
                if log_cb:
                    log_cb("info", message)
                return _skip_payload("disabled", message, trigger=trigger, state=state)

            latest_status, latest_message = _today_latest_history_status()
            if not force and latest_status == "success":
                message = f"今日已有成功执行记录，已跳过本次触发：{latest_message or 'status=success'}"
                if log_cb:
                    log_cb("info", message)
                return _skip_payload("skipped", message, trigger=trigger, state=state)

            final_draft_limit = max(1, int(draft_limit or state["draft_limit"]))
            final_publish_limit = max(1, int(publish_limit or state["publish_limit"]))
            append_runtime_log(
                "info",
                f"[自动任务] 触发执行：trigger={trigger}｜force={force}｜schedule={state['time']}｜draft_limit={final_draft_limit}｜publish_limit={final_publish_limit}",
                source="scheduler",
            )
            result = run_daily_auto_publish(
                settings,
                draft_limit=final_draft_limit,
                publish_limit=final_publish_limit,
                progress_cb=progress,
            )
            brief = result.get("brief") or {}
            message = (
                f"自动任务完成：初稿 {result['pipeline']['draft']['generated_count']} 篇｜"
                f"已选 {result['selected_count']} 篇｜"
                f"公众号成功 {result['publish']['wechat_published_count']}｜"
                f"头条成功 {result['publish']['toutiao_published_count']}"
            )
            _mark_scheduler_result("success", message)
            append_runtime_log(
                "success",
                f"[自动任务] {message}｜简报={brief.get('brief_path') or '未生成'}",
                source="scheduler",
            )
            return {
                "status": "success",
                "ran": True,
                "trigger": trigger,
                "force": force,
                "message": message,
                "result": result,
            }
    except Exception as exc:
        run_at_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        brief = create_daily_publish_brief(
            run_at=run_at_text,
            status="error",
            schedule_time=state.get("time") or "07:00",
            draft_limit=int(draft_limit or state.get("draft_limit") or 10),
            publish_limit=int(publish_limit or state.get("publish_limit") or 4),
            retry_count=int(state.get("retry_count") or 2),
            enable_wechat=bool(state.get("enable_wechat", True)),
            enable_toutiao=bool(state.get("enable_toutiao", True)),
            preference_keywords=state.get("preference_keywords") or [],
            pipeline_result={},
            selected_drafts=[],
            publish_result={},
            message="自动任务执行失败",
            error_message=str(exc),
        )
        message = f"自动任务执行失败：{exc}"
        _mark_scheduler_result("error", message)
        record_scheduler_history(
            {
                "run_at": run_at_text,
                "status": "error",
                "message": message,
                "schedule_time": state.get("time") or "07:00",
                "draft_limit": int(draft_limit or state.get("draft_limit") or 10),
                "publish_limit": int(publish_limit or state.get("publish_limit") or 4),
                "retry_count": int(state.get("retry_count") or 2),
                "enable_wechat": bool(state.get("enable_wechat", True)),
                "enable_toutiao": bool(state.get("enable_toutiao", True)),
                "brief_title": brief.get("title"),
                "brief_path": brief.get("brief_path"),
            }
        )
        append_runtime_log("error", f"[自动任务] {message}｜简报={brief.get('brief_path') or '未生成'}", source="scheduler")
        return {
            "status": "error",
            "ran": True,
            "trigger": trigger,
            "force": force,
            "message": message,
            "error": str(exc),
            "brief": brief,
        }
    finally:
        _clear_active_run()
        if process_lock_acquired:
            try:
                _PROCESS_RUN_LOCK.release()
            except RuntimeError:
                pass


def _run_once(settings: Settings, log_cb: Callable[[str, str], None] | None = None, *, trigger: str = "schedule") -> dict[str, Any]:
    return run_daily_publish_once(settings, trigger=trigger, log_cb=log_cb)


def trigger_daily_publish_now(settings: Settings) -> bool:
    lock_acquired = _PROCESS_RUN_LOCK.acquire(blocking=False)
    if not lock_acquired:
        message = _running_skip_message("manual")
        _mark_scheduler_result("skipped", message)
        append_runtime_log("warning", f"[自动任务] {message}", source="scheduler")
        return False
    thread = threading.Thread(
        target=run_daily_publish_once,
        kwargs={
            "settings": settings,
            "trigger": "manual",
            "process_lock_acquired": True,
        },
        daemon=True,
        name="hotrank-daily-publish-once",
    )
    thread.start()
    return True


def _scheduler_loop(settings: Settings) -> None:
    last_run_date = ""
    while not _SCHEDULER_STOP.is_set():
        state = _refresh_state(settings)
        now = datetime.now()
        current_date = now.strftime("%Y-%m-%d")
        current_time = now.strftime("%H:%M")
        should_run_today = state["enabled"] and current_time >= state["time"] and last_run_date != current_date
        if should_run_today:
            latest_status, latest_message = _today_latest_history_status()
            if latest_status == "success":
                last_run_date = current_date
                append_runtime_log(
                    "info",
                    f"[自动任务] 今日已有执行记录，跳过补跑：status={latest_status}｜{latest_message}",
                    source="scheduler",
                )
                _SCHEDULER_STOP.wait(20)
                continue
            last_run_date = current_date
            run_daily_publish_once(settings, trigger="web-scheduler")
        _SCHEDULER_STOP.wait(20)


def ensure_scheduler_running(settings: Settings) -> None:
    global _SCHEDULER_THREAD
    with _SCHEDULER_LOCK:
        if _SCHEDULER_THREAD and _SCHEDULER_THREAD.is_alive():
            return
        _SCHEDULER_STOP.clear()
        _refresh_state(settings)
        _SCHEDULER_THREAD = threading.Thread(
            target=_scheduler_loop,
            args=(settings,),
            daemon=True,
            name="hotrank-daily-publish-scheduler",
        )
        _SCHEDULER_THREAD.start()
    append_runtime_log("info", "[自动任务] 每日调度器已启动", source="scheduler")


def stop_scheduler() -> None:
    _SCHEDULER_STOP.set()
