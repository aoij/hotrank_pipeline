from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .config import Settings
from .models import BoardCard, BoardItem, ScrapeResult


REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/136.0.0.0 Safari/537.36"
    )
}

BASE_URL = "https://tophub.today"


def normalize_title(text: str) -> str:
    value = unicodedata.normalize("NFKC", text)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def fetch_news_page(settings: Settings) -> tuple[str, int]:
    response = requests.get(
        settings.tophub_news_url,
        headers=REQUEST_HEADERS,
        timeout=settings.request_timeout_seconds,
    )
    response.raise_for_status()
    response.encoding = "utf-8"
    return response.text, response.status_code


def save_raw_html(raw_dir: str, html: str) -> tuple[str, str]:
    now = datetime.now().strftime("%Y%m%d_%H%M%S")
    html_sha256 = hashlib.sha256(html.encode("utf-8")).hexdigest()
    target_dir = Path(raw_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"tophub_news_{now}.html"
    path.write_text(html, encoding="utf-8")
    return str(path), html_sha256


def parse_news_page(page_url: str, html: str) -> list[BoardCard]:
    soup = BeautifulSoup(html, "html.parser")
    boards: list[BoardCard] = []

    for card in soup.select("div.cc-cd"):
        board_link = card.select_one(".cc-cd-ih .cc-cd-is a[href]")
        board_name_el = card.select_one(".cc-cd-lb")
        board_type_el = card.select_one(".cc-cd-sb-st")
        updated_text_el = card.select_one(".cc-cd-if .i-h")

        if not board_link or not board_name_el:
            continue

        board_name = board_name_el.get_text(" ", strip=True)
        board_type = board_type_el.get_text(" ", strip=True) if board_type_el else ""
        board_url = urljoin(page_url, board_link.get("href", ""))
        updated_text = updated_text_el.get_text(" ", strip=True) if updated_text_el else ""

        items: list[BoardItem] = []
        for item_link in card.select(".cc-cd-cb-l > a[href]"):
            rank_el = item_link.select_one(".s")
            title_el = item_link.select_one(".t")
            hot_el = item_link.select_one(".e")

            if not rank_el or not title_el:
                continue

            rank_text = rank_el.get_text(strip=True)
            try:
                rank_num = int(rank_text)
            except ValueError:
                continue

            title = title_el.get_text(" ", strip=True)
            items.append(
                BoardItem(
                    rank_num=rank_num,
                    title=title,
                    normalized_title=normalize_title(title),
                    hot_value_raw=hot_el.get_text(" ", strip=True) if hot_el else "",
                    source_url=item_link.get("href", "").strip(),
                    source_item_id=item_link.get("itemid"),
                    raw_text=item_link.get_text(" ", strip=True),
                )
            )

        boards.append(
            BoardCard(
                tophub_node_id=card.get("id"),
                board_name=board_name,
                board_type=board_type,
                board_url=board_url,
                updated_text=updated_text,
                items=items,
            )
        )

    return boards


def scrape_tophub_news(settings: Settings) -> ScrapeResult:
    html, status_code = fetch_news_page(settings)
    raw_html_path, html_sha256 = save_raw_html(settings.raw_dir, html)
    boards = parse_news_page(settings.tophub_news_url, html)
    return ScrapeResult(
        source_name="tophub",
        page_category="news",
        page_url=settings.tophub_news_url,
        status_code=status_code,
        raw_html_path=raw_html_path,
        html_sha256=html_sha256,
        boards=boards,
    )
