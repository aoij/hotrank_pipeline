from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
import sys
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
_RUNTIME_LOG_DIR = _PROJECT_ROOT / "data" / "runtime_logs"
_WINDOWS_TASK_NAME = "HotrankPipelineDailyPublish"
_WINDOWS_TASK_RUNNER = _PROJECT_ROOT / "scripts" / "run_daily_auto_publish.ps1"

_SCHEDULER_LOCK = threading.RLock()
_PROCESS_RUN_LOCK = threading.Lock()
_SCHEDULER_THREAD: threading.Thread | None = None
_SCHEDULER_STOP = threading.Event()
_SCHEDULER_STATE = {
    "enabled": False,
    "time": "11:30",
    "times": ["11:30", "18:30", "21:30"],
    "publish_lead_minutes": 30,
    "hotspot_limit": 30,
    "draft_limit": 10,
    "publish_limit": 3,
    "wechat_publish_limit": 3,
    "toutiao_publish_limit": 3,
    "allow_cross_channel_duplicates": False,
    "min_publish_score": 8.2,
    "min_toutiao_score": 8.5,
    "toutiao_topic_whitelist": ["娱乐争议", "家庭教育", "职场消费", "安全风险", "普通人利益"],
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


def _parse_lock_payload(raw_payload: str = "") -> dict[str, Any]:
    clean = (raw_payload or "").strip()
    if not clean:
        return {}
    try:
        payload = json.loads(clean)
        return payload if isinstance(payload, dict) else {"raw": clean}
    except Exception:
        return {"raw": clean}


def _read_lock_payload() -> tuple[dict[str, Any], str]:
    if not _DAILY_RUN_LOCK_PATH.exists():
        return {}, ""
    try:
        raw = _DAILY_RUN_LOCK_PATH.read_text(encoding="utf-8")[:500]
    except Exception:
        return {}, ""
    return _parse_lock_payload(raw), raw


def _pid_from_payload(payload: dict[str, Any]) -> int:
    try:
        return max(0, int(payload.get("pid") or 0))
    except Exception:
        return 0


def _is_process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _is_manual_trigger(trigger: str) -> bool:
    clean = (trigger or "").strip().lower()
    return bool(clean) and clean.startswith("manual")


def _remove_lock_file(reason: str) -> bool:
    if not _DAILY_RUN_LOCK_PATH.exists():
        return False
    try:
        _DAILY_RUN_LOCK_PATH.unlink()
        append_runtime_log("warning", f"[自动任务] 已移除跨进程锁：{reason}", source="scheduler")
        return True
    except FileNotFoundError:
        return False
    except Exception as exc:
        append_runtime_log(
            "warning",
            f"[自动任务] 移除跨进程锁失败：reason={reason}｜{exc}",
            source="scheduler",
        )
        return False


def _clear_dead_lock_if_needed() -> tuple[bool, dict[str, Any], str]:
    payload, raw = _read_lock_payload()
    pid = _pid_from_payload(payload)
    if not raw:
        return False, payload, raw
    if pid > 0 and _is_process_alive(pid):
        return False, payload, raw
    removed = _remove_lock_file(f"检测到锁对应进程已不存在｜pid={pid or 'unknown'}")
    return removed, payload, raw


def _terminate_manual_process(pid: int) -> bool:
    if pid <= 0:
        return False
    if not _is_process_alive(pid):
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except Exception as exc:
        append_runtime_log("warning", f"[自动任务] 结束旧手动任务失败：pid={pid}｜{exc}", source="scheduler")
        return False
    deadline = time.time() + 8
    while time.time() < deadline:
        if not _is_process_alive(pid):
            return True
        time.sleep(0.2)
    return not _is_process_alive(pid)


def _manual_run_log_paths() -> tuple[Path, Path]:
    _RUNTIME_LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return (
        _RUNTIME_LOG_DIR / f"manual_auto_publish_{stamp}.out.log",
        _RUNTIME_LOG_DIR / f"manual_auto_publish_{stamp}.err.log",
    )


def _spawn_manual_run_subprocess(
    trigger: str = "manual-web",
    *,
    schedule_time: str | None = None,
) -> tuple[subprocess.Popen[Any], Path, Path]:
    stdout_path, stderr_path = _manual_run_log_paths()
    env = os.environ.copy()
    pythonpath = env.get("PYTHONPATH", "").strip()
    src_path = str(_PROJECT_ROOT / "src")
    env["PYTHONPATH"] = src_path if not pythonpath else f"{src_path}{os.pathsep}{pythonpath}"
    command = [
        sys.executable,
        "-m",
        "hotrank_pipeline.main",
        "run-daily-auto-publish",
        "--trigger",
        trigger,
        "--force",
    ]
    if schedule_time:
        command.extend(["--schedule-time", _normalize_schedule_time(schedule_time)])
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    stdout_handle = stdout_path.open("w", encoding="utf-8")
    stderr_handle = stderr_path.open("w", encoding="utf-8")
    try:
        process = subprocess.Popen(
            command,
            cwd=str(_PROJECT_ROOT),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            creationflags=creationflags,
        )
    finally:
        stdout_handle.close()
        stderr_handle.close()
    return process, stdout_path, stderr_path


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
    raw = (value or "11:30").strip()
    try:
        hour_text, minute_text = raw.split(":", 1)
        hour = max(0, min(int(hour_text), 23))
        minute = max(0, min(int(minute_text), 59))
    except Exception:
        hour = 11
        minute = 30
    return f"{hour:02d}:{minute:02d}"


def _normalize_schedule_times(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        raw_parts = [str(item).strip() for item in value]
    else:
        raw = str(value or "11:30")
        raw_parts = [part.strip() for part in raw.replace("，", ",").replace("；", ",").replace(";", ",").split(",")]
    times: list[str] = []
    seen: set[str] = set()
    for part in raw_parts:
        if not part:
            continue
        normalized = _normalize_schedule_time(part)
        if normalized in seen:
            continue
        seen.add(normalized)
        times.append(normalized)
    if not times:
        times = ["11:30"]
    return sorted(times)


def _list_windows_task_details(task_name_prefix: str = _WINDOWS_TASK_NAME) -> list[dict[str, str]]:
    if os.name != "nt":
        return []
    escaped_prefix = task_name_prefix.replace("'", "''")
    ps_script = f"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$prefix = '{escaped_prefix}'
$tasks = Get-ScheduledTask | Where-Object {{ $_.TaskName -like "$prefix*" }} | Sort-Object TaskName
$rows = foreach ($task in $tasks) {{
    $info = Get-ScheduledTaskInfo -TaskName $task.TaskName -TaskPath $task.TaskPath
    [PSCustomObject]@{{
        task_name = [string]$task.TaskName
        status = [string]$task.State
        next_run_time = if ($info.NextRunTime -and $info.NextRunTime -gt [datetime]::MinValue) {{ $info.NextRunTime.ToString('yyyy-MM-dd HH:mm:ss') }} else {{ '' }}
        last_run_time = if ($info.LastRunTime -and $info.LastRunTime -gt [datetime]::MinValue) {{ $info.LastRunTime.ToString('yyyy-MM-dd HH:mm:ss') }} else {{ '' }}
        last_result = [string]$info.LastTaskResult
        task_to_run = (($task.Actions | ForEach-Object {{
            $execute = if ($null -ne $_.Execute) {{ [string]$_.Execute }} else {{ '' }}
            $arguments = if ($null -ne $_.Arguments) {{ [string]$_.Arguments }} else {{ '' }}
            ($execute + ' ' + $arguments).Trim()
        }}) -join ' | ')
    }}
}}
if (-not $rows) {{
    '[]'
}} else {{
    $rows | ConvertTo-Json -Depth 4 -Compress
}}
""".strip()
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", ps_script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except Exception:
        return []
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        payload = json.loads(result.stdout.strip())
    except Exception:
        return []
    if isinstance(payload, dict):
        payload = [payload]
    task_details: list[dict[str, str]] = []
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict):
            continue
        task_details.append(
            {
                "task_name": str(item.get("task_name") or "").strip(),
                "status": str(item.get("status") or "").strip(),
                "next_run_time": str(item.get("next_run_time") or "").strip(),
                "last_run_time": str(item.get("last_run_time") or "").strip(),
                "last_result": str(item.get("last_result") or "").strip(),
                "task_to_run": str(item.get("task_to_run") or "").strip(),
            }
        )
    return task_details


def get_windows_task_state(task_name: str = _WINDOWS_TASK_NAME) -> dict[str, Any]:
    task_details = _list_windows_task_details(task_name)
    exists = bool(task_details)
    summary = task_details[0] if task_details else {}
    return {
        "supported": os.name == "nt",
        "task_name": task_name,
        "exists": exists,
        "status": summary.get("status") or "",
        "next_run_time": summary.get("next_run_time") or "",
        "last_run_time": summary.get("last_run_time") or "",
        "last_result": summary.get("last_result") or "",
        "task_to_run": summary.get("task_to_run") or "",
        "runner_path": str(_WINDOWS_TASK_RUNNER),
        "tasks": task_details,
    }


def _today_slot_history_status(schedule_time: str) -> tuple[str, str]:
    slot = _normalize_schedule_time(schedule_time)
    for item in _today_history_items():
        if str(item.get("schedule_time") or "").strip() != slot:
            continue
        return str(item.get("status") or ""), str(item.get("message") or "")
    return "", ""


def _today_successful_schedule_times() -> list[str]:
    slots: list[str] = []
    seen: set[str] = set()
    for item in _today_history_items():
        if str(item.get("status") or "").lower() != "success":
            continue
        slot = _normalize_schedule_time(str(item.get("schedule_time") or "11:30"))
        if slot not in seen:
            seen.add(slot)
            slots.append(slot)
    return sorted(slots)


def _resolve_schedule_slot_for_manual_trigger(settings: Settings) -> str:
    state = _refresh_state(settings)
    schedule_times = state.get("times") or ["11:30"]
    now_hm = datetime.now().strftime("%H:%M")
    due_slots = [slot for slot in schedule_times if slot <= now_hm]
    return due_slots[-1] if due_slots else schedule_times[0]


def _refresh_state(settings: Settings) -> dict:
    config = _daily_publish_config(settings)
    schedule_times = _normalize_schedule_times(config.get("schedule_times") or config.get("schedule_time") or "11:30")
    wechat_publish_limit = max(0, int(config.get("wechat_publish_limit", config.get("publish_limit", 3)) or 0))
    toutiao_publish_limit = max(0, int(config.get("toutiao_publish_limit", config.get("publish_limit", 3)) or 0))
    toutiao_topic_whitelist = [
        str(item).strip()
        for item in (config.get("toutiao_topic_whitelist") or ["娱乐争议", "家庭教育", "职场消费", "安全风险", "普通人利益"])
        if str(item).strip()
    ]
    lock_payload, _lock_raw = _read_lock_payload()
    lock_trigger = str(lock_payload.get("trigger") or "").strip()
    lock_started_at = str(lock_payload.get("created_at") or "").strip()
    lock_pid = _pid_from_payload(lock_payload)
    with _SCHEDULER_LOCK:
        _SCHEDULER_STATE["enabled"] = bool(config.get("enabled", False))
        _SCHEDULER_STATE["times"] = schedule_times
        _SCHEDULER_STATE["time"] = " / ".join(schedule_times)
        _SCHEDULER_STATE["publish_lead_minutes"] = max(0, min(int(config.get("publish_lead_minutes", 30) or 0), 180))
        _SCHEDULER_STATE["hotspot_limit"] = max(1, int(config.get("hotspot_limit") or 30))
        _SCHEDULER_STATE["draft_limit"] = max(1, int(config.get("draft_limit") or 5))
        _SCHEDULER_STATE["publish_limit"] = max(wechat_publish_limit, toutiao_publish_limit, int(config.get("publish_limit") or 0))
        _SCHEDULER_STATE["wechat_publish_limit"] = wechat_publish_limit
        _SCHEDULER_STATE["toutiao_publish_limit"] = toutiao_publish_limit
        _SCHEDULER_STATE["allow_cross_channel_duplicates"] = bool(config.get("allow_cross_channel_duplicates", False))
        _SCHEDULER_STATE["min_publish_score"] = float(config.get("min_publish_score") or 8.2)
        _SCHEDULER_STATE["min_toutiao_score"] = float(config.get("min_toutiao_score") or _SCHEDULER_STATE["min_publish_score"])
        _SCHEDULER_STATE["toutiao_topic_whitelist"] = toutiao_topic_whitelist
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
        if lock_trigger:
            state["active_run_trigger"] = lock_trigger
        if lock_started_at:
            state["active_run_started_at"] = lock_started_at
        if lock_pid > 0:
            state["active_run_pid"] = lock_pid
        state["file_lock_path"] = str(_DAILY_RUN_LOCK_PATH)
        state["today_successful_slots"] = _today_successful_schedule_times()
        state["today_has_success"] = bool(state["today_successful_slots"])
        state["windows_task"] = get_windows_task_state()
        return state


def get_scheduler_state(settings: Settings) -> dict:
    state = _refresh_state(settings)
    with _SCHEDULER_LOCK:
        state["running"] = bool(_SCHEDULER_THREAD and _SCHEDULER_THREAD.is_alive() and not _SCHEDULER_STOP.is_set())
        state["thread_scheduler_enabled"] = False
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
    schedule_time: str | None = None,
    hotspot_limit: int | None = None,
    draft_limit: int | None = None,
    publish_limit: int | None = None,
    publish_lead_minutes: int | None = None,
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

            schedule_times = state.get("times") or [_normalize_schedule_time(str(state.get("time") or "11:30"))]
            current_schedule_time = _normalize_schedule_time(schedule_time or schedule_times[0])

            latest_status, latest_message = _today_slot_history_status(current_schedule_time)
            if not force and latest_status == "success":
                message = f"当前时间槽今日已有成功执行记录，已跳过本次触发：schedule_time={current_schedule_time}｜{latest_message or 'status=success'}"
                if log_cb:
                    log_cb("info", message)
                return _skip_payload("skipped", message, trigger=trigger, state=state)

            final_hotspot_limit = max(1, int(hotspot_limit or state["hotspot_limit"]))
            final_draft_limit = max(1, int(draft_limit or state["draft_limit"]))
            final_publish_limit = max(1, int(publish_limit or state["publish_limit"]))
            final_publish_lead_minutes = max(
                0,
                min(
                    int(publish_lead_minutes if publish_lead_minutes is not None else state.get("publish_lead_minutes", 30) or 0),
                    180,
                ),
            )
            append_runtime_log(
                "info",
                f"[自动任务] 触发执行：trigger={trigger}｜force={force}｜schedule={current_schedule_time}｜lead={final_publish_lead_minutes}｜hotspot_limit={final_hotspot_limit}｜draft_limit={final_draft_limit}｜publish_limit={final_publish_limit}",
                source="scheduler",
            )
            result = run_daily_auto_publish(
                settings,
                schedule_time=current_schedule_time,
                hotspot_limit=final_hotspot_limit,
                draft_limit=final_draft_limit,
                publish_limit=final_publish_limit,
                publish_lead_minutes=final_publish_lead_minutes,
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
            schedule_time=schedule_time or (state.get("times") or [state.get("time") or "11:30"])[0],
            hotspot_limit=int(hotspot_limit or state.get("hotspot_limit") or 30),
            draft_limit=int(draft_limit or state.get("draft_limit") or 5),
            publish_limit=int(publish_limit or state.get("publish_limit") or 3),
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
                "schedule_time": schedule_time or (state.get("times") or [state.get("time") or "11:30"])[0],
                "hotspot_limit": int(hotspot_limit or state.get("hotspot_limit") or 30),
                "draft_limit": int(draft_limit or state.get("draft_limit") or 5),
                "publish_limit": int(publish_limit or state.get("publish_limit") or 3),
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


def trigger_daily_publish_now(settings: Settings) -> dict[str, Any]:
    with _SCHEDULER_LOCK:
        cleared_dead_lock, _dead_payload, _dead_raw = _clear_dead_lock_if_needed()
        payload, raw_payload = _read_lock_payload()
        replaced = False
        replaced_payload: dict[str, Any] = {}
        existing_pid = _pid_from_payload(payload)
        existing_trigger = str(payload.get("trigger") or "").strip()

        if raw_payload:
            if _is_manual_trigger(existing_trigger):
                if not _terminate_manual_process(existing_pid):
                    message = (
                        f"检测到旧手动任务仍在运行，但结束失败：trigger={existing_trigger or 'manual'}｜pid={existing_pid or 'unknown'}"
                    )
                    _mark_scheduler_result("error", message)
                    append_runtime_log("error", f"[自动任务] {message}", source="scheduler")
                    return {
                        "started": False,
                        "replaced": False,
                        "blocked": True,
                        "message": message,
                        "existing_payload": payload,
                    }
                replaced = True
                replaced_payload = dict(payload)
                _remove_lock_file(f"手动覆盖执行，已结束旧手动任务｜trigger={existing_trigger}｜pid={existing_pid}")
            else:
                state = get_scheduler_state(settings)
                message = _running_skip_message("manual", raw_payload)
                _mark_scheduler_result("skipped", message)
                append_runtime_log("warning", f"[自动任务] {message}", source="scheduler")
                return {
                    "started": False,
                    "replaced": False,
                    "blocked": True,
                    "message": message,
                    "state": state,
                    "existing_payload": payload,
                }

        selected_slot = _resolve_schedule_slot_for_manual_trigger(settings)
        process, stdout_path, stderr_path = _spawn_manual_run_subprocess(
            f"manual-web-{selected_slot}",
            schedule_time=selected_slot,
        )
        for _ in range(15):
            if _DAILY_RUN_LOCK_PATH.exists():
                break
            time.sleep(0.1)

    state = get_scheduler_state(settings)
    message = "已手动触发自动任务"
    if replaced:
        message = "已结束上一次手动任务，并重新启动自动任务"
    elif cleared_dead_lock:
        message = "检测到旧任务残留锁，已清理并重新启动自动任务"
    append_runtime_log(
        "info",
        (
            f"[自动任务] {message}：pid={process.pid}｜schedule={state['time']}｜"
            f"hotspot_limit={state['hotspot_limit']}｜draft_limit={state['draft_limit']}｜"
            f"wechat_publish_limit={state.get('wechat_publish_limit', 0)}｜"
            f"toutiao_publish_limit={state.get('toutiao_publish_limit', 0)}"
        ),
        source="scheduler",
    )
    return {
        "started": True,
        "replaced": replaced,
        "blocked": False,
        "message": message,
        "pid": process.pid,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "existing_payload": replaced_payload,
        "state": state,
        "schedule_time": selected_slot,
    }


def _scheduler_loop(settings: Settings) -> None:
    attempted_slots: set[str] = set()
    last_seen_date = ""
    while not _SCHEDULER_STOP.is_set():
        state = _refresh_state(settings)
        now = datetime.now()
        current_date = now.strftime("%Y-%m-%d")
        current_time = now.strftime("%H:%M")
        if current_date != last_seen_date:
            attempted_slots.clear()
            last_seen_date = current_date

        schedule_times = state.get("times") or [_normalize_schedule_time(str(state.get("time") or "11:30"))]
        due_slots: list[str] = []
        if state["enabled"]:
            for slot in schedule_times:
                slot_key = f"{current_date}:{slot}"
                if current_time < slot or slot_key in attempted_slots:
                    continue
                latest_status, _latest_message = _today_slot_history_status(slot)
                if latest_status == "success":
                    attempted_slots.add(slot_key)
                    continue
                due_slots.append(slot)

        if due_slots:
            # 如果服务是在多个时间点之后才启动，只补跑最近的一个时间槽，避免同一时刻连续发布多轮。
            for slot in due_slots:
                attempted_slots.add(f"{current_date}:{slot}")
            selected_slot = due_slots[-1]
            latest_status, latest_message = _today_slot_history_status(selected_slot)
            if latest_status == "success":
                append_runtime_log(
                    "info",
                    f"[自动任务] 当前时间槽已有执行记录，跳过补跑：slot={selected_slot}｜status={latest_status}｜{latest_message}",
                    source="scheduler",
                )
                _SCHEDULER_STOP.wait(20)
                continue
            run_daily_publish_once(settings, trigger=f"web-scheduler-{selected_slot}", schedule_time=selected_slot)
        _SCHEDULER_STOP.wait(20)


def ensure_scheduler_running(settings: Settings) -> None:
    _refresh_state(settings)
    append_runtime_log("info", "[自动任务] 已切换为 Windows 计划任务主驱动；Web 仅负责展示状态和手动触发", source="scheduler")


def stop_scheduler() -> None:
    _SCHEDULER_STOP.set()
