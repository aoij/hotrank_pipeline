from __future__ import annotations

from typing import Callable

from .clustering import build_clusters
from .config import Settings, load_runtime_config
from .db import (
    cleanup_old_hotspots,
    count_recent_clusters,
    delete_old_drafts,
    find_existing_draft_for_topic,
    fetch_cluster_sources_for_generation,
    fetch_latest_whitelisted_items,
    fetch_recent_clusters,
    fetch_stats,
    fetch_unreviewed_drafts,
    fetch_unfetched_cluster_items,
    init_db,
    persist_article_fetch_result,
    persist_cluster_run,
    persist_manual_topic_bundle,
    persist_scrape_result,
    persist_draft_record,
    update_draft_review,
)
from .content_filters import is_blocked_source_image_url, is_newslike_text
from .fetchers import fetch_article
from .llm import (
    archive_draft,
    generate_wechat_draft,
    polish_wechat_draft_after_self_review,
    review_wechat_draft,
)
from .multi_source import scrape_configured_sources
from .notifications import send_dingtalk_progress
from .search_sources import search_topic_sources
from .tophub import scrape_tophub_news


ProgressCallback = Callable[[str, str], None]


def _emit(progress_cb: ProgressCallback | None, level: str, message: str) -> None:
    if progress_cb:
        progress_cb(level, message)


def _notify(settings: Settings, title: str, lines: list[str], level: str = "info") -> None:
    send_dingtalk_progress(settings, title=title, lines=lines, level=level)


def _existing_draft_payload(draft: dict) -> dict:
    return {
        "draft_id": draft["id"],
        "cluster_id": draft["cluster_id"],
        "title": draft.get("title"),
        "archive_path": draft.get("archive_path"),
        "image_count": None,
        "image_candidate_count": None,
        "image_source": "existing",
        "review_score": float(draft["review_score"]) if draft.get("review_score") is not None else None,
        "review_summary": draft.get("review_summary") or "",
        "created_at_text": draft.get("created_at_text") or "",
        "skipped_existing": True,
    }


def _is_newslike_text(title: str, summary: str = "", content: str = "") -> bool:
    return is_newslike_text(title=title, summary=summary, content=content)


def _is_newslike_item(item: dict) -> bool:
    return is_newslike_text(
        title=item.get("title") or item.get("member_title") or "",
        summary=item.get("summary") or "",
        content=item.get("content_text") or "",
        source_url=item.get("source_url") or item.get("final_url") or "",
        source_host=item.get("source_host") or "",
        board_name=item.get("board_name") or "",
    )


def _filter_newslike_scrape_items(result) -> int:
    """在入库前过滤明显新闻、通稿、官方发布类热点条目。"""

    skipped = 0
    for board in result.boards:
        kept_items = []
        for item in board.items:
            if is_newslike_text(
                title=item.title,
                summary=item.raw_text,
                source_url=item.source_url,
                board_name=board.board_name,
            ):
                skipped += 1
                continue
            kept_items.append(item)
        board.items = kept_items
    return skipped


def _filter_newslike_clusters(clusters: list[dict], progress_cb: ProgressCallback | None = None) -> tuple[list[dict], int]:
    filtered: list[dict] = []
    skipped = 0
    for cluster in clusters:
        title = cluster.get("canonical_title") or ""
        summary = cluster.get("cluster_summary") or ""
        sources = cluster.get("sources") or []
        source_text = " ".join(
            (
                f"{source.get('board_name') or ''} "
                f"{source.get('title') or source.get('member_title') or ''} "
                f"{source.get('summary') or ''} "
                f"{source.get('content_text') or ''} "
                f"{source.get('source_url') or ''} "
                f"{source.get('source_host') or ''}"
            )
            for source in sources[:4]
        )
        if is_newslike_text(title=title, summary=summary, content=source_text) or any(
            _is_newslike_item(source) for source in sources[:6]
        ):
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
            if is_blocked_source_image_url(image_url):
                continue
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



