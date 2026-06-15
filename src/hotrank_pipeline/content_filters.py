from __future__ import annotations

import re
from urllib.parse import unquote, urlparse


NEWSLIKE_KEYWORDS = (
    "通报",
    "公告",
    "发布会",
    "新华社",
    "新华网",
    "央视新闻",
    "新闻联播",
    "人民日报",
    "中国新闻网",
    "中新网",
    "中新社",
    "央广网",
    "快讯",
    "突发",
    "据报道",
    "据悉",
    "获悉",
    "回应称",
    "官方回应",
    "警方",
    "法院",
    "检方",
    "判决",
    "调查组",
    "监管",
    "处罚",
    "政策",
    "条例",
    "会议",
    "声明",
    "权威发布",
    "官方发布",
    "记者从",
    "通稿",
)


NEWSLIKE_HARD_KEYWORDS = (
    "新华社",
    "新华网",
    "新华社客户端",
    "据新华社",
    "新华社北京",
    "新华社上海",
    "新华社广州",
    "新华社深圳",
    "央视新闻",
    "央视新闻客户端",
    "新闻联播",
    "人民日报",
    "人民日报客户端",
    "中国新闻网",
    "中新网",
    "中新社",
    "官方通报",
    "警方通报",
    "情况通报",
    "外交部发言人",
    "新闻发布会",
    "例行记者会",
    "领导人",
    "国家主席",
    "总书记",
    "国务院",
    "会见",
    "出席",
    "致辞",
    "发表讲话",
    "国事访问",
    "快讯",
    "新用户点击新华社即可关注",
)


NEWS_SOURCE_URL_TOKENS = (
    "xinhuanet.com",
    "xinhuanet.cn",
    "news.cn",
    "xinhua",
    "xhimg",
    "cctv.com",
    "cctv.cn",
    "cntv.cn",
    "people.com.cn",
    "people.cn",
    "rmrb",
    "chinanews.com",
    "chinanews.com.cn",
    "china.com.cn",
    "gmw.cn",
    "ce.cn",
    "cri.cn",
    "cnr.cn",
    "youth.cn",
    "legaldaily.com.cn",
    "stdaily.com",
)


SOURCE_IMAGE_URL_BLOCKLIST_TOKENS = (
    "xinhuanet.com",
    "xinhuanet.cn",
    "news.cn",
    "xinhua",
    "xhimg",
    "cctv.com",
    "cctv.cn",
    "cntv.cn",
    "people.com.cn",
    "rmrb",
    "chinanews.com",
    "chinanews.com.cn",
    "logo",
    "watermark",
    "qrcode",
    "qr_code",
    "qr-code",
    "/qr/",
    "subscribe",
    "follow",
    "avatar",
    "headimg",
    "profile_photo",
    "sprite",
    "phncdn.com",
    "pornhub",
    "rule34",
    "xvideos",
    "xnxx",
    "adult",
    "pinimg.com",
    "pinterest",
    "tumblr.com",
    "goodreads.com",
)


ADULT_IMAGE_URL_BLOCKLIST_TOKENS = (
    "porn",
    "porno",
    "sex",
    "sexy",
    "xxx",
    "hentai",
    "nsfw",
    "nude",
    "naked",
    "erotic",
    "onlyfans",
    "playboy",
    "strip",
    "fetish",
    "boob",
    "boobs",
    "breast",
    "busty",
    "lingerie",
    "bikini",
    "swimsuit",
    "cameltoe",
    "upskirt",
    "milf",
    "jav",
)


SAFE_WEB_IMAGE_HOST_ALLOWLIST = (
    "images.unsplash.com",
    "unsplash.com",
    "images.pexels.com",
    "pexels.com",
    "cdn.pixabay.com",
    "pixabay.com",
    "upload.wikimedia.org",
    "wikimedia.org",
    "wikipedia.org",
    "weibo.com",
    "sinaimg.cn",
    "sinaimg.com",
    "wx.qq.com",
    "mmbiz.qpic.cn",
    "qpic.cn",
    "gtimg.cn",
    "qq.com",
    "toutiao.com",
    "toutiaoimg.com",
    "byteimg.com",
    "douyin.com",
    "douyinpic.com",
    "sohu.com",
    "sohucs.com",
)


