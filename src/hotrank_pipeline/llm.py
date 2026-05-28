from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import requests


GENERIC_HEADING_MAP = {
    "导语",
    "事件脉络",
    "关键信息",
    "为什么值得关注",
    "结语",
}


def sanitize_filename(name: str) -> str:
    value = re.sub(r"[\\\\/:*?\"<>|]+", "_", name)
    value = re.sub(r"[“”‘’「」『』【】《》？!！，。；：、]+", "_", value)
    value = re.sub(r"\s+", "_", value).strip("_")
    return value[:80] or "draft"


def _guess_extension(content_type: str, url: str, body: bytes) -> str:
    mapping = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/jpg": ".jpg",
    }
    if content_type in mapping:
        return mapping[content_type]
    if re.search(r"\.(jpg|jpeg|png|webp|gif)(?:$|\?)", url, re.I):
        ext = re.search(r"\.(jpg|jpeg|png|webp|gif)(?:$|\?)", url, re.I).group(1).lower()
        return ".jpg" if ext == "jpeg" else f".{ext}"
    if body[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if body[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if body[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def _topic_editorial_plan(cluster: dict) -> dict:
    """Pick a deterministic but varied WeChat article direction for the cluster."""
    text = f"{cluster.get('canonical_title', '')} {cluster.get('cluster_summary', '')}"
    title_hash = sum(ord(ch) for ch in text)

    plans = [
        {
            "name": "解释型拆解",
            "tone": "像一个有判断力的编辑，把复杂信息讲清楚，少堆概念，多给读者可理解的脉络。",
            "opening": "先用一个具体细节或一句判断进入，不要上来就写“近日”。",
            "section_titles": [
                "先说最关键的变化",
                "这件事是怎么走到这一步的",
                "真正需要看懂的几个信号",
                "它会影响谁",
                "接下来更值得盯住什么",
            ],
        },
        {
            "name": "故事型观察",
            "tone": "有画面感，但不煽情；从一个人、一个细节或一个反差切入，再回到事实本身。",
            "opening": "开头要有场景感，适合公众号读者继续往下读。",
            "section_titles": [
                "一个细节，先把情绪带出来",
                "热议背后，真正发生了什么",
                "为什么大家会被触动",
                "别只看情绪，也要看事实",
                "最后留下的不是热闹",
            ],
        },
        {
            "name": "清单型快读",
            "tone": "节奏更快，适合信息量较大的热点；用清单、短段和小结降低阅读压力。",
            "opening": "开头直接给读者一个“这篇文章解决什么问题”的承诺。",
            "section_titles": [
                "先把重点摆出来",
                "三个信息点值得先看",
                "争议集中在哪里",
                "普通读者需要关心什么",
                "后续还有哪些变量",
            ],
        },
        {
            "name": "评论型分析",
            "tone": "有观点但克制，事实先行，观点后置；避免口号化，避免过度拔高。",
            "opening": "用一句不夸张的判断开场，让读者知道这不是简单复述新闻。",
            "section_titles": [
                "这不是一个孤立事件",
                "表面变化之外，还有一层逻辑",
                "公众为什么会在意",
                "需要警惕的几个误读",
                "把它放回现实里看",
            ],
        },
    ]

    if _contains_any(text, ("病毒", "感染", "疾控", "疫苗", "医院", "疾病", "症状", "健康", "流感", "疫情")):
        return {
            "name": "健康科普型",
            "tone": "准确、克制、去恐慌；先讲结论，再讲风险边界和可执行建议。",
            "opening": "开头直接回应读者最关心的问题：严重吗、该不该担心、现在能做什么。",
            "section_titles": [
                "先说结论：不用恐慌，但要留意",
                "这次提醒到底指向什么",
                "哪些人更需要关注",
                "普通人能做的几件事",
                "信息还在更新，别被情绪带跑",
            ],
        }
    if _contains_any(text, ("薪资", "年终奖", "十三薪", "裁员", "员工", "工资", "职场", "绩效", "公司", "财年")):
        return {
            "name": "职场商业拆解",
            "tone": "像职场商业号，讲清变化、利益影响和管理逻辑；不替公司站台，也不制造焦虑。",
            "opening": "开头先说明这次变化对个人最直接的影响，别从公司公告口吻写起。",
            "section_titles": [
                "先看最直接的变化",
                "钱没有只看金额，还要看到账时间",
                "公司为什么要这样调整",
                "员工真正担心的是什么",
                "放到职场里，这件事提醒了什么",
            ],
        }
    if _contains_any(text, ("学校", "学生", "老师", "老人", "家庭", "孩子", "拾荒", "论文", "致谢", "原生家庭", "打", "伤害")):
        return {
            "name": "社会情绪观察",
            "tone": "有人情味，但不消费苦难；先承认情绪，再回到事实和公共讨论价值。",
            "opening": "开头从一个最打动人的细节写起，不要一开始就下结论。",
            "section_titles": [
                "最先打动人的，是这个细节",
                "热搜之外，事实线索有哪些",
                "为什么它会击中那么多人",
                "情绪之外，还能讨论什么",
                "愿善意不只停在转发里",
            ],
        }
    if _contains_any(text, ("通报", "警方", "法院", "判决", "调查", "监管", "处罚", "官方", "回应", "政策")):
        return {
            "name": "公共事件梳理",
            "tone": "严谨、分层、少猜测；把已确认信息、仍待确认信息和影响分开写。",
            "opening": "开头先交代目前已确认的核心事实，同时提醒信息边界。",
            "section_titles": [
                "目前能确认的事实",
                "时间线需要分开看",
                "公众关注点集中在哪",
                "还有哪些问题待回应",
                "越是热议，越要守住事实边界",
            ],
        }

    return plans[title_hash % len(plans)]


def _format_section_suggestions(section_titles: list[str]) -> str:
    return "\n".join(f"   - ## {title}" for title in section_titles)


def _render_prompt_template(template: str, context: dict[str, str]) -> str:
    if not template.strip():
        return ""
    try:
        return template.format(**context)
    except Exception:
        # Keep generation usable even if a custom prompt contains unmatched braces.
        rendered = template
        for key, value in context.items():
            rendered = rendered.replace("{" + key + "}", value)
        return rendered


def _soften_generic_headings(content_md: str, plan: dict) -> str:
    """Avoid every generated article keeping the old fixed five-section skeleton."""
    section_titles = plan.get("section_titles") or []
    if not section_titles:
        return content_md

    replacement_index = 0
    output: list[str] = []
    for line in content_md.splitlines():
        match = re.match(r"^(##+)\s+(.+?)\s*$", line)
        if match:
            prefix, heading = match.groups()
            normalized = heading.strip(" #：:")
            if prefix == "##" and normalized in GENERIC_HEADING_MAP and replacement_index < len(section_titles):
                line = f"## {section_titles[replacement_index]}"
                replacement_index += 1
        output.append(line)
    return "\n".join(output).strip() + "\n"


def generate_wechat_draft(
    llm_config: dict,
    cluster: dict,
    article_sources: list[dict],
) -> tuple[str, str, str]:
    source_lines = []
    for idx, source in enumerate(article_sources, start=1):
        source_lines.append(
            "\n".join(
                [
                    f"来源{idx}：{source['board_name']} / {source['source_url']}",
                    f"标题：{source.get('title') or source.get('member_title')}",
                    f"摘要：{source.get('summary') or ''}",
                    f"正文摘录：{(source.get('content_text') or '')[:1200]}",
                ]
            ).strip()
        )

    editorial_plan = _topic_editorial_plan(cluster)
    section_suggestions = _format_section_suggestions(editorial_plan["section_titles"])
    context = {
        "topic": str(cluster["canonical_title"]),
        "cluster_summary": str(cluster.get("cluster_summary", "")),
        "item_count": str(cluster.get("item_count", len(article_sources))),
        "sources": chr(10).join(source_lines),
        "editorial_plan_name": str(editorial_plan["name"]),
        "editorial_tone": str(editorial_plan["tone"]),
        "opening_strategy": str(editorial_plan["opening"]),
        "section_suggestions": section_suggestions,
    }

    default_prompt = """你是一名资深微信公众号编辑，请根据下面的热点聚类和来源摘要，写一篇可直接用于公众号发布前审核的图文初稿。

写作目标：
1. 成稿要像成熟公众号文章，适合直接进入人工润色/排版/发布环节。
2. 风格要克制、可信、流畅，有信息密度，也要有阅读节奏和公众号阅读感。
3. 不要写成新闻播报口吻，也不要写成“AI总结”“资料汇编”口吻。
4. 文章要有“编辑选择”：不是把材料复述一遍，而是帮读者完成理解、判断和延展。

输出要求：
1. 使用中文输出，格式为 Markdown。
2. 第一行必须是一级标题：# 标题
3. 不要再使用固定五段模板，尤其不要机械套用“导语 / 事件脉络 / 关键信息 / 为什么值得关注 / 结语”。
4. 请根据素材自由组织 5-7 个二级标题；小标题要像公众号编辑写的自然标题，不要像报告目录。
5. 正文建议 1200-1800 字，段落短，每段尽量 1-3 句话，适合手机端阅读。
6. 至少加入 2 种增强阅读节奏的模块，例如：
   - `> 一句话先看：...`
   - `- 重点一 / 重点二 / 重点三`
   - `**一个容易被忽略的细节是：**...`
   - 简短时间线、影响清单、误读提醒、后续观察点
7. 可适度提炼观点，但不能编造事实、数据、引语或未经来源支持的判断。
8. 如果信息不完整，请明确写“截至目前公开报道显示”或“目前公开信息有限”，不要硬补细节。
9. 标题要适合公众号传播，清晰、自然、有可读性，不要标题党，不要夸张煽动。
10. 结尾不要写“对此你怎么看”“欢迎留言”等强互动套话，要自然收束。
11. 不要输出代码块，不要输出“以下是文章”之类说明性前缀。
12. 不要在正文中写“配图”“图片占位符”“见下图”等字样，图片会由程序自动插入。

本篇建议方向：
- 文章类型：{editorial_plan_name}
- 语气策略：{editorial_tone}
- 开头方式：{opening_strategy}
- 可参考但不要死套的小标题方向：
{section_suggestions}

风格要求：
- 语言有温度，但不过度抒情
- 多用短句和短段
- 能帮助读者快速看懂事件、抓住重点、理解背后的公共讨论价值
- 保持事实与观点分层清晰
- 每篇文章都要有一点不同的结构和表达，不要让读者感觉是同一个模板换了标题

热点主题：{topic}
热点摘要：{cluster_summary}
聚类成员数：{item_count}

来源材料：
{sources}
"""
    custom_prompt = (llm_config.get("draft_prompt") or "").strip()
    prompt = _render_prompt_template(custom_prompt or default_prompt, context)

    payload = {
        "model": llm_config["model"],
        "messages": [
            {
                "role": "system",
                "content": "你是一个擅长热点议题写作的中文公众号编辑，强调事实准确、结构清晰、语言自然、适合发布。",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": llm_config.get("temperature", 0.65),
        "max_tokens": llm_config.get("max_tokens", 2400),
    }

    response = requests.post(
        f"{llm_config['base_url'].rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {llm_config['api_key']}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=180,
    )
    response.raise_for_status()
    data = response.json()
    content = _soften_generic_headings(data["choices"][0]["message"]["content"].strip(), editorial_plan)

    title = cluster["canonical_title"]
    first_line = content.splitlines()[0] if content else ""
    if first_line.startswith("#"):
        title = first_line.lstrip("#").strip() or title

    return title, content, prompt[:1200]


def _download_images(image_urls: list[str], month_dir: Path, stem_name: str) -> list[str]:
    if not image_urls:
        return []
    asset_dir = month_dir / "assets" / stem_name
    asset_dir.mkdir(parents=True, exist_ok=True)
    saved_paths: list[str] = []
    seen = set()

    for idx, image_url in enumerate(image_urls, start=1):
        if image_url in seen:
            continue
        seen.add(image_url)
        try:
            response = requests.get(image_url, timeout=40)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if not content_type.startswith("image/") and "octet-stream" not in content_type:
                continue
            ext = _guess_extension(content_type, image_url, response.content)
            target = asset_dir / f"img_{idx:02d}{ext}"
            target.write_bytes(response.content)
            relative = Path("assets") / stem_name / target.name
            saved_paths.append(relative.as_posix())
        except Exception:
            continue
    return saved_paths


def _inject_images_into_markdown(content_md: str, image_paths: list[str]) -> str:
    if not image_paths:
        return content_md

    lines = content_md.splitlines()
    output: list[str] = []
    image_index = 0
    body_paragraph_count = 0
    inserted_after_opening = False

    for i, line in enumerate(lines):
        output.append(line)
        stripped = line.strip()
        if image_index >= len(image_paths):
            continue

        is_body_paragraph = bool(stripped) and not (
            stripped.startswith("#")
            or stripped.startswith("!")
            or stripped.startswith(">")
            or stripped.startswith("- ")
            or stripped.startswith("* ")
            or stripped.startswith("|")
            or re.match(r"^\d+[.)、]\s+", stripped)
        )
        if not is_body_paragraph:
            continue

        body_paragraph_count += 1
        should_insert = False
        if not inserted_after_opening and body_paragraph_count >= 1:
            should_insert = True
            inserted_after_opening = True
        elif body_paragraph_count >= 4 and (body_paragraph_count - 1) % 3 == 0:
            should_insert = True

        if should_insert:
            output.extend(["", f"![文章配图{image_index + 1}]({image_paths[image_index]})", ""])
            image_index += 1

    if image_index < len(image_paths):
        output.extend(["", "## 更多相关画面", ""])
        while image_index < len(image_paths):
            output.append(f"![文章配图{image_index + 1}]({image_paths[image_index]})")
            output.append("")
            image_index += 1

    return "\n".join(output).strip() + "\n"


def archive_draft(
    output_dir: str,
    title: str,
    content_md: str,
    image_urls: list[str] | None = None,
) -> tuple[str, int]:
    now = datetime.now()
    month_dir = Path(output_dir) / now.strftime("%Y-%m")
    day_dir = month_dir / now.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    safe_title = sanitize_filename(title)
    filename = f"{now.strftime('%Y%m%d_%H%M%S')}_{safe_title}.md"
    target = day_dir / filename
    stem_name = target.stem
    image_paths = _download_images(image_urls or [], day_dir, stem_name)
    final_content = _inject_images_into_markdown(content_md, image_paths)
    try:
        target.write_text(final_content, encoding="utf-8")
    except PermissionError:
        fallback = day_dir / f"{now.strftime('%Y%m%d_%H%M%S')}_{safe_title[:32]}_{now.strftime('%f')}.md"
        fallback.write_text(final_content, encoding="utf-8")
        target = fallback
    return str(target), len(image_paths)