def run_cleanup_old_hotspots(
    settings: Settings,
    progress_cb: ProgressCallback | None = None,
    retention_hours_override: int | None = None,
    include_drafts: bool = False,
) -> dict:
    runtime_config = load_runtime_config(settings)
    cleanup_config = runtime_config.get("hotspot_cleanup") or {}
    enabled = bool(cleanup_config.get("enabled", True))
    retention_hours = int(retention_hours_override or cleanup_config.get("retention_hours", 48))
    if not enabled:
        _emit(progress_cb, "info", "旧热点自动清理未启用，已跳过")
        return {
            "enabled": False,
            "retention_hours": retention_hours,
            "deleted_crawl_runs": 0,
            "deleted_cluster_runs": 0,
            "deleted_draft_count": 0,
            "deleted_drafts": [],
        }
    if include_drafts:
        _emit(progress_cb, "warning", f"开始清理旧热点和旧初稿：保留最近 {retention_hours} 小时数据；超过时间的已生成初稿也会删除")
        deleted_drafts = delete_old_drafts(settings, retention_hours=retention_hours)
        _emit(progress_cb, "info", f"旧初稿数据库清理完成：删除 {len(deleted_drafts)} 篇，准备继续清理无稿件聚类")
    else:
        deleted_drafts = []
        _emit(progress_cb, "info", f"开始清理旧热点：保留最近 {retention_hours} 小时数据；已生成稿件关联的聚类会保留")
    result = cleanup_old_hotspots(settings, retention_hours=retention_hours)
    result["enabled"] = True
    result["deleted_draft_count"] = len(deleted_drafts)
    result["deleted_drafts"] = deleted_drafts
    _emit(
        progress_cb,
        "success",
        (
            f"旧热点清理完成：删除抓取批次 {result['deleted_crawl_runs']} 个｜"
            f"删除无稿件聚类批次 {result['deleted_cluster_runs']} 个｜"
            f"删除旧初稿 {result['deleted_draft_count']} 篇"
        ),
    )
    return result


