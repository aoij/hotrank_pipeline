from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import parse_qs, quote_plus, unquote, urlparse

import requests
from bs4 import BeautifulSoup
from charset_normalizer import from_bytes

from .content_filters import is_blocked_source_image_url, is_newslike_text
from .fetchers import REQUEST_HEADERS, fetch_article
from .models import ArticleFetchResult


PLATFORM_SEARCHERS = (
    {
        "name": "Bing",
        "board_name": "Bing灵感",
        "queries": ("{topic}", "{topic} 解读", "{topic} 普通人 影响"),
        "url": "https://cn.bing.com/search?q={query}&ensearch=0&setlang=zh-CN&mkt=zh-CN",
        "extractor": "bing",
        "no_proxy": True,
    },
    {
        "name": "百度",
        "board_name": "百度灵感",
        "queries": ("{topic}", "{topic} 是什么", "{topic} 怎么看"),
        "url": "https://m.baidu.com/s?word={query}",
        "extractor": "baidu",
        "no_proxy": True,
    },
    {
        "name": "知乎",
        "board_name": "知乎灵感",
        "queries": ("{topic}", "{topic} 真实体验", "{topic} 如何评价"),
        "url": "https://www.zhihu.com/search?type=content&q={query}",
        "extractor": "generic",
    },
    {
        "name": "微信",
        "board_name": "微信灵感",
        "queries": ("{topic} 公众号", "{topic} 科普", "{topic} 解读"),
        "url": "https://weixin.sogou.com/weixin?type=2&query={query}",
        "extractor": "sogou_weixin",
    },
    {
        "name": "DuckDuckGo",
        "board_name": "全网灵感",
        "queries": ("{topic}", "{topic} 解读", "{topic} 普通人 影响"),
        "url": "https://duckduckgo.com/html/?q={query}",
        "extractor": "duckduckgo",
    },
)


HOST_PRIORITY = (
    ("zhihu.com", 1),
    ("mp.weixin.qq.com", 2),
    ("sspai.com", 3),
    ("36kr.com", 4),
    ("huxiu.com", 5),
    ("juejin.cn", 6),
    ("bilibili.com", 7),
    ("toutiao.com", 8),
)


BLOCKED_RESULT_HOST_TOKENS = (
    "bing.com",
    "microsoft.com",
    "baidu.com/linksubmit",
    "passport.baidu.com",
    "wappass.baidu.com",
    "tieba.baidu.com",
    "map.baidu.com",
    "image.baidu.com",
    "baike.baidu.com",
    "www.sohu.com/404",
    "sohu.com/404",
    "h5.news.qq.com/static",
    "douyin.com/video",
    "douyin.com/user",
    "zhihu.com/signin",
    "zhihu.com/account",
    "sogou.com/web",
    "profile.zjurl.cn",
    "www.toutiao.com/c/user",
    "toutiao.com/c/user",
    "toutiao.com/w/user",
    "toutiao.com/search",
)


QUERY_NOISE_PATTERNS = (
    r"有人说",
    r"有人认为",
    r"你认同吗",
    r"你同意吗",
    r"你怎么看",
    r"怎么看",
    r"如何看待",
    r"为什么说",
    r"为什么",
    r"是什么",
    r"有没有",
    r"会不会",
    r"能不能",
    r"是不是",
    r"是否",
    r"真的",
    r"到底",
    r"吗",
    r"呢",
)


KEYWORD_HINTS = (
    "AI",
    "人工智能",
    "大模型",
    "搜索",
    "AI搜索",
    "传统搜索",
    "替代",
    "健康",
    "值钱",
    "隐私",
    "安全",
    "网络安全",
    "工具",
    "微信",
    "公众号",
    "普通人",
    "职场",
    "工作",
    "生活",
    "风险",
    "影响",
)


GENERIC_KEYWORDS = {
    "有人",
    "认同",
    "同意",
    "怎么",
    "如何",
    "看待",
    "为什么",
    "什么",
    "普通",
    "一个",
    "这个",
    "那个",
    "问题",
    "话题",
}


