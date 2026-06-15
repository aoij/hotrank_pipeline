from __future__ import annotations

import json
import math
import re
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any

from .config import Settings


def _data_root(settings: Settings) -> Path:
    return Path(settings.local_settings_path).resolve().parent / "data" / "toutiao_growth"


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _safe_int(value: Any) -> int:
    try:
        if value is None or value == "":
            return 0
        return int(float(value))
    except Exception:
        return 0


def _safe_float(value: Any) -> float:
    try:
        if value is None or value == "":
            return 0.0
        return float(value)
    except Exception:
        return 0.0


def _article_time_text(value: Any) -> str:
    timestamp = _safe_int(value)
    if timestamp <= 0:
        return ""
    try:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(value)


def _compact_title(title: str, limit: int = 28) -> str:
    clean = re.sub(r"\s+", "", (title or "").strip())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip("，。！？、；：") + "…"


def _headline_flags(title: str) -> list[str]:
    clean = re.sub(r"\s+", "", (title or "").strip())
    flags: list[str] = []
    if not clean:
        return ["空标题"]
    if len(clean) < 12:
        flags.append("标题过短")
    if len(clean) > 30:
        flags.append("头条可能截断")
    weak_patterns = (
        r"最该看懂的是具体影响",
        r"这事和你有关",
        r"我突然看懂了",
        r"先别急着",
        r"别急着",
        r"朋友圈到底在吵啥",
        r"和很多人想的可能不太一样",
        r"真正值得看的不是热闹",
        r"重点不只是",
        r"从.+到.+",
    )
    if any(re.search(pattern, clean) for pattern in weak_patterns):
        flags.append("模板/文艺化标题")
    if not re.search(r"[\u4e00-\u9fffA-Za-z0-9]{2,}", clean):
        flags.append("缺少具体名词")
    if not re.search(r"[，？：]|吵|急|怕|坑|账|哭|尴尬|扎心|翻车|后门|细节|不安|麻烦|冲突", clean):
        flags.append("缺少冲突钩子")
    return flags


def _article_row(article: dict[str, Any]) -> dict[str, Any]:
    impressions = _safe_int(article.get("impression_count"))
    reads = _safe_int(article.get("read_count"))
    ctr = round(reads / impressions * 100, 2) if impressions > 0 else 0.0
    title = str(article.get("title") or "").strip()
    return {
        "title": title,
        "article_id": str(article.get("article_id") or ""),
        "article_url": str(article.get("article_url") or ""),
        "status_desc": str(article.get("status_desc") or ""),
        "create_time": article.get("create_time"),
        "create_time_text": _article_time_text(article.get("create_time")),
        "word_count": _safe_int(article.get("word_count")),
        "image_count": _safe_int(article.get("image_count")),
        "cover_count": _safe_int(article.get("cover_count")),
        "impression_count": impressions,
        "read_count": reads,
        "ctr_percent": ctr,
        "digg_count": _safe_int(article.get("digg_count")),
        "comment_count": _safe_int(article.get("comment_count")),
        "collection": str(article.get("collection") or ""),
        "claim_exclusive": str(article.get("claim_exclusive") or ""),
        "large_image_url": str(article.get("large_image_url") or ""),
        "headline_flags": _headline_flags(title),
    }


def _recommendations(rows: list[dict[str, Any]], summary: dict[str, Any]) -> list[str]:
    recommendations: list[str] = []
    weighted_ctr = _safe_float(summary.get("weighted_ctr_percent"))
    no_cover_count = sum(1 for row in rows if _safe_int(row.get("cover_count")) <= 0)
    one_cover_count = sum(1 for row in rows if _safe_int(row.get("cover_count")) == 1)
    weak_title_count = sum(1 for row in rows if row.get("headline_flags"))
    long_article_count = sum(1 for row in rows if _safe_int(row.get("word_count")) > 1200)

    if weighted_ctr < 0.8:
        recommendations.append("下一轮先优化点击率：标题必须一眼看出具体热点和冲突，避免文艺谜语、泛态度和万能后缀。")
    if no_cover_count or one_cover_count:
        recommendations.append(f"封面还不稳：无封面 {no_cover_count} 篇、单封面 {one_cover_count} 篇；头条优先三图封面，首图要有人、动作或明确场景。")
    if weak_title_count:
        recommendations.append(f"检测到 {weak_title_count} 篇标题有模板化/截断风险；发布前继续拦截“具体影响、先别急、我突然看懂了、从A到B”等标题。")
    if long_article_count:
        recommendations.append(f"{long_article_count} 篇超过 1200 字；头条推荐流优先 700-1000 字，一个冲突讲透，不要公众号长铺垫。")
    if not recommendations:
        recommendations.append("本轮基础指标没有明显硬伤，后续重点比较不同题材、标题钩子和封面风格的差异。")
    return recommendations[:5]


