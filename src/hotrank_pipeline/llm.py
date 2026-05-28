from __future__ import annotations

import base64
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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


RETRYABLE_REQUEST_EXCEPTIONS = (
    requests.exceptions.SSLError,
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)


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


def _image_generation_endpoint(base_url: str) -> str:
    endpoint = (base_url or "").rstrip("/")
    if not endpoint:
        return ""
    if endpoint.endswith("/images/generations"):
        return endpoint
    return f"{endpoint}/images/generations"


def _post_json_with_retry(
    url: str,
    headers: dict[str, str],
    payload: dict,
    timeout: int,
    retry_count: int = 3,
    backoff_seconds: float = 2.0,
) -> requests.Response:
    last_error: Exception | None = None
    retry_count = max(1, retry_count)
    for attempt in range(1, retry_count + 1):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=timeout)
            if response.status_code in (429, 500, 502, 503, 504) and attempt < retry_count:
                last_error = requests.HTTPError(
                    f"retryable status {response.status_code}: {response.text[:200]}",
                    response=response,
                )
                time.sleep(backoff_seconds * attempt)
                continue
            response.raise_for_status()
            return response
        except RETRYABLE_REQUEST_EXCEPTIONS as exc:
            last_error = exc
            if attempt >= retry_count:
                break
            time.sleep(backoff_seconds * attempt)
        except requests.HTTPError as exc:
            last_error = exc
            status_code = exc.response.status_code if exc.response is not None else None
            if status_code not in (429, 500, 502, 503, 504) or attempt >= retry_count:
                break
            time.sleep(backoff_seconds * attempt)
    if last_error:
        raise last_error
    raise RuntimeError("request failed without response")


def _decode_data_url(value: str) -> tuple[bytes, str]:
    header, encoded = value.split(",", 1)
    content_type = "image/png"
    match = re.match(r"data:([^;]+);base64", header, re.I)
    if match:
        content_type = match.group(1).lower()
    return base64.b64decode(encoded), content_type


def _extract_image_payloads(data: object) -> list[object]:
    if isinstance(data, dict):
        for key in ("data", "images", "output", "result"):
            value = data.get(key)
            if isinstance(value, list):
                return value
        return [data]
    if isinstance(data, list):
        return data
    return []


def _image_bytes_from_payload(payload: object, timeout_seconds: int = 180) -> tuple[bytes, str] | None:
    value: str | None = None
    if isinstance(payload, dict):
        for key in ("b64_json", "base64", "image_base64", "data"):
            candidate = payload.get(key)
            if isinstance(candidate, str) and candidate.strip():
                value = candidate.strip()
                break
        if value is None:
            for key in ("url", "image_url", "uri"):
                candidate = payload.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    value = candidate.strip()
                    break
    elif isinstance(payload, str) and payload.strip():
        value = payload.strip()

    if not value:
        return None

    if value.startswith("data:image/"):
        return _decode_data_url(value)

    if re.match(r"^https?://", value, re.I):
        response = requests.get(value, timeout=timeout_seconds)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        return response.content, content_type or "image/jpeg"

    raw = base64.b64decode(value)
    return raw, "image/png"


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


def _markdown_sections(content_md: str) -> list[dict[str, str]]:
    sections: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal current, buffer
        if current is not None:
            current["excerpt"] = re.sub(r"\s+", " ", "\n".join(buffer)).strip()[:420]
            sections.append(current)
        buffer = []

    for line in content_md.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            flush()
            current = {"title": stripped.lstrip("#").strip(), "excerpt": ""}
            continue
        if stripped and current is not None and not stripped.startswith(("#", "!", "|")):
            buffer.append(stripped)

    flush()
    return sections