def _clean_text(text: str) -> str:
    value = text or ""
    value = _repair_mojibake(value)
    return re.sub(r"\s+", " ", value).strip()


def _looks_mojibake(text: str) -> bool:
    value = text or ""
    if not value:
        return False
    bad_score = sum(value.count(ch) for ch in ("Ã", "Â", "ä", "å", "æ", "ç", "è", "é", "ê", "ï", "½", "¿", "µ", "Í", "Ò", "É", "�"))
    cjk_score = sum("\u4e00" <= ch <= "\u9fff" for ch in value)
    return bad_score >= 2 and bad_score >= cjk_score * 0.2


def _topic_search_core(topic: str) -> str:
    value = _clean_text(topic)
    value = re.sub(r"[\"“”‘’《》<>#【】\[\]（）()]", " ", value)
    value = re.sub(r"[?？!！。；;：:，,、/|]+", " ", value)
    for pattern in QUERY_NOISE_PATTERNS:
        value = re.sub(pattern, " ", value, flags=re.I)
    value = re.sub(r"\s+", " ", value).strip()
    return value or _clean_text(topic)


def _build_platform_queries(topic: str, query_templates: tuple[str, ...]) -> list[str]:
    core = _topic_search_core(topic)
    bases = [core]
    if _clean_text(topic) != core:
        bases.append(_clean_text(topic))
    queries: list[str] = []
    for base in bases:
        for template in query_templates:
            query = _clean_text(str(template).format(topic=base))
            if query and query not in queries:
                queries.append(query)
    return queries


def _topic_keywords(topic: str) -> list[str]:
    core = _topic_search_core(topic)
    keywords: list[str] = []

    def add(keyword: str) -> None:
        value = keyword.strip()
        if len(value) < 2 or value.lower() in GENERIC_KEYWORDS or value in GENERIC_KEYWORDS:
            return
        if value not in keywords:
            keywords.append(value)

    for hint in KEYWORD_HINTS:
        if hint.lower() in core.lower():
            add(hint)

    for token in re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]+", core):
        if re.match(r"^[A-Za-z0-9]+$", token):
            add(token)
            continue
        if len(token) <= 4:
            add(token)
        else:
            add(token)
            for idx in range(0, len(token) - 1):
                add(token[idx : idx + 2])

    compact = re.sub(r"\s+", "", core)
    if "健康" in compact and "值钱" in compact:
        add("身体")
        add("身体价值")
        add("健康价值")
        add("医疗")
        add("保险")
    return keywords[:20]


def _normalize_url(url: str) -> str:
    value = (url or "").strip()
    if value.startswith("//"):
        value = f"https:{value}"
    if "duckduckgo.com/l/" in value:
        parsed = urlparse(value)
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        if target:
            value = unquote(target)
    if value.startswith("/"):
        return ""
    return value


def _repair_mojibake(text: str) -> str:
    value = text or ""
    if not _looks_mojibake(value):
        return value
    candidates: list[str] = []
    for source_encoding in ("latin1", "cp1252"):
        for target_encoding in ("utf-8", "gb18030"):
            try:
                candidates.append(value.encode(source_encoding, errors="ignore").decode(target_encoding, errors="ignore"))
            except Exception:
                continue
    candidates = [candidate for candidate in candidates if candidate.strip()]
    if not candidates:
        return value
    def quality(candidate: str) -> tuple[int, int, int]:
        common_score = sum(candidate.count(ch) for ch in "的一是在不了和有我人这中大为上个国健康搜索工具普通风险影响")
        cjk_score = sum("\u4e00" <= ch <= "\u9fff" for ch in candidate)
        bad_score = sum(candidate.count(ch) for ch in ("�", "", "", "绉", "戞", "櫘", "鐭", "瘑", "搴"))
        return common_score * 10 + cjk_score - bad_score * 20, cjk_score, -bad_score

    return max(candidates, key=quality)


