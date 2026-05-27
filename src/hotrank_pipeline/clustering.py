from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from difflib import SequenceMatcher

import jieba

from .models import TopicCluster, TopicClusterMember


STOPWORDS = {
    "怎么",
    "如何",
    "为何",
    "为什么",
    "一个",
    "我们",
    "你们",
    "他们",
    "这个",
    "那个",
    "已经",
    "正在",
    "还是",
    "没有",
    "是否",
    "什么",
    "这样",
    "那样",
    "今日",
    "热榜",
    "热搜",
    "头条",
    "知乎",
    "微博",
    "微信",
    "百度",
}


def normalize_for_tokens(text: str) -> str:
    value = re.sub(r"[#＃“”\"'《》【】\[\]（）()，。！？：；、/\\|]+", " ", text)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def tokenize_title(title: str) -> list[str]:
    text = normalize_for_tokens(title)
    tokens = []
    for token in jieba.cut(text, cut_all=False):
        token = token.strip()
        if not token or token in STOPWORDS:
            continue
        if len(token) == 1 and not token.isdigit():
            continue
        tokens.append(token)
    return list(dict.fromkeys(tokens))


def parse_hot_value(raw: str) -> float:
    if not raw:
        return 0.0
    text = raw.replace("热度", "").replace(" ", "")
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)", text)
    if not match:
        return 0.0
    value = float(match.group(1))
    if "亿" in text:
        value *= 100000000
    elif "万" in text:
        value *= 10000
    return value


def base_signal(item: dict, board_weights: dict[str, float]) -> float:
    board_weight = float(board_weights.get(item["board_name"], 1.0))
    rank_score = max(0.15, (60 - min(item["rank_num"], 60)) / 60)
    hot_score = math.log10(parse_hot_value(item["hot_value_raw"]) + 1)
    return board_weight * (rank_score * 10 + hot_score)


def similarity(a: dict, b: dict, min_shared_tokens: int) -> float:
    if a["normalized_title"] == b["normalized_title"]:
        return 1.0

    token_a = set(a["tokens"])
    token_b = set(b["tokens"])
    if not token_a or not token_b:
        return SequenceMatcher(None, a["normalized_title"], b["normalized_title"]).ratio()

    shared = token_a & token_b
    union = token_a | token_b
    jaccard = len(shared) / max(1, len(union))
    seq_ratio = SequenceMatcher(None, a["normalized_title"], b["normalized_title"]).ratio()

    if len(shared) >= min_shared_tokens:
        return max(jaccard, seq_ratio)
    return max(seq_ratio * 0.9, jaccard * 0.8)


def should_link(
    a: dict,
    b: dict,
    jaccard_threshold: float,
    sequence_threshold: float,
    min_shared_tokens: int,
) -> tuple[bool, float]:
    token_a = set(a["tokens"])
    token_b = set(b["tokens"])
    shared = token_a & token_b
    union = token_a | token_b
    jaccard = len(shared) / max(1, len(union)) if union else 0.0
    seq_ratio = SequenceMatcher(None, a["normalized_title"], b["normalized_title"]).ratio()
    score = similarity(a, b, min_shared_tokens)

    linked = (
        a["normalized_title"] == b["normalized_title"]
        or seq_ratio >= sequence_threshold
        or (jaccard >= jaccard_threshold and len(shared) >= min_shared_tokens)
        or (len(shared) >= 3 and score >= 0.50)
    )
    return linked, score


class UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, x: int, y: int) -> None:
        rx, ry = self.find(x), self.find(y)
        if rx != ry:
            self.parent[ry] = rx


def build_clusters(items: list[dict], runtime_config: dict) -> list[TopicCluster]:
    cluster_cfg = runtime_config.get("cluster", {})
    board_weights = runtime_config.get("board_weights", {})
    jaccard_threshold = float(cluster_cfg.get("jaccard_threshold", 0.42))
    sequence_threshold = float(cluster_cfg.get("sequence_threshold", 0.68))
    min_shared_tokens = int(cluster_cfg.get("min_shared_tokens", 2))

    enriched_items: list[dict] = []
    for item in items:
        token_list = tokenize_title(item["title"])
        candidate = dict(item)
        candidate["tokens"] = token_list
        candidate["signal_score"] = base_signal(candidate, board_weights)
        enriched_items.append(candidate)

    uf = UnionFind(len(enriched_items))
    pair_scores: dict[tuple[int, int], float] = {}

    for i in range(len(enriched_items)):
        for j in range(i + 1, len(enriched_items)):
            linked, score = should_link(
                enriched_items[i],
                enriched_items[j],
                jaccard_threshold=jaccard_threshold,
                sequence_threshold=sequence_threshold,
                min_shared_tokens=min_shared_tokens,
            )
            if linked:
                uf.union(i, j)
                pair_scores[(i, j)] = score

    groups: dict[int, list[int]] = {}
    for idx in range(len(enriched_items)):
        root = uf.find(idx)
        groups.setdefault(root, []).append(idx)

    clusters: list[TopicCluster] = []
    for member_indexes in groups.values():
        member_items = [enriched_items[idx] for idx in member_indexes]
        member_items.sort(key=lambda item: (-item["signal_score"], item["rank_num"], item["title"]))
        canonical = member_items[0]
        titles = [item["title"] for item in member_items]
        title_counter = Counter(titles)
        summary = "；".join([title for title, _ in title_counter.most_common(4)])

        members: list[TopicClusterMember] = []
        for item in member_items:
            match_score = 1.0 if item["item_id"] == canonical["item_id"] else 0.0
            for other in member_items:
                if other["item_id"] == canonical["item_id"]:
                    continue
                if item["item_id"] == other["item_id"]:
                    continue
                pair = tuple(sorted((member_indexes[member_items.index(item)], member_indexes[member_items.index(other)])))
                match_score = max(match_score, pair_scores.get(pair, 0.0))
            members.append(
                TopicClusterMember(
                    item_id=item["item_id"],
                    board_name=item["board_name"],
                    rank_num=item["rank_num"],
                    title=item["title"],
                    normalized_title=item["normalized_title"],
                    hot_value_raw=item["hot_value_raw"],
                    source_url=item["source_url"],
                    signal_score=float(item["signal_score"]),
                    match_score=float(match_score),
                    token_list=item["tokens"],
                )
            )

        cluster_key = hashlib.sha1(
            "|".join(str(member.item_id) for member in members).encode("utf-8")
        ).hexdigest()
        signal_score = sum(member.signal_score for member in members)

        clusters.append(
            TopicCluster(
                cluster_key=cluster_key,
                canonical_title=canonical["title"],
                cluster_summary=summary,
                signal_score=signal_score,
                members=members,
            )
        )

    clusters.sort(key=lambda cluster: (-cluster.signal_score, -len(cluster.members), cluster.canonical_title))
    return clusters
