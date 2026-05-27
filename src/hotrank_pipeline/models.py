from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class BoardItem:
    rank_num: int
    title: str
    normalized_title: str
    hot_value_raw: str
    source_url: str
    source_item_id: str | None
    raw_text: str


@dataclass(slots=True)
class BoardCard:
    tophub_node_id: str | None
    board_name: str
    board_type: str
    board_url: str
    updated_text: str
    items: list[BoardItem]


@dataclass(slots=True)
class ScrapeResult:
    source_name: str
    page_category: str
    page_url: str
    status_code: int
    raw_html_path: str
    html_sha256: str
    boards: list[BoardCard]


@dataclass(slots=True)
class TopicClusterMember:
    item_id: int
    board_name: str
    rank_num: int
    title: str
    normalized_title: str
    hot_value_raw: str
    source_url: str
    signal_score: float
    match_score: float
    token_list: list[str]


@dataclass(slots=True)
class TopicCluster:
    cluster_key: str
    canonical_title: str
    cluster_summary: str
    signal_score: float
    members: list[TopicClusterMember]


@dataclass(slots=True)
class ArticleFetchResult:
    board_snapshot_item_id: int
    board_name: str
    source_url: str
    source_host: str
    final_url: str
    fetch_status: str
    http_status: int | None
    content_type: str
    title: str
    summary: str
    content_text: str
    content_hash: str
    lead_image_url: str
    image_urls: list[str]
    note: str