def _decode_response_text(response: requests.Response) -> str:
    declared = (response.encoding or "").lower()
    if declared and declared not in {"iso-8859-1", "ascii"}:
        text = response.text
        if not _looks_mojibake(text):
            return text
    try:
        detected = from_bytes(response.content).best()
        if detected:
            text = str(detected)
            if text and not _looks_mojibake(text):
                return text
    except Exception:
        pass
    response.encoding = response.apparent_encoding or response.encoding or "utf-8"
    return response.text


def _repair_fetch_result_text(fetched: ArticleFetchResult) -> ArticleFetchResult:
    title = _repair_mojibake(fetched.title)
    summary = _repair_mojibake(fetched.summary)
    content_text = _repair_mojibake(fetched.content_text)
    if title == fetched.title and summary == fetched.summary and content_text == fetched.content_text:
        return fetched
    return ArticleFetchResult(
        board_snapshot_item_id=fetched.board_snapshot_item_id,
        board_name=fetched.board_name,
        source_url=fetched.source_url,
        source_host=fetched.source_host,
        final_url=fetched.final_url,
        fetch_status=fetched.fetch_status,
        http_status=fetched.http_status,
        content_type=fetched.content_type,
        title=title,
        summary=summary,
        content_text=content_text,
        content_hash=fetched.content_hash,
        lead_image_url=fetched.lead_image_url,
        image_urls=fetched.image_urls,
        note=fetched.note,
    )


def _is_http_url(url: str) -> bool:
    return bool(re.match(r"^https?://", url or "", re.I))


def _result_allowed(url: str, title: str = "") -> bool:
    url = _normalize_url(url)
    if not _is_http_url(url) or not title:
        return False
    lowered = url.lower()
    if any(token in lowered for token in BLOCKED_RESULT_HOST_TOKENS):
        return False
    if re.search(r"\.(?:jpg|jpeg|png|gif|webp|svg|pdf|zip|rar)(?:$|\?)", lowered):
        return False
    if len(title.strip()) < 6:
        return False
    if re.fullmatch(r"(?:\d+\s*)?(?:小时前|分钟前|昨天|前天|\d{1,2}:\d{2})", title.strip()):
        return False
    if "404" in title or "访问的页面找不到" in title or "页面不存在" in title:
        return False
    if title.startswith("知乎，让每一次点击"):
        return False
    return True


def _add_result(results: list[dict], seen: set[str], title: str, url: str, summary: str, platform: str) -> None:
    url = _normalize_url(url)
    title = _clean_text(title)
    summary = _clean_text(summary)
    if not _result_allowed(url, title):
        return
    if url in seen:
        return
    seen.add(url)
    results.append(
        {
            "title": title[:160],
            "url": url,
            "summary": summary[:260],
            "platform": platform,
            "source_host": urlparse(url).netloc.lower(),
        }
    )


def _extract_bing_results(html_text: str, platform: str, limit: int = 8) -> list[dict]:
    soup = BeautifulSoup(html_text or "", "html.parser")
    results: list[dict] = []
    seen: set[str] = set()
    for item in soup.select("li.b_algo"):
        link = item.select_one("h2 a[href]") or item.select_one("a[href]")
        if not link:
            continue
        snippet_node = item.select_one(".b_caption p") or item.select_one("p")
        _add_result(
            results,
            seen,
            link.get_text(" ", strip=True),
            link.get("href") or "",
            snippet_node.get_text(" ", strip=True) if snippet_node else "",
            platform,
        )
        if len(results) >= limit:
            break
    return results


