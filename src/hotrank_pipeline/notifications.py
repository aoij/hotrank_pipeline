from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime
from urllib.parse import quote_plus, urlparse, urlunparse

import requests

from .config import Settings, load_runtime_config


def _notification_config(settings: Settings) -> dict:
    runtime = load_runtime_config(settings)
    return runtime.get("notifications") or {}


def _signed_webhook(webhook: str, secret: str) -> str:
    clean_webhook = (webhook or "").strip()
    clean_secret = (secret or "").strip()
    if not clean_webhook or not clean_secret:
        return clean_webhook
    timestamp = str(round(datetime.now().timestamp() * 1000))
    string_to_sign = f"{timestamp}\n{clean_secret}"
    digest = hmac.new(
        clean_secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    sign = quote_plus(base64.b64encode(digest))
    parsed = urlparse(clean_webhook)
    query = parsed.query
    separator = "&" if query else ""
    signed_query = f"{query}{separator}timestamp={timestamp}&sign={sign}"
    return urlunparse(parsed._replace(query=signed_query))


def send_dingtalk_progress(settings: Settings, title: str, lines: list[str], *, level: str = "info") -> bool:
    config = _notification_config(settings)
    dingtalk = config.get("dingtalk") or {}
    webhook = (dingtalk.get("webhook") or "").strip()
    secret = (dingtalk.get("secret") or "").strip()
    enabled = bool(dingtalk.get("enabled", False))
    if not enabled or not webhook:
        return False

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    level_key = (level or "info").lower()
    icon, status_text = {
        "success": ("✅", "成功"),
        "warning": ("⚠️", "提醒"),
        "error": ("❌", "失败"),
        "info": ("🟦", "进行中"),
    }.get(level_key, ("🟦", "进行中"))
    body = [
        f"### {icon} {title}",
        "",
        f"> 时间：{now}",
        f"> 状态：{status_text}",
        "---",
    ]
    for idx, line in enumerate(lines, 1):
        clean = (line or "").strip()
        if clean:
            body.append(f"{idx}. {clean}")
    payload = {
        "msgtype": "markdown",
        "markdown": {
            "title": title[:80],
            "text": "\n".join(body),
        },
    }
    timeout_seconds = int(dingtalk.get("timeout_seconds", 10))
    try:
        response = requests.post(
            _signed_webhook(webhook, secret),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=max(3, min(timeout_seconds, 30)),
        )
        response.raise_for_status()
        return True
    except Exception:
        return False
