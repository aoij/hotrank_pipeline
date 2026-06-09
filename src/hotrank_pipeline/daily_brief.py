from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


_BRIEF_ROOT = Path(__file__).resolve().parents[2] / "data" / "daily_briefs"


def _parse_run_at(run_at: str | datetime | None) -> datetime:
    if isinstance(run_at, datetime):
        return run_at
    text = str(run_at or "").strip()
    if text:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
    return datetime.now()


def _score_text(value: Any) -> str:
    try:
        if value is None or value == "":
            return "未评分"
        return f"{float(value):.1f} 分"
    except Exception:
        return "未评分"


def _channel_summary(label: str, enabled: bool, payload: dict[str, Any], prefix: str) -> list[str]:
    if not enabled:
        return [f"- {label}：本次未开启"]
    return [
        f"- 成功：{int(payload.get(f'{prefix}_published_count', 0))}",
        f"- 跳过：{int(payload.get(f'{prefix}_skipped_count', 0))}",
        f"- 失败：{int(payload.get(f'{prefix}_failed_count', 0))}",
    ]


def _failure_lines(title: str, items: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    if not items:
        return lines
    lines.append(f"### {title}")
    for item in items[:8]:
        draft_id = item.get("draft_id") or "-"
        draft_title = str(item.get("title") or item.get("canonical_title") or f"draft_id={draft_id}").strip()
        error = str(item.get("error") or "").strip() or "未知异常"
        lines.append(f"- {draft_title}（draft_id={draft_id}）：{error}")
    return lines


def build_daily_publish_brief(
    *,
    run_at: str | datetime | None,
    status: str,
    schedule_time: str,
    hotspot_limit: int,
    draft_limit: int,
    publish_limit: int,
    retry_count: int,
    enable_wechat: bool,
    enable_toutiao: bool,
    preference_keywords: list[str] | None = None,
    pipeline_result: dict[str, Any] | None = None,
    selected_drafts: list[dict[str, Any]] | None = None,
    publish_result: dict[str, Any] | None = None,
    message: str = "",
    error_message: str = "",
) -> dict[str, Any]:
    run_dt = _parse_run_at(run_at)
    run_at_text = run_dt.strftime("%Y-%m-%d %H:%M:%S")
    preference_keywords = [str(item).strip() for item in (preference_keywords or []) if str(item).strip()]
    pipeline_result = pipeline_result or {}
    selected_drafts = selected_drafts or []
    publish_result = publish_result or {}

    scrape_result = pipeline_result.get("scrape") or {}
    cluster_result = pipeline_result.get("cluster") or {}
    enrich_result = pipeline_result.get("enrich") or {}
    draft_result = pipeline_result.get("draft") or {}

    selected_titles = [str(item.get("title") or "").strip() for item in selected_drafts if str(item.get("title") or "").strip()]
    top_scores = []
    for item in selected_drafts:
        score = item.get("review_score")
        if score is None:
            continue
        try:
            top_scores.append(float(score))
        except Exception:
            top_scores.append(str(score))

    if error_message:
        summary = f"今天的自动任务执行失败了，异常：{error_message}"
    elif not selected_drafts:
        summary = "今天热点已经抓完了，但这轮暂时没有筛出适合直接分发的稿子。"
    elif status == "success":
        summary = f"今天这轮自动任务顺利完成，最终挑出 {len(selected_drafts)} 篇稿件并完成分发。"
    else:
        summary = f"今天这轮自动任务已经跑完，挑出了 {len(selected_drafts)} 篇稿件，但发布环节还有异常需要留意。"

    title = f"今日发布简报｜{run_dt.strftime('%Y-%m-%d')}"
    lines: list[str] = [
        f"# {title}",
        "",
        f"- 执行时间：{run_at_text}",
        f"- 调度时间：每天 {schedule_time}",
        f"- 任务状态：{status}",
        f"- 热点候选：{hotspot_limit} 个",
        f"- 目标初稿：{draft_limit} 篇",
        f"- 目标发布：{publish_limit} 篇",
        f"- 发布重试：{retry_count} 次",
        f"- 偏好关键词：{' / '.join(preference_keywords) if preference_keywords else '未设置'}",
        "",
        "## 流程概览",
        f"- 抓取热点：{int(scrape_result.get('item_count', 0))} 条",
        f"- 聚类结果：{int(cluster_result.get('cluster_count', 0))} 组",
        f"- 正文补抓成功：{int(enrich_result.get('fetched', 0))} 条",
        f"- 初稿生成：{int(draft_result.get('generated_count', 0))} 篇",
        f"- 重复跳过：{int(draft_result.get('skipped_existing_count', 0))} 篇",
        f"- 生成失败：{int(draft_result.get('failed_count', 0))} 篇",
        f"- 最终入选：{len(selected_drafts)} 篇",
        "",
        "## 入选稿件",
    ]

    if selected_drafts:
        for idx, item in enumerate(selected_drafts, start=1):
            draft_title = str(item.get("title") or f"draft-{idx}").strip()
            matched_keyword = str(item.get("matched_keyword") or "").strip()
            created_at_text = str(item.get("created_at_text") or "").strip()
            extra_parts = [_score_text(item.get("review_score"))]
            if matched_keyword:
                extra_parts.append(f"命中偏好：{matched_keyword}")
            if created_at_text:
                extra_parts.append(created_at_text)
            lines.append(f"{idx}. {draft_title}（{'｜'.join(extra_parts)}）")
    else:
        lines.append("- 本次没有筛出可直接分发的稿件。")

    lines.extend(
        [
            "",
            "## 发布结果",
            "### 微信公众号",
            *_channel_summary("微信公众号", enable_wechat, publish_result, "wechat"),
            "",
            "### 今日头条",
            *_channel_summary("今日头条", enable_toutiao, publish_result, "toutiao"),
        ]
    )

    failure_blocks = []
    failure_blocks.extend(_failure_lines("公众号失败明细", list(publish_result.get("wechat_failed") or [])))
    failure_blocks.extend(_failure_lines("头条失败明细", list(publish_result.get("toutiao_failed") or [])))
    if error_message:
        failure_blocks.extend(["## 异常说明", f"- {error_message}"])
    elif failure_blocks:
        failure_blocks.insert(0, "## 异常说明")

    if failure_blocks:
        lines.extend(["", *failure_blocks])

    if message:
        lines.extend(["", "## 运行说明", f"- {message}"])

    lines.extend(["", "## 一句话总结", f"- {summary}", ""])

    return {
        "title": title,
        "run_at": run_at_text,
        "status": status,
        "schedule_time": schedule_time,
        "hotspot_limit": hotspot_limit,
        "draft_limit": draft_limit,
        "publish_limit": publish_limit,
        "retry_count": retry_count,
        "draft_generated_count": int(draft_result.get("generated_count", 0)),
        "draft_failed_count": int(draft_result.get("failed_count", 0)),
        "selected_count": len(selected_drafts),
        "selected_titles": selected_titles,
        "top_scores": top_scores,
        "preference_keywords": preference_keywords,
        "summary": summary,
        "message": message,
        "brief_markdown": "\n".join(lines),
        "error_message": error_message,
    }


def write_daily_publish_brief(brief: dict[str, Any]) -> dict[str, Any]:
    run_dt = _parse_run_at(brief.get("run_at"))
    month_dir = _BRIEF_ROOT / run_dt.strftime("%Y-%m")
    month_dir.mkdir(parents=True, exist_ok=True)
    basename = f"{run_dt.strftime('%Y-%m-%d-%H%M%S')}-daily-publish-brief"
    markdown_path = month_dir / f"{basename}.md"
    json_path = month_dir / f"{basename}.json"
    markdown_path.write_text(str(brief.get("brief_markdown") or "").strip() + "\n", encoding="utf-8")
    json_path.write_text(
        json.dumps(
            {k: v for k, v in brief.items() if k != "brief_markdown"},
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    payload = dict(brief)
    payload["brief_path"] = str(markdown_path)
    payload["brief_json_path"] = str(json_path)
    return payload


def create_daily_publish_brief(**kwargs: Any) -> dict[str, Any]:
    return write_daily_publish_brief(build_daily_publish_brief(**kwargs))