def _extract_baidu_results(html_text: str, platform: str, limit: int = 8) -> list[dict]:
    soup = BeautifulSoup(html_text or "", "html.parser")
    results: list[dict] = []
    seen: set[str] = set()
    selectors = [
        "div.result",
        "div.c-container",
        "div[class*=result]",
        "article",
    ]
    containers = []
    for selector in selectors:
        containers = soup.select(selector)
        if containers:
            break
    if not containers:
        containers = soup.select("div.c-result, div.result-op, div.new-result, div[data-log]")
    if not containers:
        containers = soup.find_all(["div", "article"], limit=120)
    for item in containers:
        link = item.select_one("h3 a[href], a.c-title[href], a[class*=title][href]") or item.select_one("a[href]")
        if not link:
            continue
        snippet_node = (
            item.select_one(".c-abstract")
            or item.select_one(".c-line-clamp")
            or item.select_one("[class*=abstract]")
            or item.select_one("[class*=summary]")
            or item.select_one(".content-right_8Zs40")
            or item.select_one("span")
            or item
        )
        _add_result(
            results,
            seen,
            link.get_text(" ", strip=True),
            link.get("href") or "",
            snippet_node.get_text(" ", strip=True) if snippet_node else "",
            platform,
        )
        if len(results) >= limit:
            break
    return results


def _extract_duckduckgo_results(html_text: str, platform: str, limit: int = 8) -> list[dict]:
    soup = BeautifulSoup(html_text or "", "html.parser")
    results: list[dict] = []
    seen: set[str] = set()
    containers = soup.select(".result, .web-result")
    if not containers:
        containers = soup.find_all(["div", "article"], limit=80)
    for item in containers:
        link = item.select_one("a.result__a[href]") or item.select_one("a[href]")
        if not link:
            continue
        snippet_node = item.select_one(".result__snippet") or item.select_one("a.result__snippet") or item.select_one(".snippet")
        _add_result(
            results,
            seen,
            link.get_text(" ", strip=True),
            link.get("href") or "",
            snippet_node.get_text(" ", strip=True) if snippet_node else "",
            platform,
        )
        if len(results) >= limit:
            break
    return results


def _extract_sogou_weixin_results(html_text: str, platform: str, limit: int = 8) -> list[dict]:
    soup = BeautifulSoup(html_text or "", "html.parser")
    results: list[dict] = []
    seen: set[str] = set()
    containers = soup.select("li[id^=sogou_vr_], .news-box li, .txt-box")
    if not containers:
        containers = soup.find_all(["li", "div"], limit=50)
    for item in containers:
        link = item.select_one("h3 a[href]") or item.select_one("a[href]")
        if not link:
            continue
        snippet_node = item.select_one(".txt-info") or item.select_one("p")
        _add_result(
            results,
            seen,
            link.get_text(" ", strip=True),
            link.get("href") or "",
            snippet_node.get_text(" ", strip=True) if snippet_node else "",
            platform,
        )
        if len(results) >= limit:
            break
    return results


def _extract_generic_results(html_text: str, platform: str, limit: int = 8) -> list[dict]:
    soup = BeautifulSoup(html_text or "", "html.parser")
    results: list[dict] = []
    seen: set[str] = set()
    for link in soup.select("a[href]"):
        title = _clean_text(link.get_text(" ", strip=True))
        if len(title) < 6:
            continue
        parent = link.find_parent(["article", "section", "div", "li"]) or link
        summary = ""
        if parent is not link:
            summary = _clean_text(parent.get_text(" ", strip=True))
            summary = summary.replace(title, "", 1).strip()
        _add_result(results, seen, title, link.get("href") or "", summary, platform)
        if len(results) >= limit:
            break
    return results


def _extract_search_results(html_text: str, extractor: str, platform: str, limit: int) -> list[dict]:
    if extractor == "bing":
        return _extract_bing_results(html_text, platform=platform, limit=limit)
    if extractor == "baidu":
        return _extract_baidu_results(html_text, platform=platform, limit=limit)
    if extractor == "sogou_weixin":
        return _extract_sogou_weixin_results(html_text, platform=platform, limit=limit)
    if extractor == "duckduckgo":
        return _extract_duckduckgo_results(html_text, platform=platform, limit=limit)
    return _extract_generic_results(html_text, platform=platform, limit=limit)