def analyze_toutiao_stats(stats_payload: dict[str, Any]) -> dict[str, Any]:
    articles = stats_payload.get("articles") or []
    rows = [_article_row(article) for article in articles if isinstance(article, dict)]
    impressions = sum(row["impression_count"] for row in rows)
    reads = sum(row["read_count"] for row in rows)
    ctr_values = [row["ctr_percent"] for row in rows if row["impression_count"] > 0]
    weighted_ctr = round(reads / impressions * 100, 3) if impressions > 0 else 0.0
    median_ctr = round(float(median(ctr_values)), 3) if ctr_values else 0.0
    avg_words = round(sum(row["word_count"] for row in rows) / len(rows), 1) if rows else 0.0
    avg_images = round(sum(row["image_count"] for row in rows) / len(rows), 1) if rows else 0.0
    avg_covers = round(sum(row["cover_count"] for row in rows) / len(rows), 1) if rows else 0.0

    def performance_key(row: dict[str, Any]) -> tuple[float, float, int]:
        # 低展现高 CTR 不能直接当爆款，给展现量一个温和权重。
        exposure_weight = math.log10(max(row["impression_count"], 1) + 1)
        return (row["ctr_percent"] * exposure_weight, row["ctr_percent"], row["read_count"])

    high_impression_rows = [row for row in rows if row["impression_count"] >= 50]
    top_rows = sorted(high_impression_rows or rows, key=performance_key, reverse=True)[:5]
    weak_rows = sorted(
        [row for row in rows if row["impression_count"] >= 50],
        key=lambda row: (row["ctr_percent"], -row["impression_count"]),
    )[:5]
    summary = {
        "article_count": len(rows),
        "total_impressions": impressions,
        "total_reads": reads,
        "weighted_ctr_percent": weighted_ctr,
        "median_ctr_percent": median_ctr,
        "avg_word_count": avg_words,
        "avg_image_count": avg_images,
        "avg_cover_count": avg_covers,
        "no_cover_count": sum(1 for row in rows if row["cover_count"] <= 0),
        "one_cover_count": sum(1 for row in rows if row["cover_count"] == 1),
        "three_cover_count": sum(1 for row in rows if row["cover_count"] >= 3),
        "weak_title_count": sum(1 for row in rows if row["headline_flags"]),
    }
    summary["recommendations"] = _recommendations(rows, summary)
    return {
        "summary": summary,
        "top_articles": top_rows,
        "weak_articles": weak_rows,
        "articles": rows,
    }


def _format_article_line(row: dict[str, Any]) -> str:
    title = _compact_title(str(row.get("title") or "未命名"))
    flags = row.get("headline_flags") or []
    flag_text = f"｜问题：{'、'.join(flags[:2])}" if flags else ""
    return (
        f"{title}｜展现 {row.get('impression_count', 0)}｜阅读 {row.get('read_count', 0)}｜"
        f"CTR {row.get('ctr_percent', 0)}%｜图 {row.get('image_count', 0)}/封面 {row.get('cover_count', 0)}{flag_text}"
    )


def _append_growth_log(root: Path, record: dict[str, Any]) -> None:
    log_path = root / "growth_log.md"
    analysis = record.get("analysis") or {}
    summary = analysis.get("summary") or {}
    top_articles = analysis.get("top_articles") or []
    weak_articles = analysis.get("weak_articles") or []
    recommendations = summary.get("recommendations") or []
    note = (record.get("note") or "").strip()
    lines = [
        f"## {record.get('snapshot_at')}｜{record.get('source') or 'manual'}",
        "",
    ]
    if note:
        lines.extend([f"- 优化/执行备注：{note}", ""])
    if record.get("ok"):
        lines.extend(
            [
                (
                    f"- 总览：{summary.get('article_count', 0)} 篇｜展现 {summary.get('total_impressions', 0)}｜"
                    f"阅读 {summary.get('total_reads', 0)}｜加权 CTR {summary.get('weighted_ctr_percent', 0)}%｜"
                    f"中位 CTR {summary.get('median_ctr_percent', 0)}%"
                ),
                (
                    f"- 内容形态：平均 {summary.get('avg_word_count', 0)} 字｜平均插图 {summary.get('avg_image_count', 0)} 张｜"
                    f"平均封面 {summary.get('avg_cover_count', 0)} 张｜弱标题 {summary.get('weak_title_count', 0)} 篇"
                ),
                "",
                "表现较好的稿件：",
            ]
        )
        lines.extend([f"- {_format_article_line(row)}" for row in top_articles[:5]] or ["- 暂无"])
        lines.extend(["", "需要复盘的稿件："])
        lines.extend([f"- {_format_article_line(row)}" for row in weak_articles[:5]] or ["- 暂无"])
        lines.extend(["", "下一轮动作："])
        lines.extend([f"- {item}" for item in recommendations[:5]] or ["- 暂无"])
    else:
        lines.extend([f"- 统计拉取失败：{record.get('error') or 'unknown'}"])
    lines.append("")
    lines.append("---")
    lines.append("")
    with log_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines))


