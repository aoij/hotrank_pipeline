from __future__ import annotations

import base64
import html
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from datetime import datetime
from pathlib import Path
from tempfile import gettempdir
from urllib.parse import quote_plus

import requests
from PIL import Image, UnidentifiedImageError

from .content_filters import is_blocked_source_image_url, is_unusable_image_dimensions

IMAGE_SEARCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
}

GENERIC_HEADING_MAP = {
    "导语",
    "事件脉络",
    "关键信息",
    "为什么值得关注",
    "结语",
}


TITLE_ALIGNMENT_GENERIC_TERMS = {
    "一个",
    "一种",
    "这次",
    "这个",
    "这事",
    "事情",
    "东西",
    "问题",
    "后果",
    "影响",
    "变化",
    "努力",
    "代价",
    "风险",
    "提醒",
    "风向",
    "真相",
    "普通人",
    "所有人",
    "大家",
    "今天",
    "今年",
    "最近",
    "到底",
    "怎么",
    "为什么",
    "回事",
    "看懂",
    "看明白",
    "看清",
    "别急",
    "别慌",
    "先别",
    "说白了",
}

TITLE_ALIGNMENT_PREFIXES = (
    "别让一个",
    "别让你",
    "别让",
    "别把",
    "别再",
    "一个",
    "一种",
    "这个",
    "这次",
    "这件",
    "这种",
    "你的",
    "你家",
    "有人",
    "很多人",
    "如果",
    "当",
)

TITLE_ALIGNMENT_SUFFIXES = (
    "怎么办",
    "怎么回事",
    "别乱带",
    "别乱发",
    "别乱点",
    "别乱用",
    "别忽视",
    "别大意",
    "更严了",
    "会怎样",
    "才是关键",
    "看明白",
    "看懂",
    "看清",
)

SOURCE_PLACEHOLDER_MARKERS = (
    "sina visitor system",
    "visitor system",
    "page not found",
    "404 not found",
    "403 forbidden",
    "access denied",
    "forbidden",
    "请先登录",
    "登录后查看",
    "页面不存在",
    "无权访问",
)

TITLE_NARRATIVE_MARKERS = (
    "如果",
    "可能",
    "会",
    "让",
    "把",
    "别",
    "去年",
    "现在",
    "当时",
    "准备",
    "顺手",
    "放到",
    "说白了",
    "最难受",
    "先别",
    "别急",
    "真正",
    "后面",
    "这波",
    "这一轮",
)

TITLE_TOO_GENERIC_PATTERNS = (
    r"^你可能也刷到了",
    r"^今天朋友圈刷屏",
    r"^朋友圈刷屏的这事",
    r"^别急着下结论$",
    r"^先别急着下判断$",
    r"^这事和你有关$",
    r"^这事.+有关$",
    r"^这件事.+有关$",
)

TITLE_PRICE_DROP_KEYWORDS = (
    "价格崩盘",
    "价格跳水",
    "暴跌",
    "大跌",
    "下探",
    "贬值",
    "降价",
    "跌惨",
)


RETRYABLE_REQUEST_EXCEPTIONS = (
    requests.exceptions.SSLError,
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)


def sanitize_filename(name: str) -> str:
    value = re.sub(r"[\\\\/:*?\"<>|]+", "_", name)
    value = re.sub(r"[“”‘’「」『』【】《》？!！，。；：、]+", "_", value)
    value = re.sub(r"\s+", "_", value).strip("_")
    return value[:80] or "draft"


def _short_stem_name(stem_name: str, max_chars: int = 72) -> str:
    value = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fff-]+", "_", stem_name or "draft")
    value = re.sub(r"_+", "_", value).strip("_")
    if len(value) <= max_chars:
        return value or "draft"
    match = re.match(r"^(\d{8}_\d{6})_(.+)$", value)
    if match:
        prefix, tail = match.groups()
        keep_tail = max(16, max_chars - len(prefix) - 1)
        return f"{prefix}_{tail[:keep_tail].strip('_')}"
    return value[:max_chars].strip("_") or "draft"


def _path_error(exc: Exception) -> bool:
    return isinstance(exc, OSError) and getattr(exc, "winerror", None) in {3, 5, 32, 123, 206}


def _emit_optional(progress_cb, level: str, message: str) -> None:
    if progress_cb:
        progress_cb(level, message)


def _prepare_archive_paths(output_dir: str, title: str, progress_cb=None) -> tuple[datetime, Path, Path, str]:
    now = datetime.now()
    safe_title = sanitize_filename(title)
    filename = f"{_short_stem_name(now.strftime('%Y%m%d_%H%M%S') + '_' + safe_title, 96)}.md"

    candidates = [Path(output_dir) if output_dir else Path(".")]
    fallback_root = Path(gettempdir()) / "hotrank_pipeline_drafts"
    if fallback_root not in candidates:
        candidates.append(fallback_root)

    last_error: Exception | None = None
    for root in candidates:
        try:
            month_dir = root / now.strftime("%Y-%m")
            day_dir = month_dir / now.strftime("%Y-%m-%d")
            day_dir.mkdir(parents=True, exist_ok=True)
            target = day_dir / filename
            probe_stem = _short_stem_name(target.stem)
            probe_asset_dir = day_dir / "assets" / probe_stem
            probe_asset_dir.mkdir(parents=True, exist_ok=True)
            probe_file = day_dir / f"._write_probe_{os.getpid()}"
            probe_file.write_text("ok", encoding="utf-8")
            probe_file.unlink(missing_ok=True)
            if root != candidates[0]:
                _emit_optional(progress_cb, "warning", f"稿件归档目录不可写，已自动切换到临时目录：{day_dir}")
            return now, day_dir, target, probe_stem
        except OSError as exc:
            last_error = exc
            _emit_optional(progress_cb, "warning", f"稿件归档目录不可用：{root}｜{exc}")
            continue

    raise last_error or RuntimeError("稿件归档目录不可用")


def _ensure_asset_dir(base_dir: Path, stem_name: str, progress_cb=None) -> tuple[Path, str]:
    candidates = [_short_stem_name(stem_name, 72), _short_stem_name(stem_name, 42), f"draft_{datetime.now().strftime('%H%M%S_%f')}"]
    seen: set[str] = set()
    last_error: Exception | None = None
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            asset_dir = base_dir / "assets" / candidate
            asset_dir.mkdir(parents=True, exist_ok=True)
            return asset_dir, candidate
        except OSError as exc:
            last_error = exc
            _emit_optional(progress_cb, "warning", f"配图目录创建失败，尝试短目录：{candidate}｜{exc}")
            continue
    raise last_error or RuntimeError("配图目录创建失败")


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


def _image_generation_endpoint(base_url: str) -> str:
    endpoint = (base_url or "").rstrip("/")
    if not endpoint:
        return ""
    if endpoint.endswith("/images/generations"):
        return endpoint
    return f"{endpoint}/images/generations"


def _post_json_with_retry(
    url: str,
    headers: dict[str, str],
    payload: dict,
    timeout: int,
    retry_count: int = 3,
    backoff_seconds: float = 2.0,
    session: requests.Session | None = None,
) -> requests.Response:
    last_error: Exception | None = None
    retry_count = max(1, retry_count)
    for attempt in range(1, retry_count + 1):
        try:
            client = session or requests
            response = client.post(url, headers=headers, json=payload, timeout=timeout)
            if response.status_code in (429, 500, 502, 503, 504) and attempt < retry_count:
                last_error = requests.HTTPError(
                    f"retryable status {response.status_code}: {response.text[:200]}",
                    response=response,
                )
                time.sleep(backoff_seconds * attempt)
                continue
            response.raise_for_status()
            return response
        except RETRYABLE_REQUEST_EXCEPTIONS as exc:
            last_error = exc
            if attempt >= retry_count:
                break
            time.sleep(backoff_seconds * attempt)
        except requests.HTTPError as exc:
            last_error = exc
            status_code = exc.response.status_code if exc.response is not None else None
            if status_code not in (429, 500, 502, 503, 504) or attempt >= retry_count:
                break
            time.sleep(backoff_seconds * attempt)
    if last_error:
        raise last_error
    raise RuntimeError("request failed without response")


def _decode_data_url(value: str) -> tuple[bytes, str]:
    header, encoded = value.split(",", 1)
    content_type = "image/png"
    match = re.match(r"data:([^;]+);base64", header, re.I)
    if match:
        content_type = match.group(1).lower()
    return base64.b64decode(encoded), content_type


def _extract_image_payloads(data: object) -> list[object]:
    if isinstance(data, dict):
        for key in ("data", "images", "output", "result"):
            value = data.get(key)
            if isinstance(value, list):
                return value
        return [data]
    if isinstance(data, list):
        return data
    return []


def _image_bytes_from_payload(
    payload: object,
    timeout_seconds: int = 180,
    session: requests.Session | None = None,
) -> tuple[bytes, str] | None:
    value: str | None = None
    if isinstance(payload, dict):
        for key in ("b64_json", "base64", "image_base64", "data"):
            candidate = payload.get(key)
            if isinstance(candidate, str) and candidate.strip():
                value = candidate.strip()
                break
        if value is None:
            for key in ("url", "image_url", "uri"):
                candidate = payload.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    value = candidate.strip()
                    break
    elif isinstance(payload, str) and payload.strip():
        value = payload.strip()

    if not value:
        return None

    if value.startswith("data:image/"):
        return _decode_data_url(value)

    if re.match(r"^https?://", value, re.I):
        client = session or requests
        response = client.get(value, timeout=timeout_seconds)
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        return response.content, content_type or "image/jpeg"

    raw = base64.b64decode(value)
    return raw, "image/png"


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def _compact_keyword_text(text: str, limit: int = 80) -> str:
    value = re.sub(r"[#>*_`!\[\]\(\)]", " ", text or "")
    value = re.sub(r"\s+", " ", value).strip(" ，。！？、；：")
    return value[:limit]


def _wechat_hottrend_style_brief() -> str:
    """Current WeChat hot-article patterns distilled from live ranking samples."""
    return "\n".join(
        [
            "- 开头先抛场景、冲突、结果或反差，别先铺背景。",
            "- 正文以短段落为主，像微信里聊天，不像新闻稿或作文。",
            "- 小标题要自然、口语化，别写成“原因分析/应对建议/第一第二第三”。",
            "- 事实先讲清，再补一层人话翻译，少术语、少大词。",
            "- 结尾收得实在一点，别强行升华。",
            "- 图片要按题材来：快讯可多一点，观点/科普可少图甚至无图。",
        ]
    )


def _wechat_hottrend_visual_brief(image_count: int = 4) -> str:
    if image_count <= 1:
        return "只做一张封面感强的主视觉，画面先抓人，不要把信息堆满。"
    if image_count <= 3:
        return "第一张偏封面感，后面两张分别落在正文关键转折和最有画面的地方，不要平均分配。"
    return "第一张做封面，后面按正文关键节点插图，优先场景图、人物局部图、环境细节图，别每段都配。"


def _count_body_paragraphs(content_md: str) -> int:
    count = 0
    for line in (content_md or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("#", "!", ">", "- ", "* ", "|")):
            continue
        if re.match(r"^\d+[.)、]\s+", stripped):
            continue
        count += 1
    return count