def _http_session(no_proxy: bool = False) -> requests.Session | requests:
    if not no_proxy:
        return requests
    session = requests.Session()
    session.trust_env = False
    return session


def _search_one_platform(
    topic: str,
    searcher: dict,
    per_platform_limit: int,
    timeout_seconds: int,
) -> tuple[str, list[dict], str]:
    platform = str(searcher["name"])
    results: list[dict] = []
    seen: set[str] = set()
    for query in _build_platform_queries(topic, tuple(searcher.get("queries") or ("{topic}",))):
        if len(results) >= per_platform_limit:
            break
        url = str(searcher["url"]).format(query=quote_plus(query))
        try:
            client = _http_session(no_proxy=bool(searcher.get("no_proxy")))
            response = client.get(url, headers=REQUEST_HEADERS, timeout=timeout_seconds, allow_redirects=True)
            response.raise_for_status()
        except Exception as exc:
            if results:
                continue
            return platform, results, str(exc)

        for item in _extract_search_results(
            response.text,
            extractor=str(searcher.get("extractor") or "generic"),
            platform=platform,
            limit=per_platform_limit,
        ):
            if item["url"] in seen:
                continue
            if not _has_topic_signal(topic, item):
                continue
            seen.add(item["url"])
            results.append(item)
            if len(results) >= per_platform_limit:
                break
    return platform, results, ""


def _host_priority(url: str) -> int:
    host = urlparse(url or "").netloc.lower()
    for token, priority in HOST_PRIORITY:
        if token in host:
            return priority
    return 20


def _topic_relevance_score(topic: str, item: dict) -> int:
    text = f"{item.get('title') or ''} {item.get('summary') or ''} {item.get('source_host') or ''}".lower()
    core = _topic_search_core(topic).lower()
    tokens = [token.lower() for token in _topic_keywords(topic)]
    score = 0
    if core and core in text:
        score += 8
    for token in tokens:
        if token in text:
            score += 3 if len(token) >= 4 else 1
    score = score * 100
    score += max(0, 20 - _host_priority(item.get("url") or ""))
    if item.get("summary"):
        score += 2
    return score


def _has_topic_signal(topic: str, item: dict) -> bool:
    text = f"{item.get('title') or ''} {item.get('summary') or ''} {item.get('source_host') or ''}".lower()
    keywords = [keyword.lower() for keyword in _topic_keywords(topic)]
    if not keywords:
        return True
    return any(keyword in text for keyword in keywords)


