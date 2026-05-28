from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .config import get_settings, load_runtime_config, mask_secret, save_runtime_config
from .runtime_log import append_runtime_log, latest_notice, read_runtime_logs
from .services import (
    dashboard_payload,
    run_article_enrichment,
    run_cluster,
    run_full_pipeline,
    run_generate_drafts,
    run_scrape,
)


settings = get_settings()
app = FastAPI(title="Hotrank Pipeline")
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[2] / "templates"))


def _run_with_log(action_name: str, start_message: str, fn):
    append_runtime_log("info", start_message)
    try:
        return fn()
    except Exception as exc:
        append_runtime_log("error", f"{action_name}失败：{exc}")
        raise


@app.get("/", response_class=HTMLResponse)
def index(request: Request, message: str | None = None, cluster_page: int = 1):
    runtime = load_runtime_config(settings)
    payload = dashboard_payload(settings, cluster_page=max(1, cluster_page), cluster_page_size=12)
    logs = read_runtime_logs(limit=80)
    notice = latest_notice(logs)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "message": message,
            "runtime": runtime,
            "masked_api_key": mask_secret(runtime.get("llm", {}).get("api_key", "")),
            "runtime_logs": logs,
            "runtime_notice": notice,
            **payload,
        },
    )


@app.post("/config")
def update_config(
    llm_base_url: str = Form(...),
    llm_model: str = Form(...),
    llm_api_key: str = Form(""),
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
    runtime["draft_output_dir"] = draft_output_dir.strip()
    runtime["board_whitelist"] = [part.strip() for part in board_whitelist.split(",") if part.strip()]
    runtime.setdefault("images", {})
    runtime["images"]["max_per_draft"] = max(1, image_max_per_draft)
    runtime["images"]["max_per_source"] = max(1, image_max_per_source)
    save_runtime_config(settings, runtime)
    append_runtime_log("success", f"配置已保存：模型={runtime['llm']['model']}，单篇插图上限={runtime['images']['max_per_draft']}")
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
    result = _run_with_log(
        "正文补抓",
        f"开始执行：正文补抓，limit={limit}",
        lambda: run_article_enrichment(settings, limit=limit),
    )
    level = "warning" if result["blocked"] or result["errored"] else "success"
    append_runtime_log(
        level,
        (
            f"补抓完成：processed={result['processed']} fetched={result['fetched']} "
            f"blocked={result['blocked']} errored={result['errored']}"
        ),
    )
    return RedirectResponse(
        url=f"/?message=补抓完成：processed={result['processed']} fetched={result['fetched']}",
        status_code=303,
    )


@app.post("/actions/draft")
def action_draft(limit: int = Form(1)):
    result = _run_with_log(
        "生成初稿",
        f"开始执行：生成初稿，limit={limit}",
        lambda: run_generate_drafts(settings, limit=limit),
    )
    draft = result["drafts"][0] if result["drafts"] else None
    if draft:
        append_runtime_log(
            "success",
            f"初稿完成：{draft['title']}（配图 {draft['image_count']} 张）",
        )
    else:
        append_runtime_log("warning", "初稿生成完成，但本次没有生成新稿件。")
    return RedirectResponse(
        url=f"/?message=初稿完成：generated={result['generated_count']}",
        status_code=303,
    )


@app.post("/actions/run-all")
def action_run_all(draft_limit: int = Form(1)):
    result = _run_with_log(
        "一键跑全流程",
        f"开始执行：一键跑全流程，draft_limit={draft_limit}",
        lambda: run_full_pipeline(settings, draft_limit=draft_limit),
    )
    generated = result["draft"]["generated_count"]
    append_runtime_log(
        "success",
        (
            f"全流程完成：scrape_boards={result['scrape']['board_count']} "
            f"clusters={result['cluster']['cluster_count']} drafts={generated}"
        ),
    )
    return RedirectResponse(
        url=f"/?message=全流程完成：drafts={generated}",
        status_code=303,
    )


@app.get("/api/runtime-logs")
def runtime_logs(limit: int = 80):
    logs = read_runtime_logs(limit=max(10, min(limit, 200)))
    return JSONResponse(
        {
            "logs": logs,
            "notice": latest_notice(logs),
        },
        media_type="application/json; charset=utf-8",
    )