def _article_image_plan(title: str, content_md: str, image_config: dict) -> dict:
    clean_text = re.sub(r"[#>*_`!\[\]\(\)]", "", content_md or "")
    text = f"{title} {clean_text[:1800]}"
    paragraph_count = _count_body_paragraphs(content_md)
    max_count = max(1, int((image_config or {}).get("max_per_draft", 4)))
    text_len = len(re.sub(r"\s+", "", clean_text))

    topic = "general"
    topic_label = "通用热点"
    desired = 2
    search_hint = "城市 生活 人物 场景 真实 摄影 无水印"
    role_templates = ["封面主视觉", "中段场景图", "细节补充图", "收束氛围图"]
    visual_templates = [
        "普通人在手机上刷到这条热点，屏幕内容虚化不可读，画面重点是停顿、犹豫和真实生活环境",
        "城市日常场景里的普通人交流讨论，像手机端新闻热点引发身边人聊天，不出现可识别公众人物",
        "桌面、手机、咖啡杯或通勤环境的细节，表达信息过载和重新判断，不出现文字与Logo",
        "安静的生活场景收束，人物背影或手部动作，表达冷静下来继续生活",
    ]

    if _contains_any(text, ("跑男", "白鹿", "浪姐", "综艺", "明星", "电影", "包场", "票房", "影院", "演唱会", "剧集", "艺人")):
        topic = "entertainment"
        topic_label = "娱乐/影视热点向"
        desired = 3
        search_hint = "影院 观众 电影票 手机 社交媒体 真实 摄影 无水印"
        role_templates = ["封面主视觉", "社交讨论图", "影院/票根细节图", "情绪收束图"]
        visual_templates = [
            "电影院大厅或取票机前的普通观众，手里拿着电影票或手机购票页面，海报全部虚化不可读，不出现明星脸",
            "朋友或同事在咖啡馆/办公室看手机讨论热搜，手机屏幕虚化，表达围观和人情站台",
            "电影票、座椅、爆米花、手机购票界面的局部细节，突出“包场/支持/社交人情”的真实氛围",
            "夜晚影院散场后的普通人背影，表达热闹之后的冷静观察",
        ]
    elif _contains_any(text, ("广告", "代言", "品牌", "耳机", "新品", "发布会", "宋威龙")):
        topic = "promo"
        topic_label = "品牌/消费热点向"
        desired = 2
        search_hint = "产品 发布会 消费 决策 真实 摄影 高级感 无水印"
        role_templates = ["封面主视觉", "消费决策图", "细节补充图"]
        visual_templates = [
            "普通人在商场或桌面前查看新品信息，产品和手机界面虚化不可读，不出现品牌Logo",
            "桌面上的手机、耳机、购物页面局部细节，表达消费决策和跟风购买的犹豫",
            "城市商圈橱窗前的普通人背影，画面真实克制，不像广告海报",
        ]
    elif _contains_any(text, ("AI", "人工智能", "大模型", "工具", "软件", "应用", "教程", "提示词", "网络安全", "隐私", "诈骗", "密码", "微信", "公众号")) and not _contains_any(text, ("高考", "考试", "考场", "作文", "英语", "数学", "学生", "家长", "学校", "校园", "老师", "教师")):
        topic = "tech_tool"
        topic_label = "科技/工具向"
        desired = 3
        search_hint = "科技 办公 电脑 手机 使用场景 真实 摄影 无水印"
        role_templates = ["封面主视觉", "使用场景图", "关键细节图", "转折补充图"]
        visual_templates = [
            "普通人在电脑和手机前处理消息或工具页面，屏幕内容虚化不可读，像真实办公桌抓拍",
            "手指操作手机或电脑键盘的近景，表达工具使用、隐私设置或安全提醒",
            "聊天窗口、验证码、设置页面等抽象为虚化界面细节，不出现真实品牌和可读文字",
            "下班后的书桌和设备，表达把工具用回日常生活",
        ]
    elif _contains_any(text, ("寿命", "健康", "疾病", "医生", "睡眠", "饮食", "医院", "症状", "长寿", "养生")):
        topic = "health"
        topic_label = "健康科普向"
        desired = 3
        search_hint = "健康 生活 医生 咨询 日常场景 真实 摄影 无水印"
        role_templates = ["封面主视觉", "生活场景图", "动作细节图", "收束氛围图"]
        visual_templates = [
            "普通人在家里或诊室外看健康信息，画面克制，不出现病历文字和可识别人脸",
            "水杯、药盒、睡眠用品、运动鞋等生活细节，表达日常健康选择",
            "医生与普通人沟通的背影或手部细节，不出现医院名称和隐私信息",
            "清晨散步或居家休息的真实场景，表达可执行的日常调整",
        ]
    elif _contains_any(text, ("房价", "油价", "楼市", "股市", "基金", "消费", "价格", "通胀", "市场", "公积金", "成交")):
        topic = "market"
        topic_label = "民生/市场向"
        desired = 2
        search_hint = "财经 城市 通勤 屏幕 数据 场景 真实 摄影 无水印"
        role_templates = ["封面主视觉", "数据/场景图", "细节补充图"]
        visual_templates = [
            "普通人在地铁、办公室或家中看价格/账单相关信息，屏幕数据虚化不可读",
            "手机计算器、账单、银行卡、购物小票的生活细节，表达钱包和选择压力",
            "城市通勤人群与商业街环境，表达市场变化落到普通生活里",
        ]
    elif _contains_any(text, ("高考", "考试", "考场", "作文", "英语", "数学", "学生", "家长", "学校", "校园", "老师", "教师")):
        topic = "education"
        topic_label = "教育/考试向"
        desired = 3
        search_hint = "校园 考试 家长 文具 真实 摄影 无水印"
        role_templates = ["封面主视觉", "考场外场景图", "文具/试卷细节图", "情绪收束图"]
        visual_templates = [
            "学校门口或考场外的家长和学生背影，真实纪实摄影，不出现校名和可识别人脸",
            "书桌上的铅笔、橡皮、准考证样式纸张但文字虚化不可读，表达考试压力",
            "家长在校门外等待、低头看手机的真实场景，表达牵挂和信息焦虑",
            "傍晚校园外散场背影，表达考试之后回到生活",
        ]
    elif _contains_any(text, ("老师", "教师", "孩子", "家庭", "婚姻", "情绪", "猝死", "父母", "校园", "故事", "女生", "男生", "赛课")):
        topic = "story"
        topic_label = "人物/故事向"
        desired = 3
        search_hint = "人物 情绪 城市 生活 场景 真实 摄影 无水印"
        role_templates = ["封面主视觉", "人物场景图", "环境细节图", "情绪收束图"]
        visual_templates = [
            "普通人在家中或城市角落看手机停顿的背影，表达被故事击中的瞬间，不出现具体新闻人物",
            "家庭餐桌、办公室角落、公交站等生活环境里的普通人侧影，画面克制有真实情绪",
            "手机、钥匙、书包、纸巾、杯子等与正文相关的生活物件细节，避免夸张摆拍",
            "窗边或街头的安静背影，表达情绪沉淀和继续生活",
        ]
    elif _contains_any(text, ("事故", "安全", "通报", "警方", "调查", "法院", "判决", "公共", "责任", "风险", "提醒")):
        topic = "public_event"
        topic_label = "公共事件向"
        desired = 3
        search_hint = "城市 公共安全 风险 提醒 场景 真实 摄影 无水印"
        role_templates = ["封面主视觉", "中段场景图", "提醒细节图", "收束氛围图"]
        visual_templates = [
            "普通人在家里低头看手机新闻，屏幕内容虚化，窗外城市阴天，表达关切和谨慎判断",
            "城市街口、楼道、电梯口或公共空间的空镜头，画面真实克制，不像事故现场",
            "手机通知、门禁、电梯按钮、路灯等公共安全相关细节，避免血腥和惊悚",
            "夜晚城市街道的普通人背影，表达保持警觉但不制造恐慌",
        ]

    if paragraph_count <= 2 or text_len < 420:
        desired = 1
    elif paragraph_count <= 5 or text_len < 900:
        desired = min(desired, 2)
    elif paragraph_count >= 9 and text_len > 1800 and topic in {"tech_tool", "health", "story", "public_event"}:
        desired = max(desired, 4)
    elif paragraph_count >= 7 and text_len > 1200 and topic not in {"promo", "market"}:
        desired = max(desired, 3)

    desired = min(max_count, max(1, desired))
    if paragraph_count <= 0:
        desired = 1
    elif paragraph_count <= 3:
        desired = min(desired, 2)

    position_templates = {
        1: [1],
        2: [1, 4],
        3: [1, 3, 6],
        4: [1, 3, 6, 9],
    }
    raw_positions = position_templates.get(desired, position_templates[4])[:desired]
    positions: list[int] = []
    for pos in raw_positions:
        capped = max(1, min(pos, max(paragraph_count, 1)))
        if capped not in positions:
            positions.append(capped)
    desired = min(desired, len(positions)) if positions else 1
    positions = positions[:desired] or [1]
    roles = role_templates[:desired]
    if len(roles) < desired:
        roles.extend(["正文配图"] * (desired - len(roles)))
    visual_brief = f"{_wechat_hottrend_visual_brief(desired)} 这篇属于{topic_label}，优先贴近正文场景，别做无关的大而空画面。"
    return {
        "topic": topic,
        "topic_label": topic_label,
        "count": desired,
        "positions": positions,
        "roles": roles,
        "visual_templates": visual_templates[:desired],
        "search_hint": search_hint,
        "visual_brief": visual_brief,
        "paragraph_count": paragraph_count,
        "text_len": text_len,
    }


def _topic_editorial_plan(cluster: dict) -> dict:
    """Pick a deterministic but varied WeChat article direction for the cluster."""
    text = f"{cluster.get('canonical_title', '')} {cluster.get('cluster_summary', '')}"
    title_hash = sum(ord(ch) for ch in text)

    plans = [
        {
            "name": "解释型拆解",
            "tone": "像一个懂行朋友在微信里把事情讲明白：先给感受，再讲判断；有信息量，但不端着。",
            "opening": "先抛一个普通人会关心的问题或反差，例如“这事看起来离你很远，但麻烦可能就在后面”。",
            "section_titles": [
                "别急着划走，这事有个关键点",
                "真正麻烦的地方在后面",
                "落到普通人身上，会变成这样",
                "最后说句实在话",
            ],
        },
        {
            "name": "故事型观察",
            "tone": "有画面感，但不消费情绪；像讲一个身边发生的故事，再慢慢把事情说透。",
            "opening": "开头从一个日常瞬间、一个反差或一句读者会点头的话切入，别先交代背景。",
            "section_titles": [
                "很多人被戳中，是因为这个细节",
                "热闹背后，其实藏着另一个问题",
                "我们真正担心的是什么",
                "情绪过去后，事情还没完",
            ],
        },
        {
            "name": "快聊型短评",
            "tone": "节奏快一点，但不要像工作汇报；像朋友顺手把重点讲给你听。",
            "opening": "开头直接告诉读者：这篇不复述新闻，只聊最容易影响普通人的那一处。",
            "section_titles": [
                "先把话说简单点",
                "容易被忽略的，是这个变化",
                "你不用全懂，但要留意这里",
                "别急着下结论",
            ],
        },
        {
            "name": "评论型分析",
            "tone": "有态度，但不喊口号；像一个冷静的朋友，把热闹背后的逻辑说出来。",
            "opening": "用一句克制但有判断的话开场，让读者知道这不是新闻复述。",
            "section_titles": [
                "这事不只是一条热搜",
                "表面看是变化，背后是取舍",
                "为什么大家会在意",
                "有些误读，先别急着信",
                "放回现实里看，就没那么简单",
            ],
        },
    ]

    if _contains_any(text, ("病毒", "感染", "疾控", "疫苗", "医院", "疾病", "症状", "健康", "流感", "疫情")):
        return {
            "name": "健康科普型",
            "tone": "准确、克制、去恐慌；像给家人解释一样，先讲清严重程度，再讲能做什么。",
            "opening": "开头像诊室聊天或家人提醒，先回应读者最关心的问题：和我有关吗、该不该担心。",
            "section_titles": [
                "先别慌，真正要看的在这里",
                "这次提醒，翻译成人话是这样",
                "哪些人更需要关注",
                "回到日常，其实就这几件小事",
            ],
        }
    if _contains_any(text, ("薪资", "年终奖", "十三薪", "裁员", "员工", "工资", "职场", "绩效", "公司", "财年")):
        return {
            "name": "职场商业拆解",
            "tone": "像一个经历过职场的人在聊天：讲利益影响，也讲普通人的真实感受；不替谁站台，也不制造焦虑。",
            "opening": "开头先说明这次变化对个人最直接的影响，别从公司公告口吻写起。",
            "section_titles": [
                "打工人最先关心的，肯定不是口号",
                "表面是规则，实际是预期变了",
                "员工真正担心的是什么",
                "放到自己身上，可以多想一步",
            ],
        }
    if _contains_any(text, ("学校", "学生", "老师", "老人", "家庭", "孩子", "拾荒", "论文", "致谢", "原生家庭", "打", "伤害")):
        return {
            "name": "社会情绪观察",
            "tone": "有人情味，但不消费苦难；先承认普通人的感受，再回到事实和现实处境。",
            "opening": "开头从一个最容易让人停下来的细节写起，不要一开始就下结论。",
            "section_titles": [
                "最让人停下来的，是这个细节",
                "别急着补脑，能确定的其实有限",
                "它为什么会击中很多人",
                "情绪之外，我们还能看见什么",
            ],
        }
    if _contains_any(text, ("通报", "警方", "法院", "判决", "调查", "监管", "处罚", "官方", "回应", "政策")):
        return {
            "name": "公共事件梳理",
            "tone": "严谨但别像通报；少猜测，把能确认的、不能确认的和普通人该怎么理解分开讲。",
            "opening": "开头先说读者最该知道的一句话，不要复述通报，不要急着站队。",
            "section_titles": [
                "先把能确定的说清楚",
                "别急着站队，先把边界看清",
                "大家为什么会盯着不放",
                "越是热议，越要慢一点判断",
            ],
        }

    return plans[title_hash % len(plans)]


def _format_section_suggestions(section_titles: list[str]) -> str:
    return "\n".join(f"   - ## {title}" for title in section_titles)


def _render_prompt_template(template: str, context: dict[str, str]) -> str:
    if not template.strip():
        return ""
    try:
        return template.format(**context)
    except Exception:
        # Keep generation usable even if a custom prompt contains unmatched braces.
        rendered = template
        for key, value in context.items():
            rendered = rendered.replace("{" + key + "}", value)
        return rendered


def _markdown_sections(content_md: str) -> list[dict[str, str]]:
    sections: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal current, buffer
        if current is not None:
            current["excerpt"] = re.sub(r"\s+", " ", "\n".join(buffer)).strip()[:420]
            sections.append(current)
        buffer = []

    for line in content_md.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            flush()
            current = {"title": stripped.lstrip("#").strip(), "excerpt": ""}
            continue
        if stripped and current is not None and not stripped.startswith(("#", "!", "|")):
            buffer.append(stripped)

    flush()
    return sections