def search_topic_candidates(
    topic: str,
    max_results: int = 18,
    timeout_seconds: int = 20,
    progress_cb=None,
) -> list[dict]:
    clean_topic = (topic or "").strip()
    if not clean_topic:
        return []

    per_platform_limit = max(3, min(8, max_results // 2))
    candidates: list[dict] = []
    seen: set[str] = set()
    with ThreadPoolExecutor(max_workers=min(5, len(PLATFORM_SEARCHERS))) as executor:
        futures = [
            executor.submit(_search_one_platform, clean_topic, searcher, per_platform_limit, timeout_seconds)
            for searcher in PLATFORM_SEARCHERS
        ]
        for future in as_completed(futures):
            platform, results, error = future.result()
            if progress_cb:
                if error and not results:
                    progress_cb("warning", f"{platform} 搜索失败：{error[:100]}")
                else:
                    progress_cb("info", f"{platform} 搜索完成：候选 {len(results)} 条")
            for item in results:
                url = item.get("url") or ""
                if url in seen:
                    continue
                if not _has_topic_signal(clean_topic, item):
                    continue
                seen.add(url)
                candidates.append(item)

    candidates.sort(key=lambda item: (-_topic_relevance_score(clean_topic, item), _host_priority(item.get("url") or "")))
    return candidates[: max(1, max_results)]


def _source_from_search_item(item: dict, idx: int, timeout_seconds: int) -> dict:
    platform = item.get("platform") or "全网"
    board_name = f"{platform}灵感" if not str(platform).endswith("灵感") else str(platform)
    url = item["url"]
    fetched = fetch_article(
        board_snapshot_item_id=-idx,
        board_name=board_name,
        source_url=url,
        timeout_seconds=timeout_seconds,
    )
    try:
        if _looks_mojibake(fetched.title) or _looks_mojibake(fetched.summary) or _looks_mojibake(fetched.content_text):
            response = requests.get(url, headers=REQUEST_HEADERS, timeout=timeout_seconds, allow_redirects=True)
            html_text = _decode_response_text(response)
            from .fetchers import _collect_image_urls, _extract_search_page_summary, _extract_with_readability, _filter_image_urls, _first_meta

            soup = BeautifulSoup(html_text, "html.parser")
            readable_text = _extract_with_readability(html_text) or _extract_search_page_summary(soup)
            title_text = _clean_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
            summary_text = _first_meta(soup, ("name", "description"), ("property", "og:description"))
            image_urls = _filter_image_urls(_collect_image_urls(soup, response.url), response.url, board_name)
            fetched = ArticleFetchResult(
                board_snapshot_item_id=fetched.board_snapshot_item_id,
                board_name=fetched.board_name,
                source_url=fetched.source_url,
                source_host=urlparse(response.url).netloc.lower() or fetched.source_host,
                final_url=response.url or fetched.final_url,
                fetch_status=fetched.fetch_status,
                http_status=response.status_code,
                content_type=response.headers.get("content-type", fetched.content_type),
                title=title_text or fetched.title,
                summary=summary_text or fetched.summary,
                content_text=readable_text or fetched.content_text,
                content_hash="",
                lead_image_url=image_urls[0] if image_urls else fetched.lead_image_url,
                image_urls=image_urls or fetched.image_urls,
                note=fetched.note,
            )
    except Exception:
        pass
    fetched = _repair_fetch_result_text(fetched)
    title = fetched.title or item.get("title") or ""
    summary = fetched.summary or item.get("summary") or ""
    content_text = fetched.content_text or summary or title
    if len(content_text.strip()) < 20:
        content_text = "\n".join(part for part in [title, summary] if part)
    image_urls = [image_url for image_url in fetched.image_urls if not is_blocked_source_image_url(image_url)]
    hash_source = "\n".join([title, summary, content_text]).strip()
    source_host = fetched.source_host or item.get("source_host") or urlparse(url).netloc.lower()
    final_url = fetched.final_url or url
    return {
        "board_name": board_name,
        "rank_num": idx,
        "title": title,
        "member_title": title,
        "source_url": url,
        "source_host": source_host,
        "final_url": final_url,
        "summary": summary,
        "content_text": content_text,
        "fetch_status": fetched.fetch_status or "fetched",
        "http_status": fetched.http_status,
        "content_type": fetched.content_type,
        "content_hash": fetched.content_hash or hashlib.sha256(hash_source.encode("utf-8")).hexdigest(),
        "image_urls": image_urls,
        "platform": platform,
        "search_title": item.get("title") or "",
        "search_summary": item.get("summary") or "",
    }


def _dedupe_sources(sources: list[dict]) -> list[dict]:
    result: list[dict] = []
    seen_url: set[str] = set()
    seen_hash: set[str] = set()
    for source in sources:
        url = source.get("final_url") or source.get("source_url") or ""
        content_key = source.get("content_hash") or hashlib.sha256(
            f"{source.get('title') or ''}\n{source.get('summary') or ''}\n{(source.get('content_text') or '')[:500]}".encode("utf-8")
        ).hexdigest()
        if url in seen_url or content_key in seen_hash:
            continue
        seen_url.add(url)
        seen_hash.add(content_key)
        result.append(source)
    return result


def _build_inspiration_text(topic: str, sources: list[dict]) -> str:
    lines = [f"话题：{topic}", "", "候选资料："]
    for idx, source in enumerate(sources, start=1):
        excerpt = _clean_text(source.get("content_text") or source.get("summary") or "")[:520]
        lines.append(
            "\n".join(
                [
                    f"{idx}. [{source.get('board_name') or '全网'}] {source.get('title') or source.get('search_title') or topic}",
                    f"URL: {source.get('final_url') or source.get('source_url') or ''}",
                    f"摘要: {source.get('summary') or source.get('search_summary') or ''}",
                    f"摘录: {excerpt}",
                ]
            )
        )
    return "\n\n".join(lines)


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
    match = re.search(r"\{.*\}", content, flags=re.S)
    if match:
        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def refine_topic_sources_with_llm(
    llm_config: dict,
    topic: str,
    sources: list[dict],
    max_results: int = 6,
    progress_cb=None,
) -> tuple[list[dict], str]:
    if not sources or not llm_config.get("api_key"):
        return sources[:max_results], ""

    source_text = _build_inspiration_text(topic, sources[: min(len(sources), 14)])
    prompt = f"""你是公众号选题编辑。下面是按用户话题从百度、Bing、知乎、微信等平台搜集到的创作灵感。

请帮我做三件事：
1. 过滤明显新闻通稿、官方通报、低质量搜索页、重复内容。
2. 选出最适合写成公众号文章的 {max_results} 条资料。
3. 归纳一个“给普通人看的创作角度”，用于后续写文章。

只返回 JSON，不要解释：
{{
  "selected_indices": [1, 2, 3],
  "angle": "一句话创作角度",
  "summary": "把可用信息汇集成 120-220 字摘要"
}}

{source_text}
"""
    payload = {
        "model": llm_config["model"],
        "messages": [
            {
                "role": "system",
                "content": "你是审慎的公众号选题编辑，只基于材料筛选和归纳，不编造事实。只输出 JSON。",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.25,
        "max_tokens": 900,
    }
    effort = llm_config.get("reasoning_effort") or llm_config.get("reasoning_level") or llm_config.get("reasoning")
    if isinstance(effort, str) and effort.strip():
        payload["reasoning_effort"] = {"超高": "xhigh", "最高": "xhigh", "高": "high", "中": "medium", "低": "low"}.get(
            effort.strip(),
            effort.strip(),
        )
    session = requests.Session()
    if bool(llm_config.get("disable_env_proxy", True)):
        session.trust_env = False
    try:
        base_url = str(llm_config["base_url"]).rstrip("/")
        if base_url.endswith("/chat/completions"):
            endpoint = base_url
        elif base_url.endswith("/v1"):
            endpoint = f"{base_url}/chat/completions"
        else:
            endpoint = f"{base_url}/v1/chat/completions"
        response = session.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {llm_config['api_key']}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=int(llm_config.get("timeout_seconds", 180)),
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        data = _extract_json_object(content) or {}
    except Exception as exc:
        if progress_cb:
            progress_cb("warning", f"模型筛选资料失败，已使用搜索排序：{exc}")
        return sources[:max_results], ""
    finally:
        session.close()

    selected_indices: list[int] = []
    raw_indices = data.get("selected_indices") or data.get("indices") or data.get("selected") or []
    if isinstance(raw_indices, list):
        for value in raw_indices:
            try:
                index = int(value)
            except (TypeError, ValueError):
                continue
            if 1 <= index <= len(sources) and index not in selected_indices:
                selected_indices.append(index)

    selected = [sources[index - 1] for index in selected_indices][:max_results]
    if len(selected) < max(2, min(max_results, len(sources))):
        for source in sources:
            if source not in selected:
                selected.append(source)
            if len(selected) >= max_results:
                break
    if not selected and sources:
        selected = sources[:max_results]

    angle = _clean_text(data.get("angle") or "")
    summary = _clean_text(data.get("summary") or "")
    refined_summary = "；".join(part for part in (angle, summary) if part)
    if not refined_summary:
        refined_summary = "；".join((source.get("summary") or source.get("title") or "")[:100] for source in selected[:4])
    if progress_cb:
        progress_cb("info", f"模型已筛选资料：保留 {len(selected)} 条｜角度：{angle[:80] or '按搜索排序'}")
    return selected[:max_results], refined_summary[:500]


def search_topic_sources(
    topic: str,
    max_results: int = 6,
    timeout_seconds: int = 20,
    llm_config: dict | None = None,
    progress_cb=None,
) -> list[dict]:
    query = (topic or "").strip()
    if not query:
        return []

    candidate_limit = max(max_results * 3, 16)
    candidates = search_topic_candidates(
        query,
        max_results=candidate_limit,
        timeout_seconds=timeout_seconds,
        progress_cb=progress_cb,
    )
    if progress_cb:
        platforms = sorted({item.get("platform") or "全网" for item in candidates})
        progress_cb("info", f"全网候选汇总完成：{len(candidates)} 条｜平台：{', '.join(platforms) or '无'}")
    if not candidates:
        return []

    fetch_limit = max(max_results * 2, min(len(candidates), 12))
    fetched_sources: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(6, fetch_limit)) as executor:
        futures = [
            executor.submit(_source_from_search_item, item, idx, timeout_seconds)
            for idx, item in enumerate(candidates[:fetch_limit], start=1)
        ]
        for future in as_completed(futures):
            try:
                source = future.result()
            except Exception:
                continue
            content = source.get("content_text") or source.get("summary") or source.get("title") or ""
            if len(_clean_text(content)) < 20:
                continue
            title_text = _clean_text(source.get("title") or "")
            if (
                "404" in title_text
                or "访问的页面找不到" in title_text
                or "页面不存在" in title_text
                or title_text.startswith("知乎，让每一次点击")
            ):
                continue
            if is_newslike_text(
                title=source.get("title") or "",
                summary=source.get("summary") or "",
                content=source.get("content_text") or "",
                source_url=source.get("source_url") or source.get("final_url") or "",
                source_host=source.get("source_host") or "",
                board_name=source.get("board_name") or "",
            ):
                continue
            fetched_sources.append(source)

    fetched_sources = _dedupe_sources(fetched_sources)
    fetched_sources.sort(
        key=lambda source: (
            _host_priority(source.get("final_url") or source.get("source_url") or ""),
            -(len(source.get("content_text") or "")),
        )
    )
    if progress_cb:
        progress_cb("info", f"正文补抓与过滤完成：可用资料 {len(fetched_sources)} 条")
    if not fetched_sources:
        # 搜索结果页摘要也可以作为创作灵感兜底，不再直接失败。
        for idx, item in enumerate(candidates[:max_results], start=1):
            hash_source = "\n".join([item.get("title") or "", item.get("summary") or ""]).strip()
            fetched_sources.append(
                {
                    "board_name": f"{item.get('platform') or '全网'}灵感",
                    "rank_num": idx,
                    "title": item.get("title") or query,
                    "member_title": item.get("title") or query,
                    "source_url": item.get("url") or f"manual://{idx}",
                    "source_host": item.get("source_host") or "",
                    "final_url": item.get("url") or f"manual://{idx}",
                    "summary": item.get("summary") or "",
                    "content_text": item.get("summary") or item.get("title") or query,
                    "fetch_status": "fetched",
                    "http_status": None,
                    "content_type": "text/html",
                    "content_hash": hashlib.sha256(hash_source.encode("utf-8")).hexdigest(),
                    "image_urls": [],
                    "platform": item.get("platform") or "全网",
                }
            )

    if llm_config:
        fetched_sources, refined_summary = refine_topic_sources_with_llm(
            llm_config=llm_config,
            topic=query,
            sources=fetched_sources,
            max_results=max_results,
            progress_cb=progress_cb,
        )
        if refined_summary:
            for source in fetched_sources:
                source["llm_inspiration_summary"] = refined_summary
    return fetched_sources[:max_results]
