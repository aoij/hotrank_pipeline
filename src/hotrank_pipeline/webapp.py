from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .config import get_settings, load_runtime_config, mask_secret, save_runtime_config
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


@app.get("/", response_class=HTMLResponse)
def index(request: Request, message: str | None = None):
    runtime = load_runtime_config(settings)
    payload = dashboard_payload(settings)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "message": message,
            "runtime": runtime,
            "masked_api_key": mask_secret(runtime.get("llm", {}).get("api_key", "")),
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
    return RedirectResponse(url="/?message=配置已保存", status_code=303)


@app.post("/actions/scrape")
def action_scrape():
    result = run_scrape(settings)
    return RedirectResponse(
        url=f"/?message=抓取完成：boards={result['board_count']} items={result['item_count']}",
        status_code=303,
    )


@app.post("/actions/cluster")
def action_cluster():
    result = run_cluster(settings)
    return RedirectResponse(
        url=f"/?message=聚类完成：clusters={result['cluster_count']}",
        status_code=303,
    )


@app.post("/actions/enrich")
def action_enrich(limit: int = Form(20)):
    result = run_article_enrichment(settings, limit=limit)
    return RedirectResponse(
        url=f"/?message=补抓完成：processed={result['processed']} fetched={result['fetched']}",
        status_code=303,
    )


@app.post("/actions/draft")
def action_draft(limit: int = Form(1)):
    result = run_generate_drafts(settings, limit=limit)
    return RedirectResponse(
        url=f"/?message=初稿完成：generated={result['generated_count']}",
        status_code=303,
    )


@app.post("/actions/run-all")
def action_run_all(draft_limit: int = Form(1)):
    result = run_full_pipeline(settings, draft_limit=draft_limit)
    generated = result["draft"]["generated_count"]
    return RedirectResponse(
        url=f"/?message=全流程完成：drafts={generated}",
        status_code=303,
    )
