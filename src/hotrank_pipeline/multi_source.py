from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup

from .config import Settings
from .models import BoardCard, BoardItem, ScrapeResult
from .tophub import REQUEST_HEADERS, normalize_title


DEFAULT_MULTI_SOURCE_CONFIG = {
    "enabled": True,
    "include_tophub": True,
    "dailyhot_base_url": "",
    "dailyhot_routes": [
        "weibo",
        "zhihu",
        "baidu",
        "bilibili",
        "36kr",
        "huxiu",
        "juejin",
        "v2ex",
        "hellogithub",
    ],
    "rss_feeds": [],
    "max_items_per_board": 30,
}


DAILYHOT_BOARD_LABELS = {
    "weibo": "微博",
    "zhihu": "知乎",
    "baidu": "百度",
    "bilibili": "B站",
    "36kr": "36氪",
    "huxiu": "虎嗅",
    "juejin": "掘金",
    "v2ex": "V2EX",
    "hellogithub": "HelloGitHub",
    "ithome": "IT之家",
    "sspai": "少数派",
    "douyin": "抖音",
    "toutiao": "今日头条",
    "douban": "豆瓣",
    "github": "GitHub",
}


def merged_multi_source_config(runtime_config: dict[str, Any]) -> dict[str, Any]:
    config = dict(DEFAULT_MULTI_SOURCE_CONFIG)
    configured = runtime_config.get("content_sources") or runtime_config.get("multi_sources") or {}
    if isinstance(configured, dict):
        config.update(configured)
    config["dailyhot_routes"] = _coerce_list(config.get("dailyhot_routes"))
    config["rss_feeds"] = _normalize_rss_feeds(config.get("rss_feeds"))
    config["max_items_per_board"] = max(1, int(config.get("max_items_per_board") or 30))
    return config


def parse_lines(value: str) -> list[str]:
    output: list[str] = []
    for part in re.split(r"[\n,，]+", value or ""):
        item = part.strip()
        if item:
            output.append(item)
    return output


