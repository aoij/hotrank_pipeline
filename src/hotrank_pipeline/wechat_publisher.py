from __future__ import annotations

import base64
import json
import re
from html import escape
from io import BytesIO
from pathlib import Path
from typing import Any

import markdown
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageOps

from .config import Settings, load_runtime_config
from .db import fetch_draft_by_id, fetch_recent_drafts, mark_draft_wechat_uploaded


WECHAT_TITLE_CHAR_LIMIT = 64
MAX_DIGEST_CHARS = 0
DEFAULT_MAX_IMAGES = 4

WX_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',Arial,sans-serif"

ARTICLE_STYLE = (
    "margin:0 auto;padding:0;color:#1f2937;"
    f"font-family:{WX_FONT};font-size:16px;line-height:1.86;"
    "letter-spacing:.02em;word-break:break-word;box-sizing:border-box;"
)

PARAGRAPH_STYLE = "margin:0 0 16px;line-height:1.86;color:#1f2937;font-size:16px;text-align:left;"
H2_STYLE = "margin:32px 0 14px;padding:0;color:#111827;font-size:19px;line-height:1.55;font-weight:700;box-sizing:border-box;"
H3_STYLE = "margin:24px 0 10px;color:#111827;font-size:17px;line-height:1.6;font-weight:700;"
STRONG_STYLE = "font-weight:700;color:#111827;"
MUTED_STYLE = "color:#64748b;font-size:14px;line-height:1.7;"
IMG_STYLE = "display:block;width:100%;max-width:100%;height:auto;margin:18px auto;border-radius:8px;box-sizing:border-box;"
BLOCKQUOTE_STYLE = "margin:18px 0;padding:14px 16px;background:#f7f8fa;border-left:3px solid #cbd5e1;color:#475569;box-sizing:border-box;"


def _wechat_title(title: str, char_limit: int = WECHAT_TITLE_CHAR_LIMIT) -> str:
    value = re.sub(r"\s+", " ", (title or "").strip().strip("“”\"'"))
    if len(value) <= char_limit:
        return value
    return value[:char_limit].rstrip("，。！？、：；-— ") or value[:char_limit]


def _open_rgb_image(path: Path) -> Image.Image:
    with Image.open(path) as source:
        source = ImageOps.exif_transpose(source)
        if source.mode in {"RGBA", "LA"} or "transparency" in source.info:
            rgba = source.convert("RGBA")
            canvas = Image.new("RGB", rgba.size, "white")
            canvas.paste(rgba, mask=rgba.getchannel("A"))
            return canvas
        return source.convert("RGB")


def _jpeg_image_bytes(path: Path, max_size: tuple[int, int], quality: int = 88) -> bytes:
    with _open_rgb_image(path) as image:
        image.thumbnail(max_size)
        output = BytesIO()
        image.save(output, format="JPEG", quality=quality, optimize=True)
        return output.getvalue()


def _image_bytes_for_wechat(path: Path) -> tuple[str, str, bytes]:
    # 微信正文图片接口对部分 png/gif/webp 或异常编码图片会返回 invalid image format。
    # 统一用 Pillow 重编码成 RGB JPEG，避免公众号草稿插图上传失败。
    return f"{path.stem}.jpg", "image/jpeg", _jpeg_image_bytes(path, max_size=(1280, 1280), quality=88)


def _image_payload(path: Path, placeholder: str | None = None) -> dict[str, str]:
    filename, content_type, body = _image_bytes_for_wechat(path)
    return {
        "placeholder": placeholder,
        "filename": filename,
        "content_type": content_type,
        "content_base64": base64.b64encode(body).decode("ascii"),
    }