def _build_image_prompts(
    title: str,
    content_md: str,
    image_config: dict,
) -> list[str]:
    max_count = max(0, int(image_config.get("max_per_draft", 4)))
    if max_count <= 0:
        return []

    generation_config = image_config.get("generation") or {}
    prompt_template = (generation_config.get("prompt_template") or "").strip()
    sections = _markdown_sections(content_md)
    if not sections:
        plain = re.sub(r"[#>*_`!\[\]\(\)]", "", content_md)
        sections = [{"title": title, "excerpt": re.sub(r"\s+", " ", plain).strip()[:420]}]

    prompts: list[str] = []
    for idx in range(max_count):
        if idx == 0:
            section = {"title": "封面主视觉", "excerpt": sections[0].get("excerpt", "")}
        else:
            section = sections[min(idx - 1, len(sections) - 1)]

        context = {
            "article_title": title,
            "section_title": section.get("title") or title,
            "section_excerpt": section.get("excerpt") or "",
            "image_index": str(idx + 1),
            "image_count": str(max_count),
        }
        if prompt_template:
            prompt = _render_prompt_template(prompt_template, context)
        else:
            prompt = (
                "为一篇中文微信公众号文章生成原创插图。"
                f"文章标题：{context['article_title']}。"
                f"插图位置：第 {context['image_index']} 张 / 共 {context['image_count']} 张。"
                f"小节主题：{context['section_title']}。"
                f"内容摘要：{context['section_excerpt']}。"
                "画面要求：现代中文媒体插画风，真实但不过度新闻摄影感，构图干净，有情绪和信息量，"
                "适合手机端公众号阅读；不要出现任何文字、标题、logo、水印、二维码、品牌商标；"
                "不要直接生成真实公众人物脸部特写，避免血腥、事故现场和惊悚画面。"
            )
        prompts.append(prompt)

    return prompts


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

    response = _post_json_with_retry(
        f"{llm_config['base_url'].rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {llm_config['api_key']}",
            "Content-Type": "application/json",
        },
        payload=payload,
        timeout=int(llm_config.get("timeout_seconds", 180)),
        retry_count=int(llm_config.get("retry_count", 3)),
        backoff_seconds=float(llm_config.get("retry_backoff_seconds", 2.0)),
    )
    data = response.json()
    content = _soften_generic_headings(data["choices"][0]["message"]["content"].strip(), editorial_plan)

    title = cluster["canonical_title"]
    first_line = content.splitlines()[0] if content else ""
    if first_line.startswith("#"):
        title = first_line.lstrip("#").strip() or title

    return title, content, prompt[:1200]


def _extract_json_object(text: str) -> dict | None:
    content = (text or "").strip()
    if not content:
        return None
    content = re.sub(r"^```(?:json)?\s*", "", content, flags=re.I)
    content = re.sub(r"\s*```$", "", content)
    try:
        parsed = json.loads(content)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    candidates: list[dict] = []
    for match in re.finditer(r"\{[^{}]*\}", content):
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                candidates.append(parsed)
        except json.JSONDecodeError:
            continue
    if not candidates:
        return None
    score_keys = {"score", "review_score", "article_score", "分数", "评分"}
    for candidate in reversed(candidates):
        if score_keys.intersection(candidate.keys()):
            return candidate
    return candidates[-1]


def _parse_review_score(text: str) -> float:
    def normalize_score(value: object) -> float | None:
        if isinstance(value, str):
            match = re.search(r"\d+(?:\.\d+)?", value)
            if not match:
                return None
            raw_score = float(match.group(0))
        else:
            raw_score = float(value)
        if raw_score > 10 and raw_score <= 100:
            raw_score = raw_score / 10
        return max(0.0, min(10.0, round(raw_score, 1)))

    data = _extract_json_object(text)
    if data:
        for key in ("score", "review_score", "article_score", "分数", "评分"):
            if key in data:
                try:
                    score = normalize_score(data[key])
                    if score is not None:
                        return score
                except (TypeError, ValueError):
                    continue

    score_patterns = (
        r"\"?(?:score|review_score|article_score)\"?\s*[:：]\s*\"?(\d+(?:\.\d+)?)",
        r"\"?(?:score|review_score|article_score)\"?\s*(?:设为|为|=)\s*\"?(\d+(?:\.\d+)?)",
        r"\"?(?:分数|评分|文章分)\"?\s*[:：]?\s*\"?(\d+(?:\.\d+)?)",
        r"(\d+(?:\.\d+)?)\s*/\s*10",
    )
    for pattern in score_patterns:
        match = re.search(pattern, text or "", flags=re.I)
        if match:
            value = float(match.group(1))
            if value > 10 and value <= 100:
                value = value / 10
            return max(0.0, min(10.0, round(value, 1)))

    raise ValueError("模型评分响应中未解析到 0-10 分数")


