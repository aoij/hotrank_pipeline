from __future__ import annotations

import re
import threading
from pathlib import Path
from urllib.parse import quote

import markdown
from bs4 import BeautifulSoup
from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .config import get_settings, load_runtime_config, mask_secret, save_runtime_config
from .db import fetch_draft_by_id, fetch_recent_drafts, update_draft_content
from .llm import regenerate_draft_images_file
from .runtime_log import append_runtime_log, latest_notice, read_runtime_logs
from .services import (
    dashboard_payload,
    run_article_enrichment,
    run_cluster,
    run_full_pipeline,
    run_generate_drafts,
    run_review_drafts,
    run_scrape,
)
from .wechat_publisher import publish_draft_to_wechat


settings = get_settings()
app = FastAPI(title="Hotrank Pipeline")
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[2] / "templates"))
ROOT_DIR = Path(__file__).resolve().parents[2]
_RUN_LOCK = threading.Lock()
_RUN_STATE = {"running": False, "action": ""}

WECHAT_ARTICLE_STYLE = (
    "color:#1f2937;"
    "font:16px/1.9 -apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;"
    "word-break:break-word;letter-spacing:.02em;box-sizing:border-box;"
)

WECHAT_STYLE_MAP = {
    "h1": "font-size:26px;line-height:1.45;margin:0 0 18px;color:#111827;font-weight:700;",
    "h2": (
        "font-size:22px;line-height:1.55;margin:30px 0 14px;"
        "padding-left:12px;border-left:4px solid #2563eb;color:#111827;font-weight:700;box-sizing:border-box;"
    ),
    "h3": "font-size:18px;line-height:1.55;margin:22px 0 10px;color:#111827;font-weight:700;",
    "p": "margin:14px 0;line-height:1.9;color:#1f2937;font-size:16px;text-align:justify;",
    "blockquote": (
        "margin:16px 0;padding:12px 14px;background:#f8fafc;"
        "border-left:4px solid #93c5fd;color:#475569;box-sizing:border-box;"
    ),
    "ul": "padding-left:22px;margin:14px 0;",
    "ol": "padding-left:22px;margin:14px 0;",
    "li": "margin:6px 0;line-height:1.8;",
    "strong": "font-weight:700;color:#111827;",
    "em": "font-style:normal;color:#475569;",
    "a": "color:#2563eb;text-decoration:none;border-bottom:1px solid #bfdbfe;",
    "hr": "border:0;border-top:1px solid #e5e7eb;margin:26px 0;",
    "code": "font-family:Menlo,Consolas,monospace;background:#f1f5f9;border-radius:4px;padding:2px 4px;font-size:14px;",
    "pre": "white-space:pre-wrap;word-break:break-word;background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:12px;margin:16px 0;font-size:14px;line-height:1.7;",
    "img": "display:block;width:100%;max-width:100%;height:auto;margin:18px auto;border-radius:12px;box-sizing:border-box;",
    "table": "width:100%;border-collapse:collapse;margin:18px 0;font-size:14px;box-sizing:border-box;",
    "th": "border:1px solid #dbe3ef;padding:8px 10px;",
    "td": "border:1px solid #dbe3ef;padding:8px 10px;",
}


def _run_with_log(action_name: str, start_message: str, fn):
    append_runtime_log("info", start_message)
    try:
        return fn()
    except Exception as exc:
        append_runtime_log("error", f"{action_name}失败：{exc}")
        raise


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
    roots = [(ROOT_DIR / "data").resolve()]
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
    for tag, style in WECHAT_STYLE_MAP.items():
        for element in soup.find_all(tag):
            element["style"] = style
    return _rewrite_asset_sources(soup.decode(), asset_base_dir)


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


