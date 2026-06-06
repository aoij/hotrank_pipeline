from __future__ import annotations

import re
import threading
from pathlib import Path
from tempfile import gettempdir
from urllib.parse import quote

import markdown
from bs4 import BeautifulSoup
from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .config import get_settings, load_runtime_config, mask_secret, save_runtime_config
from .daily_brief import _BRIEF_ROOT
from .db import (
    delete_drafts_by_ids,
    fetch_draft_by_id,
    fetch_draft_source_images,
    fetch_recent_drafts,
    update_draft_content,
)
from .llm import regenerate_draft_images_file
from .multi_source import merged_multi_source_config, parse_lines, parse_rss_feed_lines, rss_feeds_to_text
from .notifications import send_dingtalk_progress
from .scheduler import ensure_scheduler_running, get_scheduler_state, trigger_daily_publish_now
from .scheduler_history import latest_scheduler_brief_entry, latest_scheduler_history, read_scheduler_history
from .runtime_log import append_runtime_log, clear_runtime_logs, latest_notice, read_runtime_logs
from .services import (
    dashboard_payload,
    run_article_enrichment,
    run_cleanup_old_hotspots,
    run_cluster,
    run_full_pipeline,
    run_generate_drafts,
    run_manual_topic_draft,
    run_review_drafts,
    run_scrape,
)
from .toutiao_publisher import ToutiaoPublishError, login_toutiao, publish_draft_to_toutiao
from .wechat_publisher import ARTICLE_STYLE as PUBLISH_ARTICLE_STYLE
from .wechat_publisher import _wechat_compatible_html, publish_draft_to_wechat


settings = get_settings()
app = FastAPI(title="Hotrank Pipeline")
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[2] / "templates"))
ROOT_DIR = Path(__file__).resolve().parents[2]
TEMP_DRAFT_ROOT = Path(gettempdir()) / "hotrank_pipeline_drafts"
_RUN_LOCK = threading.Lock()
_RUN_STATE = {"running": False, "action": ""}

ensure_scheduler_running(settings)

WECHAT_ARTICLE_STYLE = PUBLISH_ARTICLE_STYLE


def _run_with_log(action_name: str, start_message: str, fn):
    append_runtime_log("info", start_message)
    try:
        return fn()
    except Exception as exc:
        append_runtime_log("error", f"{action_name}失败：{exc}")
        raise


def _notify_progress(title: str, lines: list[str], level: str = "info") -> None:
    ok = send_dingtalk_progress(settings, title=title, lines=lines, level=level)
    if ok:
        append_runtime_log("info", f"钉钉通知已发送：{title}")
    else:
        append_runtime_log("warning", f"钉钉通知未发送：{title}（未启用或未配置 webhook）")


def _summarize_toutiao_publish_error(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, ToutiaoPublishError):
        summary = exc.user_message
        details: list[str] = []
        if exc.diagnostic_path:
            details.append(f"诊断文件：{exc.diagnostic_path}")
        if exc.screenshot_path:
            details.append(f"截图：{exc.screenshot_path}")
        if not details and exc.diagnostics:
            likely_reason = str(exc.diagnostics.get('likely_reason') or '').strip()
            if likely_reason and likely_reason != summary:
                details.append(f"诊断：{likely_reason}")
        return summary, "｜".join(details)
    message = str(exc).strip()
    if "｜" in message:
        summary, detail = message.split("｜", 1)
        return summary.strip(), detail.strip()
    return message or "今日头条发布失败", ""


def _set_run_state(running: bool, action: str = "") -> None:
    with _RUN_LOCK:
        _RUN_STATE["running"] = running
        _RUN_STATE["action"] = action


def _get_run_state() -> dict[str, str | bool]:
    with _RUN_LOCK:
        return dict(_RUN_STATE)


def _start_background_job(action_name: str, worker) -> bool:
    state = _get_run_state()
    if state["running"]:
        append_runtime_log("warning", f"已有任务执行中：{state['action']}，本次请求已忽略。")
        return False

    _set_run_state(True, action_name)

    def runner():
        append_runtime_log("info", f"任务已进入后台：{action_name}")
        try:
            worker()
        except Exception as exc:
            append_runtime_log("error", f"{action_name}后台执行失败：{exc}")
        finally:
            append_runtime_log("info", f"后台任务结束：{action_name}")
            _set_run_state(False, "")

    threading.Thread(target=runner, daemon=True).start()
    return True


def _allowed_local_roots() -> list[Path]:
    roots = [(ROOT_DIR / "data").resolve(), TEMP_DRAFT_ROOT.resolve(), _BRIEF_ROOT.resolve()]
    runtime = load_runtime_config(settings)
    draft_output_dir = (runtime.get("draft_output_dir") or "").strip()
    if draft_output_dir:
        try:
            roots.append(Path(draft_output_dir).resolve())
        except OSError:
            pass
    return roots


def _is_within_allowed_roots(target: Path) -> bool:
    resolved = target.resolve()
    for root in _allowed_local_roots():
        if resolved == root or root in resolved.parents:
            return True
    return False


def _rewrite_asset_sources(rendered_html: str, asset_base_dir: Path | None) -> str:
    if not asset_base_dir:
        return rendered_html

    def replace_src(match: re.Match[str]) -> str:
        before = match.group(1)
        src = (match.group(2) or "").strip()
        after = match.group(3)
        if not src or re.match(r"^(https?:)?//", src) or src.startswith(("data:", "/", "#")):
            return match.group(0)
        try:
            asset_path = (asset_base_dir / src).resolve()
        except OSError:
            return match.group(0)
        if not asset_path.exists() or not asset_path.is_file() or not _is_within_allowed_roots(asset_path):
            return match.group(0)
        return f'{before}/draft-asset?path={quote(str(asset_path))}{after}'

    return re.sub(r'(<img\b[^>]*?\bsrc=")([^"]+)(")', replace_src, rendered_html, flags=re.I)


