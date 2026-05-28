from __future__ import annotations

from .clustering import build_clusters
from .config import Settings, load_runtime_config
from .db import (
    count_recent_clusters,
    fetch_cluster_sources_for_generation,
    fetch_latest_whitelisted_items,
    fetch_recent_clusters,
    fetch_stats,
    fetch_unfetched_cluster_items,
    init_db,
    persist_article_fetch_result,
    persist_cluster_run,
    persist_scrape_result,
    persist_draft_record,
)
from .fetchers import fetch_article
from .llm import archive_draft, generate_wechat_draft
from .tophub import scrape_tophub_news


def _board_image_priority(board_name: str) -> int:
    return {
        "澎湃": 1,
        "微信": 2,
        "微博": 3,
        "今日头条": 4,
        "百度": 5,
    }.get(board_name, 9)


def _collect_draft_images(cluster: dict, image_config: dict) -> list[str]:
    max_per_draft = int(image_config.get("max_per_draft", 6))
    max_per_source = int(image_config.get("max_per_source", 4))
    ordered_sources = sorted(
        [source for source in cluster["sources"] if source.get("fetch_status") == "fetched"],
        key=lambda source: (
            _board_image_priority(source.get("board_name") or ""),
            -(len(source.get("image_urls") or [])),
            source.get("rank_num") or 9999,
        ),
    )

    source_buckets: list[list[str]] = []
    for source in ordered_sources:
        deduped: list[str] = []
        seen = set()
        for image_url in source.get("image_urls") or []:
            if image_url and image_url not in seen:
                deduped.append(image_url)
                seen.add(image_url)
            if len(deduped) >= max_per_source:
                break
        if deduped:
            source_buckets.append(deduped)

    if not source_buckets:
        return []

    selected: list[str] = []
    selected_seen = set()
    round_index = 0
    while len(selected) < max_per_draft:
        added_this_round = False
        for bucket in source_buckets:
            if round_index >= len(bucket):
                continue
            image_url = bucket[round_index]
            if image_url in selected_seen:
                continue
            selected.append(image_url)
            selected_seen.add(image_url)
            added_this_round = True
            if len(selected) >= max_per_draft:
                break
        if not added_this_round:
            break
        round_index += 1
    return selected


def run_scrape(settings: Settings) -> dict:
    result = scrape_tophub_news(settings)
    summary = persist_scrape_result(settings, result)
    return {
        "page_url": result.page_url,
        "status_code": result.status_code,
        "raw_html_path": result.raw_html_path,
        "html_sha256": result.html_sha256,
        **summary,
    }


def run_cluster(settings: Settings) -> dict:
    runtime_config = load_runtime_config(settings)
    whitelist = runtime_config.get("board_whitelist", [])
    items = fetch_latest_whitelisted_items(settings, whitelist)
    clusters = build_clusters(items, runtime_config)
    cluster_run_id = persist_cluster_run(settings, whitelist, clusters)
    return {
        "cluster_run_id": cluster_run_id,
        "cluster_count": len(clusters),
        "whitelist_boards": whitelist,
    }


def run_article_enrichment(settings: Settings, limit: int = 20) -> dict:
    pending_items = fetch_unfetched_cluster_items(settings, limit=limit)
    processed = 0
    fetched = 0
    blocked = 0
    errored = 0

    for item in pending_items:
        result = fetch_article(
            board_snapshot_item_id=item["item_id"],
            board_name=item["board_name"],
            source_url=item["source_url"],
            timeout_seconds=settings.request_timeout_seconds,
        )
        persist_article_fetch_result(settings, result)
        processed += 1
        if result.fetch_status == "fetched":
            fetched += 1
        elif result.fetch_status == "blocked":
            blocked += 1
        else:
            errored += 1

    return {
        "processed": processed,
        "fetched": fetched,
        "blocked": blocked,
        "errored": errored,
    }


def run_generate_drafts(settings: Settings, limit: int = 1) -> dict:
    runtime_config = load_runtime_config(settings)
    llm_config = runtime_config.get("llm", {})
    image_config = runtime_config.get("images", {})
    if not llm_config.get("api_key"):
        raise RuntimeError("local_settings.json 未配置 llm.api_key")

    clusters = fetch_cluster_sources_for_generation(settings, limit=limit)
    generated = []

    for cluster in clusters:
        image_urls = _collect_draft_images(cluster, image_config)

        title, content_md, prompt_excerpt = generate_wechat_draft(
            llm_config=llm_config,
            cluster=cluster,
            article_sources=cluster["sources"],
        )
        archive_path, downloaded_images = archive_draft(
            runtime_config["draft_output_dir"],
            title,
            content_md,
            image_urls=image_urls,
        )
        draft_id = persist_draft_record(
            settings=settings,
            cluster_id=cluster["cluster_id"],
            model_name=llm_config["model"],
            model_base_url=llm_config["base_url"],
            title=title,
            content_md=content_md,
            archive_path=archive_path,
            prompt_excerpt=prompt_excerpt,
        )
        generated.append(
            {
                "draft_id": draft_id,
                "cluster_id": cluster["cluster_id"],
                "title": title,
                "archive_path": archive_path,
                "image_count": downloaded_images,
                "image_candidate_count": len(image_urls),
            }
        )

    return {
        "generated_count": len(generated),
        "drafts": generated,
    }


def run_full_pipeline(settings: Settings, draft_limit: int = 1) -> dict:
    init_db(settings)
    scrape_result = run_scrape(settings)
    cluster_result = run_cluster(settings)
    enrich_result = run_article_enrichment(settings, limit=30)
    draft_result = run_generate_drafts(settings, limit=draft_limit)
    return {
        "scrape": scrape_result,
        "cluster": cluster_result,
        "enrich": enrich_result,
        "draft": draft_result,
    }


def _group_clusters_by_date(clusters: list[dict]) -> list[dict]:
    groups: list[dict] = []
    current_group: dict | None = None

    for cluster in clusters:
        created_date = cluster.get("created_date") or "未知日期"
        if current_group is None or current_group["created_date"] != created_date:
            current_group = {
                "created_date": created_date,
                "count": 0,
                "clusters": [],
            }
            groups.append(current_group)
        current_group["clusters"].append(cluster)
        current_group["count"] += 1

    return groups


def dashboard_payload(settings: Settings, cluster_page: int = 1, cluster_page_size: int = 12) -> dict:
    page_size = max(1, min(cluster_page_size, 50))
    total = count_recent_clusters(settings)
    total_pages = max(1, (total + page_size - 1) // page_size) if total else 1
    page = max(1, min(cluster_page, total_pages))
    offset = (page - 1) * page_size
    clusters = fetch_recent_clusters(settings, limit=page_size, offset=offset)
    page_numbers = list(range(max(1, page - 2), min(total_pages, page + 2) + 1))
    start_index = offset + 1 if total else 0
    end_index = min(offset + len(clusters), total) if total else 0

    return {
        "stats": fetch_stats(settings),
        "clusters": clusters,
        "cluster_groups": _group_clusters_by_date(clusters),
        "cluster_pagination": {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
            "has_prev": page > 1,
            "has_next": page < total_pages,
            "prev_page": page - 1 if page > 1 else 1,
            "next_page": page + 1 if page < total_pages else total_pages,
            "page_numbers": page_numbers,
            "start_index": start_index,
            "end_index": end_index,
        },
    }
