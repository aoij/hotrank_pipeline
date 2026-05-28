from __future__ import annotations

from typing import Callable

from .clustering import build_clusters
from .config import Settings, load_runtime_config
from .db import (
    count_recent_clusters,
    fetch_cluster_sources_for_generation,
    fetch_latest_whitelisted_items,
    fetch_recent_clusters,
    fetch_stats,
    fetch_unreviewed_drafts,
    fetch_unfetched_cluster_items,
    init_db,
    persist_article_fetch_result,
    persist_cluster_run,
    persist_scrape_result,
    persist_draft_record,
    update_draft_review,
)
from .fetchers import fetch_article
from .llm import archive_draft, generate_wechat_draft, review_wechat_draft
from .tophub import scrape_tophub_news


ProgressCallback = Callable[[str, str], None]


NEWSLIKE_KEYWORDS = (
    "通报",
    "公告",
    "发布会",
    "新华社",
    "央视新闻",
    "新闻联播",
    "快讯",
    "突发",
    "据报道",
    "据悉",
    "回应称",
    "官方回应",
    "警方",
    "法院",
    "检方",
    "判决",
    "调查组",
    "监管",
    "处罚",
    "政策",
    "条例",
    "会议",
    "声明",
)

NEWSLIKE_HARD_KEYWORDS = (
    "新华社",
    "央视新闻",
    "新闻联播",
    "发布会",
    "官方通报",
    "警方通报",
    "情况通报",
    "快讯",
)


def _emit(progress_cb: ProgressCallback | None, level: str, message: str) -> None:
    if progress_cb:
        progress_cb(level, message)


def _is_newslike_text(title: str, summary: str = "", content: str = "") -> bool:
    text = f"{title or ''} {summary or ''} {(content or '')[:800]}"
    if any(keyword in text for keyword in NEWSLIKE_HARD_KEYWORDS):
        return True
    score = sum(1 for keyword in NEWSLIKE_KEYWORDS if keyword in text)
    return score >= 2


def _filter_newslike_clusters(clusters: list[dict], progress_cb: ProgressCallback | None = None) -> tuple[list[dict], int]:
    filtered: list[dict] = []
    skipped = 0
    for cluster in clusters:
        title = cluster.get("canonical_title") or ""
        summary = cluster.get("cluster_summary") or ""
        sources = cluster.get("sources") or []
        source_text = " ".join(
            f"{source.get('title') or source.get('member_title') or ''} {source.get('summary') or ''}"
            for source in sources[:4]
        )
        if _is_newslike_text(title, summary, source_text):
            skipped += 1
            _emit(progress_cb, "info", f"跳过新闻类选题：{title[:60]}")
            continue
        filtered.append(cluster)
    return filtered, skipped


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


def run_scrape(settings: Settings, progress_cb: ProgressCallback | None = None) -> dict:
    _emit(progress_cb, "info", "开始抓取 TopHub 新闻页")
    result = scrape_tophub_news(settings)
    summary = persist_scrape_result(settings, result)
    payload = {
        "page_url": result.page_url,
        "status_code": result.status_code,
        "raw_html_path": result.raw_html_path,
        "html_sha256": result.html_sha256,
        **summary,
    }
    _emit(
        progress_cb,
        "success",
        f"抓取完成：boards={payload['board_count']} items={payload['item_count']} status={payload['status_code']}",
    )
    return payload


def run_cluster(settings: Settings, progress_cb: ProgressCallback | None = None) -> dict:
    runtime_config = load_runtime_config(settings)
    whitelist = runtime_config.get("board_whitelist", [])
    _emit(progress_cb, "info", f"开始热点聚类：白名单榜单 {len(whitelist)} 个")
    items = fetch_latest_whitelisted_items(settings, whitelist)
    _emit(progress_cb, "info", f"聚类输入条目：{len(items)} 条")
    clusters = build_clusters(items, runtime_config)
    cluster_run_id = persist_cluster_run(settings, whitelist, clusters)
    payload = {
        "cluster_run_id": cluster_run_id,
        "cluster_count": len(clusters),
        "whitelist_boards": whitelist,
    }
    _emit(progress_cb, "success", f"聚类完成：cluster_run_id={cluster_run_id} clusters={len(clusters)}")
    return payload