def _render_markdown_to_wechat_html(content_md: str, asset_base_dir: Path | None = None) -> str:
    rendered = markdown.markdown(
        (content_md or "").replace("\ufeff", ""),
        extensions=["extra", "sane_lists", "tables", "nl2br", "fenced_code"],
        output_format="html5",
    )
    soup = BeautifulSoup(rendered, "html.parser")
    h1 = soup.find("h1")
    if h1:
        h1.decompose()
    return _rewrite_asset_sources(_wechat_compatible_html(soup), asset_base_dir)


def _render_markdown_to_wechat_html_document(content_md: str, asset_base_dir: Path | None = None) -> str:
    article_html = _render_markdown_to_wechat_html(content_md, asset_base_dir=asset_base_dir)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <title>微信公众号文章预览</title>
  <style>
    body {{ margin: 0; padding: 24px; background: #f6f7f9; color: #1f2937; }}
    .wx-article {{ max-width: 720px; margin: 0 auto; padding: 28px; background: #fff; {WECHAT_ARTICLE_STYLE} }}
    .wx-article h1 {{ font-size: 26px; line-height: 1.45; margin: 0 0 18px; color: #111827; }}
    .wx-article h2 {{ font-size: 22px; line-height: 1.55; margin: 30px 0 14px; padding-left: 12px; border-left: 4px solid #2563eb; color: #111827; }}
    .wx-article h3 {{ font-size: 18px; line-height: 1.55; margin: 22px 0 10px; color: #111827; }}
    .wx-article p {{ margin: 14px 0; }}
    .wx-article blockquote {{ margin: 16px 0; padding: 12px 14px; background: #f8fafc; border-left: 4px solid #93c5fd; color: #475569; }}
    .wx-article ul, .wx-article ol {{ padding-left: 22px; margin: 14px 0; }}
    .wx-article img {{ display: block; max-width: 100%; margin: 18px auto; border-radius: 12px; }}
    .wx-article table {{ width: 100%; border-collapse: collapse; margin: 18px 0; font-size: 14px; }}
    .wx-article th, .wx-article td {{ border: 1px solid #dbe3ef; padding: 8px 10px; }}
  </style>
</head>
<body>
  <article class="wx-article" style="{WECHAT_ARTICLE_STYLE}">
{article_html}
  </article>
</body>
</html>"""


def _safe_open_local_markdown(path_value: str) -> tuple[Path, str]:
    if not path_value:
        raise HTTPException(status_code=400, detail="未提供稿件文件路径")

    target = Path(path_value)
    if not target.is_absolute():
        target = (ROOT_DIR / target).resolve()
    else:
        target = target.resolve()

    if not _is_within_allowed_roots(target):
        raise HTTPException(status_code=400, detail="仅允许打开项目 data 目录或稿件归档目录下的文件")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="稿件文件不存在")
    if target.suffix.lower() != ".md":
        raise HTTPException(status_code=400, detail="仅支持打开 Markdown 稿件")

    raw = target.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("gb18030", errors="replace")
    return target, text.replace("\r\n", "\n").replace("\r", "\n").replace("\ufeff", "")


def _safe_resolve_local_path(path_value: str) -> Path:
    if not path_value:
        raise HTTPException(status_code=400, detail="未提供文件路径")
    path = Path(path_value)
    if not path.is_absolute():
        path = (ROOT_DIR / path).resolve()
    else:
        path = path.resolve()
    if not _is_within_allowed_roots(path):
        raise HTTPException(status_code=400, detail="文件路径超出允许范围")
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    return path


def _cleanup_draft_files(deleted_rows: list[dict]) -> dict[str, int]:
    removed_files = 0
    removed_dirs = 0
    skipped = 0
    for row in deleted_rows:
        archive_path = (row.get("archive_path") or "").strip()
        if not archive_path:
            skipped += 1
            continue
        try:
            target = _safe_resolve_local_path(archive_path)
        except HTTPException:
            skipped += 1
            continue
        stem_name = target.stem
        asset_dir = target.parent / "assets" / stem_name
        try:
            target.unlink(missing_ok=True)
            removed_files += 1
        except OSError:
            skipped += 1
        try:
            if asset_dir.exists() and asset_dir.is_dir() and _is_within_allowed_roots(asset_dir):
                import shutil

                shutil.rmtree(asset_dir)
                removed_dirs += 1
        except OSError:
            skipped += 1
    return {"removed_files": removed_files, "removed_dirs": removed_dirs, "skipped": skipped}


@app.get("/", response_class=HTMLResponse)
def index(request: Request, message: str | None = None, cluster_page: int = 1):
    runtime = load_runtime_config(settings)
    content_sources = merged_multi_source_config(runtime)
    payload = dashboard_payload(settings, cluster_page=max(1, cluster_page), cluster_page_size=12)
    logs = read_runtime_logs(limit=80)
    notice = latest_notice(logs)
    recent_drafts = fetch_recent_drafts(settings, limit=8)
    scheduler_state = get_scheduler_state(settings)
    scheduler_history = read_scheduler_history(limit=8)
    latest_brief = latest_scheduler_brief_entry() or latest_scheduler_history()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "message": message,
            "runtime": runtime,
            "masked_api_key": mask_secret(runtime.get("llm", {}).get("api_key", "")),
            "masked_image_api_key": mask_secret(
                runtime.get("images", {}).get("generation", {}).get("api_key", "")
            ),
            "masked_dingtalk_webhook": mask_secret(
                runtime.get("notifications", {}).get("dingtalk", {}).get("webhook", "")
            ),
            "masked_toutiao_username": mask_secret(runtime.get("toutiao", {}).get("username", ""), keep=3),
            "masked_toutiao_password": mask_secret(runtime.get("toutiao", {}).get("password", ""), keep=2),
            "content_sources": content_sources,
            "rss_feeds_text": rss_feeds_to_text(content_sources.get("rss_feeds", [])),
            "runtime_logs": logs,
            "runtime_notice": notice,
            "recent_drafts": recent_drafts,
            "scheduler_state": scheduler_state,
            "scheduler_history": scheduler_history,
            "latest_scheduler_brief": latest_brief,
            **payload,
        },
    )


@app.post("/config")
def update_config(
    llm_base_url: str = Form(...),
    llm_model: str = Form(...),
    llm_api_key: str = Form(""),
    llm_draft_prompt: str = Form(""),
    image_generation_base_url: str = Form(""),
    image_generation_model: str = Form(""),
    image_generation_api_key: str = Form(""),
    image_generation_size: str = Form("1024x1024"),
    image_generation_concurrency: int = Form(4),
    image_generation_timeout_seconds: int = Form(180),
    image_generation_total_timeout_seconds: int = Form(240),
    image_generation_prompt_template: str = Form(""),
    wechat_gateway_base_url: str = Form(""),
    wechat_gateway_token: str = Form(""),
    wechat_gateway_max_images: int = Form(4),
    toutiao_channel: str = Form("chrome"),
    toutiao_browser_profile_dir: str = Form(""),
    toutiao_headless: str = Form(""),
    toutiao_login_wait_seconds: int = Form(180),
    toutiao_publish_timeout_seconds: int = Form(240),
    toutiao_title_char_limit: int = Form(100),
    toutiao_max_inline_images: int = Form(6),
    toutiao_verify_list_limit: int = Form(20),
    toutiao_auto_open_login_on_publish: str = Form(""),
    toutiao_username: str = Form(""),
    toutiao_password: str = Form(""),
    toutiao_auto_password_login: str = Form(""),
    toutiao_publish_ad_enabled: str = Form(""),
    toutiao_claim_exclusive: str = Form(""),
    toutiao_publish_more_income: str = Form(""),
    toutiao_collection_name: str = Form(""),
    toutiao_statement_labels: str = Form(""),
    toutiao_disable_auto_rights_protection: str = Form(""),
    dingtalk_enabled: str = Form(""),
    dingtalk_webhook: str = Form(""),
    dingtalk_secret: str = Form(""),
    dingtalk_timeout_seconds: int = Form(10),
    auto_daily_publish_enabled: str = Form(""),
    auto_daily_publish_time: str = Form("07:00"),
    auto_daily_publish_draft_limit: int = Form(10),
    auto_daily_publish_publish_limit: int = Form(4),
    auto_daily_publish_retry_count: int = Form(2),
    auto_daily_publish_enable_wechat: str = Form(""),
    auto_daily_publish_enable_toutiao: str = Form(""),
    auto_daily_publish_notify_on_scrape: str = Form(""),
    auto_daily_publish_notify_on_draft_generated: str = Form(""),
    auto_daily_publish_notify_on_publish_finished: str = Form(""),
    auto_daily_publish_preference_keywords: str = Form(""),
    image_prefer_ai_generated: str = Form(""),
    image_fallback_to_source: str = Form(""),
    image_fallback_to_web_search: str = Form(""),
    content_filter_exclude_newslike: str = Form(""),
    content_sources_enabled: str = Form(""),
    content_sources_include_tophub: str = Form(""),
    dailyhot_base_url: str = Form(""),
    dailyhot_routes: str = Form(""),
    rss_feeds: str = Form(""),
    content_sources_max_items: int = Form(30),
    hotspot_cleanup_enabled: str = Form(""),
    hotspot_cleanup_retention_hours: int = Form(48),
    draft_output_dir: str = Form(...),
    board_whitelist: str = Form(...),
    image_max_per_draft: int = Form(6),
    image_max_per_source: int = Form(4),
):
    runtime = load_runtime_config(settings)
    runtime.setdefault("llm", {})
    runtime["llm"]["base_url"] = llm_base_url.strip()
    runtime["llm"]["model"] = llm_model.strip()
    if llm_api_key.strip():
        runtime["llm"]["api_key"] = llm_api_key.strip()
    runtime["llm"]["draft_prompt"] = llm_draft_prompt.strip()
    runtime["draft_output_dir"] = draft_output_dir.strip()
    runtime["board_whitelist"] = [part.strip() for part in board_whitelist.split(",") if part.strip()]
    runtime.setdefault("images", {})
    runtime["images"]["max_per_draft"] = max(1, image_max_per_draft)
    runtime["images"]["max_per_source"] = max(1, image_max_per_source)
    runtime["images"]["prefer_ai_generated"] = image_prefer_ai_generated == "on"
    runtime["images"]["fallback_to_source"] = image_fallback_to_source == "on"
    runtime["images"]["fallback_to_web_search"] = image_fallback_to_web_search == "on"
    runtime["images"].setdefault("generation", {})
    runtime["images"]["generation"]["base_url"] = image_generation_base_url.strip()
    runtime["images"]["generation"]["model"] = image_generation_model.strip()
    if image_generation_api_key.strip():
        runtime["images"]["generation"]["api_key"] = image_generation_api_key.strip()
    elif "api_key" not in runtime["images"]["generation"]:
        runtime["images"]["generation"]["api_key"] = ""
    runtime["images"]["generation"]["size"] = image_generation_size.strip() or "1024x1024"
    runtime["images"]["generation"]["concurrency"] = max(1, min(image_generation_concurrency, 12))
    runtime["images"]["generation"]["timeout_seconds"] = max(10, min(image_generation_timeout_seconds, 600))
    runtime["images"]["generation"]["total_timeout_seconds"] = max(10, min(image_generation_total_timeout_seconds, 900))
    runtime["images"]["generation"].setdefault("disable_env_proxy", True)
    runtime["images"]["generation"]["prompt_template"] = image_generation_prompt_template.strip()
    runtime.setdefault("notifications", {})
    runtime["notifications"].setdefault("dingtalk", {})
    runtime["notifications"]["dingtalk"].setdefault("enabled", False)
    runtime["notifications"]["dingtalk"].setdefault("webhook", "")
    runtime["notifications"]["dingtalk"].setdefault("secret", "")
    runtime["notifications"]["dingtalk"].setdefault("timeout_seconds", 10)
    runtime.setdefault("content_filter", {})
    runtime["content_filter"]["exclude_newslike"] = content_filter_exclude_newslike == "on"
    runtime["content_sources"] = {
        "enabled": content_sources_enabled == "on",
        "include_tophub": content_sources_include_tophub == "on",
        "dailyhot_base_url": dailyhot_base_url.strip(),
        "dailyhot_routes": parse_lines(dailyhot_routes),
        "rss_feeds": parse_rss_feed_lines(rss_feeds),
        "max_items_per_board": max(1, min(content_sources_max_items, 100)),
    }
    runtime["hotspot_cleanup"] = {
        "enabled": hotspot_cleanup_enabled == "on",
        "retention_hours": max(1, min(int(hotspot_cleanup_retention_hours), 24 * 30)),
    }
    runtime.setdefault("wechat_gateway", {})
    runtime["wechat_gateway"]["base_url"] = wechat_gateway_base_url.strip() or "http://106.12.11.147:18080"
    if wechat_gateway_token.strip():
        runtime["wechat_gateway"]["token"] = wechat_gateway_token.strip()
    elif "token" not in runtime["wechat_gateway"]:
        runtime["wechat_gateway"]["token"] = ""
    runtime["wechat_gateway"]["max_images"] = max(1, min(wechat_gateway_max_images, 8))
    runtime.setdefault("toutiao", {})
    runtime["toutiao"]["channel"] = toutiao_channel.strip() or "chrome"
    runtime["toutiao"]["browser_profile_dir"] = (
        toutiao_browser_profile_dir.strip() or str(ROOT_DIR / "data" / "browser_profiles" / "toutiao")
    )
    runtime["toutiao"]["headless"] = toutiao_headless == "on"
    runtime["toutiao"]["login_wait_seconds"] = max(30, min(int(toutiao_login_wait_seconds), 900))
    runtime["toutiao"]["publish_timeout_seconds"] = max(30, min(int(toutiao_publish_timeout_seconds), 900))
    runtime["toutiao"]["title_char_limit"] = max(20, min(int(toutiao_title_char_limit), 200))
    runtime["toutiao"]["max_inline_images"] = max(0, min(int(toutiao_max_inline_images), 12))
    runtime["toutiao"]["verify_list_limit"] = max(5, min(int(toutiao_verify_list_limit), 50))
    runtime["toutiao"]["auto_open_login_on_publish"] = toutiao_auto_open_login_on_publish == "on"
    if toutiao_username.strip():
        runtime["toutiao"]["username"] = toutiao_username.strip()
    elif "username" not in runtime["toutiao"]:
        runtime["toutiao"]["username"] = ""
    if toutiao_password.strip():
        runtime["toutiao"]["password"] = toutiao_password
    elif "password" not in runtime["toutiao"]:
        runtime["toutiao"]["password"] = ""
    runtime["toutiao"]["auto_password_login"] = toutiao_auto_password_login == "on"
    runtime["toutiao"]["publish_options"] = {
        "ad_enabled": toutiao_publish_ad_enabled == "on",
        "claim_exclusive": toutiao_claim_exclusive == "on",
        "publish_more_income": toutiao_publish_more_income == "on",
        "collection_name": (toutiao_collection_name.strip() or "这事和你有关"),
        "statement_labels": [part.strip() for part in re.split(r"[\r\n,，]+", toutiao_statement_labels) if part.strip()]
        or ["个人观点，仅供参考"],
        "disable_auto_rights_protection": toutiao_disable_auto_rights_protection == "on",
    }
    runtime["notifications"]["dingtalk"]["enabled"] = dingtalk_enabled == "on"
    if dingtalk_webhook.strip():
        runtime["notifications"]["dingtalk"]["webhook"] = dingtalk_webhook.strip()
    elif "webhook" not in runtime["notifications"]["dingtalk"]:
        runtime["notifications"]["dingtalk"]["webhook"] = ""
    if dingtalk_secret.strip():
        runtime["notifications"]["dingtalk"]["secret"] = dingtalk_secret.strip()
    elif "secret" not in runtime["notifications"]["dingtalk"]:
        runtime["notifications"]["dingtalk"]["secret"] = ""
    runtime["notifications"]["dingtalk"]["timeout_seconds"] = max(3, min(int(dingtalk_timeout_seconds), 30))
    runtime.setdefault("automation", {})
    runtime["automation"]["daily_publish"] = {
        "enabled": auto_daily_publish_enabled == "on",
        "schedule_time": auto_daily_publish_time.strip() or "07:00",
        "draft_limit": max(1, min(int(auto_daily_publish_draft_limit), 30)),
        "publish_limit": max(1, min(int(auto_daily_publish_publish_limit), 10)),
        "retry_count": max(1, min(int(auto_daily_publish_retry_count), 5)),
        "enable_wechat": auto_daily_publish_enable_wechat == "on",
        "enable_toutiao": auto_daily_publish_enable_toutiao == "on",
        "notify_on_scrape": auto_daily_publish_notify_on_scrape == "on",
        "notify_on_draft_generated": auto_daily_publish_notify_on_draft_generated == "on",
        "notify_on_publish_finished": auto_daily_publish_notify_on_publish_finished == "on",
        "preference_keywords": [part.strip() for part in re.split(r"[\r\n,，]+", auto_daily_publish_preference_keywords) if part.strip()],
    }
    save_runtime_config(settings, runtime)
    append_runtime_log(
        "success",
        (
            f"配置已保存：模型={runtime['llm']['model']}；"
            f"配图={'AI优先' if runtime['images']['prefer_ai_generated'] else '原文取图'}；"
            f"钉钉={'已启用' if runtime['notifications']['dingtalk']['enabled'] else '未启用'}；"
            f"自动任务={'已启用' if runtime['automation']['daily_publish']['enabled'] else '未启用'}"
        ),
    )
    scheduler_state = get_scheduler_state(settings)
    _notify_progress(
        "配置已保存",
        [
            f"模型：{runtime['llm']['model'] or '未配置'}",
            f"配图模式：{'AI优先' if runtime['images']['prefer_ai_generated'] else '原文取图'}",
            f"单篇插图上限：{runtime['images']['max_per_draft']} 张",
            f"今日头条目录：{runtime['toutiao']['browser_profile_dir']}",
            f"头条自动密码登录：{'已开启' if runtime['toutiao']['auto_password_login'] else '未开启'}",
            f"头条合集：{runtime['toutiao']['publish_options']['collection_name']}",
            f"头条声明：{' / '.join(runtime['toutiao']['publish_options']['statement_labels'])}",
            f"钉钉通知：{'已启用' if runtime['notifications']['dingtalk']['enabled'] else '未启用'}",
            f"钉钉超时：{runtime['notifications']['dingtalk']['timeout_seconds']} 秒",
            f"自动任务：{'已开启' if runtime['automation']['daily_publish']['enabled'] else '未开启'}",
            f"自动执行时间：{runtime['automation']['daily_publish']['schedule_time']}",
            f"自动抓取成稿：{runtime['automation']['daily_publish']['draft_limit']} 篇",
            f"自动推送发布：{runtime['automation']['daily_publish']['publish_limit']} 篇",
            f"自动任务重试：{runtime['automation']['daily_publish']['retry_count']} 次",
            f"自动任务渠道：{'公众号' if runtime['automation']['daily_publish']['enable_wechat'] else ''}{' + ' if runtime['automation']['daily_publish']['enable_wechat'] and runtime['automation']['daily_publish']['enable_toutiao'] else ''}{'头条' if runtime['automation']['daily_publish']['enable_toutiao'] else ''}".strip() or '未启用',
            f"偏好关键词：{' / '.join(runtime['automation']['daily_publish']['preference_keywords']) if runtime['automation']['daily_publish']['preference_keywords'] else '未设置'}",
        ],
        level="success",
    )
    append_runtime_log(
        "info",
        (
            f"自动任务状态已刷新：time={scheduler_state['time']}｜"
            f"draft_limit={scheduler_state['draft_limit']}｜publish_limit={scheduler_state['publish_limit']}"
        ),
    )
    return RedirectResponse(url="/?message=配置已保存", status_code=303)


@app.post("/actions/cleanup-hotspots")
def action_cleanup_hotspots(retention_days: int = Form(2), include_drafts: str = Form("")):
    retention_days = max(1, min(int(retention_days), 30))
    retention_hours = retention_days * 24
    cleanup_drafts = include_drafts == "on"
    result = _run_with_log(
        "清理旧热点",
        (
            f"开始执行：清理旧热点，保留最近 {retention_days} 天"
            f"{'，并同步清理旧初稿' if cleanup_drafts else ''}"
        ),
        lambda: run_cleanup_old_hotspots(
            settings,
            retention_hours_override=retention_hours,
            include_drafts=cleanup_drafts,
        ),
    )
    draft_cleanup = _cleanup_draft_files(result.get("deleted_drafts") or []) if cleanup_drafts else {
        "removed_files": 0,
        "removed_dirs": 0,
        "skipped": 0,
    }
    append_runtime_log(
        "success",
        (
            f"旧热点清理完成：保留最近 {retention_days} 天｜"
            f"删除抓取批次 {result['deleted_crawl_runs']} 个｜"
            f"删除无稿件聚类批次 {result['deleted_cluster_runs']} 个｜"
            f"删除旧初稿 {result.get('deleted_draft_count', 0)} 篇｜"
            f"稿件文件 {draft_cleanup['removed_files']} 个｜配图目录 {draft_cleanup['removed_dirs']} 个"
        ),
    )
    message = (
        f"旧热点清理完成：保留最近 {retention_days} 天"
        + (f"，同步删除旧初稿 {result.get('deleted_draft_count', 0)} 篇" if cleanup_drafts else "")
    )
    return RedirectResponse(url=f"/?message={quote(message)}", status_code=303)


@app.post("/actions/auto-daily-publish-run-now")
def action_auto_daily_publish_run_now():
    scheduler_state = get_scheduler_state(settings)
    started = trigger_daily_publish_now(settings)
    if not started:
        message = "自动任务已有运行中，本次手动触发已跳过"
        append_runtime_log(
            "warning",
            (
                f"{message}：schedule={scheduler_state['time']}｜"
                f"draft_limit={scheduler_state['draft_limit']}｜publish_limit={scheduler_state['publish_limit']}"
            ),
            source="scheduler",
        )
        _notify_progress(
            "自动任务未重复启动",
            [
                "已有一轮自动任务正在执行，本次手动触发已跳过。",
                f"计划执行时间：{scheduler_state['time']}",
                f"本次生成配置：{scheduler_state['draft_limit']} 篇",
                f"本次推送配置：{scheduler_state['publish_limit']} 篇",
            ],
            level="warning",
        )
        return RedirectResponse(url=f"/?message={quote(message)}", status_code=303)
    append_runtime_log(
        "info",
        (
            f"已手动触发自动任务：schedule={scheduler_state['time']}｜"
            f"draft_limit={scheduler_state['draft_limit']}｜publish_limit={scheduler_state['publish_limit']}"
        ),
        source="scheduler",
    )
    _notify_progress(
        "已手动触发自动任务",
        [
            f"计划执行时间：{scheduler_state['time']}",
            f"本次生成：{scheduler_state['draft_limit']} 篇",
            f"本次推送：{scheduler_state['publish_limit']} 篇",
        ],
        level="info",
    )
    return RedirectResponse(url="/?message=已手动触发自动任务", status_code=303)


@app.post("/actions/manual-topic")
def action_manual_topic(topic: str = Form(...), max_sources: int = Form(6)):
    clean_topic = (topic or "").strip()
    max_sources = max(2, min(int(max_sources), 10))

    def progress(level: str, message: str):
        append_runtime_log(level, message)

    def worker():
        append_runtime_log("info", f"开始执行：手动话题生成｜{clean_topic}｜max_sources={max_sources}")
        result = run_manual_topic_draft(settings, topic=clean_topic, max_sources=max_sources, progress_cb=progress)
        append_runtime_log(
            "success",
            f"手动话题生成完成：draft_id={result['draft_id']}｜资料 {result['source_count']} 条｜配图 {result['image_count']} 张｜{result['title']}",
        )

    started = _start_background_job("手动话题生成", worker)
    message = f"话题《{clean_topic}》已开始搜索并生成文章，可在上方日志查看进度。" if started else "已有任务执行中，请先查看前方日志进度。"
    return RedirectResponse(url=f"/?message={quote(message)}", status_code=303)


@app.post("/actions/scrape")
def action_scrape():
    result = _run_with_log("抓取热点", "开始执行：抓取热点", lambda: run_scrape(settings))
    append_runtime_log(
        "success",
        f"抓取完成：boards={result['board_count']} items={result['item_count']}",
    )
    return RedirectResponse(
        url=f"/?message=抓取完成：boards={result['board_count']} items={result['item_count']}",
        status_code=303,
    )


@app.post("/actions/cluster")
def action_cluster():
    result = _run_with_log("热点聚类", "开始执行：热点聚类", lambda: run_cluster(settings))
    append_runtime_log("success", f"聚类完成：clusters={result['cluster_count']}")
    return RedirectResponse(
        url=f"/?message=聚类完成：clusters={result['cluster_count']}",
        status_code=303,
    )


@app.post("/actions/enrich")
def action_enrich(limit: int = Form(20)):
    def progress(level: str, message: str):
        append_runtime_log(level, message)

    def worker():
        append_runtime_log("info", f"开始执行：正文补抓，limit={limit}")
        result = run_article_enrichment(settings, limit=limit, progress_cb=progress)
        level = "warning" if result["blocked"] or result["errored"] else "success"
        append_runtime_log(
            level,
            (
                f"补抓完成：processed={result['processed']} fetched={result['fetched']} "
                f"blocked={result['blocked']} errored={result['errored']}"
            ),
        )

    started = _start_background_job("正文补抓", worker)
    message = "正文补抓已开始，正在后台处理，可直接看前方日志面板。" if started else "已有任务执行中，请先查看前方日志进度。"
    return RedirectResponse(
        url=f"/?message={quote(message)}",
        status_code=303,
    )


@app.post("/actions/draft")
def action_draft(limit: int = Form(1)):
    def progress(level: str, message: str):
        append_runtime_log(level, message)

    def worker():
        append_runtime_log("info", f"开始执行：生成初稿，limit={limit}")
        result = run_generate_drafts(settings, limit=limit, progress_cb=progress)
        draft = result["drafts"][0] if result["drafts"] else None
        if draft:
            append_runtime_log(
                "warning" if result.get("failed_count") else "success",
                (
                    f"初稿完成：成功 {result['generated_count']} 篇，失败 {result.get('failed_count', 0)} 篇；"
                    f"重复跳过 {result.get('skipped_existing_count', 0)} 篇；"
                    f"最新：{draft['title']}（配图 {draft['image_count']} 张）"
                ),
            )
        else:
            append_runtime_log(
                "warning",
                (
                    f"初稿生成完成，但本次没有生成新稿件。"
                    f"重复跳过 {result.get('skipped_existing_count', 0)} 篇｜"
                    f"失败 {result.get('failed_count', 0)} 篇"
                ),
            )

    started = _start_background_job("生成初稿", worker)
    message = "生成初稿已开始，正在后台处理，可直接看前方日志面板。" if started else "已有任务执行中，请先查看前方日志进度。"
    return RedirectResponse(
        url=f"/?message={quote(message)}",
        status_code=303,
    )


@app.post("/actions/review-drafts")
def action_review_drafts(limit: int = Form(10)):
    def progress(level: str, message: str):
        append_runtime_log(level, message)

    def worker():
        append_runtime_log("info", f"开始执行：模型审核文章评分，limit={limit}")
        result = run_review_drafts(settings, limit=limit, progress_cb=progress)
        append_runtime_log(
            "warning" if result.get("failed_count") else "success",
            f"文章评分完成：成功 {result['reviewed_count']} 篇，失败 {result.get('failed_count', 0)} 篇",
        )

    started = _start_background_job("模型审核评分", worker)
    message = "模型审核评分已开始，完成后公众号编辑器列表会按文章分优先展示。" if started else "已有任务执行中，请先查看前方日志进度。"
    return RedirectResponse(
        url=f"/?message={quote(message)}",
        status_code=303,
    )


@app.post("/actions/run-all")
def action_run_all(draft_limit: int = Form(1)):
    draft_limit = max(1, min(int(draft_limit), 3))
    def progress(level: str, message: str):
        append_runtime_log(level, message)

    def worker():
        append_runtime_log("info", f"开始执行：一键跑全流程，draft_limit={draft_limit}")
        result = run_full_pipeline(settings, draft_limit=draft_limit, progress_cb=progress)
        generated = result["draft"]["generated_count"]
        failed = result["draft"].get("failed_count", 0)
        append_runtime_log(
            "warning" if failed else "success",
            (
                f"全流程完成：scrape_boards={result['scrape']['board_count']} "
                f"clusters={result['cluster']['cluster_count']} drafts={generated} failed={failed}"
            ),
        )

    started = _start_background_job("一键跑全流程", worker)
    message = "一键执行已开始，正在后台持续处理，可直接看前方日志面板。" if started else "已有任务执行中，请先查看前方日志进度。"
    return RedirectResponse(
        url=f"/?message={quote(message)}",
        status_code=303,
    )


@app.post("/actions/login-toutiao")
def action_login_toutiao():
    def worker():
        append_runtime_log("info", "开始执行：初始化今日头条登录态")
        result = login_toutiao(settings)
        level = "success" if result.get("ok") else "warning"
        append_runtime_log(level, f"今日头条登录态结果：{result.get('message')}")
        _notify_progress(
            "今日头条登录态",
            [
                result.get("message") or "未返回结果",
                f"登录目录：{result.get('browser_profile_dir') or '未返回'}",
            ],
            level="success" if result.get("ok") else "warning",
        )

    started = _start_background_job("初始化今日头条登录态", worker)
    message = "今日头条登录页已开始初始化，请在弹出的浏览器里完成登录。" if started else "已有任务执行中，请先查看前方日志进度。"
    return RedirectResponse(url=f"/?message={quote(message)}", status_code=303)


@app.get("/editor", response_class=HTMLResponse)
def editor_page(
    request: Request,
    draft_id: int | None = None,
    path: str | None = Query(default=None),
):
    draft_list = fetch_recent_drafts(settings, limit=30)
    current_draft = None
    editor_title = "新建公众号稿件"
    content_md = "# 标题\n\n## 导语\n\n在这里开始编辑公众号正文。\n"
    archive_path = ""
    source_type = "blank"
    source_id = None

    if draft_id is not None:
        draft = fetch_draft_by_id(settings, draft_id)
        if not draft:
            raise HTTPException(status_code=404, detail="未找到对应稿件")
        current_draft = draft
        editor_title = draft["title"]
        archive_path = draft.get("archive_path") or ""
        source_type = "db"
        source_id = draft["id"]
        if archive_path:
            try:
                target, file_content = _safe_open_local_markdown(archive_path)
                content_md = file_content
                archive_path = str(target)
            except HTTPException:
                content_md = draft["content_md"] or ""
        else:
            content_md = draft["content_md"] or ""
    elif path:
        target, file_content = _safe_open_local_markdown(path)
        editor_title = target.stem
        content_md = file_content
        archive_path = str(target)
        source_type = "file"

    asset_base_dir = Path(archive_path).parent if archive_path else None
    rendered_html = _render_markdown_to_wechat_html(content_md, asset_base_dir=asset_base_dir)
    return templates.TemplateResponse(
        request,
        "editor.html",
        {
            "drafts": draft_list,
            "editor_title": editor_title,
            "content_md": content_md,
            "rendered_html": rendered_html,
            "archive_path": archive_path,
            "asset_base_path": archive_path,
            "source_type": source_type,
            "source_id": source_id,
            "current_draft": current_draft,
        },
    )


@app.post("/api/render-markdown")
def render_markdown(content: str = Form(...), asset_base_path: str = Form("")):
    asset_base_dir = None
    if asset_base_path.strip():
        try:
            asset_base_dir = _safe_resolve_local_path(asset_base_path.strip()).parent
        except HTTPException:
            asset_base_dir = None
    rendered_html = _render_markdown_to_wechat_html(content, asset_base_dir=asset_base_dir)
    return JSONResponse(
        {
            "html": rendered_html,
            "length": len(content or ""),
        },
        media_type="application/json; charset=utf-8",
    )


@app.post("/api/render-html-document")
def render_html_document(content: str = Form(...), asset_base_path: str = Form("")):
    asset_base_dir = None
    if asset_base_path.strip():
        try:
            asset_base_dir = _safe_resolve_local_path(asset_base_path.strip()).parent
        except HTTPException:
            asset_base_dir = None
    return HTMLResponse(
        _render_markdown_to_wechat_html_document(content, asset_base_dir=asset_base_dir),
        media_type="text/html; charset=utf-8",
    )


@app.post("/editor/delete-drafts")
def delete_editor_drafts(draft_ids: list[int] = Form(default=[]), current_draft_id: int | None = Form(default=None)):
    ids = sorted({int(draft_id) for draft_id in draft_ids if int(draft_id) > 0})
    if not ids:
        return RedirectResponse(url="/editor?delete=none", status_code=303)
    deleted_rows = delete_drafts_by_ids(settings, ids)
    cleanup = _cleanup_draft_files(deleted_rows)
    append_runtime_log(
        "success" if len(deleted_rows) == len(ids) else "warning",
        (
            f"删除稿件完成：请求 {len(ids)} 篇｜删除记录 {len(deleted_rows)} 篇｜"
            f"文件 {cleanup['removed_files']} 个｜配图目录 {cleanup['removed_dirs']} 个｜跳过 {cleanup['skipped']} 项"
        ),
    )
    remaining_current = current_draft_id and int(current_draft_id) not in ids
    if remaining_current:
        return RedirectResponse(url=f"/editor?draft_id={int(current_draft_id)}&delete=success", status_code=303)
    return RedirectResponse(url="/editor?delete=success", status_code=303)


@app.post("/editor/{draft_id}/delete")
def delete_editor_draft(draft_id: int):
    deleted_rows = delete_drafts_by_ids(settings, [draft_id])
    cleanup = _cleanup_draft_files(deleted_rows)
    level = "success" if deleted_rows else "warning"
    append_runtime_log(
        level,
        (
            f"删除稿件：draft_id={draft_id}｜删除记录 {len(deleted_rows)} 篇｜"
            f"文件 {cleanup['removed_files']} 个｜配图目录 {cleanup['removed_dirs']} 个"
        ),
    )
    return RedirectResponse(url="/editor?delete=success" if deleted_rows else "/editor?delete=missing", status_code=303)


@app.post("/editor/{draft_id}/regenerate-images")
def regenerate_editor_images(draft_id: int):
    draft = fetch_draft_by_id(settings, draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="未找到对应稿件")
    archive_path = draft.get("archive_path") or ""
    if not archive_path:
        raise HTTPException(status_code=400, detail="当前稿件没有归档路径，无法重新生成插图")

    target, file_content = _safe_open_local_markdown(archive_path)
    runtime = load_runtime_config(settings)
    image_config = runtime.get("images", {})
    title = draft.get("title") or draft.get("canonical_title") or target.stem
    source_images = fetch_draft_source_images(settings, draft_id)
    append_runtime_log(
        "info",
        (
            f"开始重新生成插图：draft_id={draft_id}｜{title}｜"
            f"AI模型={(image_config.get('generation') or {}).get('model') or '未配置'}｜回退候选图 {len(source_images)} 张"
        ),
    )

    def progress(level: str, message: str):
        append_runtime_log(level, f"重新生成插图：draft_id={draft_id}｜{message}")

    new_path, image_count, image_source, final_content = regenerate_draft_images_file(
        archive_path=str(target),
        title=title,
        content_md=file_content or draft.get("content_md") or "",
        image_config=image_config,
        image_urls=source_images,
        progress_cb=progress,
    )
    update_draft_content(settings, draft_id=draft_id, content_md=final_content, archive_path=new_path)
    level = "success" if image_count else "warning"
    append_runtime_log(
        level,
        f"重新生成插图完成：draft_id={draft_id}｜配图 {image_count} 张｜来源={image_source}",
    )
    return RedirectResponse(
        url=f"/editor?draft_id={draft_id}",
        status_code=303,
    )


@app.post("/editor/{draft_id}/publish-wechat")
def publish_editor_wechat(draft_id: int):
    draft = fetch_draft_by_id(settings, draft_id)
    title = (draft or {}).get("title") or f"draft_id={draft_id}"
    append_runtime_log("info", f"开始推送公众号草稿箱：draft_id={draft_id}｜{title[:60]}")
    try:
        result = publish_draft_to_wechat(settings, draft_id)
    except Exception as exc:
        append_runtime_log("error", f"公众号草稿箱推送失败：draft_id={draft_id}｜{exc}")
        return RedirectResponse(
            url=f"/editor?draft_id={draft_id}&wechat_publish=failed&error={quote(str(exc))}",
            status_code=303,
        )
    if result.get("already_uploaded"):
        message = (
            f"该稿件已上传过微信公众号草稿箱："
            f"{result.get('uploaded_at_text') or '时间未知'}"
            + (f"｜media_id={result.get('media_id')}" if result.get("media_id") else "")
        )
        append_runtime_log("warning", f"公众号草稿箱重复上传已拦截：draft_id={draft_id}｜{message}")
        _notify_progress(
            "公众号草稿箱重复上传已拦截",
            [
                f"稿件：{result.get('wechat_title') or title}",
                f"已上传时间：{result.get('uploaded_at_text') or '未知'}",
                f"media_id：{result.get('media_id') or '未记录'}",
            ],
            level="warning",
        )
        return RedirectResponse(
            url=f"/editor?draft_id={draft_id}&wechat_publish=already&error={quote(message)}",
            status_code=303,
        )
    append_runtime_log(
        "success",
        (
            f"公众号草稿箱推送成功：draft_id={draft_id}｜"
            f"标题={result['wechat_title']}｜插图={result['uploaded_image_count']}｜media_id={result['media_id']}"
        ),
    )
    _notify_progress(
        "公众号草稿箱推送成功",
        [
            f"稿件：{result['wechat_title']}",
            f"插图：{result['uploaded_image_count']} 张",
            f"media_id：{result.get('media_id') or '未返回'}",
        ],
        level="success",
    )
    return RedirectResponse(
        url=f"/editor?draft_id={draft_id}&wechat_publish=success",
        status_code=303,
    )


@app.post("/editor/{draft_id}/publish-toutiao")
def publish_editor_toutiao(draft_id: int):
    draft = fetch_draft_by_id(settings, draft_id)
    title = (draft or {}).get("title") or f"draft_id={draft_id}"
    append_runtime_log("info", f"开始发布到今日头条：draft_id={draft_id}｜{title[:60]}")
    try:
        result = publish_draft_to_toutiao(settings, draft_id)
    except Exception as exc:
        summary, details = _summarize_toutiao_publish_error(exc)
        append_runtime_log("error", f"今日头条发布失败：draft_id={draft_id}｜{summary}")
        _notify_progress(
            "今日头条发布失败",
            [
                f"稿件：{title[:60]}",
                f"原因：{summary}",
                *( [details] if details else [] ),
            ],
            level="error",
        )
        error_text = summary if not details else f"{summary}｜{details}"
        return RedirectResponse(
            url=f"/editor?draft_id={draft_id}&toutiao_publish=failed&error={quote(error_text)}",
            status_code=303,
        )
    if result.get("already_uploaded"):
        message = (
            f"该稿件已上传过今日头条："
            f"{result.get('uploaded_at_text') or '时间未知'}"
            + (f"｜article_id={result.get('article_id')}" if result.get("article_id") else "")
        )
        append_runtime_log("warning", f"今日头条重复上传已拦截：draft_id={draft_id}｜{message}")
        _notify_progress(
            "今日头条重复上传已拦截",
            [
                f"稿件：{result.get('toutiao_title') or title}",
                f"已上传时间：{result.get('uploaded_at_text') or '未知'}",
                f"article_id：{result.get('article_id') or '未记录'}",
            ],
            level="warning",
        )
        return RedirectResponse(
            url=f"/editor?draft_id={draft_id}&toutiao_publish=already&error={quote(message)}",
            status_code=303,
        )
    append_runtime_log(
        "success",
        (
            f"今日头条提交流程成功：draft_id={draft_id}｜"
            f"标题={result['toutiao_title']}｜正文={result['content_mode']}｜插图={result['inline_image_count']}｜"
            f"封面={result.get('cover_mode') or '未知'}｜"
            f"发布方式={result.get('publish_mode') or 'unknown'}｜"
            f"状态={result.get('status_desc') or ('已发布' if result.get('published') else '已提交')}"
        ),
    )
    _notify_progress(
        "今日头条已提交",
        [
            f"稿件：{result['toutiao_title']}",
            f"正文写入方式：{result['content_mode']}",
            f"插图数：{result['inline_image_count']} 张",
            f"封面模式：{result.get('cover_mode') or '未知'}",
            f"合集：{result.get('collection_name') or '未设置'}",
            f"作品声明：{' / '.join(result.get('statement_labels') or []) or '未设置'}",
            f"发布方式：{result.get('publish_mode') or 'unknown'}",
            f"平台状态：{result.get('status_desc') or ('已发布' if result.get('published') else '已提交审核')}",
            f"article_id：{result.get('article_id') or '未记录'}",
        ],
        level="success",
    )
    return RedirectResponse(
        url=f"/editor?draft_id={draft_id}&toutiao_publish=success",
        status_code=303,
    )


@app.get("/draft-asset")
def draft_asset(path: str):
    target = _safe_resolve_local_path(path)
    return FileResponse(target)


@app.get("/api/runtime-logs")
def runtime_logs(limit: int = 80):
    logs = read_runtime_logs(limit=max(10, min(limit, 200)))
    return JSONResponse(
        {
            "logs": logs,
            "notice": latest_notice(logs),
            "run_state": _get_run_state(),
        },
        media_type="application/json; charset=utf-8",
    )


@app.post("/api/runtime-logs/clear")
def clear_runtime_logs_api():
    cleared = clear_runtime_logs()
    return JSONResponse(
        {
            "ok": True,
            "cleared": cleared,
            "notice": None,
            "logs": [],
        },
        media_type="application/json; charset=utf-8",
    )
