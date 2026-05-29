from __future__ import annotations

import hashlib
import re
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from readability import Document

from .content_filters import is_blocked_image_context, is_blocked_source_image_url
from .models import ArticleFetchResult


REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/136.0.0.0 Safari/537.36"
    )
}


def _clean_text(text: str) -> str:
    value = re.sub(r"\s+", " ", text or "")
    return value.strip()


def _first_meta(soup: BeautifulSoup, *keys: tuple[str, str]) -> str:
    for attr, key in keys:
        node = soup.find("meta", attrs={attr: key})
        if node and node.get("content"):
            return _clean_text(node.get("content"))
    return ""


def _extract_with_readability(html: str) -> str:
    try:
        summary_html = Document(html).summary()
    except Exception:
        return ""
    soup = BeautifulSoup(summary_html, "html.parser")
    texts = []
    for tag in soup.find_all(["p", "h1", "h2", "h3", "li"]):
        text = _clean_text(tag.get_text(" ", strip=True))
        if len(text) >= 12:
            texts.append(text)
    return "\n".join(texts)


def _extract_thepaper_text(soup: BeautifulSoup) -> str:
    paragraphs = []
    for p in soup.find_all("p"):
        text = _clean_text(p.get_text(" ", strip=True))
        if len(text) >= 18:
            paragraphs.append(text)
    return "\n".join(paragraphs)


def _extract_search_page_summary(soup: BeautifulSoup) -> str:
    chunks = []
    for tag in soup.find_all(["title", "h1", "h2", "h3", "a", "p"]):
        text = _clean_text(tag.get_text(" ", strip=True))
        if 18 <= len(text) <= 120:
            chunks.append(text)
        if len(chunks) >= 10:
            break
    return "\n".join(dict.fromkeys(chunks))


def _is_probable_content_image(url: str) -> bool:
    lowered = url.lower()
    if not url or lowered.startswith("data:"):
        return False
    if is_blocked_source_image_url(url):
        return False
    if any(
        token in lowered
        for token in [
            "logo",
            "avatar",
            "icon",
            "emoji",
            "qrcode",
            "qr_code",
            "sprite",
            "/_next/static/",
            "pp_report",
            "defaultimg",
            "placeholder",
        ]
    ):
        return False
    if lowered.endswith(".svg"):
        return False
    return True


def _collect_image_urls(soup: BeautifulSoup, base_url: str, max_images: int = 12) -> list[str]:
    candidates: list[str] = []

    def add_candidate(raw_url: str | None, context: str = "") -> None:
        if not raw_url:
            return
        if is_blocked_image_context(context):
            return
        full = urljoin(base_url, raw_url.strip())
        if not _is_probable_content_image(full):
            return
        if full not in candidates:
            candidates.append(full)

    for attr, key in [
        ("property", "og:image"),
        ("name", "twitter:image"),
        ("itemprop", "image"),
    ]:
        node = soup.find("meta", attrs={attr: key})
        if node and node.get("content"):
            add_candidate(node.get("content"), node.get("content") or "")

    for img in soup.find_all("img"):
        context = " ".join(
            str(img.get(field) or "")
            for field in ("alt", "title", "aria-label", "data-caption", "data-alt")
        )
        for field in ("src", "data-src", "data-original", "data-actualsrc", "srcset"):
            value = img.get(field)
            if not value:
                continue
            if field == "srcset":
                value = value.split(",")[0].strip().split(" ")[0]
            add_candidate(value, context)

    return candidates[:max_images]