def run_article_enrichment(settings: Settings, limit: int = 20, progress_cb: ProgressCallback | None = None) -> dict:
    runtime_config = load_runtime_config(settings)
    content_filter_config = runtime_config.get("content_filter", {})
    filter_newslike = bool(content_filter_config.get("exclude_newslike", True))
    pending_items = fetch_unfetched_cluster_items(settings, limit=limit)
    if filter_newslike:
        before_count = len(pending_items)
        pending_items = [item for item in pending_items if not _is_newslike_text(item.get("title") or "")]
        skipped_by_title = before_count - len(pending_items)
        if skipped_by_title:
            _emit(progress_cb, "info", f"已过滤新闻类待补抓条目：{skipped_by_title} 条")
    processed = 0
    fetched = 0
    blocked = 0
    errored = 0
    skipped = 0
    total = len(pending_items)

    _emit(progress_cb, "info", f"正文补抓待处理：{total} 条")

    for idx, item in enumerate(pending_items, start=1):
        _emit(
            progress_cb,
            "info",
            f"[补抓 {idx}/{total}] {item['board_name']}｜{item['title'][:40]}",
        )
        result = fetch_article(
            board_snapshot_item_id=item["item_id"],
            board_name=item["board_name"],
            source_url=item["source_url"],
            timeout_seconds=settings.request_timeout_seconds,
        )
        persist_article_fetch_result(settings, result)
        processed += 1
        if filter_newslike and _is_newslike_text(result.title or item["title"], result.summary, result.content_text):
            skipped += 1
            _emit(
                progress_cb,
                "info",
                f"[补抓 {idx}/{total}] 已识别为新闻类内容，后续成稿会跳过：{(result.title or item['title'])[:50]}",
            )
        if result.fetch_status == "fetched":
            fetched += 1
            _emit(
                progress_cb,
                "success",
                f"[补抓 {idx}/{total}] 抓取成功：图片 {len(result.image_urls)} 张｜{(result.title or item['title'])[:50]}",
            )
        elif result.fetch_status == "blocked":
            blocked += 1
            _emit(
                progress_cb,
                "warning",
                f"[补抓 {idx}/{total}] 触发拦截：{item['source_url']}｜{result.note or '未获取到正文'}",
            )
        else:
            errored += 1
            _emit(
                progress_cb,
                "error",
                f"[补抓 {idx}/{total}] 抓取失败：{item['source_url']}｜{result.note or '未知错误'}",
            )

    payload = {
        "processed": processed,
        "fetched": fetched,
        "blocked": blocked,
        "errored": errored,
        "skipped_newslike": skipped,
    }
    _emit(
        progress_cb,
        "success" if not blocked and not errored else "warning",
        (
            f"正文补抓完成：processed={processed} fetched={fetched} "
            f"blocked={blocked} errored={errored} skipped_newslike={skipped}"
        ),
    )
    return payload