def _build_image_prompts(
    title: str,
    content_md: str,
    image_config: dict,
    image_plan: dict | None = None,
) -> list[str]:
    image_plan = image_plan or _article_image_plan(title, content_md, image_config)
    max_count = max(0, int(image_plan.get("count") or image_config.get("max_per_draft", 4)))
    if max_count <= 0:
        return []

    generation_config = image_config.get("generation") or {}
    prompt_template = (generation_config.get("prompt_template") or "").strip()
    sections = _markdown_sections(content_md)
    if not sections:
        plain = re.sub(r"[#>*_`!\[\]\(\)]", "", content_md)
        sections = [{"title": title, "excerpt": re.sub(r"\s+", " ", plain).strip()[:420]}]

    visual_brief = str(image_plan.get("visual_brief") or _wechat_hottrend_visual_brief(max_count))
    roles = list(image_plan.get("roles") or [])
    visual_templates = list(image_plan.get("visual_templates") or [])
    opening_excerpt = _compact_keyword_text(_first_meaningful_paragraph(content_md), 220)
    prompts: list[str] = []
    for idx in range(max_count):
        if idx == 0:
            section = {
                "title": roles[idx] if idx < len(roles) else "封面主视觉",
                "excerpt": opening_excerpt or sections[0].get("excerpt", ""),
            }
        else:
            section = sections[min(idx - 1, len(sections) - 1)]
            if idx < len(roles):
                section = {
                    "title": roles[idx],
                    "excerpt": section.get("excerpt", ""),
                }

        context = {
            "article_title": title,
            "section_title": section.get("title") or title,
            "section_excerpt": section.get("excerpt") or "",
            "image_index": str(idx + 1),
            "image_count": str(max_count),
            "visual_brief": visual_brief,
            "topic_label": str(image_plan.get("topic_label") or "通用热点"),
            "visual_concept": visual_templates[idx] if idx < len(visual_templates) else "",
        }
        if prompt_template:
            prompt = _render_prompt_template(prompt_template, context)
        else:
            prompt = (
                "为一篇中文微信公众号文章生成原创配图，必须是高真实感摄影，不要插画、不要海报、不要AI概念图。"
                f"文章标题：{context['article_title']}。"
                f"插图位置：第 {context['image_index']} 张 / 共 {context['image_count']} 张。"
                f"小节主题：{context['section_title']}。"
                f"内容摘要：{context['section_excerpt']}。"
                f"文章类型：{context['topic_label']}。"
                f"排版节奏：{context['visual_brief']}"
                f"这张图的具体画面必须优先按这个来画：{context['visual_concept'] or '围绕标题和小节摘要做具体生活场景'}。"
                "画面内容：用真实生活场景、真实物件、普通人背影/手部/侧影或环境细节来表达主题，"
                "不要做夸张隐喻，不要漂浮图标，不要把标题文字画进图片。"
                "摄影风格：photorealistic editorial documentary photography, real-world location, natural light,"
                " candid composition, 35mm or 50mm lens look, realistic skin texture, realistic colors,"
                " imperfect everyday details, moderate depth of field, like a licensed editorial stock photo."
                "构图要求：适合公众号手机端阅读，主体清晰，背景真实但不杂乱，画面留有呼吸感，可裁切为方图。"
                "严格避免：illustration, cartoon, anime, CGI, 3D render, digital art, glossy advertising poster,"
                " plastic skin, over-saturated colors, perfect symmetrical fake faces, surreal objects,"
                " text, title, logo, watermark, QR code, brand trademark."
                "安全边界：不要生成真实公众人物可识别脸部；涉及事故、疾病、公共事件、金融风险时，"
                "用间接场景表达，避免血腥、事故现场、惊悚画面和可能误导为真实新闻照片的具体人物。"
            )
        prompts.append(prompt)

    return prompts


def _soften_generic_headings(content_md: str, plan: dict) -> str:
    """Avoid every generated article keeping the old fixed five-section skeleton."""
    content_md = _extract_publishable_draft(content_md)
    section_titles = plan.get("section_titles") or []
    if not section_titles:
        return content_md

    replacement_index = 0
    output: list[str] = []
    for line in content_md.splitlines():
        match = re.match(r"^(##+)\s+(.+?)\s*$", line)
        if match:
            prefix, heading = match.groups()
            normalized = heading.strip(" #：:")
            if prefix == "##" and normalized in GENERIC_HEADING_MAP and replacement_index < len(section_titles):
                line = f"## {section_titles[replacement_index]}"
                replacement_index += 1
        output.append(line)
    return "\n".join(output).strip() + "\n"


ESSAY_TRANSITION_PATTERNS = (
    r"首先",
    r"其次",
    r"再次",
    r"最后",
    r"第一",
    r"第二",
    r"第三",
    r"第四",
    r"第五",
    r"第六",
    r"第七",
    r"第八",
    r"第九",
    r"第十",
    r"第一点",
    r"第二点",
    r"第三点",
    r"一方面",
    r"另一方面",
    r"总的来说",
    r"总体来看",
    r"综上所述",
    r"由此可见",
)


AI_CLICHE_REPLACEMENTS = (
    ("本文将从多个角度分析", "换个角度看"),
    ("本文将", "这篇想说清楚"),
    ("本文旨在", "这篇主要想说清楚"),
    ("本文试图", "这篇主要想说清楚"),
    ("值得注意的是，", ""),
    ("值得注意的是", "更值得注意的是"),
    ("需要注意的是，", ""),
    ("需要注意的是", "更麻烦的是"),
    ("不可否认的是，", ""),
    ("不可否认的是", ""),
    ("毋庸置疑，", ""),
    ("毋庸置疑", ""),
    ("可以说，", ""),
    ("可以说", ""),
    ("从某种意义上说，", ""),
    ("从某种意义上说", ""),
    ("在这个过程中，", ""),
    ("在这个过程中", ""),
    ("在这种情况下，", "这时候，"),
    ("在这种情况下", "这时候"),
    ("引发了广泛关注", "让很多人开始关注"),
    ("引发广泛关注", "让很多人开始关注"),
    ("引起热议", "被很多人讨论"),
    ("引发热议", "被很多人讨论"),
    ("相关部门", "有关方面"),
    ("业内人士", "一些从业者"),
    ("广大网友", "不少网友"),
    ("对于普通人而言", "放到普通人身上"),
    ("对于普通用户而言", "放到普通用户身上"),
    ("这对普通人而言", "放到普通人身上"),
    ("这对普通用户而言", "放到普通用户身上"),
)


HUMAN_STYLE_REPLACEMENTS = (
    ("综上所述，", ""),
    ("综上所述", ""),
    ("由此可见，", ""),
    ("由此可见", ""),
    ("总的来说，", ""),
    ("总的来说", ""),
    ("总体来看，", ""),
    ("总体来看", ""),
    ("具体来说，", ""),
    ("具体来说", ""),
    ("换言之，", "说白了，"),
    ("换言之", "说白了"),
    ("简而言之，", "说简单点，"),
    ("简而言之", "说简单点"),
    ("从用户角度来看，", "放到普通用户身上，"),
    ("从消费者角度来看，", "放到买东西的人身上，"),
    ("从公众角度来看，", "放到普通人身上，"),
    ("对于消费者来说，", "放到买东西的人身上，"),
    ("对于用户来说，", "放到普通用户身上，"),
    ("对于公众来说，", "放到普通人身上，"),
    ("这也提醒我们，", "这事也给了一个提醒："),
    ("这提醒我们，", "这事也给了一个提醒："),
    ("我们需要认识到，", ""),
    ("我们需要认识到", ""),
    ("需要认识到，", ""),
    ("需要认识到", ""),
    ("不难发现，", ""),
    ("不难发现", ""),
    ("显而易见，", ""),
    ("显而易见", ""),
)


CLASSROOM_HEADING_MAP = {
    "背景": "先把事情放回现实里",
    "事件背景": "先把事情放回现实里",
    "介绍": "先把事情放回现实里",
    "原因分析": "为什么会走到这一步",
    "影响分析": "它影响的不只是热闹",
    "风险分析": "真正麻烦的地方在这里",
    "应对建议": "普通人能做的，其实不复杂",
    "优化建议": "接下来可以这样看",
    "主要问题": "问题卡在这里",
    "解决方案": "能往前走的路在这里",
    "总结": "最后说句实在话",
    "结语": "最后说句实在话",
}


AI_CLICHE_PREFIX_PATTERNS = (
    r"近日[，,]\s*",
    r"据悉[，,]\s*",
    r"记者了解到[，,]\s*",
    r"有媒体报道[，,]\s*",
)


SOURCE_PLACEHOLDER_MARKER_RE = re.compile(
    r"(?:需补来源|待补来源|补来源|补充来源|需补充来源|待补充来源|"
    r"待核实|需核实|待确认|需确认|原文未明确|原文未说明|来源未明确|缺少来源|缺乏来源)"
)


SOURCE_PLACEHOLDER_PATTERNS = (
    r"【[^】]*(?:需补来源|待补来源|补来源|补充来源|需补充来源|待补充来源|待核实|需核实|待确认|需确认|原文未明确|原文未说明|来源未明确|缺少来源|缺乏来源)[^】]*】",
    r"\[[^\]]*(?:需补来源|待补来源|补来源|补充来源|需补充来源|待补充来源|待核实|需核实|待确认|需确认|原文未明确|原文未说明|来源未明确|缺少来源|缺乏来源)[^\]]*\]",
    r"（[^）]*(?:需补来源|待补来源|补来源|补充来源|需补充来源|待补充来源|待核实|需核实|待确认|需确认|原文未明确|原文未说明|来源未明确|缺少来源|缺乏来源)[^）]*）",
    r"\([^)]*(?:需补来源|待补来源|补来源|补充来源|需补充来源|待补充来源|待核实|需核实|待确认|需确认|原文未明确|原文未说明|来源未明确|缺少来源|缺乏来源)[^)]*\)",
    r"[，,；;、]?\s*(?:核心事实|具体细节|完整细节|这部分|相关细节)?(?:仍|还|尚|也)?(?:有待|需要|需|待)(?:进一步)?(?:补充|补|核实|确认|明确)(?:来源|材料|信息|细节|事实|数据)?",
    r"[，,；;、]?\s*(?:来源|材料|信息|细节|事实|数据)(?:仍|还|尚|也)?(?:不够|不足|有限|不完整|不明确|不清晰)",
)


def _remove_inline_source_placeholder_chunks(text: str) -> str:
    """Drop any bracketed editor note that still contains a source placeholder marker."""

    value = text
    for pattern in (r"【[^】]*】", r"\[[^\]]*\]", r"（[^）]*）", r"\([^)]*\)"):
        value = re.sub(
            pattern,
            lambda match: "" if SOURCE_PLACEHOLDER_MARKER_RE.search(match.group(0)) else match.group(0),
            value,
        )
    return value


def _remove_ai_cliche_text(text: str) -> str:
    value = text
    for old, new in AI_CLICHE_REPLACEMENTS:
        value = value.replace(old, new)
    for old, new in HUMAN_STYLE_REPLACEMENTS:
        value = value.replace(old, new)
    for pattern in AI_CLICHE_PREFIX_PATTERNS:
        value = re.sub(rf"^{pattern}", "", value.strip())
    value = re.sub(r"随着[^，。！？\n]{2,24}的发展[，,]\s*", "", value)
    value = re.sub(r"在[^，。！？\n]{2,24}时代[，,]\s*", "", value)
    value = re.sub(r"^(?:那么|因此|所以|然而|此外|与此同时)[，,]\s*", "", value.strip())
    value = re.sub(r"^(?:从[^，。！？\n]{1,18}(?:角度|层面)(?:来看|来说)?)[，,]\s*", "", value.strip())
    value = re.sub(r"^[一二三四五六七八九十]+是[，,、：:\s]*", "", value)
    return value.strip()


def _strip_essay_transition_prefix(text: str) -> str:
    """Remove school-essay style ordering words only when they are line prefixes."""
    value = text.strip()
    if not value:
        return value

    transition = "|".join(ESSAY_TRANSITION_PATTERNS)
    value = re.sub(rf"^(?:{transition})[，,、：:\s]+", "", value)
    value = re.sub(r"^第[一二三四五六七八九十]+(?:点|个|层|步|件事|部分)[是为：:，,、\s]*", "", value)
    value = re.sub(r"^[一二三四五六七八九十](?:是|来|要看|点是|个原因是)[：:，,、\s]*", "", value)
    value = re.sub(r"^(?:先|再)来看[：:，,、\s]*", "", value)
    value = _remove_ai_cliche_text(value)
    return value.strip() or text.strip()


def _strip_markdown_emphasis(text: str) -> str:
    value = (text or "").strip()
    value = re.sub(r"^\*\*(.+?)\*\*$", r"\1", value)
    value = re.sub(r"^__(.+?)__$", r"\1", value)
    return value.strip()


def _chat_fragment_from_list_body(body: str) -> list[str]:
    """Turn list-like notes into short WeChat-friendly paragraphs."""

    value = _remove_ai_cliche_text(_strip_essay_transition_prefix(body))
    value = re.sub(r"^\*\*(.+?)\*\*\s*[：:]\s*(.+)$", r"\1。\2", value)
    value = re.sub(r"^__(.+?)__\s*[：:]\s*(.+)$", r"\1。\2", value)

    label_match = re.match(r"^(.{2,18}?)[：:]\s*(.+)$", value)
    if label_match:
        label, rest = label_match.groups()
        label = _strip_markdown_emphasis(label).strip(" ：:")
        rest = rest.strip()
        if label and rest and not re.search(r"[。！？!?]$", label):
            value = f"{label}。{rest}"

    value = re.sub(r"\*\*(.{2,18}?)\*\*", r"\1", value)
    value = re.sub(r"__(.{2,18}?)__", r"\1", value)
    value = re.sub(r"\s+", " ", value).strip()
    return [value] if value else []