def _filter_image_urls(
    image_urls: list[str],
    source_url: str,
    board_name: str,
    max_images: int = 8,
) -> list[str]:
    result: list[str] = []
    parsed_host = urlparse(source_url).netloc.lower()

    for url in image_urls:
        lowered = url.lower()
        if is_blocked_source_image_url(url):
            continue
        if board_name == "百度" and any(
            token in lowered
            for token in [
                "gips0.baidu.com",
                "gips1.baidu.com",
                "gips2.baidu.com",
                "t8.baidu.com/it/",
                "t9.baidu.com/it/",
                "gimg3.baidu.com/search/",
            ]
        ):
            continue
        if board_name == "今日头条" and any(token in lowered for token in ["searchpstatp.com", "/search/synthesis/"]):
            continue
        if parsed_host and parsed_host not in lowered and board_name in {"百度", "今日头条"}:
            continue
        if board_name == "澎湃" and any(token in lowered for token in ["/_next/static/", "pp_report"]):
            continue
        if lowered not in result:
            result.append(url)
        if len(result) >= max_images:
            break
    return result


def fetch_article(
    board_snapshot_item_id: int,
    board_name: str,
    source_url: str,
    timeout_seconds: int,
) -> ArticleFetchResult:
    parsed = urlparse(source_url)
    host = parsed.netloc.lower()

    try:
        response = requests.get(
            source_url,
            headers=REQUEST_HEADERS,
            timeout=timeout_seconds,
            allow_redirects=True,
        )
        response.encoding = response.encoding or "utf-8"
        html = response.text
        soup = BeautifulSoup(html, "html.parser")
        final_url = response.url
        content_type = response.headers.get("content-type", "")
        title = _clean_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
        summary = _first_meta(
            soup,
            ("name", "description"),
            ("property", "og:description"),
            ("name", "twitter:description"),
        )
        content_text = ""
        note = ""
        fetch_status = "fetched"
        image_urls: list[str] = []

        if "mp.weixin.qq.com" in host and "wappoc_appmsgcaptcha" in final_url:
            fetch_status = "blocked"
            note = "微信文章触发验证码页，未获取正文。"
        elif "baidu.com" in host and ("captcha" in final_url or "wappass.baidu.com" in final_url):
            fetch_status = "blocked"
            note = "百度搜索触发验证码页。"
        elif "thepaper.cn" in host:
            content_text = _extract_thepaper_text(soup) or _extract_with_readability(html)
            image_urls = _filter_image_urls(_collect_image_urls(soup, final_url), final_url, board_name)
        elif "toutiao.com" in host:
            content_text = _extract_search_page_summary(soup)
            image_urls = _filter_image_urls(_collect_image_urls(soup, final_url), final_url, board_name)
            note = "今日头条为搜索页，当前提取的是搜索摘要。"
        else:
            content_text = _extract_with_readability(html)
            image_urls = _filter_image_urls(_collect_image_urls(soup, final_url), final_url, board_name)

        if not content_text:
            content_text = _extract_search_page_summary(soup)
        if not image_urls and fetch_status == "fetched":
            image_urls = _filter_image_urls(_collect_image_urls(soup, final_url), final_url, board_name)

        if not summary and content_text:
            summary = _clean_text(content_text.splitlines()[0])[:220]

        if not title and summary:
            title = summary[:60]

        hash_source = "\n".join([title, summary, content_text]).strip()
        content_hash = hashlib.sha256(hash_source.encode("utf-8")).hexdigest() if hash_source else ""

        return ArticleFetchResult(
            board_snapshot_item_id=board_snapshot_item_id,
            board_name=board_name,
            source_url=source_url,
            source_host=host,
            final_url=final_url,
            fetch_status=fetch_status,
            http_status=response.status_code,
            content_type=content_type,
            title=title,
            summary=summary,
            content_text=content_text,
            content_hash=content_hash,
            lead_image_url=image_urls[0] if image_urls else "",
            image_urls=image_urls,
            note=note,
        )
    except Exception as exc:
        return ArticleFetchResult(
            board_snapshot_item_id=board_snapshot_item_id,
            board_name=board_name,
            source_url=source_url,
            source_host=host,
            final_url=source_url,
            fetch_status="error",
            http_status=None,
            content_type="",
            title="",
            summary="",
            content_text="",
            content_hash="",
            lead_image_url="",
            image_urls=[],
            note=str(exc),
        )
