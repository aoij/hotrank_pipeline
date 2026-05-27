from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import requests


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

    prompt = f"""你是一名资深微信公众号编辑，请根据下面的热点聚类和来源摘要，写一篇可直接用于公众号发布前审核的图文初稿。

写作目标：
1. 成稿要像成熟公众号文章，适合直接进入人工润色/排版/发布环节。
2. 风格要克制、可信、流畅，有信息密度，也要有阅读节奏。
3. 不要写成新闻播报口吻，也不要写成“AI总结”“资料汇编”口吻。

输出要求：
1. 使用中文输出，格式为 Markdown。
2. 第一行必须是一级标题：# 标题
3. 结构固定为以下 5 个二级标题：
   - ## 导语
   - ## 事件脉络
   - ## 关键信息
   - ## 为什么值得关注
   - ## 结语
4. 每个小节尽量短段落表达，适合手机端阅读；避免整段过长。
5. 可适度提炼观点，但不能编造事实、数据、引语或未经来源支持的判断。
6. 如果信息不完整，请明确写“截至目前公开报道显示”或“目前公开信息有限”，不要硬补细节。
7. 标题要适合公众号传播，清晰、自然、有可读性，不要标题党，不要夸张煽动。
8. 结尾不要写“对此你怎么看”“欢迎留言”等强互动套话，要自然收束。
9. 不要输出代码块，不要输出“以下是文章”之类说明性前缀。

风格要求：
- 语言有温度，但不过度抒情
- 多用短句和短段
- 能帮助读者快速看懂事件、抓住重点、理解背后的公共讨论价值
- 保持事实与观点分层清晰

热点主题：{cluster['canonical_title']}
热点摘要：{cluster.get('cluster_summary', '')}
聚类成员数：{cluster.get('item_count', len(article_sources))}

来源材料：
{chr(10).join(source_lines)}
"""

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
    content = data["choices"][0]["message"]["content"].strip()

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

    for i, line in enumerate(lines):
        output.append(line)
        stripped = line.strip()
        if image_index >= len(image_paths):
            continue

        if i == 0 and stripped.startswith("# "):
            output.extend(["", f"![配图{image_index + 1}]({image_paths[image_index]})", ""])
            image_index += 1
            continue

        if stripped.startswith("## "):
            output.extend(["", f"![配图{image_index + 1}]({image_paths[image_index]})", ""])
            image_index += 1

    if image_index < len(image_paths):
        output.extend(["", "## 图集补充", ""])
        while image_index < len(image_paths):
            output.append(f"![配图{image_index + 1}]({image_paths[image_index]})")
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
    year_dir = Path(output_dir) / now.strftime("%Y")
    month_dir = year_dir / now.strftime("%Y-%m")
    month_dir.mkdir(parents=True, exist_ok=True)
    safe_title = sanitize_filename(title)
    filename = f"{now.strftime('%Y%m%d_%H%M%S')}_{safe_title}.md"
    target = month_dir / filename
    stem_name = target.stem
    image_paths = _download_images(image_urls or [], month_dir, stem_name)
    final_content = _inject_images_into_markdown(content_md, image_paths)
    try:
        target.write_text(final_content, encoding="utf-8")
    except PermissionError:
        fallback = month_dir / f"{now.strftime('%Y%m%d_%H%M%S')}_{safe_title[:32]}_{now.strftime('%f')}.md"
        fallback.write_text(final_content, encoding="utf-8")
        target = fallback
    return str(target), len(image_paths)