@app.get("/", response_class=HTMLResponse)
def index(request: Request, message: str | None = None, cluster_page: int = 1):
    runtime = load_runtime_config(settings)
    payload = dashboard_payload(settings, cluster_page=max(1, cluster_page), cluster_page_size=12)
    logs = read_runtime_logs(limit=80)
    notice = latest_notice(logs)
    recent_drafts = fetch_recent_drafts(settings, limit=8)
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
            "runtime_logs": logs,
            "runtime_notice": notice,
            "recent_drafts": recent_drafts,
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
    image_generation_prompt_template: str = Form(""),
    wechat_gateway_base_url: str = Form(""),
    wechat_gateway_token: str = Form(""),
    wechat_gateway_max_images: int = Form(4),
    image_prefer_ai_generated: str = Form(""),
    image_fallback_to_source: str = Form(""),
    content_filter_exclude_newslike: str = Form(""),
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
    runtime["images"].setdefault("generation", {})
    runtime["images"]["generation"]["base_url"] = image_generation_base_url.strip()
    runtime["images"]["generation"]["model"] = image_generation_model.strip()
    if image_generation_api_key.strip():
        runtime["images"]["generation"]["api_key"] = image_generation_api_key.strip()
    elif "api_key" not in runtime["images"]["generation"]:
        runtime["images"]["generation"]["api_key"] = ""
    runtime["images"]["generation"]["size"] = image_generation_size.strip() or "1024x1024"
    runtime["images"]["generation"]["concurrency"] = max(1, min(image_generation_concurrency, 12))
    runtime["images"]["generation"]["prompt_template"] = image_generation_prompt_template.strip()
    runtime.setdefault("content_filter", {})
    runtime["content_filter"]["exclude_newslike"] = content_filter_exclude_newslike == "on"
    runtime.setdefault("wechat_gateway", {})
    runtime["wechat_gateway"]["base_url"] = wechat_gateway_base_url.strip() or "http://106.12.11.147:18080"
    if wechat_gateway_token.strip():
        runtime["wechat_gateway"]["token"] = wechat_gateway_token.strip()
    elif "token" not in runtime["wechat_gateway"]:
        runtime["wechat_gateway"]["token"] = ""
    runtime["wechat_gateway"]["max_images"] = max(1, min(wechat_gateway_max_images, 8))
    save_runtime_config(settings, runtime)
    append_runtime_log(
        "success",
        (
            f"配置已保存：模型={runtime['llm']['model']}，"
            f"配图={'AI优先' if runtime['images']['prefer_ai_generated'] else '原文取图'}，"
            f"单篇插图上限={runtime['images']['max_per_draft']}"
        ),
    )
    return RedirectResponse(url="/?message=配置已保存", status_code=303)


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
                    f"最新：{draft['title']}（配图 {draft['image_count']} 张）"
                ),
            )
        else:
            append_runtime_log(
                "warning",
                f"初稿生成完成，但本次没有生成新稿件。失败 {result.get('failed_count', 0)} 篇",
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


@app.get("/editor", response_class=HTMLResponse)
def editor_page(
    request: Request,
    draft_id: int | None = None,
    path: str | None = Query(default=None),
):
    draft_list = fetch_recent_drafts(settings, limit=30)
    editor_title = "新建公众号稿件"
    content_md = "# 标题\n\n## 导语\n\n在这里开始编辑公众号正文。\n"
    archive_path = ""
    source_type = "blank"
    source_id = None

    if draft_id is not None:
        draft = fetch_draft_by_id(settings, draft_id)
        if not draft:
            raise HTTPException(status_code=404, detail="未找到对应稿件")
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
    append_runtime_log("info", f"开始重新生成插图：draft_id={draft_id}｜{title}")
    new_path, image_count, image_source, final_content = regenerate_draft_images_file(
        archive_path=str(target),
        title=title,
        content_md=file_content or draft.get("content_md") or "",
        image_config=image_config,
        image_urls=[],
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
    append_runtime_log(
        "success",
        (
            f"公众号草稿箱推送成功：draft_id={draft_id}｜"
            f"标题={result['wechat_title']}｜插图={result['uploaded_image_count']}｜media_id={result['media_id']}"
        ),
    )
    return RedirectResponse(
        url=f"/editor?draft_id={draft_id}&wechat_publish=success",
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