def run_manual_topic_draft(settings: Settings, topic: str, max_sources: int = 6, progress_cb: ProgressCallback | None = None) -> dict:
    runtime_config = load_runtime_config(settings)
    llm_config = runtime_config.get("llm", {})
    image_config = runtime_config.get("images", {})
    clean_topic = (topic or "").strip()
    if not clean_topic:
        raise RuntimeError("请输入要生成文章的话题")
    if not llm_config.get("api_key"):
        raise RuntimeError("local_settings.json 未配置 llm.api_key")

    max_sources = max(2, min(int(max_sources or 6), 10))
    _emit(progress_cb, "info", f"开始按话题全网搜集创作灵感：{clean_topic}｜目标资料 {max_sources} 条")
    sources = search_topic_sources(
        clean_topic,
        max_results=max_sources,
        timeout_seconds=settings.request_timeout_seconds,
        llm_config=llm_config,
        progress_cb=progress_cb,
    )
    if runtime_config.get("content_filter", {}).get("exclude_newslike", True):
        before = len(sources)
        sources = [source for source in sources if not _is_newslike_item(source)]
        skipped = before - len(sources)
        if skipped:
            _emit(progress_cb, "info", f"手动话题已过滤新闻/通稿类资料：{skipped} 条")
    if not sources:
        raise RuntimeError("没有搜索到可用于生成文章的资料，建议换一个更具体的话题")

    existing_draft = find_existing_draft_for_topic(settings, canonical_title=clean_topic)
    if existing_draft:
        _emit(
            progress_cb,
            "warning",
            (
                f"检测到同主题初稿已存在，跳过重复生成：draft_id={existing_draft['id']}｜"
                f"{existing_draft.get('title') or clean_topic}"
            ),
        )
        _notify(
            settings,
            "已跳过：手动话题重复生成",
            [
                f"话题：{clean_topic}",
                f"已存在稿件：draft_id={existing_draft['id']}",
                f"标题：{existing_draft.get('title') or clean_topic}",
            ],
            level="warning",
        )
        payload = _existing_draft_payload(existing_draft)
        payload.update(
            {
                "cluster_id": existing_draft["cluster_id"],
                "source_count": len(sources),
            }
        )
        return payload

    llm_inspiration_summary = next((source.get("llm_inspiration_summary") for source in sources if source.get("llm_inspiration_summary")), "")
    cluster_summary = llm_inspiration_summary or "；".join((source.get("summary") or source.get("title") or "")[:120] for source in sources[:4])
    bundle = persist_manual_topic_bundle(settings, clean_topic, sources, cluster_summary=cluster_summary)
    cluster = {
        "cluster_id": bundle["cluster_id"],
        "canonical_title": clean_topic,
        "cluster_summary": cluster_summary,
        "signal_score": 9999.0,
        "item_count": len(sources),
        "sources": sources,
    }
    source_boards = "、".join(sorted({source.get("board_name") or "全网灵感" for source in sources})[:8])
    _emit(progress_cb, "success", f"创作灵感汇集完成：有效资料 {len(sources)} 条｜来源={source_boards}｜cluster_id={bundle['cluster_id']}")
    _notify(
        settings,
        "已完成：全网灵感汇集",
        [
            f"话题：{clean_topic}",
            f"有效资料：{len(sources)} 条",
            f"来源：{source_boards or '全网灵感'}",
        ],
        level="success",
    )

    image_urls = _collect_draft_images(cluster, image_config)
    _emit(progress_cb, "info", f"开始生成手动话题初稿：{clean_topic}｜回退候选图 {len(image_urls)} 张")
    title, content_md, prompt_excerpt, title_alignment = generate_wechat_draft(
        llm_config=llm_config,
        cluster=cluster,
        article_sources=sources,
    )
    if title_alignment.get("changed"):
        _emit(progress_cb, "warning", f"标题一致性校验触发纠偏：原标题={title_alignment.get('original_title')}｜改后={title_alignment.get('final_title')}｜原因={title_alignment.get('reason')}")
        _notify(
            settings,
            "已触发：标题一致性纠偏",
            [
                f"话题：{clean_topic}",
                f"原标题：{title_alignment.get('original_title') or title}",
                f"改后标题：{title_alignment.get('final_title') or title}",
                f"原因：{title_alignment.get('reason') or '标题与内容不一致'}",
            ],
            level="warning",
        )
    else:
        _emit(progress_cb, "info", f"标题一致性校验通过：{title}")
    _emit(progress_cb, "info", f"手动话题初稿模型返回完成：{title}")
    if llm_config.get("auto_polish_draft", True):
        try:
            _emit(progress_cb, "info", f"开始AI自检并改稿：{title}")
            title, content_md, polish_prompt_excerpt, polish_alignment = polish_wechat_draft_after_self_review(
                llm_config=llm_config,
                title=title,
                content_md=content_md,
                cluster=cluster,
                source_materials=sources,
                max_chars=int(llm_config.get("draft_max_chars", 2600)),
            )
            if polish_alignment.get("changed"):
                _emit(progress_cb, "warning", f"AI自检后标题再次被纠偏：原标题={polish_alignment.get('original_title')}｜改后={polish_alignment.get('final_title')}｜原因={polish_alignment.get('reason')}")
            prompt_excerpt = (
                f"{prompt_excerpt[:800]}\n\n"
                f"--- AI自检改稿 ---\n{polish_prompt_excerpt[:400]}"
            )[:1200]
            _emit(progress_cb, "success", f"AI自检改稿完成：{title}")
        except Exception as exc:
            _emit(progress_cb, "warning", f"AI自检改稿失败，继续使用原初稿：{exc}")
    duplicate_after_generation = find_existing_draft_for_topic(
        settings,
        canonical_title=clean_topic,
        draft_title=title,
    )
    if duplicate_after_generation:
        _emit(
            progress_cb,
            "warning",
            (
                f"模型成稿与已有稿件重复，已跳过归档：draft_id={duplicate_after_generation['id']}｜"
                f"{duplicate_after_generation.get('title') or title}"
            ),
        )
        _notify(
            settings,
            "已跳过：重复初稿拦截",
            [
                f"话题：{clean_topic}",
                f"重复稿件：draft_id={duplicate_after_generation['id']}",
                f"标题：{duplicate_after_generation.get('title') or title}",
            ],
            level="warning",
        )
        payload = _existing_draft_payload(duplicate_after_generation)
        payload.update(
            {
                "cluster_id": duplicate_after_generation["cluster_id"],
                "source_count": len(sources),
            }
        )
        return payload
    archive_path, downloaded_images, image_source, final_content = archive_draft(
        runtime_config["draft_output_dir"],
        title,
        content_md,
        image_urls=image_urls,
        image_config=image_config,
        progress_cb=progress_cb,
    )
    draft_id = persist_draft_record(
        settings=settings,
        cluster_id=bundle["cluster_id"],
        model_name=llm_config["model"],
        model_base_url=llm_config["base_url"],
        title=title,
        content_md=final_content,
        archive_path=archive_path,
        prompt_excerpt=prompt_excerpt,
    )

    review_score = None
    review_summary = ""
    try:
        _emit(progress_cb, "info", f"开始模型审核文章评分：draft_id={draft_id}")
        review_score, review_summary, _ = review_wechat_draft(llm_config=llm_config, title=title, content_md=final_content)
        update_draft_review(settings, draft_id, review_score, review_summary, llm_config["model"])
        _emit(progress_cb, "success", f"模型审核完成：文章分 {review_score:.1f}｜{review_summary[:80]}")
    except Exception as exc:
        _emit(progress_cb, "warning", f"初稿已生成，但评分失败：draft_id={draft_id}｜{exc}")

    payload = {
        "draft_id": draft_id,
        "cluster_id": bundle["cluster_id"],
        "title": title,
        "archive_path": archive_path,
        "source_count": len(sources),
        "image_count": downloaded_images,
        "image_source": image_source,
        "review_score": review_score,
        "review_summary": review_summary,
    }
    _emit(progress_cb, "success", f"手动话题初稿完成：draft_id={draft_id}｜配图 {downloaded_images} 张｜{title}")
    _notify(
        settings,
        "已完成：手动话题初稿",
        [
            f"标题：{title}",
            f"配图：{downloaded_images} 张（来源={image_source}）",
            f"文章分：{review_score:.1f}" if review_score is not None else "文章分：暂未生成",
        ],
        level="success",
    )
    return payload


