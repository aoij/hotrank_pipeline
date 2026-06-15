from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any


WECHAT_WEAK_TITLE_PATTERNS = (
    r"你可能也刷到了",
    r"今天朋友圈刷屏",
    r"朋友圈刷屏",
    r"这事和你有关",
    r"这件事和你有关",
    r"和很多人想的可能不太一样",
    r"别被.+带跑",
    r"别让.+替你",
    r"别把.+读过头",
    r"看热闹可以",
    r"热闹可以看",
    r"最该看懂的是具体影响",
    r"真正值得看的不是热闹",
    r"重点不只是",
    r"先别急着下结论",
    r"先别急着下判断",
)

WECHAT_REPORT_STYLE_PATTERNS = (
    r"近日",
    r"据悉",
    r"记者了解到",
    r"引发广泛关注",
    r"相关部门表示",
    r"业内人士认为",
    r"随着.+的发展",
    r"在这个.+时代",
    r"综上所述",
    r"由此可见",
    r"本文将",
    r"本文认为",
    r"首先",
    r"其次",
    r"最后",
    r"一方面",
    r"另一方面",
    r"原因分析",
    r"影响分析",
    r"应对建议",
    r"解决方案",
    r"背景介绍",
)

WECHAT_LOW_VALUE_HINTS = (
    "复述",
    "搬运",
    "拼凑",
    "新闻稿",
    "通稿",
    "模板",
    "AI",
    "AIGC",
    "空泛",
    "信息量不足",
    "信息密度不足",
    "缺少增量",
    "低创作",
    "同质",
    "列表",
    "报告",
    "作文",
)

WECHAT_PLACEHOLDER_PATTERNS = (
    r"【\s*需补来源\s*】",
    r"【\s*待核实\s*】",
    r"原文未明确",
    r"需补来源",
    r"待补充",
)

GENERIC_TITLE_WORDS = {
    "这事",
    "这件事",
    "热搜",
    "朋友圈",
    "很多人",
    "普通人",
    "影响",
    "风险",
    "背后",
    "真相",
    "原因",
    "细节",
}


def load_draft_markdown(draft: dict[str, Any]) -> str:
    archive_path = str(draft.get("archive_path") or "").strip()
    if not archive_path:
        return str(draft.get("content_md") or "")
    try:
        path = Path(archive_path)
        if path.exists() and path.is_file():
            return path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        pass
    return str(draft.get("content_md") or "")