def _append_jsonl(path: Path, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(rows)


def _sample_record(row: dict[str, Any], *, recorded_at: str, source: str, sample_type: str) -> dict[str, Any]:
    return {
        "recorded_at": recorded_at,
        "sample_type": sample_type,
        "source": source,
        "title": row.get("title") or "",
        "article_id": row.get("article_id") or "",
        "article_url": row.get("article_url") or "",
        "ctr_percent": row.get("ctr_percent", 0),
        "impression_count": row.get("impression_count", 0),
        "read_count": row.get("read_count", 0),
        "word_count": row.get("word_count", 0),
        "image_count": row.get("image_count", 0),
        "cover_count": row.get("cover_count", 0),
        "headline_flags": row.get("headline_flags") or [],
        "collection": row.get("collection") or "",
    }


def _write_toutiao_samples(root: Path, analysis: dict[str, Any], *, recorded_at: str, source: str) -> dict[str, Any]:
    rows = list(analysis.get("articles") or []) if analysis else []
    positive_rows: list[dict[str, Any]] = []
    negative_rows: list[dict[str, Any]] = []
    for row in rows:
        impressions = _safe_int(row.get("impression_count"))
        ctr = _safe_float(row.get("ctr_percent"))
        if impressions < 50:
            continue
        if ctr > 1.0:
            positive_rows.append(_sample_record(row, recorded_at=recorded_at, source=source, sample_type="positive"))
        elif ctr == 0:
            negative_rows.append(_sample_record(row, recorded_at=recorded_at, source=source, sample_type="negative"))

    positive_path = root / "positive_samples.jsonl"
    negative_path = root / "negative_samples.jsonl"
    positive_count = _append_jsonl(positive_path, positive_rows)
    negative_count = _append_jsonl(negative_path, negative_rows)
    return {
        "positive_samples_path": str(positive_path),
        "negative_samples_path": str(negative_path),
        "positive_sample_count": positive_count,
        "negative_sample_count": negative_count,
    }


def record_toutiao_growth_note(settings: Settings, note: str, *, source: str = "optimization") -> dict[str, Any]:
    root = _data_root(settings)
    root.mkdir(parents=True, exist_ok=True)
    note_record = {
        "noted_at": _now_text(),
        "source": source,
        "note": (note or "").strip(),
    }
    note_path = root / "optimization_notes.jsonl"
    with note_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(note_record, ensure_ascii=False) + "\n")
    log_path = root / "growth_log.md"
    with log_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"## {note_record['noted_at']}｜{source}\n\n- 优化备注：{note_record['note']}\n\n---\n\n")
    return {"ok": True, "note_path": str(note_path), "log_path": str(log_path), "record": note_record}


def save_toutiao_growth_snapshot(
    settings: Settings,
    *,
    limit: int = 50,
    note: str = "",
    source: str = "cli",
    stats_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = _data_root(settings)
    snapshots_dir = root / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    root.mkdir(parents=True, exist_ok=True)

    if stats_payload is None:
        from .toutiao_publisher import fetch_toutiao_article_stats

        stats_payload = fetch_toutiao_article_stats(settings, limit=limit)

    analysis = analyze_toutiao_stats(stats_payload) if stats_payload.get("ok") else {}
    snapshot_at = _now_text()
    record = {
        "ok": bool(stats_payload.get("ok")),
        "snapshot_at": snapshot_at,
        "source": source,
        "note": (note or "").strip(),
        "api_url": stats_payload.get("api_url"),
        "raw_count": stats_payload.get("count"),
        "analysis": analysis,
        "error": stats_payload.get("error"),
    }
    snapshot_path = snapshots_dir / f"{_stamp()}.json"
    snapshot_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    latest_path = root / "latest.json"
    latest_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

    # 兼容旧的诊断文件名，方便人工快速打开最新原始统计。
    legacy_latest = Path(settings.local_settings_path).resolve().parent / "data" / "toutiao_stats_latest.json"
    legacy_latest.write_text(json.dumps(stats_payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

    _append_growth_log(root, record)
    sample_payload = _write_toutiao_samples(root, analysis, recorded_at=snapshot_at, source=source) if record["ok"] else {
        "positive_samples_path": str(root / "positive_samples.jsonl"),
        "negative_samples_path": str(root / "negative_samples.jsonl"),
        "positive_sample_count": 0,
        "negative_sample_count": 0,
    }
    return {
        "ok": record["ok"],
        "snapshot_path": str(snapshot_path),
        "latest_path": str(latest_path),
        "growth_log_path": str(root / "growth_log.md"),
        **sample_payload,
        "summary": analysis.get("summary") if analysis else {},
        "error": record.get("error"),
    }