def _convert_ordered_list_line(line: str) -> str:
    ordered_list_match = re.match(r"^(\s*)(?:\d+|[一二三四五六七八九十]+)[.．、)）]\s+(.+?)\s*$", line)
    if not ordered_list_match:
        return line
    indent, body = ordered_list_match.groups()
    body = _remove_ai_cliche_text(_strip_essay_transition_prefix(body))
    return f"{indent}{body}"


def _soften_listicle_heading(heading: str) -> str:
    value = _remove_ai_cliche_text(_strip_essay_transition_prefix(heading)).strip()
    value = CLASSROOM_HEADING_MAP.get(value.strip(" ：:"), value)
    value = CLASSROOM_HEADING_MAP.get(value.strip(" ：:"), value)
    normalized = re.sub(r"\s+", "", value)
    if re.search(r"(作为消费者|消费者).*(需要知道|注意|怎么做|怎么办)", normalized):
        return "先别急，给自己留点余地"
    if re.search(r"(你|我们|普通人).*(需要知道|需要注意|怎么做|怎么办)", normalized):
        return "这事落到自己身上，其实很具体"
    if re.search(r"(建议|应对|避坑|维权|处理办法|注意事项)", normalized):
        return "普通人能做的，其实不复杂"
    if re.search(r"(以下|几点|清单|攻略|指南|一文看懂)", normalized):
        return "把话说得简单一点"
    return value


def _de_listify_article_draft(content_md: str) -> str:
    """Avoid classroom/listicle formatting; keep the article in chat-like paragraphs."""

    output: list[str] = []
    for raw_line in (content_md or "").splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            output.append("")
            continue

        heading_match = re.match(r"^(#{1,6}\s+)(.+?)\s*$", line)
        if heading_match:
            prefix, heading = heading_match.groups()
            output.append(f"{prefix}{_soften_listicle_heading(heading)}")
            continue

        list_match = re.match(r"^\s*(?:[-*+•]\s+|(?:\d+|[一二三四五六七八九十]+)[.．、)）]\s+)(.+?)\s*$", line)
        if list_match:
            fragments = _chat_fragment_from_list_body(list_match.group(1))
            if output and output[-1] != "":
                output.append("")
            for fragment in fragments:
                output.append(fragment)
                output.append("")
            continue

        label_line_match = re.match(r"^(\*\*.+?\*\*|__.{2,24}?__|.{2,18}?)[：:]\s*(.+)$", stripped)
        if label_line_match and not stripped.startswith(("http://", "https://")):
            output.extend(_chat_fragment_from_list_body(stripped))
            continue

        output.append(line)

    cleaned_content = "\n".join(output)
    cleaned_content = re.sub(r"\n{3,}", "\n\n", cleaned_content).strip()
    return cleaned_content + "\n" if cleaned_content else ""


def _apply_wechat_conversational_style(content_md: str) -> str:
    """Make the draft closer to common WeChat reading: short, chatty, non-report-like."""

    lines = (content_md or "").splitlines()
    output: list[str] = []
    body_seen = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            output.append("")
            continue

        heading_match = re.match(r"^(#{1,6}\s+)(.+?)\s*$", line)
        if heading_match:
            prefix, heading = heading_match.groups()
            heading = _soften_listicle_heading(heading)
            heading = re.sub(r"^(?:一|二|三|四|五|六|七|八|九|十)[、.．：:）)]\s*", "", heading)
            output.append(f"{prefix}{heading}")
            continue

        if stripped.startswith(("!", "|")):
            output.append(line)
            continue

        cleaned = _remove_ai_cliche_text(_strip_essay_transition_prefix(stripped))
        cleaned = re.sub(r"^(?:这篇文章|本文|本篇文章)(?:主要)?(?:想|要|将)?(?:讨论|分析|介绍|说明)[^。！？]{0,30}[。！？]?", "", cleaned).strip()
        cleaned = re.sub(r"^(?:接下来|下面)(?:我们)?(?:来看|聊聊|说说)[：:，,、\s]*", "", cleaned).strip()
        cleaned = re.sub(r"(。)\s*(说白了|其实|但|不过|问题是|更麻烦的是)", r"\1\n\n\2", cleaned)
        cleaned = re.sub(r"(？)\s*(答案|问题|麻烦|关键)", r"\1\n\n\2", cleaned)

        fragments = [fragment.strip() for fragment in cleaned.split("\n\n") if fragment.strip()]
        for fragment in fragments:
            if not body_seen:
                body_seen = True
                if re.match(r"^(?:这个|这次|这件事|该事件|相关事件)[^。！？]{0,40}(?:引发|受到|成为)[^。！？]{0,40}(?:关注|热议)", fragment):
                    fragment = "这事之所以会被很多人讨论，不只是因为它上了热搜。"
            output.append(fragment)

    cleaned_content = "\n".join(output)
    cleaned_content = re.sub(r"\n{3,}", "\n\n", cleaned_content).strip()
    return cleaned_content + "\n" if cleaned_content else ""


def _decompose_school_essay_style(content_md: str) -> str:
    """Make drafts less like exam essays: no 第一/第二/第三 skeletons or ordered lists."""
    output: list[str] = []

    for line in (content_md or "").splitlines():
        stripped = line.strip()
        if not stripped:
            output.append(line)
            continue

        heading_match = re.match(r"^(#{2,6}\s+)(.+?)\s*$", line)
        if heading_match:
            prefix, heading = heading_match.groups()
            heading = re.sub(r"^(?:第?[一二三四五六七八九十]+[、.．：:）)]\s*)", "", heading.strip())
            heading = _strip_essay_transition_prefix(heading)
            output.append(f"{prefix}{_soften_listicle_heading(heading)}")
            continue

        converted_list_line = _convert_ordered_list_line(line)
        if converted_list_line != line:
            if output and output[-1] != "":
                output.append("")
            output.append(converted_list_line)
            output.append("")
            continue

        bullet_match = re.match(r"^(\s*[-*+]\s+)(.+?)\s*$", line)
        if bullet_match:
            _, body = bullet_match.groups()
            if output and output[-1] != "":
                output.append("")
            for fragment in _chat_fragment_from_list_body(body):
                output.append(fragment)
                output.append("")
            continue

        quote_match = re.match(r"^(\s*>\s*)(.+?)\s*$", line)
        if quote_match:
            prefix, body = quote_match.groups()
            output.append(f"{prefix}{_strip_essay_transition_prefix(body)}")
            continue

        leading_space = line[: len(line) - len(line.lstrip())]
        cleaned = _strip_essay_transition_prefix(stripped)
        output.append(f"{leading_space}{cleaned}")

    cleaned_content = "\n".join(output)
    cleaned_content = re.sub(r"\n{3,}", "\n\n", cleaned_content).strip()
    return cleaned_content + "\n" if cleaned_content else ""


def _remove_source_placeholders(content_md: str) -> str:
    """Remove internal fact-check placeholders from publishable drafts.

    The prompt asks the model to stay cautious, but draft content itself should not
    expose editor-only markers such as "【需补来源，原文未明确后续】".
    """
    output: list[str] = []
    for line in (content_md or "").splitlines():
        cleaned = _remove_inline_source_placeholder_chunks(line)
        for pattern in SOURCE_PLACEHOLDER_PATTERNS:
            cleaned = re.sub(pattern, "", cleaned)
        cleaned = _remove_inline_source_placeholder_chunks(cleaned)
        cleaned = re.sub(r"虽然([^。！？\n]{0,80}?)(?:但|但是)", r"\1。", cleaned)
        cleaned = re.sub(r"\s+([，。！？；：,.!?;:])", r"\1", cleaned)
        cleaned = re.sub(r"([（(])\s+", r"\1", cleaned)
        cleaned = re.sub(r"\s+([）)])", r"\1", cleaned)
        cleaned = re.sub(r"[，,；;、]\s*([。！？；;,.!?])", r"\1", cleaned)
        cleaned = re.sub(r"([。！？])\s*[，,；;]\s*", r"\1", cleaned)
        cleaned = re.sub(r"([。！？]){2,}", r"\1", cleaned)
        cleaned = re.sub(r"([，,；;、]){2,}", r"\1", cleaned)
        cleaned = re.sub(r"^[，,；;、]\s*", "", cleaned)
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).rstrip()

        stripped = cleaned.strip()
        if SOURCE_PLACEHOLDER_MARKER_RE.search(stripped):
            continue
        if not stripped and line.strip():
            continue
        if re.fullmatch(r"#{1,6}\s*", stripped):
            continue
        if stripped in {"-", "*", "+", "—", "，", "。", "；", "：", ",", ".", ";", ":"}:
            continue
        output.append(cleaned)

    cleaned_content = "\n".join(output)
    cleaned_content = re.sub(r"\n{3,}", "\n\n", cleaned_content).strip()
    return cleaned_content + "\n" if cleaned_content else ""


def _humanize_article_draft(content_md: str) -> str:
    """Light cleanup for AI-ish writing without changing article facts."""
    output: list[str] = []
    for line in (content_md or "").splitlines():
        stripped = line.strip()
        if not stripped:
            output.append(line)
            continue

        converted_list_line = _convert_ordered_list_line(line)
        if converted_list_line != line:
            if output and output[-1] != "":
                output.append("")
            output.append(converted_list_line)
            output.append("")
            continue

        heading_match = re.match(r"^(#{1,6}\s+)(.+?)\s*$", line)
        if heading_match:
            prefix, heading = heading_match.groups()
            heading = _remove_ai_cliche_text(_strip_essay_transition_prefix(heading))
            heading = re.sub(r"^(?:原因|建议|影响|风险|背景|总结)[一二三四五六七八九十\d]*[：:、\s-]*", "", heading).strip()
            output.append(f"{prefix}{_soften_listicle_heading(heading or heading_match.group(2).strip())}")
            continue

        bullet_match = re.match(r"^(\s*[-*+]\s+)(.+?)\s*$", line)
        if bullet_match:
            _, body = bullet_match.groups()
            if output and output[-1] != "":
                output.append("")
            for fragment in _chat_fragment_from_list_body(body):
                output.append(fragment)
                output.append("")
            continue

        quote_match = re.match(r"^(\s*>\s*)(.+?)\s*$", line)
        if quote_match:
            prefix, body = quote_match.groups()
            output.append(f"{prefix}{_remove_ai_cliche_text(_strip_essay_transition_prefix(body))}")
            continue

        leading_space = line[: len(line) - len(line.lstrip())]
        cleaned = _remove_ai_cliche_text(_strip_essay_transition_prefix(stripped))
        output.append(f"{leading_space}{cleaned}")

    cleaned_content = "\n".join(output)
    cleaned_content = re.sub(r"\n{3,}", "\n\n", cleaned_content).strip()
    return cleaned_content + "\n" if cleaned_content else ""


def _extract_publishable_draft(content_md: str) -> str:
    content = (content_md or "").strip()
    if not content:
        return content
    content = re.sub(r"^```(?:markdown|md)?\s*", "", content, flags=re.I)
    content = re.sub(r"\s*```$", "", content)

    markers = [
        "六、优化后完整稿件",
        "六、优化后完整稿",
        "## 六、优化后完整稿件",
        "# 六、优化后完整稿件",
        "优化后完整稿件",
    ]
    for marker in markers:
        index = content.find(marker)
        if index < 0:
            continue
        extracted = content[index + len(marker) :].strip()
        extracted = re.sub(r"^[：:\-\s]+", "", extracted).strip()
        next_section = re.search(r"\n(?:#{1,3}\s*)?七、备选标题", extracted)
        if next_section:
            extracted = extracted[: next_section.start()].strip()
        if extracted:
            content = extracted
            break

    heading_match = re.search(r"(?m)^#\s+.+$", content)
    if heading_match and heading_match.start() > 0:
        content = content[heading_match.start() :].strip()
    return content.strip() + "\n"


def _trim_article_if_needed(content_md: str, max_chars: int = 2600) -> str:
    """Keep generated WeChat drafts compact without cutting through the title."""

    content = (content_md or "").strip()
    if not content or len(content) <= max_chars:
        return content + "\n" if content else ""

    lines = content.splitlines()
    title_line = lines[0] if lines and lines[0].startswith("#") else ""
    body_lines = lines[1:] if title_line else lines

    sections: list[list[str]] = []
    current: list[str] = []
    for line in body_lines:
        if line.startswith("## ") and current:
            sections.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append(current)

    kept_sections = sections[:4] if sections else [body_lines]
    rebuilt_lines = [title_line, ""] if title_line else []
    for section in kept_sections:
        if section and section[0].startswith("## "):
            rebuilt_lines.extend(section)
        else:
            rebuilt_lines.extend(section[:8])
        rebuilt_lines.append("")
        if len("\n".join(rebuilt_lines)) >= max_chars:
            break

    trimmed = "\n".join(rebuilt_lines).strip()
    if len(trimmed) > max_chars:
        trimmed = trimmed[:max_chars].rstrip()
        last_break = max(trimmed.rfind("\n\n"), trimmed.rfind("。"), trimmed.rfind("！"), trimmed.rfind("？"))
        if last_break > max_chars * 0.65:
            trimmed = trimmed[: last_break + 1].rstrip()
    return trimmed.strip() + "\n"


def _normalize_text_for_matching(text: str) -> str:
    value = re.sub(r"\s+", "", (text or "").lower())
    return re.sub(r"[“”\"'‘’`·•,，。！？!?:：;；、（）()【】\[\]<>《》/\\|—\-_=+]+", "", value)