def run_generate_drafts(settings: Settings, limit: int = 1, progress_cb: ProgressCallback | None = None) -> dict:
    runtime_config = load_runtime_config(settings)
    llm_config = runtime_config.get("llm", {})
    image_config = runtime_config.get("images", {})
    if not llm_config.get("api_key"):
        raise RuntimeError("local_settings.json 未配置 llm.api_key")

    fetch_limit = max(limit * 4, limit)
    clusters = fetch_cluster_sources_for_generation(settings, limit=fetch_limit)
    if runtime_config.get("content_filter", {}).get("exclude_newslike", True):
        clusters, skipped_newslike = _filter_newslike_clusters(clusters, progress_cb=progress_cb)
    else:
        skipped_newslike = 0
    clusters = clusters[:limit]
    generated = []
    failed = []
    total = len(clusters)

    image_generation = image_config.get("generation") or {}
    image_mode = "AI生图优先" if image_config.get("prefer_ai_generated", True) else "原文取图"
    _emit(
        progress_cb,
        "info",
        (
            f"待生成稿件：{total} 篇｜模型={llm_config.get('model', '')}｜"
            f"配图模式={image_mode}｜新闻类跳过={skipped_newslike}"
        ),
    )

    for idx, cluster in enumerate(clusters, start=1):
        image_urls = _collect_draft_images(cluster, image_config)
        _emit(
            progress_cb,
            "info",
            (
                f"[成稿 {idx}/{total}] 开始生成：{cluster['canonical_title']}｜"
                f"AI生图模型={image_generation.get('model') or '未配置'}｜回退候选图 {len(image_urls)} 张"
            ),
        )

        try:
            title, content_md, prompt_excerpt = generate_wechat_draft(
                llm_config=llm_config,
                cluster=cluster,
                article_sources=cluster["sources"],
            )
            _emit(progress_cb, "info", f"[成稿 {idx}/{total}] 模型返回完成：{title}")
            archive_path, downloaded_images, image_source = archive_draft(
                runtime_config["draft_output_dir"],
                title,
                content_md,
                image_urls=image_urls,
                image_config=image_config,
            )
            _emit(
                progress_cb,
                "info",
                f"[成稿 {idx}/{total}] 已归档：{archive_path}｜配图 {downloaded_images} 张｜来源={image_source}",
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
            review_score = None
            review_summary = ""
            try:
                _emit(progress_cb, "info", f"[成稿 {idx}/{total}] 开始模型审核评分：draft_id={draft_id}")
                review_score, review_summary, _ = review_wechat_draft(
                    llm_config=llm_config,
                    title=title,
                    content_md=content_md,
                )
                update_draft_review(
                    settings=settings,
                    draft_id=draft_id,
                    review_score=review_score,
                    review_summary=review_summary,
                    review_model=llm_config["model"],
                )
                _emit(
                    progress_cb,
                    "success",
                    f"[成稿 {idx}/{total}] 模型审核完成：文章分 {review_score:.1f}｜{review_summary[:80]}",
                )
            except Exception as review_exc:
                _emit(
                    progress_cb,
                    "warning",
                    f"[成稿 {idx}/{total}] 初稿已生成，但模型审核评分失败：draft_id={draft_id}｜{review_exc}",
                )
            generated.append(
                {
                    "draft_id": draft_id,
                    "cluster_id": cluster["cluster_id"],
                    "title": title,
                    "archive_path": archive_path,
                    "image_count": downloaded_images,
                    "image_candidate_count": len(image_urls),
                    "image_source": image_source,
                    "review_score": review_score,
                    "review_summary": review_summary,
                }
            )
            _emit(
                progress_cb,
                "success",
                f"[成稿 {idx}/{total}] 生成完成：draft_id={draft_id}｜文章分={review_score if review_score is not None else '未评分'}｜{title}",
            )
        except Exception as exc:
            failed.append(
                {
                    "cluster_id": cluster["cluster_id"],
                    "title": cluster["canonical_title"],
                    "error": str(exc),
                }
            )
            _emit(
                progress_cb,
                "error",
                f"[成稿 {idx}/{total}] 生成失败，已跳过继续下一篇：{cluster['canonical_title'][:50]}｜{exc}",
            )
            continue

    payload = {
        "generated_count": len(generated),
        "failed_count": len(failed),
        "skipped_newslike": skipped_newslike,
        "drafts": generated,
        "failed": failed,
    }
    finish_level = "success" if not failed else "warning"
    _emit(progress_cb, finish_level, f"公众号初稿生成完成：generated={len(generated)} failed={len(failed)}")
    return payload


def run_review_drafts(settings: Settings, limit: int = 10, progress_cb: ProgressCallback | None = None) -> dict:
    runtime_config = load_runtime_config(settings)
    llm_config = runtime_config.get("llm", {})
    if not llm_config.get("api_key"):
        raise RuntimeError("local_settings.json 未配置 llm.api_key")

    drafts = fetch_unreviewed_drafts(settings, limit=max(1, limit))
    total = len(drafts)
    reviewed = []
    failed = []
    _emit(progress_cb, "info", f"待模型审核评分初稿：{total} 篇｜模型={llm_config.get('model', '')}")

    for idx, draft in enumerate(drafts, start=1):
        title = draft.get("title") or draft.get("canonical_title") or f"draft-{draft['id']}"
        _emit(progress_cb, "info", f"[评分 {idx}/{total}] 开始审核：draft_id={draft['id']}｜{title[:60]}")
        try:
            score, summary, _ = review_wechat_draft(
                llm_config=llm_config,
                title=title,
                content_md=draft.get("content_md") or "",
            )
            update_draft_review(
                settings=settings,
                draft_id=draft["id"],
                review_score=score,
                review_summary=summary,
                review_model=llm_config["model"],
            )
            reviewed.append(
                {
                    "draft_id": draft["id"],
                    "title": title,
                    "review_score": score,
                    "review_summary": summary,
                }
            )
            _emit(progress_cb, "success", f"[评分 {idx}/{total}] 完成：文章分 {score:.1f}｜{summary[:90]}")
        except Exception as exc:
            failed.append({"draft_id": draft["id"], "title": title, "error": str(exc)})
            _emit(progress_cb, "error", f"[评分 {idx}/{total}] 失败：draft_id={draft['id']}｜{exc}")

    payload = {
        "reviewed_count": len(reviewed),
        "failed_count": len(failed),
        "reviewed": reviewed,
        "failed": failed,
    }
    level = "success" if not failed else "warning"
    _emit(progress_cb, level, f"模型审核评分完成：reviewed={len(reviewed)} failed={len(failed)}")
    return payload


def run_full_pipeline(settings: Settings, draft_limit: int = 1, progress_cb: ProgressCallback | None = None) -> dict:
    _emit(progress_cb, "info", "阶段 0/4：初始化数据库结构")
    init_db(settings)
    _emit(progress_cb, "success", "阶段 0/4：数据库结构已就绪")

    _emit(progress_cb, "info", "阶段 1/4：开始抓取热点")
    scrape_result = run_scrape(settings, progress_cb=progress_cb)
    _emit(progress_cb, "success", f"阶段 1/4：抓取完成，items={scrape_result['item_count']}")

    _emit(progress_cb, "info", "阶段 2/4：开始热点聚类")
    cluster_result = run_cluster(settings, progress_cb=progress_cb)
    _emit(progress_cb, "success", f"阶段 2/4：聚类完成，clusters={cluster_result['cluster_count']}")

    _emit(progress_cb, "info", "阶段 3/4：开始正文补抓")
    enrich_result = run_article_enrichment(settings, limit=30, progress_cb=progress_cb)
    _emit(progress_cb, "success", f"阶段 3/4：正文补抓完成，fetched={enrich_result['fetched']}")

    _emit(progress_cb, "info", f"阶段 4/4：开始生成初稿，draft_limit={draft_limit}")
    draft_result = run_generate_drafts(settings, limit=draft_limit, progress_cb=progress_cb)
    _emit(progress_cb, "success", f"阶段 4/4：初稿生成完成，drafts={draft_result['generated_count']}")

    payload = {
        "scrape": scrape_result,
        "cluster": cluster_result,
        "enrich": enrich_result,
        "draft": draft_result,
    }
    _emit(progress_cb, "success", "一键全流程全部完成")
    return payload


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