def run_scrape(settings: Settings, progress_cb: ProgressCallback | None = None) -> dict:
    runtime_config = load_runtime_config(settings)
    source_config = runtime_config.get("content_sources") or {}
    include_tophub = source_config.get("include_tophub", True)
    multi_enabled = bool(source_config.get("enabled", False))
    results = []

    if include_tophub:
        _emit(progress_cb, "info", "开始抓取 TopHub 新闻页")
        results.append(scrape_tophub_news(settings))

    if multi_enabled:
        _emit(progress_cb, "info", "开始抓取扩展内容源：DailyHot / RSS")
        multi_result = scrape_configured_sources(settings, runtime_config)
        if multi_result.boards:
            results.append(multi_result)
            _emit(
                progress_cb,
                "info",
                f"扩展内容源抓取完成：boards={len(multi_result.boards)} items={sum(len(board.items) for board in multi_result.boards)}",
            )
        else:
            _emit(progress_cb, "warning", "扩展内容源未抓到可用数据，请检查 DailyHot 地址或 RSS 配置。")

    if not results:
        _emit(progress_cb, "info", "未启用任何内容源，默认回退抓取 TopHub 新闻页")
        results.append(scrape_tophub_news(settings))

    total_board_count = 0
    total_item_count = 0
    total_skipped_newslike = 0
    run_count = 0
    raw_paths = []
    status_code = 200
    page_urls = []

    skipped_newslike = 0
    filter_newslike = runtime_config.get("content_filter", {}).get("exclude_newslike", True)

    for result in results:
        skipped_newslike = 0
        if filter_newslike:
            skipped_newslike = _filter_newslike_scrape_items(result)
            if skipped_newslike:
                _emit(progress_cb, "info", f"{result.source_name} 入库前已过滤新闻/通稿类热点条目：{skipped_newslike} 条")
        summary = persist_scrape_result(settings, result)
        total_board_count += summary["board_count"]
        total_item_count += summary["item_count"]
        run_count += summary["run_count"]
        total_skipped_newslike += skipped_newslike
        raw_paths.append(result.raw_html_path)
        page_urls.append(result.page_url)
        status_code = result.status_code if result.status_code >= status_code else status_code

    payload = {
        "page_url": " | ".join(page_urls),
        "status_code": status_code,
        "raw_html_path": " | ".join(raw_paths),
        "html_sha256": "",
        "skipped_newslike": total_skipped_newslike,
        "board_count": total_board_count,
        "item_count": total_item_count,
        "run_count": run_count,
    }
    _emit(
        progress_cb,
        "success",
        (
            f"抓取完成：runs={payload['run_count']} boards={payload['board_count']} "
            f"items={payload['item_count']} skipped_newslike={payload['skipped_newslike']}"
        ),
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
        pending_items = [item for item in pending_items if not _is_newslike_item(item)]
        skipped_by_title = before_count - len(pending_items)
        if skipped_by_title:
            _emit(progress_cb, "info", f"已过滤新闻/通稿类待补抓条目：{skipped_by_title} 条")
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
        if filter_newslike and is_newslike_text(
            title=result.title or item["title"],
            summary=result.summary,
            content=result.content_text,
            source_url=result.final_url or result.source_url,
            source_host=result.source_host,
            board_name=result.board_name,
        ):
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
    skipped_existing = []
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
        existing_draft = find_existing_draft_for_topic(
            settings,
            cluster_id=int(cluster["cluster_id"]),
            canonical_title=cluster.get("canonical_title") or "",
        )
        if existing_draft:
            skipped_existing.append(_existing_draft_payload(existing_draft))
            _emit(
                progress_cb,
                "warning",
                (
                    f"[成稿 {idx}/{total}] 检测到同篇初稿已存在，跳过重复生成："
                    f"draft_id={existing_draft['id']}｜{existing_draft.get('title') or cluster['canonical_title']}"
                ),
            )
            continue

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
            title, content_md, prompt_excerpt, title_alignment = generate_wechat_draft(
                llm_config=llm_config,
                cluster=cluster,
                article_sources=cluster["sources"],
            )
            if title_alignment.get("changed"):
                _emit(progress_cb, "warning", f"[成稿 {idx}/{total}] 标题一致性校验触发纠偏：原标题={title_alignment.get('original_title')}｜改后={title_alignment.get('final_title')}｜原因={title_alignment.get('reason')}")
                _notify(
                    settings,
                    "已触发：标题一致性纠偏",
                    [
                        f"热点：{cluster.get('canonical_title') or title}",
                        f"原标题：{title_alignment.get('original_title') or title}",
                        f"改后标题：{title_alignment.get('final_title') or title}",
                        f"原因：{title_alignment.get('reason') or '标题与内容不一致'}",
                    ],
                    level="warning",
                )
            else:
                _emit(progress_cb, "info", f"[成稿 {idx}/{total}] 标题一致性校验通过：{title}")
            _emit(progress_cb, "info", f"[成稿 {idx}/{total}] 模型返回完成：{title}")
            if llm_config.get("auto_polish_draft", True):
                try:
                    _emit(progress_cb, "info", f"[成稿 {idx}/{total}] 开始AI自检并改稿：{title}")
                    title, content_md, polish_prompt_excerpt, polish_alignment = polish_wechat_draft_after_self_review(
                        llm_config=llm_config,
                        title=title,
                        content_md=content_md,
                        cluster=cluster,
                        source_materials=cluster["sources"],
                        max_chars=int(llm_config.get("draft_max_chars", 2600)),
                    )
                    if polish_alignment.get("changed"):
                        _emit(progress_cb, "warning", f"[成稿 {idx}/{total}] AI自检后标题再次被纠偏：原标题={polish_alignment.get('original_title')}｜改后={polish_alignment.get('final_title')}｜原因={polish_alignment.get('reason')}")
                    prompt_excerpt = (
                        f"{prompt_excerpt[:800]}\n\n"
                        f"--- AI自检改稿 ---\n{polish_prompt_excerpt[:400]}"
                    )[:1200]
                    _emit(progress_cb, "success", f"[成稿 {idx}/{total}] AI自检改稿完成：{title}")
                except Exception as polish_exc:
                    _emit(
                        progress_cb,
                        "warning",
                        f"[成稿 {idx}/{total}] AI自检改稿失败，继续使用原初稿：{polish_exc}",
                    )
            duplicate_after_generation = find_existing_draft_for_topic(
                settings,
                cluster_id=int(cluster["cluster_id"]),
                canonical_title=cluster.get("canonical_title") or "",
                draft_title=title,
            )
            if duplicate_after_generation:
                skipped_existing.append(_existing_draft_payload(duplicate_after_generation))
                _emit(
                    progress_cb,
                    "warning",
                    (
                        f"[成稿 {idx}/{total}] 模型成稿与已有稿件重复，跳过归档："
                        f"draft_id={duplicate_after_generation['id']}｜"
                        f"{duplicate_after_generation.get('title') or title}"
                    ),
                )
                _notify(
                    settings,
                    "已跳过：重复初稿拦截",
                    [
                        f"热点：{cluster.get('canonical_title') or title}",
                        f"重复稿件：draft_id={duplicate_after_generation['id']}",
                        f"标题：{duplicate_after_generation.get('title') or title}",
                    ],
                    level="warning",
                )
                continue
            archive_path, downloaded_images, image_source, final_content = archive_draft(
                runtime_config["draft_output_dir"],
                title,
                content_md,
                image_urls=image_urls,
                image_config=image_config,
                progress_cb=progress_cb,
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
                content_md=final_content,
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
                    content_md=final_content,
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
            _notify(
                settings,
                "已完成：热点成稿优化",
                [
                    f"标题：{title}",
                    f"配图：{downloaded_images} 张（来源={image_source}）",
                    f"文章分：{review_score:.1f}" if review_score is not None else "文章分：暂未生成",
                ],
                level="success",
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
        "skipped_existing_count": len(skipped_existing),
        "drafts": generated,
        "skipped_existing": skipped_existing,
        "failed": failed,
    }
    finish_level = "success" if not failed else "warning"
    _emit(
        progress_cb,
        finish_level,
        (
            f"公众号初稿生成完成：generated={len(generated)} "
            f"skipped_existing={len(skipped_existing)} failed={len(failed)}"
        ),
    )
    _notify(
        settings,
        "已完成：公众号初稿批量生成",
        [
            f"成功：{len(generated)} 篇",
            f"重复跳过：{len(skipped_existing)} 篇",
            f"失败：{len(failed)} 篇",
            f"新闻类跳过：{skipped_newslike} 条",
        ],
        level=finish_level,
    )
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
    _notify(
        settings,
        "已完成：模型审核评分",
        [
            f"已评分：{len(reviewed)} 篇",
            f"失败：{len(failed)} 篇",
        ],
        level=level,
    )
    return payload


def run_full_pipeline(settings: Settings, draft_limit: int = 1, progress_cb: ProgressCallback | None = None) -> dict:
    _emit(progress_cb, "info", "阶段 0/5：初始化数据库结构")
    init_db(settings)
    _emit(progress_cb, "success", "阶段 0/5：数据库结构已就绪")

    cleanup_result = run_cleanup_old_hotspots(settings, progress_cb=progress_cb)

    _emit(progress_cb, "info", "阶段 1/5：开始抓取热点")
    scrape_result = run_scrape(settings, progress_cb=progress_cb)
    _emit(progress_cb, "success", f"阶段 1/5：抓取完成，items={scrape_result['item_count']}")

    _emit(progress_cb, "info", "阶段 2/5：开始热点聚类")
    cluster_result = run_cluster(settings, progress_cb=progress_cb)
    _emit(progress_cb, "success", f"阶段 2/5：聚类完成，clusters={cluster_result['cluster_count']}")

    _emit(progress_cb, "info", "阶段 3/5：开始正文补抓")
    enrich_result = run_article_enrichment(settings, limit=30, progress_cb=progress_cb)
    _emit(progress_cb, "success", f"阶段 3/5：正文补抓完成，fetched={enrich_result['fetched']}")

    _emit(progress_cb, "info", f"阶段 4/5：开始生成初稿，draft_limit={draft_limit}")
    draft_result = run_generate_drafts(settings, limit=draft_limit, progress_cb=progress_cb)
    _emit(progress_cb, "success", f"阶段 4/5：初稿生成完成，drafts={draft_result['generated_count']}")

    payload = {
        "cleanup": cleanup_result,
        "scrape": scrape_result,
        "cluster": cluster_result,
        "enrich": enrich_result,
        "draft": draft_result,
    }
    _emit(progress_cb, "success", "一键全流程全部完成")
    _notify(
        settings,
        "已完成：一键全流程",
        [
            f"抓取热点：{scrape_result['item_count']} 条",
            f"聚类：{cluster_result['cluster_count']} 组",
            f"正文补抓成功：{enrich_result['fetched']} 条",
            f"生成初稿：{draft_result['generated_count']} 篇",
        ],
        level="success",
    )
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