def _parse_review_summary(text: str) -> str:
    data = _extract_json_object(text)
    if data:
        for key in ("summary", "reason", "review_summary", "点评", "理由", "建议"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return re.sub(r"\s+", " ", value).strip()[:220]
        strengths = data.get("strengths")
        weaknesses = data.get("weaknesses")
        if isinstance(strengths, list) or isinstance(weaknesses, list):
            parts = []
            if strengths:
                parts.append("优点：" + "；".join(str(x) for x in strengths[:3]))
            if weaknesses:
                parts.append("待优化：" + "；".join(str(x) for x in weaknesses[:3]))
            if parts:
                return " ".join(parts)[:220]

    cleaned = re.sub(r"^```(?:json)?\s*", "", text or "", flags=re.I)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    for match in re.finditer(r'"summary"\s*:\s*"([^"]{6,260})', cleaned, flags=re.I):
        value = match.group(1).strip()
        if value:
            return re.sub(r"\s+", " ", value).strip()[:220]
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if "扣分项" in cleaned or "评分标准" in cleaned or len(cleaned) > 260:
        return "模型已完成审核评分；建议人工复核标题、结构、事实边界和手机端阅读节奏。"
    return cleaned[:220] or "模型已完成审核，但未返回明确点评。"


def review_wechat_draft(
    llm_config: dict,
    title: str,
    content_md: str,
) -> tuple[float, str, str]:
    plain_content = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", content_md or "")
    plain_content = re.sub(r"\n{3,}", "\n\n", plain_content).strip()
    prompt = f"""你是一名严格但务实的微信公众号主编，请审核下面这篇已经生成的公众号初稿，并给出可用于排序的文章质量分。

评分范围：0-10 分，保留 1 位小数。

请按公众号发布前审核标准评分，重点看：
1. 标题是否清晰、有打开欲，但不标题党。
2. 开头是否能抓住读者，并快速交代价值。
3. 结构是否自然，不像固定模板或 AI 摘要。
4. 信息密度、事实边界、观点分层是否合格。
5. 段落节奏是否适合手机端阅读。
6. 是否具备发布可用度：越接近可直接发，分数越高。

扣分项：
- 明显空泛、模板化、复述材料、像新闻播报。
- 编造事实、过度煽动、事实与观点混在一起。
- 段落太长、标题生硬、结尾套路。
- 与微信公众号读者场景不匹配。

只输出 JSON，不要输出 Markdown，不要解释 JSON 之外的内容：
{{
  "score": 8.4,
  "summary": "一句话说明为什么给这个分数，并指出最需要优化的一点"
}}

文章标题：{title}

文章正文：
{plain_content[:6000]}
"""
    payload = {
        "model": llm_config["model"],
        "messages": [
            {
                "role": "system",
                "content": "你是中文微信公众号主编，负责审核文章质量并给出稳定、可比较的评分。",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 1200,
    }

    response = _post_json_with_retry(
        f"{llm_config['base_url'].rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {llm_config['api_key']}",
            "Content-Type": "application/json",
        },
        payload=payload,
        timeout=int(llm_config.get("timeout_seconds", 180)),
        retry_count=int(llm_config.get("retry_count", 3)),
        backoff_seconds=float(llm_config.get("retry_backoff_seconds", 2.0)),
    )
    data = response.json()
    message = data["choices"][0]["message"]
    raw = (message.get("content") or message.get("reasoning_content") or "").strip()
    score = _parse_review_score(raw)
    summary = _parse_review_summary(raw)
    return score, summary, prompt[:1200]


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


def _generate_ai_images(
    title: str,
    content_md: str,
    image_config: dict,
    day_dir: Path,
    stem_name: str,
) -> tuple[list[str], list[str]]:
    generation_config = image_config.get("generation") or {}
    base_url = (generation_config.get("base_url") or "").strip()
    model = (generation_config.get("model") or "").strip()
    endpoint = _image_generation_endpoint(base_url)
    if not endpoint or not model:
        return [], []

    prompts = _build_image_prompts(title, content_md, image_config)
    if not prompts:
        return [], []

    asset_dir = day_dir / "assets" / stem_name
    asset_dir.mkdir(parents=True, exist_ok=True)
    timeout_seconds = int(generation_config.get("timeout_seconds", 180))
    size = (generation_config.get("size") or "1024x1024").strip()
    api_key = (generation_config.get("api_key") or "").strip()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    max_workers = max(1, int(generation_config.get("concurrency", min(4, len(prompts)))))
    max_workers = min(max_workers, len(prompts))

    def generate_one(idx: int, prompt: str) -> tuple[int, str, str] | None:
        payload = {
            "model": model,
            "prompt": prompt,
            "n": 1,
            "size": size,
        }
        try:
            response = requests.post(endpoint, headers=headers, json=payload, timeout=timeout_seconds)
            response.raise_for_status()
            data = response.json()
            image_payloads = _extract_image_payloads(data)
            image_data: tuple[bytes, str] | None = None
            for image_payload in image_payloads:
                image_data = _image_bytes_from_payload(image_payload, timeout_seconds=timeout_seconds)
                if image_data:
                    break
            if not image_data:
                return None
            body, content_type = image_data
            ext = _guess_extension(content_type, "", body)
            target = asset_dir / f"ai_img_{idx:02d}{ext}"
            target.write_bytes(body)
            relative = Path("assets") / stem_name / target.name
            return idx, relative.as_posix(), prompt
        except Exception:
            return None

    results: list[tuple[int, str, str]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(generate_one, idx, prompt) for idx, prompt in enumerate(prompts, start=1)]
        for future in as_completed(futures):
            result = future.result()
            if result:
                results.append(result)

    results.sort(key=lambda item: item[0])
    saved_paths = [path for _, path, _ in results]
    used_prompts = [prompt for _, _, prompt in results]

    if used_prompts:
        prompt_file = asset_dir / "ai_image_prompts.txt"
        prompt_file.write_text(
            "\n\n".join(f"--- image {idx} ---\n{prompt}" for idx, prompt in enumerate(used_prompts, start=1)),
            encoding="utf-8",
        )

    return saved_paths, used_prompts


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


def strip_markdown_images(content_md: str) -> str:
    output: list[str] = []
    skipping_image_gallery = False
    for line in content_md.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            heading = stripped.lstrip("#").strip()
            if heading in {"更多相关画面", "图集补充"}:
                skipping_image_gallery = True
                continue
            skipping_image_gallery = False

        if skipping_image_gallery:
            if stripped.startswith("## "):
                skipping_image_gallery = False
            else:
                continue

        if re.match(r"^!\[[^\]]*\]\([^)]+\)\s*$", stripped):
            continue
        output.append(line)

    cleaned = "\n".join(output)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned + "\n" if cleaned else ""


def archive_draft(
    output_dir: str,
    title: str,
    content_md: str,
    image_urls: list[str] | None = None,
    image_config: dict | None = None,
) -> tuple[str, int, str]:
    now = datetime.now()
    month_dir = Path(output_dir) / now.strftime("%Y-%m")
    day_dir = month_dir / now.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    safe_title = sanitize_filename(title)
    filename = f"{now.strftime('%Y%m%d_%H%M%S')}_{safe_title}.md"
    target = day_dir / filename
    stem_name = target.stem
    image_config = image_config or {}
    prefer_ai_generated = bool(image_config.get("prefer_ai_generated", True))
    fallback_to_source = bool(image_config.get("fallback_to_source", True))
    image_source = "none"
    image_paths: list[str] = []

    if prefer_ai_generated:
        image_paths, _ = _generate_ai_images(title, content_md, image_config, day_dir, stem_name)
        if image_paths:
            image_source = "ai"

    if not image_paths and fallback_to_source:
        image_paths = _download_images(image_urls or [], day_dir, stem_name)
        if image_paths:
            image_source = "source"

    final_content = _inject_images_into_markdown(content_md, image_paths)
    try:
        target.write_text(final_content, encoding="utf-8")
    except PermissionError:
        fallback = day_dir / f"{now.strftime('%Y%m%d_%H%M%S')}_{safe_title[:32]}_{now.strftime('%f')}.md"
        fallback.write_text(final_content, encoding="utf-8")
        target = fallback
    return str(target), len(image_paths), image_source


def regenerate_draft_images_file(
    archive_path: str,
    title: str,
    content_md: str,
    image_config: dict | None = None,
    image_urls: list[str] | None = None,
) -> tuple[str, int, str, str]:
    target = Path(archive_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    stem_name = target.stem or sanitize_filename(title)
    image_config = image_config or {}
    prefer_ai_generated = bool(image_config.get("prefer_ai_generated", True))
    fallback_to_source = bool(image_config.get("fallback_to_source", True))
    clean_content = strip_markdown_images(content_md)
    image_paths: list[str] = []
    image_source = "none"

    if prefer_ai_generated:
        image_paths, _ = _generate_ai_images(title, clean_content, image_config, target.parent, stem_name)
        if image_paths:
            image_source = "ai"

    if not image_paths and fallback_to_source:
        image_paths = _download_images(image_urls or [], target.parent, stem_name)
        if image_paths:
            image_source = "source"

    final_content = _inject_images_into_markdown(clean_content, image_paths)
    target.write_text(final_content, encoding="utf-8")
    return str(target), len(image_paths), image_source, final_content
