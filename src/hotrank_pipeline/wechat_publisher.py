from __future__ import annotations

import base64
import json
import re
from io import BytesIO
from pathlib import Path
from typing import Any

import markdown
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageOps

from .config import Settings, load_runtime_config
from .db import fetch_draft_by_id, fetch_recent_drafts


TITLE_BYTE_LIMIT = 30
MAX_DIGEST_CHARS = 0
DEFAULT_MAX_IMAGES = 4

ARTICLE_STYLE = (
    "color:#1f2937;"
    "font:16px/1.9 -apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;"
    "word-break:break-word;letter-spacing:.02em;box-sizing:border-box;"
)

STYLE_MAP = {
    "h1": "font-size:26px;line-height:1.45;margin:0 0 18px;color:#111827;font-weight:700;",
    "h2": (
        "font-size:22px;line-height:1.55;margin:30px 0 14px;"
        "padding-left:12px;border-left:4px solid #2563eb;color:#111827;font-weight:700;box-sizing:border-box;"
    ),
    "h3": "font-size:18px;line-height:1.55;margin:22px 0 10px;color:#111827;font-weight:700;",
    "p": "margin:14px 0;line-height:1.9;color:#1f2937;font-size:16px;text-align:justify;",
    "blockquote": (
        "margin:16px 0;padding:12px 14px;background:#f8fafc;"
        "border-left:4px solid #93c5fd;color:#475569;box-sizing:border-box;"
    ),
    "ul": "padding-left:22px;margin:14px 0;",
    "ol": "padding-left:22px;margin:14px 0;",
    "li": "margin:6px 0;line-height:1.8;",
    "strong": "font-weight:700;color:#111827;",
    "em": "font-style:normal;color:#475569;",
    "a": "color:#2563eb;text-decoration:none;border-bottom:1px solid #bfdbfe;",
    "hr": "border:0;border-top:1px solid #e5e7eb;margin:26px 0;",
    "code": "font-family:Menlo,Consolas,monospace;background:#f1f5f9;border-radius:4px;padding:2px 4px;font-size:14px;",
    "pre": "white-space:pre-wrap;word-break:break-word;background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:12px;margin:16px 0;font-size:14px;line-height:1.7;",
    "img": "display:block;width:100%;max-width:100%;height:auto;margin:18px auto;border-radius:12px;box-sizing:border-box;",
    "table": "width:100%;border-collapse:collapse;margin:18px 0;font-size:14px;box-sizing:border-box;",
    "th": "border:1px solid #dbe3ef;padding:8px 10px;",
    "td": "border:1px solid #dbe3ef;padding:8px 10px;",
}


def _short_title(title: str, byte_limit: int = TITLE_BYTE_LIMIT) -> str:
    value = re.sub(r"\s+", " ", (title or "").strip().strip("“”\"'"))
    replacements = [
        ("的人，后来都怎样了？", "会怎样？"),
        ("的人，后来都怎样了", "会怎样"),
        ("到底是不是大问题？", "是不是大问题？"),
        ("我们帮你快速捋清", "快速看懂"),
        ("（附应对方法）", ""),
    ]
    for old, new in replacements:
        value = value.replace(old, new)
    if len(value.encode("utf-8")) <= byte_limit:
        return value

    result = ""
    for char in value:
        candidate = result + char
        if len(candidate.encode("utf-8")) > byte_limit:
            break
        result = candidate
    return result.rstrip("，。！？、：；-— ") or value[:12]


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


def _inline_styles(soup: BeautifulSoup) -> None:
    for tag, style in STYLE_MAP.items():
        for element in soup.find_all(tag):
            element["style"] = style


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

    _inline_styles(soup)
    article_html = f'<section style="{ARTICLE_STYLE}">{soup.decode()}</section>'
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
    archive_path = Path(draft.get("archive_path") or "")
    if not archive_path.exists():
        raise RuntimeError(f"稿件归档文件不存在：{archive_path}")

    title = _short_title(draft.get("title") or draft.get("canonical_title") or archive_path.stem)
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

    return {
        "draft_id": draft_id,
        "title": draft.get("title"),
        "wechat_title": title,
        "review_score": float(draft.get("review_score") or 0),
        "image_count": len(images),
        "media_id": body.get("media_id"),
        "uploaded_image_count": body.get("uploaded_image_count"),
    }


def publish_recent_drafts_to_wechat(settings: Settings, limit: int = 10) -> dict[str, Any]:
    candidates = fetch_recent_drafts(settings, limit=max(1, limit * 3))
    published = []
    failed = []
    for draft in candidates:
        if len(published) >= limit:
            break
        try:
            published.append(publish_draft_to_wechat(settings, int(draft["id"])))
        except Exception as exc:
            failed.append({"draft_id": draft["id"], "title": draft["title"], "error": str(exc)})
    return {
        "requested": limit,
        "published_count": len(published),
        "failed_count": len(failed),
        "published": published,
        "failed": failed,
    }