SOURCE_IMAGE_CONTEXT_BLOCKLIST_KEYWORDS = (
    "新用户点击",
    "即可关注",
    "点击关注",
    "扫码关注",
    "长按识别",
    "二维码",
    "公众号",
    "来源：",
    "图片来源",
    "新华社",
    "新华网",
    "央视新闻",
    "人民日报",
    "中国新闻网",
    "中新社",
    "水印",
)


def _safe_unquote(value: str) -> str:
    try:
        return unquote(value or "")
    except Exception:
        return value or ""


def _lower_url_text(*values: str) -> str:
    return " ".join(_safe_unquote(value).lower() for value in values if value)


def is_newslike_text(
    title: str = "",
    summary: str = "",
    content: str = "",
    source_url: str = "",
    source_host: str = "",
    board_name: str = "",
) -> bool:
    """判断是否为不适合二创成稿的新闻、通稿、官方发布类内容。"""

    parsed_host = source_host or urlparse(source_url or "").netloc
    text = f"{board_name or ''} {title or ''} {summary or ''} {(content or '')[:1200]}"
    url_text = _lower_url_text(source_url, parsed_host)

    if any(keyword in text for keyword in NEWSLIKE_HARD_KEYWORDS):
        return True
    if any(token in url_text for token in NEWS_SOURCE_URL_TOKENS):
        return True
    if re.search(r"新华社.{0,16}\d{1,2}月\d{1,2}日电", text):
        return True
    if re.search(r"(?:中新网|中新社).{0,20}电", text):
        return True
    if re.search(r"电（?记者", text):
        return True

    score = sum(1 for keyword in NEWSLIKE_KEYWORDS if keyword in text)
    if "记者从" in text and any(keyword in text for keyword in ("获悉", "通报", "发布", "会议")):
        score += 1
    if any(keyword in text for keyword in ("权威发布", "官方发布", "发布通告", "发布公告")):
        score += 1
    return score >= 2


def is_blocked_source_image_url(url: str) -> bool:
    """过滤明显来源标识、关注引导、新闻通稿站点、头像/logo/二维码等图片 URL。"""

    lowered = _lower_url_text(url)
    return any(token in lowered for token in SOURCE_IMAGE_URL_BLOCKLIST_TOKENS) or any(
        token in lowered for token in ADULT_IMAGE_URL_BLOCKLIST_TOKENS
    )


def is_adult_or_risky_image_url(url: str) -> bool:
    """过滤成人视频/擦边/高风险 URL。"""

    lowered = _lower_url_text(url)
    return any(token in lowered for token in ADULT_IMAGE_URL_BLOCKLIST_TOKENS)


def is_allowed_web_search_image_url(url: str) -> bool:
    """联网搜图兜底只保留白名单域名，宁可少图也不要违规图。"""

    if not url or is_blocked_source_image_url(url) or is_adult_or_risky_image_url(url):
        return False
    host = (urlparse(url).netloc or "").lower().strip()
    if host.startswith("www."):
        host = host[4:]
    return any(host == domain or host.endswith("." + domain) for domain in SAFE_WEB_IMAGE_HOST_ALLOWLIST)


def is_blocked_image_context(text: str) -> bool:
    """过滤图片 alt/title 中带有来源标注、关注引导、水印等语义的图片。"""

    value = text or ""
    return any(keyword in value for keyword in SOURCE_IMAGE_CONTEXT_BLOCKLIST_KEYWORDS)


def is_unusable_image_dimensions(width: int, height: int, url: str = "") -> bool:
    """过滤横幅、关注条、logo、小图等不适合公众号正文的图片尺寸。"""

    if width <= 0 or height <= 0:
        return True
    if width < 240 or height < 140:
        return True
    if width * height < 70_000:
        return True

    ratio = width / max(height, 1)
    if ratio >= 4:
        return True
    if ratio >= 3.2 and height <= 260:
        return True
    if ratio <= 0.25 and width <= 220:
        return True
    if is_blocked_source_image_url(url):
        return True
    return False