def _looks_like_placeholder_source_text(text: str) -> bool:
    collapsed = re.sub(r"\s+", " ", (text or "").strip()).lower()
    if not collapsed:
        return True
    if collapsed in SOURCE_PLACEHOLDER_MARKERS:
        return True
    return len(collapsed) <= 48 and any(marker in collapsed for marker in SOURCE_PLACEHOLDER_MARKERS)


def _collect_source_support_text(cluster: dict, article_sources: list[dict]) -> tuple[str, int, int]:
    fragments = [
        str(cluster.get("canonical_title") or ""),
        str(cluster.get("cluster_summary") or ""),
    ]
    effective_source_count = 0
    support_chars = 0
    for source in (article_sources or [])[:12]:
        source_fragments: list[str] = []
        visible_chars = 0
        for field in ("title", "member_title", "summary", "content_text"):
            raw = (source.get(field) or "").strip()
            if not raw or _looks_like_placeholder_source_text(raw):
                continue
            source_fragments.append(raw[:1800] if field == "content_text" else raw[:400])
            visible_chars += len(re.sub(r"\s+", "", raw))
        if source_fragments:
            merged = " ".join(source_fragments)
            fragments.append(merged)
            support_chars += len(re.sub(r"\s+", "", merged))
            if visible_chars >= 24:
                effective_source_count += 1
    merged_fragments = "\n".join(fragment for fragment in fragments if fragment.strip())
    return merged_fragments, effective_source_count, support_chars


def _clean_title_segment(segment: str) -> str:
    value = re.sub(r"^\s*#\s*", "", (segment or "").strip())
    value = value.strip("《》“”\"'【】[]()（） ")
    changed = True
    while value and changed:
        changed = False
        for prefix in TITLE_ALIGNMENT_PREFIXES:
            if value.startswith(prefix) and len(value) - len(prefix) >= 2:
                value = value[len(prefix) :].lstrip()
                changed = True
        for suffix in TITLE_ALIGNMENT_SUFFIXES:
            if value.endswith(suffix) and len(value) - len(suffix) >= 2:
                value = value[: -len(suffix)].rstrip()
                changed = True
    return value.strip("，。！？、；：:,.!?-— ")


def _append_title_term(bucket: list[str], seen: set[str], term: str, *, min_len: int = 2) -> None:
    clean = _clean_title_segment(term)
    normalized = _normalize_text_for_matching(clean)
    if not clean or not normalized or len(normalized) < min_len:
        return
    if normalized.isdigit():
        return
    if normalized in TITLE_ALIGNMENT_GENERIC_TERMS:
        return
    if len(normalized) <= 2 and re.fullmatch(r"[a-z]+", normalized):
        return
    if normalized in seen:
        return
    seen.add(normalized)
    bucket.append(clean)


def _extract_title_keyword_pack(title: str) -> dict[str, list[str]]:
    plain_title = re.sub(r"^\s*#\s*", "", (title or "").strip())
    anchor_terms: list[str] = []
    overlap_terms: list[str] = []
    seen_anchor: set[str] = set()
    seen_overlap: set[str] = set()

    for match in re.finditer(r"[\u4e00-\u9fff]{0,4}[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*[\u4e00-\u9fff]{0,4}", plain_title):
        token = match.group(0)
        _append_title_term(anchor_terms, seen_anchor, token)
        _append_title_term(overlap_terms, seen_overlap, token)
        inner = re.sub(r"^[\u4e00-\u9fff]+|[\u4e00-\u9fff]+$", "", token)
        if inner and inner != token:
            _append_title_term(anchor_terms, seen_anchor, inner)
            _append_title_term(overlap_terms, seen_overlap, inner)

    for segment in re.split(r"[，。！？、；：,.!?/|｜\s—\-]+", plain_title):
        clean_segment = _clean_title_segment(segment)
        _append_title_term(anchor_terms, seen_anchor, clean_segment)
        _append_title_term(overlap_terms, seen_overlap, clean_segment)
        chinese_only = re.sub(r"[^\u4e00-\u9fff]", "", clean_segment)
        if 2 <= len(chinese_only) <= 8:
            _append_title_term(overlap_terms, seen_overlap, chinese_only)
        for n in (4, 3, 2):
            if len(chinese_only) < n:
                continue
            for idx in range(len(chinese_only) - n + 1):
                gram = chinese_only[idx : idx + n]
                if re.search(r"[的一了是让把别再这那有很太又也都]", gram):
                    continue
                _append_title_term(overlap_terms, seen_overlap, gram)
    return {
        "anchor_terms": anchor_terms,
        "overlap_terms": overlap_terms,
    }


def _replace_or_prepend_title_heading(content_md: str, title: str) -> str:
    heading = f"# {(title or '').strip()}".rstrip()
    content = (content_md or "").strip()
    if not content:
        return heading + "\n"
    lines = content.splitlines()
    if lines and lines[0].lstrip().startswith("#"):
        lines[0] = heading
        return "\n".join(lines).strip() + "\n"
    return f"{heading}\n\n{content}\n"


def _first_meaningful_paragraph(content_md: str) -> str:
    plain = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", content_md or "")
    lines = []
    for raw in plain.splitlines():
        line = raw.strip()
        if not line:
            if lines:
                break
            continue
        if line.startswith("#"):
            continue
        if line.startswith(">"):
            line = line.lstrip(">").strip()
        if line.startswith(("-", "*")):
            line = line.lstrip("-*").strip()
        if re.match(r"^\d+[.)、]\s*", line):
            line = re.sub(r"^\d+[.)、]\s*", "", line)
        if line:
            lines.append(line)
    return " ".join(lines)[:180]


def _looks_like_narrative_hook_title(title: str) -> bool:
    clean = re.sub(r"^\s*#\s*", "", (title or "").strip())
    if not clean:
        return False
    if any(marker in clean for marker in TITLE_NARRATIVE_MARKERS):
        return True
    return len(clean) >= 14 and any(punct in clean for punct in ("，", "：", "？", "!", "！"))


def _looks_too_generic_for_distribution(title: str) -> bool:
    clean = re.sub(r"^\s*#\s*", "", (title or "").strip())
    clean = re.sub(r"\s+", "", clean)
    if not clean:
        return True
    return any(re.search(pattern, clean) for pattern in TITLE_TOO_GENERIC_PATTERNS)


def _source_specific_terms(title: str, cluster: dict | None, article_sources: list[dict] | None) -> list[str]:
    pack = _extract_title_keyword_pack(title)
    support_text, _, _ = _collect_source_support_text(cluster or {"canonical_title": title}, article_sources or [])
    support_norm = _normalize_text_for_matching(support_text)
    terms: list[str] = []
    seen: set[str] = set()
    for term in pack["anchor_terms"] + pack["overlap_terms"]:
        norm = _normalize_text_for_matching(term)
        if not norm or norm in seen or norm in TITLE_ALIGNMENT_GENERIC_TERMS:
            continue
        if len(norm) < 2:
            continue
        if norm in support_norm:
            seen.add(norm)
            terms.append(term)
    return terms


def _build_engaging_fallback_title(
    fallback_title: str,
    content_md: str,
    cluster: dict | None,
    article_sources: list[dict] | None,
) -> str:
    fallback = (fallback_title or "").strip()
    if not fallback:
        return fallback_title
    paragraph = _first_meaningful_paragraph(content_md)
    cluster_summary = str((cluster or {}).get("cluster_summary") or "")
    source_text = " ".join(
        f"{source.get('title') or source.get('member_title') or ''} {source.get('summary') or ''}"
        for source in (article_sources or [])[:5]
    )
    combined = f"{fallback} {cluster_summary} {paragraph} {source_text}"

    if any(keyword in combined for keyword in TITLE_PRICE_DROP_KEYWORDS) and "二手车" in fallback:
        return "二手车卖早的人，可能已经开始后悔了"
    if any(keyword in combined for keyword in TITLE_PRICE_DROP_KEYWORDS) and "二手油车" in fallback:
        return "二手油车卖早的人，这一轮可能最难受"
    if "高考安检" in fallback:
        return "今年高考安检这道线，很多人可能会踩"
    if "构成作弊" in combined and "高考" in fallback:
        return "高考安检收紧后，这些东西别顺手带进考场"
    if "带入考场" in combined and "高考" in fallback:
        return "带进考场就麻烦了，今年高考安检盯得更细"
    if "双胞胎" in fallback and ("遇袭" in fallback or "一死一伤" in combined):
        return "双胞胎姐妹遇袭刷屏，先别急着下结论"
    if "包场" in fallback and ("电影" in combined or "票房" in combined or "明星" in combined):
        return "明星包场冲上热搜，重点不只是人缘"
    if "浪姐" in fallback and ("排名" in fallback or "人气" in fallback):
        return "浪姐人气排名刷屏，观众到底在看什么"
    if "AI" in fallback and "工具" in combined:
        return f"{fallback}，普通人真会用和跟风用差别很大"
    if len(fallback) <= 12:
        return f"{fallback}，和很多人想的可能不太一样"
    return fallback


def _should_upgrade_plain_title(
    candidate_title: str,
    fallback_title: str,
    weak_source: bool,
    overlap_ratio: float,
) -> bool:
    candidate = (candidate_title or "").strip()
    fallback = (fallback_title or "").strip()
    if not candidate:
        return False
    if _looks_like_narrative_hook_title(candidate):
        return False
    if candidate != fallback and len(candidate) >= 14:
        return False
    return overlap_ratio >= 0.18 and len(candidate) <= 14


def _ensure_title_alignment(
    draft_title: str,
    content_md: str,
    cluster: dict | None,
    article_sources: list[dict] | None,
) -> tuple[str, str, dict]:
    cluster = cluster or {}
    fallback_title = (cluster.get("canonical_title") or draft_title or "未命名标题").strip() or "未命名标题"
    candidate_title = re.sub(r"^\s*#\s*", "", (draft_title or "").strip()) or fallback_title
    support_text, effective_source_count, support_chars = _collect_source_support_text(cluster, article_sources or [])
    support_norm = _normalize_text_for_matching(support_text)
    plain_content = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", content_md or "")
    content_lines = plain_content.splitlines()
    if content_lines and content_lines[0].lstrip().startswith("#"):
        plain_content = "\n".join(content_lines[1:])
    content_norm = _normalize_text_for_matching(plain_content)
    keyword_pack = _extract_title_keyword_pack(candidate_title)
    anchor_terms = keyword_pack["anchor_terms"]
    overlap_terms = keyword_pack["overlap_terms"]

    supported_source_terms = [
        term for term in overlap_terms if _normalize_text_for_matching(term) and _normalize_text_for_matching(term) in support_norm
    ]
    supported_content_terms = [
        term for term in overlap_terms if _normalize_text_for_matching(term) and _normalize_text_for_matching(term) in content_norm
    ]

    supported_overlap_norms = {
        _normalize_text_for_matching(term)
        for term in supported_source_terms + supported_content_terms
        if _normalize_text_for_matching(term)
    }
    unsupported_anchor_terms: list[str] = []
    for term in anchor_terms:
        normalized = _normalize_text_for_matching(term)
        if not normalized:
            continue
        if normalized in support_norm or normalized in content_norm:
            continue
        if any(len(other) >= 2 and (other in normalized or normalized in other) for other in supported_overlap_norms):
            continue
        unsupported_anchor_terms.append(term)

    weak_source = effective_source_count < 1 or support_chars < 80
    unique_overlap_norms = {_normalize_text_for_matching(term) for term in overlap_terms if _normalize_text_for_matching(term)}
    overlap_ratio = len(supported_overlap_norms) / max(len(unique_overlap_norms), 1)
    source_anchor_supported = [
        term for term in anchor_terms if _normalize_text_for_matching(term) and _normalize_text_for_matching(term) in support_norm
    ]
    content_anchor_supported = [
        term for term in anchor_terms if _normalize_text_for_matching(term) and _normalize_text_for_matching(term) in content_norm
    ]
    unsupported_mixed_terms = [term for term in unsupported_anchor_terms if re.search(r"[A-Za-z0-9]", term)]
    specific_title_terms = _source_specific_terms(candidate_title, cluster, article_sources or [])
    generic_for_distribution = (
        candidate_title != fallback_title
        and _looks_too_generic_for_distribution(candidate_title)
        and len(specific_title_terms) < 1
    )

    reasons: list[str] = []
    should_fallback = False
    if generic_for_distribution:
        should_fallback = True
        reasons.append("标题过于泛化，缺少热搜核心名词，影响头条推荐点击")
    elif candidate_title != fallback_title and unsupported_mixed_terms:
        should_fallback = True
        reasons.append("标题引入来源和正文都未出现的具体词：" + "、".join(unsupported_mixed_terms[:3]))
    elif candidate_title != fallback_title and unsupported_anchor_terms and weak_source:
        should_fallback = True
        reasons.append("热点来源过弱，标题引入未被材料支撑的表述：" + "、".join(unsupported_anchor_terms[:3]))
    elif candidate_title != fallback_title and not source_anchor_supported and not content_anchor_supported:
        should_fallback = True
        reasons.append("标题与热点主题缺少明确重合词")
    elif candidate_title != fallback_title and weak_source and overlap_ratio < 0.18:
        should_fallback = True
        reasons.append(f"热点来源过弱（有效来源 {effective_source_count} 条）")
    elif candidate_title != fallback_title and not content_anchor_supported and overlap_ratio < 0.12:
        should_fallback = True
        reasons.append("标题关键信息没有在正文展开")

    final_title = candidate_title
    if should_fallback:
        if (
            not generic_for_distribution
            and _looks_like_narrative_hook_title(candidate_title)
            and not unsupported_mixed_terms
            and overlap_ratio >= 0.18
        ):
            reasons = ["标题与内容相关，保留更有传播性的表达"]
        else:
            final_title = _build_engaging_fallback_title(fallback_title, content_md, cluster, article_sources)
    elif _should_upgrade_plain_title(candidate_title, fallback_title, weak_source, overlap_ratio):
        upgraded_title = _build_engaging_fallback_title(candidate_title, content_md, cluster, article_sources)
        if upgraded_title and upgraded_title != candidate_title:
            final_title = upgraded_title
            reasons = ["原标题过于平直，已提升为更适合公众号打开的表达"]
    final_content = _replace_or_prepend_title_heading(content_md, final_title)
    report = {
        "changed": final_title != candidate_title,
        "original_title": candidate_title,
        "final_title": final_title,
        "fallback_title": fallback_title,
        "reason": "；".join(reasons) if reasons else "标题与来源、正文一致",
        "weak_source": weak_source,
        "effective_source_count": effective_source_count,
        "unsupported_terms": unsupported_anchor_terms[:6],
        "supported_terms": (supported_source_terms + supported_content_terms)[:8],
        "specific_title_terms": specific_title_terms[:8],
        "generic_for_distribution": generic_for_distribution,
        "overlap_ratio": round(overlap_ratio, 3),
    }
    return final_title, final_content, report