def parse_rss_feed_lines(value: str) -> list[dict[str, str]]:
    feeds: list[dict[str, str]] = []
    for line in (value or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name = ""
        url = line
        if "|" in line:
            left, right = line.split("|", 1)
            name = left.strip()
            url = right.strip()
        if url:
            feeds.append({"name": name, "url": url})
    return feeds


def rss_feeds_to_text(feeds: list[dict[str, str]]) -> str:
    lines = []
    for feed in feeds or []:
        name = (feed.get("name") or "").strip()
        url = (feed.get("url") or "").strip()
        if not url:
            continue
        lines.append(f"{name}|{url}" if name else url)
    return "\n".join(lines)


def _coerce_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return parse_lines(value)
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _normalize_rss_feeds(value: Any) -> list[dict[str, str]]:
    if isinstance(value, str):
        return parse_rss_feed_lines(value)
    feeds: list[dict[str, str]] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                feeds.extend(parse_rss_feed_lines(item))
            elif isinstance(item, dict):
                url = str(item.get("url") or "").strip()
                name = str(item.get("name") or "").strip()
                if url:
                    feeds.append({"name": name, "url": url})
    return feeds


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    return ""


def _strip_html(text: str) -> str:
    if not text:
        return ""
    soup = BeautifulSoup(text, "html.parser")
    value = soup.get_text(" ", strip=True)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _extract_by_path(data: Any, path: str) -> Any:
    current = data
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _find_first_list(data: Any) -> list[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for path in ("data", "data.items", "data.list", "result", "result.list", "items", "list"):
            value = _extract_by_path(data, path)
            if isinstance(value, list):
                return value
        for value in data.values():
            found = _find_first_list(value)
            if found:
                return found
    return []


def _first_value(item: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, dict):
            for nested_key in ("text", "title", "name", "value"):
                nested_value = _safe_text(value.get(nested_key))
                if nested_value:
                    return nested_value
            continue
        text = _safe_text(value)
        if text:
            return text
    return ""


def _dailyhot_url(base_url: str, route: str) -> str:
    base = (base_url or "").rstrip("/")
    if not base:
        return ""
    route = route.strip().strip("/")
    if not route:
        return ""
    if base.endswith("/api"):
        return f"{base}/{route}"
    return f"{base}/{route}"


def _build_json_board(
    route: str,
    url: str,
    payload: Any,
    max_items: int,
) -> BoardCard:
    records = _find_first_list(payload)
    board_name = DAILYHOT_BOARD_LABELS.get(route.lower(), route)
    items: list[BoardItem] = []

    for index, record in enumerate(records[:max_items], start=1):
        if not isinstance(record, dict):
            continue
        title = _strip_html(
            _first_value(record, ("title", "name", "word", "desc", "text", "hotword", "keyword"))
        )
        if not title:
            continue
        source_url = _first_value(
            record,
            ("url", "link", "mobilUrl", "mobileUrl", "pcUrl", "href", "sourceUrl", "articleUrl"),
        )
        hot = _first_value(record, ("hot", "hotValue", "views", "score", "desc", "index", "rank"))
        item_id = _first_value(record, ("id", "uid", "key", "hash"))
        if not source_url:
            source_url = url
        items.append(
            BoardItem(
                rank_num=index,
                title=title,
                normalized_title=normalize_title(title),
                hot_value_raw=hot,
                source_url=source_url,
                source_item_id=item_id or hashlib.sha1(f"{route}|{title}".encode("utf-8")).hexdigest()[:16],
                raw_text=_strip_html(json.dumps(record, ensure_ascii=False))[:1000],
            )
        )

    return BoardCard(
        tophub_node_id=f"dailyhot:{route}",
        board_name=board_name,
        board_type="dailyhot",
        board_url=url,
        updated_text=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        items=items,
    )


def fetch_dailyhot_boards(config: dict[str, Any], timeout_seconds: int) -> list[BoardCard]:
    base_url = (config.get("dailyhot_base_url") or "").strip()
    routes = _coerce_list(config.get("dailyhot_routes"))
    if not base_url or not routes:
        return []

    boards: list[BoardCard] = []
    max_items = int(config.get("max_items_per_board") or 30)
    for route in routes:
        url = _dailyhot_url(base_url, route)
        if not url:
            continue
        try:
            response = requests.get(url, headers=REQUEST_HEADERS, timeout=timeout_seconds)
            response.raise_for_status()
            payload = response.json()
        except Exception:
            continue
        board = _build_json_board(route, url, payload, max_items=max_items)
        if board.items:
            boards.append(board)
    return boards


def _xml_text(node: ET.Element, tag: str) -> str:
    child = node.find(tag)
    if child is None or child.text is None:
        return ""
    return child.text.strip()


def _rss_entry_text(entry: ET.Element, names: tuple[str, ...]) -> str:
    lowered_names = {name.lower() for name in names}
    for name in names:
        value = _xml_text(entry, name)
        if value:
            return value
    for child in list(entry):
        local_name = child.tag.rsplit("}", 1)[-1].lower()
        if local_name in lowered_names and child.text:
            return child.text.strip()
    return ""


def _rss_entry_link(entry: ET.Element) -> str:
    link = _rss_entry_text(entry, ("link",))
    if link:
        return link
    for child in list(entry):
        if child.tag.rsplit("}", 1)[-1].lower() == "link":
            href = child.attrib.get("href")
            if href:
                return href.strip()
    return ""


def _rss_updated_text(entry: ET.Element) -> str:
    value = _rss_entry_text(entry, ("pubDate", "published", "updated", "date"))
    if not value:
        return ""
    try:
        return parsedate_to_datetime(value).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return value


def _parse_rss_board(feed: dict[str, str], xml_text: str, max_items: int) -> BoardCard:
    url = (feed.get("url") or "").strip()
    configured_name = (feed.get("name") or "").strip()
    xml_text = re.sub(r"^\s*<\?xml[^>]+encoding=['\"][^'\"]+['\"][^>]*\?>", "", xml_text, count=1, flags=re.I)
    root = ET.fromstring(xml_text.encode("utf-8"))
    channel = root.find("channel")
    entries = []
    if channel is not None:
        entries = list(channel.findall("item"))
        feed_title = _xml_text(channel, "title")
    else:
        entries = [node for node in list(root) if node.tag.rsplit("}", 1)[-1].lower() == "entry"]
        feed_title = _xml_text(root, "title")
    board_name = configured_name or feed_title or url

    items: list[BoardItem] = []
    for index, entry in enumerate(entries[:max_items], start=1):
        title = _strip_html(_rss_entry_text(entry, ("title",)))
        if not title:
            continue
        link = _rss_entry_link(entry) or url
        summary = _strip_html(_rss_entry_text(entry, ("description", "summary", "content", "encoded")))
        guid = _rss_entry_text(entry, ("guid", "id")) or hashlib.sha1(f"{url}|{title}".encode("utf-8")).hexdigest()[:16]
        items.append(
            BoardItem(
                rank_num=index,
                title=title,
                normalized_title=normalize_title(title),
                hot_value_raw="",
                source_url=urljoin(url, link),
                source_item_id=guid,
                raw_text=summary or title,
            )
        )

    return BoardCard(
        tophub_node_id=f"rss:{hashlib.sha1(url.encode('utf-8')).hexdigest()[:12]}",
        board_name=board_name,
        board_type="rss",
        board_url=url,
        updated_text=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        items=items,
    )


def fetch_rss_boards(config: dict[str, Any], timeout_seconds: int) -> list[BoardCard]:
    feeds = _normalize_rss_feeds(config.get("rss_feeds"))
    if not feeds:
        return []
    boards: list[BoardCard] = []
    max_items = int(config.get("max_items_per_board") or 30)
    for feed in feeds:
        url = (feed.get("url") or "").strip()
        if not url:
            continue
        try:
            response = requests.get(url, headers=REQUEST_HEADERS, timeout=timeout_seconds)
            response.raise_for_status()
            response.encoding = response.encoding or "utf-8"
            board = _parse_rss_board(feed, response.text, max_items=max_items)
        except Exception:
            continue
        if board.items:
            boards.append(board)
    return boards


def _save_raw_payload(raw_dir: str, source_name: str, payload: str) -> tuple[str, str]:
    now = datetime.now().strftime("%Y%m%d_%H%M%S")
    sha256 = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    target_dir = Path(raw_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_source = re.sub(r"[^a-zA-Z0-9_-]+", "_", source_name).strip("_") or "sources"
    path = target_dir / f"{safe_source}_{now}.json"
    path.write_text(payload, encoding="utf-8")
    return str(path), sha256


def scrape_configured_sources(settings: Settings, runtime_config: dict[str, Any]) -> ScrapeResult:
    config = merged_multi_source_config(runtime_config)
    boards: list[BoardCard] = []
    boards.extend(fetch_dailyhot_boards(config, timeout_seconds=settings.request_timeout_seconds))
    boards.extend(fetch_rss_boards(config, timeout_seconds=settings.request_timeout_seconds))

    payload = json.dumps(
        [
            {
                "board_name": board.board_name,
                "board_type": board.board_type,
                "board_url": board.board_url,
                "item_count": len(board.items),
            }
            for board in boards
        ],
        ensure_ascii=False,
        indent=2,
    )
    raw_path, sha256 = _save_raw_payload(settings.raw_dir, "multi_sources", payload)
    page_url = "multi://configured-sources"
    return ScrapeResult(
        source_name="multi_sources",
        page_category="configured",
        page_url=page_url,
        status_code=200,
        raw_html_path=raw_path,
        html_sha256=sha256,
        boards=boards,
    )