def markdown_to_plain_text(content_md: str) -> str:
    text = content_md or ""
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", text)
    text = re.sub(r"\[[^\]]+\]\([^)]+\)", lambda m: m.group(0).split("](", 1)[0].lstrip("["), text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"`{1,3}[^`]*`{1,3}", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def markdown_image_count(content_md: str) -> int:
    return len(re.findall(r"!\[[^\]]*\]\([^)]+\)", content_md or ""))


def _normalized_title_terms(title: str) -> list[str]:
    clean = unicodedata.normalize("NFKC", title or "")
    return [
        term
        for term in re.findall(r"[A-Za-z0-9]{2,}|[\u4e00-\u9fff]{2,8}", clean)
        if term and term not in GENERIC_TITLE_WORDS
    ]


def _first_body_block(plain_text: str) -> str:
    blocks = [block.strip() for block in re.split(r"\n\s*\n", plain_text or "") if block.strip()]
    if not blocks:
        return ""
    if len(blocks[0]) <= 36 and not blocks[0].endswith(("。", "！", "？")) and len(blocks) > 1:
        return blocks[1]
    return blocks[0]


def _review_score_value(draft: dict[str, Any]) -> float | None:
    value = draft.get("review_score")
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None


def evaluate_wechat_publish_quality(
    draft: dict[str, Any],
    content_md: str | None = None,
    *,
    min_score: float | None = None,
    require_image: bool = True,
) -> dict[str, Any]:
    """Local gate for WeChat low-originality / low-value content risk."""

    title = str(draft.get("title") or draft.get("canonical_title") or "").strip()
    content = content_md if content_md is not None else load_draft_markdown(draft)
    plain = markdown_to_plain_text(content)
    compact = re.sub(r"\s+", "", plain)
    title_compact = re.sub(r"\s+", "", title)
    review_summary = str(draft.get("review_summary") or "")
    review_score = _review_score_value(draft)
    min_score_value = float(min_score if min_score is not None else 8.2)
    image_count = markdown_image_count(content)
    heading_count = len(re.findall(r"^\s{0,3}#{2,6}\s+", content or "", flags=re.M))
    list_line_count = len(re.findall(r"^\s*(?:[-*+]\s+|\d+[.、]\s+|[一二三四五六七八九十][、.])", content or "", flags=re.M))
    report_hits = [pattern for pattern in WECHAT_REPORT_STYLE_PATTERNS if re.search(pattern, plain)]
    placeholder_hits = [pattern for pattern in WECHAT_PLACEHOLDER_PATTERNS if re.search(pattern, plain)]
    low_value_hits = [hint for hint in WECHAT_LOW_VALUE_HINTS if hint.lower() in review_summary.lower()]
    weak_title_hits = [pattern for pattern in WECHAT_WEAK_TITLE_PATTERNS if re.search(pattern, title_compact)]
    opening = _first_body_block(plain)[:180]
    title_terms = _normalized_title_terms(title)
    source_count = int(draft.get("fetched_source_count") or draft.get("source_count") or draft.get("item_count") or 0)

    blockers: list[str] = []
    warnings: list[str] = []
    quality_score = 10.0

    if not title_compact:
        blockers.append("标题为空")
        quality_score -= 2.0
    elif len(title_compact) < 10:
        blockers.append("标题过短，缺少明确点击理由")
        quality_score -= 1.0
    elif len(title_compact) > 34:
        warnings.append("标题偏长，公众号/头条端容易显得啰嗦")
        quality_score -= 0.4

    if weak_title_hits:
        blockers.append("标题命中模板化/泛化表达")
        quality_score -= 1.6
    if len(title_terms) <= 1:
        blockers.append("标题缺少具体热点名词、人名、平台名或事件名")
        quality_score -= 1.0

    if placeholder_hits:
        blockers.append("正文仍有内部占位或待补来源标记")
        quality_score -= 2.5

    text_len = len(compact)
    if text_len < 700:
        blockers.append("正文过短，容易被判定信息量不足")
        quality_score -= 1.5
    elif text_len < 900:
        warnings.append("正文偏短，需要更明显的观点增量或场景增量")
        quality_score -= 0.5

    if require_image and image_count <= 0:
        blockers.append("缺少正文插图，图文完整度不足")
        quality_score -= 1.0
    elif image_count == 1:
        warnings.append("正文插图偏少，建议至少 2 张匹配内容的图")
        quality_score -= 0.2

    if not opening:
        blockers.append("开头为空或不可读")
        quality_score -= 1.0
    elif re.search(r"^(近日|据悉|最近|你可能也刷到了|这事|这件事|今天朋友圈)", opening):
        blockers.append("开头过于新闻稿/模板化，缺少原创切入")
        quality_score -= 1.0

    if len(report_hits) >= 4:
        blockers.append("正文报告腔/新闻稿腔/作文腔过重")
        quality_score -= 1.2
    elif len(report_hits) >= 2:
        warnings.append("正文存在新闻稿腔或作文腔")
        quality_score -= 0.4

    if heading_count > 3:
        blockers.append("小标题过多，像目录式文章")
        quality_score -= 0.8
    if list_line_count >= 4:
        blockers.append("列表/条款过多，像低创作度整理稿")
        quality_score -= 0.8

    repeated_transition_hits = 0
    for word in ("问题是", "说白了", "说到底", "别急", "真正麻烦", "最后说句实在话", "普通人该怎么做"):
        count = plain.count(word)
        if count >= 2:
            repeated_transition_hits += count
    if repeated_transition_hits >= 3:
        blockers.append("万能转场重复过多，AI模板感明显")
        quality_score -= 0.9

    if low_value_hits and (review_score is None or review_score < max(8.6, min_score_value + 0.2)):
        blockers.append("模型点评已提示低创作度/信息增量不足风险")
        quality_score -= 1.0

    if review_score is not None and review_score < min_score_value:
        blockers.append(f"公众号模型评分低于发布线 {min_score_value:.1f}")
        quality_score -= 1.0

    if source_count and source_count < 2 and text_len < 1000 and (review_score is None or review_score < 8.8):
        blockers.append("来源材料偏少且正文增量不足，容易像改写稿")
        quality_score -= 0.8

    if re.search(r"(本文|本篇文章|下面我们|接下来我们|我们可以从以下|以下几点)", plain):
        blockers.append("正文存在明显课堂/说明文口吻")
        quality_score -= 0.7

    quality_score = max(0.0, min(10.0, round(quality_score, 1)))
    ok = not blockers and quality_score >= 7.6
    summary_parts = blockers[:3] if blockers else (warnings[:2] or ["公众号质量闸门通过"])
    return {
        "ok": ok,
        "score": quality_score,
        "summary": "；".join(summary_parts),
        "blockers": blockers,
        "warnings": warnings,
        "metrics": {
            "text_length": text_len,
            "image_count": image_count,
            "heading_count": heading_count,
            "list_line_count": list_line_count,
            "report_hit_count": len(report_hits),
            "review_score": review_score,
            "source_count": source_count,
        },
    }