def generate_wechat_draft(
    llm_config: dict,
    cluster: dict,
    article_sources: list[dict],
) -> tuple[str, str, str, dict]:
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

    editorial_plan = _topic_editorial_plan(cluster)
    section_suggestions = _format_section_suggestions(editorial_plan["section_titles"])
    context = {
        "topic": str(cluster["canonical_title"]),
        "cluster_summary": str(cluster.get("cluster_summary", "")),
        "item_count": str(cluster.get("item_count", len(article_sources))),
        "sources": chr(10).join(source_lines),
        "editorial_plan_name": str(editorial_plan["name"]),
        "editorial_tone": str(editorial_plan["tone"]),
        "opening_strategy": str(editorial_plan["opening"]),
        "section_suggestions": section_suggestions,
        "purpose": "热点解读 / 科普 / 风险提醒",
        "expected_words": "900-1300 字",
        "timeliness": "来自当前热点聚类，存在一定时效性；具体事实以来源材料为准",
        "hottrend_style_brief": _wechat_hottrend_style_brief(),
    }

    default_prompt = """你要为公众号《这事和你有关》写一篇可直接发布的初稿。

账号读者不是专家，也不是来读新闻通稿的。他们更关心：这件热搜里的事，到底和我的生活、工作、钱包、隐私、安全感有什么关系。

这次写法参考成熟公众号的阅读习惯：开头像聊天，正文像解释，结尾像一句实在提醒。不要像新闻稿、课堂笔记、AI 总结或申论作文。

## 写作身份
你不是客服、不是公告、不是新闻记者，也不是“AI 助手”。
你是一位有经验的公众号主笔：懂科技生活、AI 工具、网络安全和社会热点，也懂普通人看到热搜时的困惑和情绪。

写出来要像一个懂行的朋友，在微信里认真把事情讲给读者听。

## 本文信息
- 主题：{topic}
- 类型：{editorial_plan_name}
- 目的：{purpose}
- 长度：{expected_words}
- 时效性：{timeliness}
- 热点摘要：{cluster_summary}
- 聚类成员数：{item_count}

## 这篇的写法方向
- 语气：{editorial_tone}
- 开头：{opening_strategy}
- 可以参考的小标题方向，但不要照抄：
{section_suggestions}

## 当前微信热搜文章的通用节奏
{hottrend_style_brief}

## 最重要的写作要求
- 第一段不要交代“近日”“据悉”“有媒体报道”，直接从读者能感知的场景、反差、利益冲突或一句判断切入。
- 开头可以像这样，但不要照抄：“你可能也遇到过这种情况……”“这事乍一看离普通人很远，但真正麻烦的地方在后面。”“别急着站队，先看它会怎么落到自己身上。”
- 每写一段，都问自己一句：这段和普通人有什么关系？如果没关系，就删掉。
- 少用大词，多用具体场景。比如不要只说“隐私风险”，要说“你填过的手机号、定位、照片，可能会被怎样使用”。
- 允许有态度，但态度要克制；不要喊口号，不要上价值，不要制造恐慌。
- 像人写文章：可以有停顿、有转折、有轻微口语感，例如“说真的”“别急”“问题是”“但这里有个坑”“说白了”；不要油腻，不要段段金句。
- 先给读者一个情绪或问题，再给信息；不要一上来铺背景、讲定义、列目录。
- 中间必须有一次“泼冷水”或“换个角度看”：别只说好，也别只吓人。
- 不要写成“第一、第二、第三”的作文，也不要写成固定模板报告。
- 不要写成知识点清单、课堂笔记、消费指南或说明书。不要用一串项目符号教读者“需要知道什么”。
- 如果要给建议，拆成几个聊天式小片段，用自然段讲完，像朋友提醒一句，而不是列条款。
- 不要输出诊断、说明、标题清单，只输出正文 Markdown。

## 事实边界
- 所有具体事实只能来自下面“来源材料”。
- 不确定的内容不要写进正文；要么删掉，要么改成“目前公开信息还不充分”“现在还不能下结论”这类谨慎表达。
- 严禁输出【需补来源】、【待核实】、“原文未明确”等内部占位标记。
- 不要编造数字、案例、专家观点、后续进展和具体出处。

## 绝对不要出现的味道
- 新闻稿腔：近日、据悉、记者了解到、引发广泛关注、相关部门表示、业内人士认为。
- AI 腔：随着……的发展、在这个……时代、不仅……而且、综上所述、由此可见、让我们一起、值得注意的是。
- 作文腔：首先、其次、最后、一方面、另一方面、第一点、第二点、总的来说、本文将、本文认为。
- 报告腔：背景介绍、原因分析、影响分析、应对建议、主要问题、解决方案。
- 恐吓腔：细思极恐、所有人都危险了、赶紧删、再不看就晚了。

## 推荐文章节奏
- 开头：一个读者熟悉的场景 / 一个反常识问题 / 一个跟钱包、工作、隐私、安全感有关的冲突。
- 第二段：把热搜翻译成人话，告诉读者“这事到底在说什么”。
- 中段：讲清真正值得关注的变化或坑，不要堆资料。
- 转折：补一句冷静判断，避免站队和夸大。
- 结尾：用一句实在话收住，不要升华成作文。

## 输出格式
- 第一行必须是 Markdown 一级标题：# 标题
- 标题要像公众号标题，短一点、有代入感，不要只复制热搜标题。
- 标题必须保留热搜核心名词、人名、平台名或事件名里的至少一个具体词；不能只写“这事”“你可能也刷到了”“朋友圈刷屏”“先别急着下结论”这种看不出主题的泛标题。
- 标题适配今日头条推荐流：前半句先给具体对象，后半句给普通人的阅读理由，例如“浪姐人气排名刷屏，观众到底在看什么”，不要让标题和正文脱节。
- 正文 3-4 个自然小标题即可，小标题要像编辑起的，不要像报告目录；不要写“作为消费者，你需要知道什么？”“原因分析”“应对建议”这类课堂式标题。
- 每段 1-3 句话，适合手机阅读。
- 尽量不用列表。除非必须列出步骤，否则不要用短横线 bullet；更不要用有序编号。
- 结尾自然收住，可以是一句个人化提醒或判断；不要写“欢迎留言”“对此你怎么看”。
- 不要写“配图”“见下图”“图片占位符”，图片会由程序自动插入。

## 来源材料
{sources}
"""
    custom_prompt = (llm_config.get("draft_prompt") or "").strip()
    prompt = _render_prompt_template(custom_prompt or default_prompt, context)
    style_guard = """# 最后一遍自检后再输出
- 读起来必须像人写的公众号文章，而不是系统说明、新闻稿、AI 总结或考试作文。
- 开头 120 字内必须让读者知道“这事和我有什么关系”。
- 第一段必须有场景、问题、反差或判断，不能是背景介绍。
- 中间要有一句冷静转折，例如“但别急着高兴/担心”“问题是”“说白了”这类自然表达。
- 不要使用 Markdown 有序列表（1. / 2. / 3.）或“一是、二是、三是”。
- 尽量不要用短横线 bullet。不要把建议写成清单，改成几个短小自然段，像聊天一样把话说明白。
- 小标题要自然、有情绪温度，不要写成“原因一/原因二/影响分析/优化建议/作为消费者需要知道什么/背景介绍”。
- 删除所有内部占位标记：例如【需补来源】、【待核实】、“原文未明确”。
"""
    prompt = f"{prompt.rstrip()}\n\n{style_guard}"

    payload = {
        "model": llm_config["model"],
        "messages": [
            {
                "role": "system",
                "content": "你是中文公众号资深主笔。写得像人：开头像聊天，正文像解释，结尾像一句实在提醒；有场景、有判断、有共鸣，但克制、不煽动、不编造。只输出可发布的 Markdown 正文；不要新闻稿腔、AI 总结腔、系统说明腔、考试作文腔、报告腔。事实必须来自用户提供的来源材料，不确定就删掉或改成谨慎表述，严禁输出【需补来源】、【待核实】、“原文未明确”等内部占位标记。",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": llm_config.get("temperature", 0.65),
        "max_tokens": llm_config.get("max_tokens", 2400),
    }

    response = _post_json_with_retry(
        f"{llm_config['base_url'].rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {llm_config['api_key']}",
            "Content-Type": "application/json",
        },
        payload=payload,
        timeout=int(llm_config.get("timeout_seconds", 180)),
        retry_count=int(llm_config.get("retry_count", 3)),
        backoff_seconds=float(llm_config.get("retry_backoff_seconds", 2.0)),
    )
    data = response.json()
    content = _soften_generic_headings(data["choices"][0]["message"]["content"].strip(), editorial_plan)
    content = _decompose_school_essay_style(content)
    content = _humanize_article_draft(content)
    content = _de_listify_article_draft(content)
    content = _apply_wechat_conversational_style(content)
    content = _remove_source_placeholders(content)
    content = _trim_article_if_needed(content, max_chars=int(llm_config.get("draft_max_chars", 2600)))

    title = cluster["canonical_title"]
    first_line = content.splitlines()[0] if content else ""
    if first_line.startswith("#"):
        title = first_line.lstrip("#").strip() or title

    title, content, alignment_report = _ensure_title_alignment(title, content, cluster, article_sources)
    return title, content, prompt[:1200], alignment_report



def polish_wechat_draft_after_self_review(
    llm_config: dict,
    title: str,
    content_md: str,
    cluster: dict | None = None,
    source_materials: list[dict] | None = None,
    max_chars: int | None = None,
) -> tuple[str, str, str, dict]:
    """Ask the writing model to read the draft once, fix issues, and return publishable Markdown."""
    plain_content = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", content_md or "")
    plain_content = re.sub(r"\n{3,}", "\n\n", plain_content).strip()
    source_lines: list[str] = []
    for idx, source in enumerate((source_materials or [])[:8], start=1):
        source_lines.append(
            "\n".join(
                [
                    f"来源{idx}：{source.get('board_name') or source.get('source_host') or '资料'} / {source.get('source_url') or source.get('final_url') or ''}",
                    f"标题：{source.get('title') or source.get('member_title') or ''}",
                    f"摘要：{source.get('summary') or ''}",
                    f"正文摘录：{(source.get('content_text') or '')[:900]}",
                ]
            ).strip()
        )
    source_block = "\n\n".join(source_lines).strip() or "无额外来源材料；只能基于初稿做风格、结构和明显风险修正，不能新增事实。"
    max_chars = int(max_chars or llm_config.get("draft_max_chars", 2600))
    prompt = f"""你是公众号《这事和你有关》的终审编辑。

下面是一篇刚生成的公众号初稿。请你先默读一遍，直接改成更适合发布的版本。

改稿目标：像一个懂行朋友在微信里讲事。开头像聊天，正文像解释，结尾像一句实在提醒。不要像新闻稿、课堂笔记、AI 总结、申论作文或行业报告。

当前微信热搜文章更常见的写法是：先抛冲突或结果，再把普通人最关心的影响说清楚；短段落推进；小标题自然；配图按题材匹配，不平均塞图。

重点检查并修正：
- 有没有事实边界问题：没有来源支撑的具体数字、案例、专家观点、后续进展要删掉或改谨慎。
- 有没有内部占位：删除【需补来源】、【待核实】、“原文未明确”等。
- 有没有 AI 味、新闻稿味、系统味、作文味、报告味：去掉“近日、据悉、本文将、值得注意的是、随着……的发展、综上所述、首先其次最后、背景介绍、原因分析、应对建议”等。
- 开头是否能抓住普通读者：前 120 字内讲清“这事和我有什么关系”。
- 开头不能是背景介绍；要换成场景、问题、反差、利益冲突或一句判断。
- 标题是否完整、有代入感、不标题党。
- 标题是否能一眼看出具体热点：必须保留核心名词、人名、平台名或事件名里的至少一个具体词；不要改成“这事”“你可能也刷到了”“朋友圈刷屏”这种泛标题。
- 段落是否适合微信：短段落、自然小标题，不要长篇报告。
- 有没有像课堂笔记、知识点清单、消费指南：如果有，把项目符号和条款改成聊天式小片段。
- 不要出现“作为消费者，你需要知道什么？”“以下几点”“注意事项”“一文看懂”这类让人抵触的标题。
- 小标题要像编辑写的，不要像目录。可以用“先别急着下结论”“真正麻烦的是这里”“说到底，别把自己放进坑里”这种感觉，但不要照抄太多。
- 中间加一处自然转折：别只讲好处，也别只制造焦虑。用“但别急”“问题是”“说白了”“换个角度看”这类口语表达把节奏拉回来。
- 不要写“配图”“见下图”“图片占位符”。
- 如果正文里出现“第一、第二、第三”“原因分析”“应对建议”“背景介绍”这类目录感表达，尽量改成聊天式小片段。

硬性要求：
- 只输出改正后的完整 Markdown 正文。
- 第一行必须是一级标题：# 标题
- 不要输出诊断、修改说明、JSON、评分或任何额外解释。
- 不要新增来源材料里没有的事实。
- 尽量不用列表；必须给建议时，也写成自然短段落，不要像学习资料。
- 删除“本文/本文将/本文认为/综上所述/由此可见”等作文痕迹。
- 篇幅控制在 {max_chars} 字以内。

原标题：{title}

初稿：
{plain_content[:7000]}

可核对来源材料：
{source_block[:9000]}
"""
    payload = {
        "model": llm_config["model"],
        "messages": [
            {
                "role": "system",
                "content": "你是中文公众号终审编辑。任务是直接改稿，不解释；保留核心观点，修正事实边界、AI味、新闻稿味、作文味、报告味和微信阅读节奏问题。最终稿要像懂行朋友在微信里聊天，不像机器总结。",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": min(float(llm_config.get("temperature", 0.65)), 0.45),
        "max_tokens": llm_config.get("max_tokens", 2400),
    }
    response = _post_json_with_retry(
        f"{llm_config['base_url'].rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {llm_config['api_key']}",
            "Content-Type": "application/json",
        },
        payload=payload,
        timeout=int(llm_config.get("timeout_seconds", 180)),
        retry_count=int(llm_config.get("retry_count", 3)),
        backoff_seconds=float(llm_config.get("retry_backoff_seconds", 2.0)),
    )
    data = response.json()
    polished = _extract_publishable_draft(data["choices"][0]["message"]["content"].strip())
    polished = _decompose_school_essay_style(polished)
    polished = _humanize_article_draft(polished)
    polished = _de_listify_article_draft(polished)
    polished = _apply_wechat_conversational_style(polished)
    polished = _remove_source_placeholders(polished)
    polished = _trim_article_if_needed(polished, max_chars=max_chars)
    if not polished.strip():
        polished = content_md

    polished_title = title
    first_line = polished.splitlines()[0] if polished else ""
    if first_line.startswith("#"):
        polished_title = first_line.lstrip("#").strip() or title
    polished_title, polished, alignment_report = _ensure_title_alignment(
        polished_title,
        polished,
        cluster or {"canonical_title": title},
        source_materials or [],
    )
    return polished_title, polished, prompt[:1200], alignment_report


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

    candidates: list[dict] = []
    for match in re.finditer(r"\{[^{}]*\}", content):
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                candidates.append(parsed)
        except json.JSONDecodeError:
            continue
    if not candidates:
        return None
    score_keys = {"score", "review_score", "article_score", "分数", "评分"}
    for candidate in reversed(candidates):
        if score_keys.intersection(candidate.keys()):
            return candidate
    return candidates[-1]


def _parse_review_score(text: str) -> float:
    def normalize_score(value: object) -> float | None:
        if isinstance(value, str):
            match = re.search(r"\d+(?:\.\d+)?", value)
            if not match:
                return None
            raw_score = float(match.group(0))
        else:
            raw_score = float(value)
        if raw_score > 10 and raw_score <= 100:
            raw_score = raw_score / 10
        return max(0.0, min(10.0, round(raw_score, 1)))

    data = _extract_json_object(text)
    if data:
        for key in ("score", "review_score", "article_score", "分数", "评分"):
            if key in data:
                try:
                    score = normalize_score(data[key])
                    if score is not None:
                        return score
                except (TypeError, ValueError):
                    continue

    score_patterns = (
        r"\"?(?:score|review_score|article_score)\"?\s*[:：]\s*\"?(\d+(?:\.\d+)?)",
        r"\"?(?:score|review_score|article_score)\"?\s*(?:设为|为|=)\s*\"?(\d+(?:\.\d+)?)",
        r"\"?(?:分数|评分|文章分)\"?\s*[:：]?\s*\"?(\d+(?:\.\d+)?)",
        r"(\d+(?:\.\d+)?)\s*/\s*10",
    )
    for pattern in score_patterns:
        match = re.search(pattern, text or "", flags=re.I)
        if match:
            value = float(match.group(1))
            if value > 10 and value <= 100:
                value = value / 10
            return max(0.0, min(10.0, round(value, 1)))

    raise ValueError("模型评分响应中未解析到 0-10 分数")


def _parse_review_summary(text: str) -> str:
    data = _extract_json_object(text)
    if data:
        for key in ("summary", "reason", "review_summary", "点评", "理由", "建议"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return re.sub(r"\s+", " ", value).strip()[:220]
        strengths = data.get("strengths")
        weaknesses = data.get("weaknesses")
        if isinstance(strengths, list) or isinstance(weaknesses, list):
            parts = []
            if strengths:
                parts.append("优点：" + "；".join(str(x) for x in strengths[:3]))
            if weaknesses:
                parts.append("待优化：" + "；".join(str(x) for x in weaknesses[:3]))
            if parts:
                return " ".join(parts)[:220]

    cleaned = re.sub(r"^```(?:json)?\s*", "", text or "", flags=re.I)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    for match in re.finditer(r'"summary"\s*:\s*"([^"]{6,260})', cleaned, flags=re.I):
        value = match.group(1).strip()
        if value:
            return re.sub(r"\s+", " ", value).strip()[:220]
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if "扣分项" in cleaned or "评分标准" in cleaned or len(cleaned) > 260:
        return "模型已完成审核评分；建议人工复核标题、结构、事实边界和手机端阅读节奏。"
    return cleaned[:220] or "模型已完成审核，但未返回明确点评。"


def review_wechat_draft(
    llm_config: dict,
    title: str,
    content_md: str,
) -> tuple[float, str, str]:
    plain_content = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", content_md or "")
    plain_content = re.sub(r"\n{3,}", "\n\n", plain_content).strip()
    prompt = f"""你是一名严格但务实的微信公众号主编，请审核下面这篇已经生成的公众号初稿，并给出可用于排序的文章质量分。

评分范围：0-10 分，保留 1 位小数。

当前更像头部微信热搜文章的内容，分数应更高；明显的新闻稿腔、作文腔、报告腔、列表腔要降分。

请按公众号发布前审核标准评分，重点看：
1. 标题是否清晰、有打开欲，但不标题党。
2. 开头是否能抓住读者，并快速交代价值。
3. 结构是否自然，不像固定模板或 AI 摘要。
4. 信息密度、事实边界、观点分层是否合格。
5. 段落节奏是否适合手机端阅读。
6. 是否具备发布可用度：越接近可直接发，分数越高。
7. 是否符合当前微信热搜文章的真实阅读节奏：开头快、段落短、表达口语化、标题和正文强相关。

扣分项：
- 明显空泛、模板化、复述材料、像新闻播报。
- 编造事实、过度煽动、事实与观点混在一起。
- 段落太长、标题生硬、结尾套路。
- 与微信公众号读者场景不匹配。

只输出 JSON，不要输出 Markdown，不要解释 JSON 之外的内容：
{{
  "score": 8.4,
  "summary": "一句话说明为什么给这个分数，并指出最需要优化的一点"
}}

文章标题：{title}

文章正文：
{plain_content[:6000]}
"""
    payload = {
        "model": llm_config["model"],
        "messages": [
            {
                "role": "system",
                "content": "你是中文微信公众号主编，负责审核文章质量并给出稳定、可比较的评分。",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 1200,
    }

    response = _post_json_with_retry(
        f"{llm_config['base_url'].rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {llm_config['api_key']}",
            "Content-Type": "application/json",
        },
        payload=payload,
        timeout=int(llm_config.get("timeout_seconds", 180)),
        retry_count=int(llm_config.get("retry_count", 3)),
        backoff_seconds=float(llm_config.get("retry_backoff_seconds", 2.0)),
    )
    data = response.json()
    message = data["choices"][0]["message"]
    raw = (message.get("content") or message.get("reasoning_content") or "").strip()
    score = _parse_review_score(raw)
    summary = _parse_review_summary(raw)
    return score, summary, prompt[:1200]


def _download_images(
    image_urls: list[str],
    month_dir: Path,
    stem_name: str,
    progress_cb=None,
    limit: int | None = None,
) -> list[str]:
    if not image_urls:
        return []
    try:
        asset_dir, stem_name = _ensure_asset_dir(month_dir, stem_name, progress_cb=progress_cb)
    except OSError as exc:
        _emit_optional(progress_cb, "warning", f"配图目录不可用，本次跳过下载配图：{exc}")
        return []
    saved_paths: list[str] = []
    seen = set()

    max_count = max(0, int(limit)) if limit is not None else 0
    for idx, image_url in enumerate(image_urls, start=1):
        if max_count and len(saved_paths) >= max_count:
            break
        if image_url in seen:
            continue
        if is_blocked_source_image_url(image_url):
            continue
        seen.add(image_url)
        try:
            response = requests.get(image_url, timeout=40)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if not content_type.startswith("image/") and "octet-stream" not in content_type:
                continue
            if _is_unusable_downloaded_image(response.content, image_url):
                continue
            ext = _guess_extension(content_type, image_url, response.content)
            target = asset_dir / f"img_{idx:02d}{ext}"
            target.write_bytes(response.content)
            relative = Path("assets") / stem_name / target.name
            saved_paths.append(relative.as_posix())
        except Exception:
            continue
    return saved_paths


def _simplify_image_search_topic(title: str, content_md: str) -> str:
    text = f"{title} {content_md[:1000]}"
    if _contains_any(text, ("AI", "人工智能", "大模型", "机器人", "智能体", "科技", "工具", "软件", "网络安全")):
        return "人工智能 科技 电脑 办公 真实 摄影"
    if _contains_any(text, ("旅游", "出行", "景区", "机票", "酒店", "高铁", "机场")):
        return "中国 出行 旅行 行李 机场 真实 摄影"
    if _contains_any(text, ("健康", "疾病", "医院", "症状", "睡眠", "饮食", "老人", "儿童")):
        return "健康 生活 医生 咨询 真实 摄影"
    if _contains_any(text, ("股市", "股票", "油价", "金融", "消费", "价格", "汽车", "房价")):
        return "财经 市场 数据 屏幕 办公室 真实 摄影"
    if _contains_any(text, ("学校", "学生", "老师", "硕士", "考试", "读书", "学习")):
        return "图书馆 书本 学习 真实 摄影"
    if _contains_any(text, ("职场", "员工", "工资", "公司", "裁员", "绩效", "年终奖")):
        return "职场 办公室 会议 真实 摄影"
    if _contains_any(text, ("安全", "诈骗", "隐私", "密码", "手机", "微信", "短信")):
        return "手机 网络安全 隐私 真实 摄影"
    return "城市 生活 手机 阅读 真实 摄影"


def _image_search_queries(title: str, content_md: str, image_config: dict) -> list[str]:
    search_config = image_config.get("search") or {}
    custom_query = (search_config.get("query") or "").strip()
    if custom_query:
        return [custom_query]

    image_plan = _article_image_plan(title, content_md, image_config)
    base = (image_plan.get("search_hint") or _simplify_image_search_topic(title, content_md)).strip()

    return [
        base,
        f"{base} 生活 场景 无水印",
        f"{base} editorial stock photo no watermark",
    ]


def _extract_bing_image_urls(html_text: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for pattern in (
        r'"murl"\s*:\s*"([^"]+)"',
        r"&quot;murl&quot;\s*:\s*&quot;([^&]+)&quot;",
        r"murl&quot;:&quot;([^&]+)&quot;",
    ):
        for raw in re.findall(pattern, html_text or "", flags=re.I):
            url = html.unescape(raw).encode("utf-8", "ignore").decode("unicode_escape", "ignore")
            url = url.replace("\\/", "/").strip()
            if not url or url in seen or is_blocked_source_image_url(url):
                continue
            if not re.match(r"^https?://", url, re.I):
                continue
            seen.add(url)
            urls.append(url)
    return urls


def _search_web_images(title: str, content_md: str, image_config: dict, progress_cb=None) -> list[str]:
    search_config = image_config.get("search") or {}
    if search_config.get("enabled") is False:
        return []

    max_candidates = max(1, int(search_config.get("max_candidates", 18)))
    timeout_seconds = int(search_config.get("timeout_seconds", 15))
    queries = _image_search_queries(title, content_md, image_config)
    found: list[str] = []
    seen: set[str] = set()

    def emit(level: str, message: str) -> None:
        if progress_cb:
            progress_cb(level, message)

    for query in queries:
        if len(found) >= max_candidates:
            break
        try:
            url = f"https://www.bing.com/images/search?q={quote_plus(query)}&form=HDRSC2&first=1&adlt=strict"
            response = requests.get(url, headers=IMAGE_SEARCH_HEADERS, timeout=timeout_seconds)
            response.raise_for_status()
            for image_url in _extract_bing_image_urls(response.text):
                if image_url in seen:
                    continue
                seen.add(image_url)
                found.append(image_url)
                if len(found) >= max_candidates:
                    break
        except Exception as exc:
            emit("warning", f"联网搜图失败：{query}｜{type(exc).__name__}: {str(exc)[:100]}")

    if found:
        emit("info", f"联网搜图候选：{len(found)} 张")
    return found


def _is_unusable_downloaded_image(body: bytes, url: str = "") -> bool:
    if not body or len(body) < 8 * 1024:
        return True
    if is_blocked_source_image_url(url):
        return True
    try:
        from io import BytesIO

        with Image.open(BytesIO(body)) as image:
            width, height = image.size
    except (UnidentifiedImageError, OSError, ValueError):
        return True
    return is_unusable_image_dimensions(width, height, url)


def _generate_ai_images(
    title: str,
    content_md: str,
    image_config: dict,
    day_dir: Path,
    stem_name: str,
    progress_cb=None,
) -> tuple[list[str], list[str]]:
    generation_config = image_config.get("generation") or {}
    base_url = (generation_config.get("base_url") or "").strip()
    model = (generation_config.get("model") or "").strip()
    endpoint = _image_generation_endpoint(base_url)
    if not endpoint or not model:
        return [], []

    image_plan = _article_image_plan(title, content_md, image_config)
    prompts = _build_image_prompts(title, content_md, image_config, image_plan=image_plan)
    if not prompts:
        return [], []

    def emit(level: str, message: str) -> None:
        if progress_cb:
            progress_cb(level, message)

    try:
        asset_dir, stem_name = _ensure_asset_dir(day_dir, stem_name, progress_cb=progress_cb)
    except OSError as exc:
        emit("warning", f"配图目录不可用，本次跳过 AI 生图：{exc}")
        return [], []
    configured_timeout_seconds = int(generation_config.get("timeout_seconds", 180))
    total_timeout_seconds = int(
        generation_config.get(
            "total_timeout_seconds",
            max(configured_timeout_seconds + 30, configured_timeout_seconds * 2),
        )
    )
    timeout_seconds = min(configured_timeout_seconds, max(10, total_timeout_seconds))
    disable_env_proxy = bool(generation_config.get("disable_env_proxy", True))
    size = (generation_config.get("size") or "1024x1024").strip()
    api_key = (generation_config.get("api_key") or "").strip()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    max_workers = max(1, int(generation_config.get("concurrency", min(4, len(prompts)))))
    max_workers = min(max_workers, len(prompts))
    emit(
        "info",
        (
            f"AI 生图开始：{len(prompts)} 张｜题材={image_plan.get('topic_label')}｜"
            f"段落={image_plan.get('paragraph_count')}｜并发={max_workers}｜"
            f"单张超时={timeout_seconds}s｜整批上限={total_timeout_seconds}s"
        ),
    )

    def make_session() -> requests.Session:
        session = requests.Session()
        if disable_env_proxy:
            session.trust_env = False
        return session

    def generate_one(idx: int, prompt: str) -> tuple[int, str, str] | None:
        payload = {
            "model": model,
            "prompt": prompt,
            "n": 1,
            "size": size,
        }
        try:
            with make_session() as session:
                response = _post_json_with_retry(
                    endpoint,
                    headers=headers,
                    payload=payload,
                    timeout=timeout_seconds,
                    retry_count=int(generation_config.get("retry_count", 2)),
                    backoff_seconds=float(generation_config.get("retry_backoff_seconds", 3.0)),
                    session=session,
                )
                data = response.json()
                image_payloads = _extract_image_payloads(data)
                image_data: tuple[bytes, str] | None = None
                for image_payload in image_payloads:
                    image_data = _image_bytes_from_payload(image_payload, timeout_seconds=timeout_seconds, session=session)
                    if image_data:
                        break
            if not image_data:
                return None
            body, content_type = image_data
            ext = _guess_extension(content_type, "", body)
            target = asset_dir / f"ai_img_{idx:02d}{ext}"
            target.write_bytes(body)
            relative = Path("assets") / stem_name / target.name
            emit("success", f"AI 生图成功：第 {idx} 张")
            return idx, relative.as_posix(), prompt
        except Exception as exc:
            emit("warning", f"AI 生图失败：第 {idx} 张｜{type(exc).__name__}: {str(exc)[:120]}")
            return None

    results: list[tuple[int, str, str]] = []
    executor = ThreadPoolExecutor(max_workers=max_workers)
    timed_out = False
    try:
        futures = [executor.submit(generate_one, idx, prompt) for idx, prompt in enumerate(prompts, start=1)]
        try:
            for future in as_completed(futures, timeout=total_timeout_seconds):
                result = future.result()
                if result:
                    results.append(result)
        except TimeoutError:
            timed_out = True
            for future in futures:
                future.cancel()
            emit(
                "warning",
                f"AI 生图整体超时：{total_timeout_seconds}s 内完成 {len(results)}/{len(prompts)} 张，未完成任务已跳过",
            )
    finally:
        executor.shutdown(wait=not timed_out, cancel_futures=timed_out)

    results.sort(key=lambda item: item[0])
    saved_paths = [path for _, path, _ in results]
    used_prompts = [prompt for _, _, prompt in results]

    if used_prompts:
        prompt_file = asset_dir / "ai_image_prompts.txt"
        try:
            prompt_file.write_text(
                "\n\n".join(f"--- image {idx} ---\n{prompt}" for idx, prompt in enumerate(used_prompts, start=1)),
                encoding="utf-8",
            )
        except OSError as exc:
            emit("warning", f"AI 生图提示词记录写入失败，已忽略：{exc}")

    return saved_paths, used_prompts


def _inject_images_into_markdown(content_md: str, image_paths: list[str], positions: list[int] | None = None) -> str:
    if not image_paths:
        return content_md

    lines = content_md.splitlines()
    output: list[str] = []
    image_index = 0
    body_paragraph_count = 0
    inserted_after_opening = False
    insert_positions = [max(1, int(pos)) for pos in (positions or [])]

    for line in lines:
        output.append(line)
        stripped = line.strip()
        if image_index >= len(image_paths):
            continue

        is_body_paragraph = bool(stripped) and not (
            stripped.startswith("#")
            or stripped.startswith("!")
            or stripped.startswith(">")
            or stripped.startswith("- ")
            or stripped.startswith("* ")
            or stripped.startswith("|")
            or re.match(r"^\d+[.)、]\s+", stripped)
        )
        if not is_body_paragraph:
            continue

        body_paragraph_count += 1
        should_insert = False
        if insert_positions:
            if body_paragraph_count >= insert_positions[min(image_index, len(insert_positions) - 1)]:
                should_insert = True
        elif not inserted_after_opening and body_paragraph_count >= 1:
            should_insert = True
            inserted_after_opening = True
        elif body_paragraph_count in (3, 6, 9):
            should_insert = True

        if should_insert:
            output.extend(["", f"![文章配图{image_index + 1}]({image_paths[image_index]})", ""])
            image_index += 1
            inserted_after_opening = True

    if image_index < len(image_paths):
        if output and output[-1].strip():
            output.append("")
        while image_index < len(image_paths):
            output.append(f"![文章配图{image_index + 1}]({image_paths[image_index]})")
            output.append("")
            image_index += 1

    return "\n".join(output).strip() + "\n"


def strip_markdown_images(content_md: str) -> str:
    output: list[str] = []
    skipping_image_gallery = False
    for line in content_md.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            heading = stripped.lstrip("#").strip()
            if heading in {"更多相关画面", "图集补充"}:
                skipping_image_gallery = True
                continue
            skipping_image_gallery = False

        if skipping_image_gallery:
            if stripped.startswith("## "):
                skipping_image_gallery = False
            else:
                continue

        if re.match(r"^!\[[^\]]*\]\([^)]+\)\s*$", stripped):
            continue
        output.append(line)

    cleaned = "\n".join(output)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned + "\n" if cleaned else ""


def archive_draft(
    output_dir: str,
    title: str,
    content_md: str,
    image_urls: list[str] | None = None,
    image_config: dict | None = None,
    progress_cb=None,
) -> tuple[str, int, str, str]:
    now, day_dir, target, stem_name = _prepare_archive_paths(output_dir, title, progress_cb=progress_cb)
    safe_title = sanitize_filename(title)
    image_config = image_config or {}
    image_plan = _article_image_plan(title, content_md, image_config)
    planned_count = max(0, int(image_plan.get("count") or image_config.get("max_per_draft", 0)))
    prefer_ai_generated = bool(image_config.get("prefer_ai_generated", True))
    fallback_to_source = bool(image_config.get("fallback_to_source", True))
    image_source = "none"
    image_paths: list[str] = []
    _emit_optional(
        progress_cb,
        "info",
        (
            f"配图规划：题材={image_plan.get('topic_label')}｜预计 {image_plan.get('count')} 张｜"
            f"插入段位={','.join(str(v) for v in image_plan.get('positions') or [])}"
        ),
    )

    if prefer_ai_generated:
        image_paths, _ = _generate_ai_images(title, content_md, image_config, day_dir, stem_name, progress_cb=progress_cb)
        if planned_count > 0:
            image_paths = image_paths[:planned_count]
        if image_paths:
            image_source = "ai"

    if not image_paths and fallback_to_source:
        image_paths = _download_images(
            image_urls or [],
            day_dir,
            stem_name,
            progress_cb=progress_cb,
            limit=planned_count if planned_count > 0 else None,
        )
        if image_paths:
            image_source = "source"

    if not image_paths and bool(image_config.get("fallback_to_web_search", True)):
        web_image_urls = _search_web_images(title, content_md, image_config, progress_cb=progress_cb)
        image_paths = _download_images(
            web_image_urls,
            day_dir,
            stem_name,
            progress_cb=progress_cb,
            limit=planned_count if planned_count > 0 else None,
        )
        if image_paths:
            image_source = "web_search"

    final_content = _inject_images_into_markdown(content_md, image_paths, positions=image_plan.get("positions"))
    try:
        target.write_text(final_content, encoding="utf-8")
    except OSError as exc:
        if not _path_error(exc):
            raise
        fallback = day_dir / f"{now.strftime('%Y%m%d_%H%M%S')}_{safe_title[:24]}_{now.strftime('%f')}.md"
        _emit_optional(progress_cb, "warning", f"稿件文件写入失败，改用短文件名：{fallback.name}｜{exc}")
        fallback.write_text(final_content, encoding="utf-8")
        target = fallback
    return str(target), len(image_paths), image_source, final_content


def regenerate_draft_images_file(
    archive_path: str,
    title: str,
    content_md: str,
    image_config: dict | None = None,
    image_urls: list[str] | None = None,
    progress_cb=None,
) -> tuple[str, int, str, str]:
    target = Path(archive_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    stem_name = _short_stem_name(target.stem or sanitize_filename(title))
    image_config = image_config or {}
    prefer_ai_generated = bool(image_config.get("prefer_ai_generated", True))
    fallback_to_source = bool(image_config.get("fallback_to_source", True))
    clean_content = strip_markdown_images(content_md)
    image_plan = _article_image_plan(title, clean_content, image_config)
    planned_count = max(0, int(image_plan.get("count") or image_config.get("max_per_draft", 0)))
    image_paths: list[str] = []
    image_source = "none"
    _emit_optional(
        progress_cb,
        "info",
        (
            f"重新配图规划：题材={image_plan.get('topic_label')}｜预计 {image_plan.get('count')} 张｜"
            f"插入段位={','.join(str(v) for v in image_plan.get('positions') or [])}"
        ),
    )

    if prefer_ai_generated:
        image_paths, _ = _generate_ai_images(title, clean_content, image_config, target.parent, stem_name, progress_cb=progress_cb)
        if planned_count > 0:
            image_paths = image_paths[:planned_count]
        if image_paths:
            image_source = "ai"

    if not image_paths and fallback_to_source:
        image_paths = _download_images(
            image_urls or [],
            target.parent,
            stem_name,
            progress_cb=progress_cb,
            limit=planned_count if planned_count > 0 else None,
        )
        if image_paths:
            image_source = "source"

    if not image_paths and bool(image_config.get("fallback_to_web_search", True)):
        web_image_urls = _search_web_images(title, clean_content, image_config, progress_cb=progress_cb)
        image_paths = _download_images(
            web_image_urls,
            target.parent,
            stem_name,
            progress_cb=progress_cb,
            limit=planned_count if planned_count > 0 else None,
        )
        if image_paths:
            image_source = "web_search"

    final_content = _inject_images_into_markdown(clean_content, image_paths, positions=image_plan.get("positions"))
    try:
        target.write_text(final_content, encoding="utf-8")
    except OSError as exc:
        if not _path_error(exc):
            raise
        fallback = target.parent / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{sanitize_filename(title)[:24]}_{datetime.now().strftime('%f')}.md"
        _emit_optional(progress_cb, "warning", f"稿件文件写入失败，改用短文件名：{fallback.name}｜{exc}")
        fallback.write_text(final_content, encoding="utf-8")
        target = fallback
    return str(target), len(image_paths), image_source, final_content