def _cover_payload(path: Path) -> dict[str, str]:
    with _open_rgb_image(path) as image:
        image.thumbnail((900, 500))
        canvas = Image.new("RGB", (900, 500), "#f8fafc")
        canvas.paste(image, ((900 - image.width) // 2, (500 - image.height) // 2))
        output = BytesIO()
        canvas.save(output, format="JPEG", quality=82, optimize=True)
    return {
        "filename": "cover.jpg",
        "content_type": "image/jpeg",
        "content_base64": base64.b64encode(output.getvalue()).decode("ascii"),
    }


def _fallback_cover_payload() -> dict[str, str]:
    image = Image.new("RGB", (900, 500), "#f8fafc")
    output = BytesIO()
    image.save(output, format="JPEG", quality=82, optimize=True)
    return {
        "filename": "cover.jpg",
        "content_type": "image/jpeg",
        "content_base64": base64.b64encode(output.getvalue()).decode("ascii"),
    }


def _strip_first_h1(soup: BeautifulSoup) -> None:
    h1 = soup.find("h1")
    if h1:
        h1.decompose()


def _text_with_inline_styles(node) -> str:
    parts: list[str] = []
    for child in getattr(node, "contents", []):
        if getattr(child, "name", None) is None:
            parts.append(escape(str(child)))
            continue
        if child.name == "strong":
            parts.append(f'<strong style="{STRONG_STYLE}">{_text_with_inline_styles(child)}</strong>')
        elif child.name == "em":
            parts.append(f'<span style="font-style:normal;color:#475569;">{_text_with_inline_styles(child)}</span>')
        elif child.name == "code":
            parts.append(f'<span style="font-family:Menlo,Consolas,monospace;background:#f1f5f9;border-radius:4px;padding:2px 4px;font-size:14px;">{_text_with_inline_styles(child)}</span>')
        elif child.name == "br":
            parts.append("<br/>")
        elif child.name == "a":
            parts.append(f'<span style="color:#2563eb;">{_text_with_inline_styles(child)}</span>')
        else:
            parts.append(_text_with_inline_styles(child))
    return "".join(parts)


def _paragraph_html(inner: str, style: str = PARAGRAPH_STYLE) -> str:
    inner = (inner or "").strip()
    if not inner:
        return ""
    return f'<p style="{style}">{inner}</p>'


def _heading_html(text: str, level: str = "h3") -> str:
    style = H2_STYLE if level == "h2" else H3_STYLE
    return f'<section style="{style}">{text}</section>'


def _blockquote_html(node) -> str:
    paragraphs = []
    for child in node.find_all(["p", "li"], recursive=False):
        text = _text_with_inline_styles(child).strip()
        if text:
            paragraphs.append(f'<p style="margin:0 0 8px;line-height:1.8;color:#475569;font-size:15px;">{text}</p>')
    if not paragraphs:
        text = _text_with_inline_styles(node).strip()
        if text:
            paragraphs.append(f'<p style="margin:0;line-height:1.8;color:#475569;font-size:15px;">{text}</p>')
    return f'<section style="{BLOCKQUOTE_STYLE}">{"".join(paragraphs)}</section>' if paragraphs else ""


def _list_html(node, ordered: bool = False) -> list[str]:
    rows: list[str] = []
    for index, li in enumerate(node.find_all("li", recursive=False), start=1):
        text = _text_with_inline_styles(li).strip()
        if not text:
            continue
        marker = f"{index}." if ordered else "•"
        rows.append(
            '<p style="margin:0 0 12px;line-height:1.85;color:#1f2937;font-size:16px;text-align:left;">'
            f'<span style="display:inline-block;width:1.8em;color:#2563eb;font-weight:700;">{marker}</span>'
            f'<span>{text}</span></p>'
        )
    return rows


def _table_html(node) -> str:
    rows: list[str] = []
    for tr in node.find_all("tr"):
        cells = [_text_with_inline_styles(cell).strip() for cell in tr.find_all(["th", "td"], recursive=False)]
        if cells:
            rows.append(" ｜ ".join(cell for cell in cells if cell))
    if not rows:
        return ""
    body = "<br/>".join(rows)
    return f'<section style="margin:18px 0;padding:12px 14px;background:#f8fafc;border-radius:8px;color:#334155;font-size:14px;line-height:1.8;">{body}</section>'


def _wechat_compatible_html(soup: BeautifulSoup) -> str:
    blocks: list[str] = []
    for node in list(soup.contents):
        name = getattr(node, "name", None)
        if name is None:
            text = escape(str(node).strip())
            if text:
                blocks.append(_paragraph_html(text))
            continue
        if name == "h1":
            continue
        if name == "h2":
            blocks.append(_heading_html(_text_with_inline_styles(node), "h2"))
        elif name == "h3":
            blocks.append(_heading_html(_text_with_inline_styles(node), "h3"))
        elif name in {"h4", "h5", "h6"}:
            blocks.append(_heading_html(_text_with_inline_styles(node), "h3"))
        elif name == "p":
            imgs = node.find_all("img", recursive=False)
            text_without_images = BeautifulSoup(str(node), "html.parser")
            for img in text_without_images.find_all("img"):
                img.decompose()
            text_part = _text_with_inline_styles(text_without_images).strip()
            if text_part:
                blocks.append(_paragraph_html(text_part))
            for img in imgs:
                src = (img.get("src") or "").strip()
                alt = escape((img.get("alt") or "文章配图").strip(), quote=True)
                if src:
                    blocks.append(f'<p style="margin:22px 0;text-align:center;"><img alt="{alt}" src="{src}" style="{IMG_STYLE}"/></p>')
        elif name == "img":
            src = (node.get("src") or "").strip()
            alt = escape((node.get("alt") or "文章配图").strip(), quote=True)
            if src:
                blocks.append(f'<p style="margin:22px 0;text-align:center;"><img alt="{alt}" src="{src}" style="{IMG_STYLE}"/></p>')
        elif name == "blockquote":
            html = _blockquote_html(node)
            if html:
                blocks.append(html)
        elif name == "ul":
            blocks.extend(_list_html(node, ordered=False))
        elif name == "ol":
            blocks.extend(_list_html(node, ordered=True))
        elif name == "hr":
            blocks.append('<section style="margin:28px 0;border-top:1px solid #e5e7eb;height:1px;line-height:1px;overflow:hidden;"></section>')
        elif name == "table":
            html = _table_html(node)
            if html:
                blocks.append(html)
        elif name == "pre":
            text = escape(node.get_text("\n", strip=True))
            if text:
                blocks.append(f'<section style="white-space:pre-wrap;word-break:break-word;background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:12px;margin:16px 0;font-size:14px;line-height:1.7;color:#334155;">{text}</section>')
        else:
            text = _text_with_inline_styles(node).strip()
            if text:
                blocks.append(_paragraph_html(text))
    return "".join(blocks)


def _read_markdown(path: Path) -> str:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        # 兼容少量从 Windows 工具另存的 GBK/GB18030 文档，避免推送到公众号时出现乱码。
        text = raw.decode("gb18030", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\ufeff", "")
    text = text.replace("\u00a0", " ")
    return text


def _markdown_to_payload_html(
    content_md: str,
    title: str,
    base_dir: Path,
    max_images: int = DEFAULT_MAX_IMAGES,
) -> tuple[str, list[dict[str, str]], Path]:
    content_md = re.sub(r"^#\s+.*$", f"# {title}", content_md or "", count=1, flags=re.M)
    rendered = markdown.markdown(
        content_md,
        extensions=["extra", "sane_lists", "tables", "nl2br", "fenced_code"],
        output_format="html5",
    )
    soup = BeautifulSoup(rendered, "html.parser")
    _strip_first_h1(soup)

    images: list[dict[str, str]] = []
    cover_path: Path | None = None
    for img in list(soup.find_all("img")):
        if len(images) >= max_images:
            img.decompose()
            continue
        src = (img.get("src") or "").strip()
        if not src or re.match(r"^(https?:)?//", src) or src.startswith("data:"):
            img.decompose()
            continue
        image_path = (base_dir / src).resolve()
        if not image_path.exists() or not image_path.is_file():
            img.decompose()
            continue
        if cover_path is None:
            cover_path = image_path
        placeholder = f"hotrank://image/{len(images) + 1:02d}"
        img["src"] = placeholder
        img["alt"] = img.get("alt") or f"文章配图{len(images) + 1}"
        images.append(_image_payload(image_path, placeholder=placeholder))

    article_html = f'<section style="{ARTICLE_STYLE}">{_wechat_compatible_html(soup)}</section>'
    return article_html, images, cover_path


def _gateway_config(settings: Settings) -> dict[str, Any]:
    runtime = load_runtime_config(settings)
    gateway = runtime.get("wechat_gateway") or {}
    return {
        "base_url": (gateway.get("base_url") or "http://106.12.11.147:18080").rstrip("/"),
        "token": gateway.get("token") or "",
        "timeout_seconds": int(gateway.get("timeout_seconds") or 240),
        "max_images": max(1, min(int(gateway.get("max_images") or DEFAULT_MAX_IMAGES), 8)),
    }


def publish_draft_to_wechat(settings: Settings, draft_id: int) -> dict[str, Any]:
    config = _gateway_config(settings)
    if not config["token"]:
        raise RuntimeError("local_settings.json 未配置 wechat_gateway.token")

    draft = fetch_draft_by_id(settings, draft_id)
    if not draft:
        raise RuntimeError(f"稿件不存在：draft_id={draft_id}")
    if draft.get("wechat_uploaded_at"):
        return {
            "draft_id": draft_id,
            "title": draft.get("title"),
            "wechat_title": _wechat_title(draft.get("title") or draft.get("canonical_title") or f"draft_id={draft_id}"),
            "review_score": float(draft.get("review_score") or 0),
            "image_count": 0,
            "media_id": draft.get("wechat_media_id"),
            "uploaded_image_count": 0,
            "already_uploaded": True,
            "uploaded_at_text": draft.get("wechat_uploaded_at_text") or "",
        }
    archive_path = Path(draft.get("archive_path") or "")
    if not archive_path.exists():
        raise RuntimeError(f"稿件归档文件不存在：{archive_path}")

    title = _wechat_title(draft.get("title") or draft.get("canonical_title") or archive_path.stem)
    content_md = _read_markdown(archive_path)
    content_html, images, cover_path = _markdown_to_payload_html(
        content_md=content_md,
        title=title,
        base_dir=archive_path.parent,
        max_images=config["max_images"],
    )
    payload: dict[str, Any] = {
        "title": title,
        "content_html": content_html,
        "cover_image": _cover_payload(cover_path) if cover_path else _fallback_cover_payload(),
        "images": images,
    }
    if MAX_DIGEST_CHARS > 0:
        payload["digest"] = ""

    response = requests.post(
        f"{config['base_url']}/api/wechat/drafts",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {config['token']}",
            "Content-Type": "application/json; charset=utf-8",
        },
        timeout=config["timeout_seconds"],
    )
    try:
        body = response.json()
    except Exception:
        body = {"raw": response.text[:500]}
    if not response.ok or not body.get("ok"):
        raise RuntimeError(body.get("detail") or body.get("raw") or response.text[:500] or f"HTTP {response.status_code}")

    mark_draft_wechat_uploaded(settings, draft_id, body.get("media_id"))

    return {
        "draft_id": draft_id,
        "title": draft.get("title"),
        "wechat_title": title,
        "review_score": float(draft.get("review_score") or 0),
        "image_count": len(images),
        "media_id": body.get("media_id"),
        "uploaded_image_count": body.get("uploaded_image_count"),
        "already_uploaded": False,
    }


def publish_recent_drafts_to_wechat(settings: Settings, limit: int = 10) -> dict[str, Any]:
    candidates = fetch_recent_drafts(settings, limit=max(1, min(limit * 6, 200)))
    published = []
    failed = []
    skipped = []
    for draft in candidates:
        if len(published) >= limit:
            break
        try:
            result = publish_draft_to_wechat(settings, int(draft["id"]))
            if result.get("already_uploaded"):
                skipped.append(result)
                continue
            published.append(result)
        except Exception as exc:
            failed.append({"draft_id": draft["id"], "title": draft["title"], "error": str(exc)})
    return {
        "requested": limit,
        "published_count": len(published),
        "skipped_count": len(skipped),
        "failed_count": len(failed),
        "published": published,
        "skipped": skipped,
        "failed": failed,
    }
