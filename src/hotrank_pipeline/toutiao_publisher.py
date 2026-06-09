from __future__ import annotations

import asyncio
import base64
import json
import re
import os
import shutil
import time
from pathlib import Path
from datetime import datetime
from tempfile import gettempdir
from typing import Any
from urllib.parse import parse_qs

import markdown
from bs4 import BeautifulSoup
from playwright.async_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError

from .config import Settings, load_runtime_config
from .db import fetch_draft_by_id, fetch_recent_drafts, mark_draft_toutiao_uploaded
from .wechat_publisher import ARTICLE_STYLE, _image_bytes_for_wechat, _read_markdown, _wechat_compatible_html


TOUTIAO_LOGIN_URL = "https://mp.toutiao.com/auth/page/login"
TOUTIAO_PUBLISH_URL = "https://mp.toutiao.com/profile_v4/graphic/publish"
TOUTIAO_LIST_API = "https://mp.toutiao.com/mp/agw/article/list/"
TOUTIAO_PUBLISH_API = "https://mp.toutiao.com/mp/agw/article/publish"
DEFAULT_TITLE_CHAR_LIMIT = 30
DEFAULT_MAX_INLINE_IMAGES = 6
DEFAULT_VERIFY_LIST_LIMIT = 20
DEFAULT_TOUTIAO_COLLECTION_NAME = "这事和你有关"
DEFAULT_TOUTIAO_STATEMENT_LABELS = ["个人观点，仅供参考"]
DEFAULT_PUBLISH_LOCK_WAIT_SECONDS = 900
DEFAULT_PUBLISH_LOCK_STALE_SECONDS = 1800
RECOVERABLE_AFTER_PUBLISH_STAGES = {
    "click_publish",
    "wait_publish_response",
    "check_success_toast",
    "verify_article_list",
    "build_failure_diagnostic",
    "mark_uploaded",
}


class ToutiaoPublishError(RuntimeError):
    def __init__(
        self,
        user_message: str,
        *,
        diagnostics: dict[str, Any] | None = None,
        diagnostic_path: str = "",
        screenshot_path: str = "",
        retryable: bool = True,
    ) -> None:
        self.user_message = (user_message or "今日头条发布失败").strip()
        self.diagnostics = diagnostics or {}
        self.diagnostic_path = (diagnostic_path or "").strip()
        self.screenshot_path = (screenshot_path or "").strip()
        self.retryable = bool(retryable)
        super().__init__(self.user_message)

    def with_screenshot(self, screenshot_path: str) -> "ToutiaoPublishError":
        if screenshot_path and not self.screenshot_path:
            self.screenshot_path = screenshot_path
        return self

    def __str__(self) -> str:
        parts = [self.user_message]
        if self.diagnostic_path:
            parts.append(f"诊断文件：{self.diagnostic_path}")
        if self.screenshot_path:
            parts.append(f"截图：{self.screenshot_path}")
        return "｜".join(parts)


def _toutiao_config(settings: Settings) -> dict[str, Any]:
    runtime = load_runtime_config(settings)
    raw = runtime.get("toutiao") or {}
    publish_options = raw.get("publish_options") or {}
    statement_labels_raw = publish_options.get("statement_labels")
    statement_labels: list[str] = []
    if isinstance(statement_labels_raw, list):
        statement_labels = [str(item).strip() for item in statement_labels_raw if str(item).strip()]
    elif isinstance(statement_labels_raw, str):
        statement_labels = [part.strip() for part in re.split(r"[\r\n,，]+", statement_labels_raw) if part.strip()]
    if not statement_labels:
        statement_labels = list(DEFAULT_TOUTIAO_STATEMENT_LABELS)
    profile_dir = raw.get("browser_profile_dir") or str(
        Path(__file__).resolve().parents[2] / "data" / "browser_profiles" / "toutiao"
    )
    publish_timeout_seconds = max(30, min(int(raw.get("publish_timeout_seconds") or 240), 900))
    publish_total_timeout_seconds = max(
        publish_timeout_seconds + 60,
        min(int(raw.get("publish_total_timeout_seconds") or 360), 1200),
    )
    publish_lock_wait_seconds = max(
        10,
        min(int(raw.get("publish_lock_wait_seconds") or DEFAULT_PUBLISH_LOCK_WAIT_SECONDS), 1800),
    )
    publish_verify_retries = max(1, min(int(raw.get("publish_verify_retries") or 3), 8))
    publish_verify_retry_interval_seconds = max(
        1,
        min(int(raw.get("publish_verify_retry_interval_seconds") or 4), 20),
    )
    return {
        "channel": (raw.get("channel") or "chrome").strip() or "chrome",
        "browser_profile_dir": profile_dir,
        "headless": bool(raw.get("headless", False)),
        "login_wait_seconds": max(30, min(int(raw.get("login_wait_seconds") or 180), 900)),
        "publish_timeout_seconds": publish_timeout_seconds,
        "publish_total_timeout_seconds": publish_total_timeout_seconds,
        "publish_lock_wait_seconds": publish_lock_wait_seconds,
        "publish_verify_retries": publish_verify_retries,
        "publish_verify_retry_interval_seconds": publish_verify_retry_interval_seconds,
        # 头条图文发布页实际按 30 个中文字符校验；超过后会出现 “31 / 30” 并导致 7050 保存失败。
        # 即使 local_settings 里历史配置为 100，也在发布链路强制收敛到平台上限，避免自动任务卡在保存失败。
        "title_char_limit": max(20, min(int(raw.get("title_char_limit") or DEFAULT_TITLE_CHAR_LIMIT), DEFAULT_TITLE_CHAR_LIMIT)),
        "max_inline_images": max(0, min(int(raw.get("max_inline_images") or DEFAULT_MAX_INLINE_IMAGES), 12)),
        "verify_list_limit": max(5, min(int(raw.get("verify_list_limit") or DEFAULT_VERIFY_LIST_LIMIT), 50)),
        "auto_open_login_on_publish": bool(raw.get("auto_open_login_on_publish", True)),
        "username": (raw.get("username") or "").strip(),
        "password": raw.get("password") or "",
        "auto_password_login": bool(raw.get("auto_password_login", True)),
        "publish_options": {
            "ad_enabled": bool(publish_options.get("ad_enabled", True)),
            "claim_exclusive": bool(publish_options.get("claim_exclusive", True)),
            "collection_name": (
                str(publish_options.get("collection_name") or DEFAULT_TOUTIAO_COLLECTION_NAME).strip()
            ),
            "statement_labels": statement_labels,
            "publish_more_income": bool(publish_options.get("publish_more_income", False)),
            "disable_auto_rights_protection": bool(
                publish_options.get("disable_auto_rights_protection", True)
            ),
        },
        "debug_dir": str(
            Path(raw.get("debug_dir") or (Path(__file__).resolve().parents[2] / "data" / "toutiao_debug")).resolve()
        ),
    }


class _ToutiaoPublishFileLock:
    """跨线程/跨进程发布锁，避免多个入口同时抢同一个头条浏览器登录态。"""

    def __init__(self, config: dict[str, Any]) -> None:
        debug_dir = Path(config["debug_dir"]).resolve()
        debug_dir.mkdir(parents=True, exist_ok=True)
        self.path = debug_dir / "toutiao_publish.lock"
        self.timeout_seconds = int(config.get("publish_lock_wait_seconds") or DEFAULT_PUBLISH_LOCK_WAIT_SECONDS)
        self.stale_seconds = DEFAULT_PUBLISH_LOCK_STALE_SECONDS
        self.fd: int | None = None

    def _lock_payload(self) -> str:
        payload = {
            "pid": os.getpid(),
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        return json.dumps(payload, ensure_ascii=False)

    def _read_existing(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8")[:300]
        except Exception:
            return ""

    def _remove_stale_if_needed(self) -> None:
        try:
            age_seconds = time.time() - self.path.stat().st_mtime
        except FileNotFoundError:
            return
        except Exception:
            return
        if age_seconds >= self.stale_seconds:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            except Exception:
                pass

    def __enter__(self) -> "_ToutiaoPublishFileLock":
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            self._remove_stale_if_needed()
            try:
                self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, self._lock_payload().encode("utf-8"))
                return self
            except FileExistsError:
                if time.monotonic() >= deadline:
                    existing = self._read_existing()
                    raise ToutiaoPublishError(
                        "今日头条已有发布任务正在执行，已为避免浏览器登录态冲突而停止本次发布；"
                        f"请稍后重试。锁文件：{self.path}"
                        + (f"｜当前锁：{existing}" if existing else ""),
                        retryable=True,
                    )
                time.sleep(1)

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except Exception:
                pass
            self.fd = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass


def _toutiao_title(title: str, char_limit: int = DEFAULT_TITLE_CHAR_LIMIT) -> str:
    clean = re.sub(r"\s+", " ", (title or "").strip()).strip("“”\"'")
    if len(clean) <= char_limit:
        return clean
    return clean[:char_limit].rstrip("，。！？、；：…-— ") or clean[:char_limit]


def _existing_uploaded_payload(config: dict[str, Any], draft_id: int, draft: dict[str, Any]) -> dict[str, Any]:
    archive_title = draft.get("title") or draft.get("canonical_title") or f"draft_id={draft_id}"
    return {
        "draft_id": draft_id,
        "title": draft.get("title"),
        "toutiao_title": _toutiao_title(archive_title, char_limit=config["title_char_limit"]),
        "inline_image_count": 0,
        "content_mode": "existing",
        "content_length": len(draft.get("content_md") or ""),
        "editor_image_count": 0,
        "cover_count": 0,
        "cover_uploaded": False,
        "success_toast": "",
        "publish_events": [],
        "verified": bool(draft.get("toutiao_article_id")),
        "verified_status": "existing",
        "article_id": draft.get("toutiao_article_id"),
        "browser_profile_dir": config["browser_profile_dir"],
        "already_uploaded": True,
        "uploaded_at_text": draft.get("toutiao_uploaded_at_text") or "",
    }


def _strip_first_h1(soup: BeautifulSoup) -> None:
    h1 = soup.find("h1")
    if h1:
        h1.decompose()


def _image_data_url(path: Path) -> str:
    _filename, content_type, body = _image_bytes_for_wechat(path)
    return f"data:{content_type};base64,{base64.b64encode(body).decode('ascii')}"


def _render_markdown_to_toutiao_html(
    content_md: str,
    title: str,
    base_dir: Path,
    max_inline_images: int,
) -> tuple[str, list[Path], int]:
    content_md = re.sub(r"^#\s+.*$", f"# {title}", content_md or "", count=1, flags=re.M)
    rendered = markdown.markdown(
        content_md,
        extensions=["extra", "sane_lists", "tables", "nl2br", "fenced_code"],
        output_format="html5",
    )
    soup = BeautifulSoup(rendered, "html.parser")
    _strip_first_h1(soup)

    cover_paths: list[Path] = []
    inline_count = 0
    for img in list(soup.find_all("img")):
        src = (img.get("src") or "").strip()
        if not src:
            img.decompose()
            continue
        if src.startswith("data:"):
            inline_count += 1
            continue
        if re.match(r"^(https?:)?//", src):
            inline_count += 1
            continue
        asset_path = (base_dir / src).resolve()
        if not asset_path.exists() or not asset_path.is_file():
            img.decompose()
            continue
        if asset_path not in cover_paths:
            cover_paths.append(asset_path)
        if max_inline_images and inline_count >= max_inline_images:
            img.decompose()
            continue
        img["src"] = _image_data_url(asset_path)
        inline_count += 1

    html = f'<section style="{ARTICLE_STYLE}">{_wechat_compatible_html(soup)}</section>'
    return html, cover_paths, inline_count


def _html_to_plain_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    return re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True)).strip()


def _is_login_url(url: str) -> bool:
    lowered = (url or "").lower()
    return "auth/page/login" in lowered or "/login" in lowered


async def _new_context(config: dict[str, Any], *, headless: bool | None = None) -> BrowserContext:
    from playwright.async_api import async_playwright

    profile_dir = Path(config["browser_profile_dir"]).resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    launch_headless = config["headless"] if headless is None else headless
    launch_profile_dir = profile_dir
    temp_profile_dir: Path | None = None

    if launch_headless:
        temp_profile_dir = Path(gettempdir()) / "hotrank_toutiao_profiles" / (
            f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{os.getpid()}_{abs(hash(str(profile_dir))) % 100000}"
        )
        ignore_names = {
            "SingletonLock",
            "SingletonSocket",
            "SingletonCookie",
            "lockfile",
            "DevToolsActivePort",
        }

        def ignore_copy(_src: str, names: list[str]) -> list[str]:
            return [name for name in names if name in ignore_names or name.startswith("Singleton")]

        if temp_profile_dir.exists():
            shutil.rmtree(temp_profile_dir, ignore_errors=True)
        if profile_dir.exists():
            shutil.copytree(profile_dir, temp_profile_dir, ignore=ignore_copy, dirs_exist_ok=True)
        else:
            temp_profile_dir.mkdir(parents=True, exist_ok=True)
        launch_profile_dir = temp_profile_dir

    playwright = await async_playwright().start()
    context = await playwright.chromium.launch_persistent_context(
        user_data_dir=str(launch_profile_dir),
        channel=config["channel"],
        headless=launch_headless,
        viewport={"width": 1440, "height": 1200},
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--disable-default-apps",
        ],
    )
    setattr(context, "_hotrank_playwright", playwright)
    setattr(context, "_hotrank_temp_profile_dir", str(temp_profile_dir) if temp_profile_dir else "")
    setattr(context, "_hotrank_profile_dir", str(profile_dir))
    try:
        await context.grant_permissions(["clipboard-read", "clipboard-write"], origin="https://mp.toutiao.com")
    except Exception:
        pass
    return context


async def _close_context(context: BrowserContext | None) -> None:
    if not context:
        return
    playwright = getattr(context, "_hotrank_playwright", None)
    temp_profile_dir = getattr(context, "_hotrank_temp_profile_dir", "")
    profile_dir = getattr(context, "_hotrank_profile_dir", "")
    try:
        await context.close()
    finally:
        if temp_profile_dir and profile_dir:
            temp_dir = Path(temp_profile_dir)
            target_dir = Path(profile_dir)
            ignore_names = {
                "SingletonLock",
                "SingletonSocket",
                "SingletonCookie",
                "lockfile",
                "DevToolsActivePort",
            }

            def ignore_copy(_src: str, names: list[str]) -> list[str]:
                return [name for name in names if name in ignore_names or name.startswith("Singleton")]

            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                shutil.copytree(temp_dir, target_dir, ignore=ignore_copy, dirs_exist_ok=True)
            except Exception:
                pass
        if playwright:
            await playwright.stop()
        if temp_profile_dir:
            shutil.rmtree(temp_profile_dir, ignore_errors=True)


async def _ensure_publish_page(page: Page, timeout_seconds: int) -> None:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            await page.goto(TOUTIAO_PUBLISH_URL, wait_until="domcontentloaded", timeout=timeout_seconds * 1000)
            await page.wait_for_timeout(3000)
            return
        except Exception as exc:
            last_error = exc
            if attempt >= 2:
                break
            await page.wait_for_timeout(1800)
    raise last_error if last_error else RuntimeError("头条号发文页打开失败")


def _debug_path(config: dict[str, Any], prefix: str) -> Path:
    debug_dir = Path(config["debug_dir"]).resolve()
    debug_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return debug_dir / f"{stamp}_{prefix}.png"


def _debug_json_path(config: dict[str, Any], prefix: str) -> Path:
    debug_dir = Path(config["debug_dir"]).resolve()
    debug_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return debug_dir / f"{stamp}_{prefix}.json"


async def _capture_debug_screenshot(page: Page | None, config: dict[str, Any], prefix: str) -> str:
    path = _debug_path(config, prefix)
    if page is None:
        return str(path)
    try:
        await page.screenshot(path=str(path), full_page=True)
    except Exception:
        try:
            await page.screenshot(path=str(path))
        except Exception:
            pass
    return str(path)


async def _set_login_input_value(page: Page, locator, value: str) -> str:
    await locator.wait_for(state="visible", timeout=15000)
    await locator.click()
    try:
        await locator.press("Control+A")
        await locator.press("Delete")
    except Exception:
        pass
    await locator.type(value, delay=40)
    current = (await locator.input_value()).strip()
    if current != value.strip():
        await locator.fill(value)
        await page.wait_for_timeout(300)
        current = (await locator.input_value()).strip()
    if current != value.strip():
        await locator.evaluate(
            """(el, inputValue) => {
                el.focus();
                el.value = inputValue;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            value,
        )
        await page.wait_for_timeout(300)
        current = (await locator.input_value()).strip()
    return current


async def _switch_to_password_login(page: Page) -> None:
    password_input = page.locator("input[type='password'], input[placeholder='密码']").first
    try:
        if await password_input.count() and await password_input.is_visible():
            return
    except Exception:
        pass

    selectors = [
        "[role='button']:has-text('密码登录')",
        "text=密码登录",
    ]
    for selector in selectors:
        try:
            target = page.locator(selector).first
            await target.wait_for(state="visible", timeout=5000)
            await target.click(timeout=10000)
            await page.wait_for_timeout(1500)
            return
        except Exception:
            continue


async def _ensure_login_terms_checked(page: Page) -> None:
    checkbox = page.locator("[role='checkbox'], .web-login-confirm-info__checkbox").first
    try:
        if not await checkbox.count():
            return
        checked = (await checkbox.get_attribute("aria-checked")) or ""
        if checked.lower() != "true":
            await checkbox.click(timeout=5000)
            await page.wait_for_timeout(500)
    except Exception:
        return


async def _extract_login_feedback(page: Page) -> str:
    try:
        body_text = await page.locator("body").inner_text(timeout=3000)
    except Exception:
        return ""
    candidates = [
        "账号或密码错误",
        "密码错误",
        "账号错误",
        "请先阅读并同意",
        "请勾选",
        "请完成验证",
        "安全验证",
        "登录失败",
        "网络异常",
        "操作过于频繁",
    ]
    for item in candidates:
        if item in body_text:
            return item
    return ""


async def _auto_login_with_password(page: Page, config: dict[str, Any]) -> dict[str, Any]:
    username = (config.get("username") or "").strip()
    password = config.get("password") or ""
    if not config.get("auto_password_login", True):
        return {"attempted": False, "success": False, "message": "已关闭账号密码自动登录"}
    if not username or not password:
        return {"attempted": False, "success": False, "message": "未配置头条账号密码，跳过自动登录"}

    await _switch_to_password_login(page)
    await _ensure_login_terms_checked(page)

    account_input = page.locator(
        "input[placeholder*='手机号/邮箱'], input[placeholder='手机号'], input.web-login-normal-input__input"
    ).first
    password_input = page.locator("input[type='password'], input[placeholder='密码']").first

    current_username = await _set_login_input_value(page, account_input, username)
    current_password = await _set_login_input_value(page, password_input, password)
    await _ensure_login_terms_checked(page)
    if current_username != username.strip() or current_password != password.strip():
        raise RuntimeError("头条号账号或密码填充失败")

    login_button = page.locator("button[type='submit'], button:has-text('登录')").first
    await login_button.wait_for(state="visible", timeout=15000)
    try:
        await login_button.click(timeout=10000)
    except Exception:
        await password_input.press("Enter")

    deadline = asyncio.get_running_loop().time() + 20
    feedback = ""
    while asyncio.get_running_loop().time() < deadline:
        if not _is_login_url(page.url):
            return {"attempted": True, "success": True, "message": "已使用配置的头条账号密码自动登录"}
        feedback = await _extract_login_feedback(page)
        if feedback in {"账号或密码错误", "密码错误", "账号错误", "请先阅读并同意", "请勾选", "登录失败"}:
            break
        await page.wait_for_timeout(1200)

    suffix = f"；页面提示：{feedback}" if feedback else ""
    return {
        "attempted": True,
        "success": False,
        "message": f"已尝试使用配置的头条账号密码自动登录，但暂未完成登录{suffix}",
    }


async def _wait_until_logged_in(context: BrowserContext, config: dict[str, Any]) -> dict[str, Any]:
    page = context.pages[0] if context.pages else await context.new_page()
    await _ensure_publish_page(page, config["publish_timeout_seconds"])
    if not _is_login_url(page.url):
        return {"logged_in": True, "message": "已检测到头条号登录态"}

    auto_login_result = await _auto_login_with_password(page, config)
    if auto_login_result.get("success"):
        try:
            await _ensure_publish_page(page, config["publish_timeout_seconds"])
        except Exception:
            pass
        if not _is_login_url(page.url):
            return {"logged_in": True, "message": auto_login_result["message"]}

    wait_seconds = config["login_wait_seconds"]
    deadline = asyncio.get_running_loop().time() + wait_seconds
    while asyncio.get_running_loop().time() < deadline:
        await page.wait_for_timeout(2000)
        try:
            await page.goto(TOUTIAO_PUBLISH_URL, wait_until="domcontentloaded", timeout=30000)
        except Exception:
            continue
        if not _is_login_url(page.url):
            try:
                await page.locator("textarea[placeholder*='标题'], textarea[placeholder*='文章标题']").first.wait_for(
                    state="visible",
                    timeout=15000,
                )
            except Exception:
                continue
            if _is_login_url(page.url):
                continue
            if auto_login_result.get("attempted"):
                return {
                    "logged_in": True,
                    "message": f"{auto_login_result['message']}，随后已进入头条号发文页",
                }
            return {"logged_in": True, "message": "登录完成，已进入头条号发文页"}

    if auto_login_result.get("attempted"):
        return {
            "logged_in": False,
            "message": f"{auto_login_result['message']}；并且在 {wait_seconds} 秒内未完成登录",
        }
    return {"logged_in": False, "message": f"已自动打开头条号登录页，但在 {wait_seconds} 秒内未完成登录"}


async def launch_toutiao_login(settings: Settings) -> dict[str, Any]:
    config = _toutiao_config(settings)
    context: BrowserContext | None = None
    page: Page | None = None
    try:
        context = await _new_context(config, headless=True)
        page = context.pages[0] if context.pages else await context.new_page()
        result = await _wait_until_logged_in(context, config)
        return {
            "ok": bool(result["logged_in"]),
            "message": result["message"],
            "browser_profile_dir": config["browser_profile_dir"],
        }
    except Exception as exc:
        screenshot = await _capture_debug_screenshot(page, config, "login_error")
        raise RuntimeError(f"今日头条登录失败：{exc}｜截图：{screenshot}") from exc
    finally:
        await _close_context(context)


def _can_recover_after_publish(state: dict[str, Any]) -> bool:
    if str(state.get("stage") or "") in RECOVERABLE_AFTER_PUBLISH_STAGES:
        return True
    return bool(state.get("publish_clicked"))


def login_toutiao(settings: Settings) -> dict[str, Any]:
    return asyncio.run(launch_toutiao_login(settings))


async def _dismiss_masks(page: Page) -> None:
    try:
        mask = page.locator(".byte-drawer-mask").first
        if await mask.count():
            await mask.click(timeout=1500)
            await page.wait_for_timeout(600)
    except Exception:
        pass
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(400)
    except Exception:
        pass


async def _write_plain_clipboard(page: Page, text: str) -> bool:
    try:
        await page.evaluate(
            """async (value) => {
                try {
                    if (!navigator.clipboard || !window.ClipboardItem) return false;
                    await navigator.clipboard.write([
                        new ClipboardItem({
                            'text/plain': new Blob([value], { type: 'text/plain' }),
                        }),
                    ]);
                    return true;
                } catch (err) {
                    return false;
                }
            }""",
            text,
        )
        return True
    except Exception:
        return False


async def _context_menu_paste_text(page: Page, locator, expected_text: str) -> str:
    await locator.click(timeout=3000)
    await page.keyboard.press("Control+A")
    await page.keyboard.press("Delete")
    await page.wait_for_timeout(150)
    try:
        await locator.click(button="right", timeout=3000)
        await page.wait_for_timeout(500)
        paste_item = page.locator(
            "text=粘贴, [role='menuitem']:has-text('粘贴'), li:has-text('粘贴'), button:has-text('粘贴')"
        ).first
        if await paste_item.count():
            await paste_item.click(timeout=3000)
        else:
            await page.keyboard.press("Control+V")
    except Exception:
        await page.keyboard.press("Control+V")
    await page.wait_for_timeout(500)
    try:
        return (await locator.input_value()).strip()
    except Exception:
        return expected_text.strip()


async def _wait_publish_editor_ready(page: Page, timeout_seconds: int = 30) -> None:
    title_box = page.locator("textarea[placeholder*='标题'], textarea[placeholder*='文章标题']").first
    skeleton = page.locator(
        ".publish-content [class*='skeleton'], .publish-content [class*='loading'], "
        ".article-edit [class*='skeleton'], .article-edit [class*='loading'], "
        ".byte-loading, .vc-spin, .semi-spin"
    ).first
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(5, timeout_seconds)
    last_error = ""

    await title_box.wait_for(state="visible", timeout=max(5000, timeout_seconds * 1000))

    while loop.time() < deadline:
        await _dismiss_masks(page)
        try:
            has_skeleton = await skeleton.count() > 0 and await skeleton.is_visible()
        except Exception:
            has_skeleton = False
        if has_skeleton:
            await page.wait_for_timeout(800)
            continue

        try:
            probe = "就绪检测"
            if not await _write_plain_clipboard(page, probe):
                await title_box.click(timeout=2000)
                await page.keyboard.press("Control+A")
                await page.keyboard.press("Delete")
                await page.keyboard.type(probe, delay=20)
                await page.wait_for_timeout(300)
                current = (await title_box.input_value()).strip()
            else:
                current = await _context_menu_paste_text(page, title_box, probe)
            if current == probe:
                await page.keyboard.press("Control+A")
                await page.keyboard.press("Delete")
                await page.wait_for_timeout(200)
                return
            last_error = f"标题框当前不可写入，检测值={current or '空'}"
        except Exception as exc:
            last_error = str(exc)
        await page.wait_for_timeout(800)

    raise RuntimeError(f"头条发布页编辑器长时间未就绪（超过 {timeout_seconds} 秒）{('：' + last_error) if last_error else ''}")


async def _fill_title(page: Page, title: str) -> str:
    title_box = page.locator("textarea[placeholder*='标题'], textarea[placeholder*='文章标题']").first
    await title_box.wait_for(state="visible", timeout=30000)
    current = ""
    if await _write_plain_clipboard(page, title):
        current = await _context_menu_paste_text(page, title_box, title)
    if current != title.strip():
        await title_box.click()
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Delete")
        await page.keyboard.type(title, delay=25)
        await page.wait_for_timeout(600)
        current = (await title_box.input_value()).strip()
    if current != title.strip():
        await title_box.fill(title)
        await page.wait_for_timeout(400)
        current = (await title_box.input_value()).strip()
    await page.wait_for_timeout(1200)
    stable = (await title_box.input_value()).strip()
    if stable != title.strip():
        raise RuntimeError(f"头条号标题输入失败，当前标题为：{stable or '空'}")
    return stable


async def _paste_html_via_clipboard(page: Page, editor_selector: str, html: str, plain_text: str) -> bool:
    try:
        ok = await page.evaluate(
            """async ({ html, plainText }) => {
                try {
                    if (!navigator.clipboard || !window.ClipboardItem) return false;
                    await navigator.clipboard.write([
                        new ClipboardItem({
                            'text/html': new Blob([html], { type: 'text/html' }),
                            'text/plain': new Blob([plainText], { type: 'text/plain' }),
                        }),
                    ]);
                    return true;
                } catch (err) {
                    return false;
                }
            }""",
            {"html": html, "plainText": plain_text},
        )
        if not ok:
            return False
        editor = page.locator(editor_selector).first
        await editor.click()
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Delete")
        await page.keyboard.press("Control+V")
        await page.wait_for_timeout(5000)
        current = (await editor.inner_text()).strip()
        return len(current) >= max(20, min(len(plain_text) // 3, 120))
    except Exception:
        return False


async def _fill_content(page: Page, html: str, plain_text: str) -> dict[str, Any]:
    editor_selector = ".ProseMirror"
    editor = page.locator(editor_selector).first
    await editor.wait_for(state="visible", timeout=30000)
    await editor.click()
    expected_image_count = len(BeautifulSoup(html or "", "html.parser").find_all("img"))
    pasted = await _paste_html_via_clipboard(page, editor_selector, html, plain_text)
    if pasted and expected_image_count:
        actual_image_count = await page.locator(f"{editor_selector} img").count()
        if actual_image_count < expected_image_count:
            pasted = False
    if not pasted:
        await editor.evaluate(
            """(el, payload) => {
                el.innerHTML = payload.html;
                el.dispatchEvent(new InputEvent('input', { bubbles: true, data: payload.plainText }));
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }""",
            {"html": html, "plainText": plain_text},
        )
        await page.wait_for_timeout(2000)
    current = (await editor.inner_text()).strip()
    if len(current) < 20:
        raise RuntimeError("头条号正文写入失败，编辑器内容为空或过短")
    return {
        "mode": "clipboard" if pasted else "dom",
        "content_length": len(current),
        "expected_image_count": expected_image_count,
        "actual_image_count": await page.locator(f"{editor_selector} img").count(),
    }


async def _wait_after_initial_edit(page: Page, timeout_seconds: int = 12) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(3, timeout_seconds)
    while loop.time() < deadline:
        await _dismiss_masks(page)
        try:
            body_text = await page.locator("body").inner_text(timeout=2000)
        except Exception:
            body_text = ""
        if "保存失败" not in body_text and "保存中" not in body_text:
            return
        await page.wait_for_timeout(800)


async def _upload_cover(page: Page, cover_paths: list[Path], *, expected_count: int = 1) -> bool:
    valid_paths = [path for path in cover_paths if path.exists() and path.is_file()]
    if not valid_paths:
        return False
    try:
        await _dismiss_masks(page)
        add_cover = page.locator("div.article-cover-add, [class*='article-cover-add']").first
        await add_cover.wait_for(state="visible", timeout=10000)
        await add_cover.click()
        await page.wait_for_timeout(1500)
    except Exception:
        pass

    file_input = page.locator(".byte-drawer-wrapper input[type='file']").first
    try:
        await file_input.wait_for(state="attached", timeout=15000)
    except Exception as exc:
        raise RuntimeError(f"头条号封面上传弹层未出现文件选择控件：{exc}") from exc
    try:
        await file_input.set_input_files([str(path) for path in valid_paths])
        await page.wait_for_timeout(3000)
    except Exception as exc:
        raise RuntimeError(f"头条号封面上传失败：{exc}") from exc

    confirm_candidates = [
        ".byte-drawer-wrapper button:has-text('确定')",
        ".byte-drawer-wrapper [role='button']:has-text('确定')",
        ".byte-drawer-wrapper button:has-text('本地上传')",
        "button[data-e2e='imageUploadConfirm-btn']",
        "button:has-text('确定')",
        "button:has-text('完成')",
        "button:has-text('确认')",
    ]
    for selector in confirm_candidates:
        try:
            btn = page.locator(selector).first
            if await btn.count():
                await btn.click(timeout=10000)
                await page.wait_for_timeout(2500)
                break
        except Exception:
            continue
    await _dismiss_masks(page)
    cover_count = await _wait_for_cover_ready(page, timeout_seconds=18, expected_min_count=max(1, expected_count))
    return cover_count >= max(1, expected_count)


async def _wait_for_cover_ready(page: Page, *, timeout_seconds: int = 8, expected_min_count: int = 1) -> int:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    while loop.time() < deadline:
        try:
            cover_count = await page.locator(".article-cover-images-wrap img").count()
        except Exception:
            cover_count = 0
        if cover_count >= max(0, expected_min_count):
            return cover_count
        await page.wait_for_timeout(400)
    try:
        return await page.locator(".article-cover-images-wrap img").count()
    except Exception:
        return 0


def _cover_mode_from_paths(cover_paths: list[Path]) -> tuple[str, int, list[Path]]:
    usable = [path for path in cover_paths if path.exists() and path.is_file()]
    if len(usable) >= 3:
        return "三图", 3, usable[:3]
    if usable:
        return "单图", 1, usable[:1]
    return "无封面", 0, []


async def _locator_checked(locator) -> bool:
    return bool(
        await locator.evaluate(
            """(el) => {
                const nodes = [el, ...Array.from(el.querySelectorAll('*'))];
                return nodes.some((node) => {
                    const cls = typeof node.className === 'string' ? node.className : '';
                    if (cls.includes('checked')) return true;
                    if (node.getAttribute && node.getAttribute('aria-checked') === 'true') return true;
                    if (node.tagName === 'INPUT' && node.checked) return true;
                    return false;
                });
            }"""
        )
    )


async def _find_label(page: Page, label_text: str):
    locator = page.locator("label").filter(has_text=label_text).first
    if await locator.count():
        return locator
    return None


async def _ensure_radio_selected(page: Page, label_text: str, *, required: bool = True) -> bool:
    locator = await _find_label(page, label_text)
    if locator is None:
        if required:
            raise RuntimeError(f"未找到头条配置项：{label_text}")
        return False
    await locator.wait_for(state="visible", timeout=10000)
    if await _locator_checked(locator):
        return True
    for _ in range(2):
        try:
            await locator.click(timeout=8000)
        except Exception:
            await locator.evaluate("(el) => el.click()")
        await page.wait_for_timeout(800)
        if await _locator_checked(locator):
            return True
    raise RuntimeError(f"头条配置项点击失败：{label_text}")


async def _set_checkbox_state(page: Page, label_text: str, desired: bool, *, required: bool = False) -> bool:
    locator = await _find_label(page, label_text)
    if locator is None:
        if required:
            raise RuntimeError(f"未找到头条复选项：{label_text}")
        return False
    await locator.wait_for(state="visible", timeout=8000)
    current = await _locator_checked(locator)
    if current == desired:
        return current
    for _ in range(2):
        try:
            await locator.click(timeout=8000)
        except Exception:
            await locator.evaluate("(el) => el.click()")
        await page.wait_for_timeout(800)
        current = await _locator_checked(locator)
        if current == desired:
            return current
    if required:
        raise RuntimeError(f"头条复选项状态设置失败：{label_text}")
    return current


async def _ensure_toutiao_first_publish(page: Page, desired: bool, *, required: bool = False) -> bool:
    """Set Toutiao's "首发/头条首发" switch.

    The publish page has changed this control several times.  Sometimes it is a
    normal label, sometimes the clickable node is a parent/sibling switch around
    the text.  Keep the generic checkbox path first, then fall back to a small
    DOM search that clicks the nearest interactive container and verifies state.
    """
    labels = ("头条首发", "首发", "原创首发")
    for label in labels:
        try:
            applied = await _set_checkbox_state(page, label, desired, required=False)
            if applied == desired:
                return applied
        except Exception:
            continue

    result = await page.evaluate(
        """async ({ labels, desired }) => {
            const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
            const textOf = (el) => (el?.innerText || el?.textContent || '').trim();
            const isChecked = (el) => {
                if (!el) return false;
                const nodes = [el, ...Array.from(el.querySelectorAll('*'))];
                return nodes.some((node) => {
                    const cls = typeof node.className === 'string' ? node.className : '';
                    if (cls.includes('checked') || cls.includes('active') || cls.includes('is-checked')) return true;
                    if (node.getAttribute?.('aria-checked') === 'true') return true;
                    if (node.getAttribute?.('aria-selected') === 'true') return true;
                    if (node.tagName === 'INPUT' && node.checked) return true;
                    return false;
                });
            };
            const all = Array.from(document.querySelectorAll('label, span, div, button'));
            const textNode = all.find((el) => labels.some((label) => textOf(el).includes(label)));
            if (!textNode) return { found: false, checked: false, text: '' };
            const candidates = [
                textNode.closest('label'),
                textNode.closest('[role="checkbox"]'),
                textNode.closest('[aria-checked]'),
                textNode.closest('.byte-checkbox, .semi-checkbox, .arco-checkbox, .switch, .byte-switch, .semi-switch, .arco-switch'),
                textNode.parentElement,
                textNode.parentElement?.parentElement,
            ].filter(Boolean);
            const unique = Array.from(new Set(candidates));
            let checked = unique.some(isChecked);
            if (checked !== desired) {
                const clickable = unique.find((el) => {
                    const cls = typeof el.className === 'string' ? el.className : '';
                    return (
                        el.tagName === 'LABEL' ||
                        el.tagName === 'BUTTON' ||
                        el.getAttribute?.('role') === 'checkbox' ||
                        el.getAttribute?.('aria-checked') != null ||
                        /checkbox|switch|radio|option|item|wrap|container/.test(cls)
                    );
                }) || textNode;
                clickable.scrollIntoView({ block: 'center', inline: 'center' });
                clickable.click();
                await sleep(800);
                checked = unique.some(isChecked);
            }
            return { found: true, checked, text: textOf(textNode).slice(0, 80) };
        }""",
        {"labels": list(labels), "desired": bool(desired)},
    )
    if result.get("found") and result.get("checked") == bool(desired):
        return bool(result.get("checked"))
    if required:
        raise RuntimeError(f"头条首发按钮设置失败：desired={desired} result={result}")
    return bool(result.get("checked"))


async def _configure_collection(page: Page, collection_name: str) -> str:
    collection_name = (collection_name or "").strip()
    if not collection_name:
        return ""
    button = page.locator("button.collection-btn, button:has-text('添加至合集')").first
    await button.wait_for(state="visible", timeout=10000)
    await button.click(timeout=10000)
    modal = page.locator(".article-publish-add-collection, .add-collection-modal").first
    await modal.wait_for(state="visible", timeout=10000)
    item = modal.locator(".add-collection-item").filter(has_text=collection_name).first
    if not await item.count():
        cancel_btn = modal.locator("button:has-text('取消')").first
        if await cancel_btn.count():
            try:
                await cancel_btn.click(timeout=3000)
            except Exception:
                pass
        raise RuntimeError(f"未找到头条合集：{collection_name}")
    await item.click(timeout=10000)
    await page.wait_for_timeout(800)
    confirm_btn = modal.locator("button:has-text('确定')").first
    await confirm_btn.wait_for(state="visible", timeout=8000)
    disabled = await confirm_btn.evaluate(
        """(el) => {
            const cls = typeof el.className === 'string' ? el.className : '';
            return !!el.disabled || cls.includes('disabled');
        }"""
    )
    if disabled:
        raise RuntimeError(f"头条合集未成功选中：{collection_name}")
    await confirm_btn.click(timeout=8000)
    await page.wait_for_timeout(1200)
    return collection_name


async def _configure_statement_labels(page: Page, desired_labels: list[str]) -> list[str]:
    normalized = [label.strip() for label in desired_labels if label and label.strip()]
    if not normalized:
        return []
    all_labels = [
        "取材网络",
        "引用站内",
        "个人观点，仅供参考",
        "引用AI",
        "虚构演绎，故事经历",
        "投资观点，仅供参考",
        "健康医疗分享，仅供参考",
    ]
    selected: list[str] = []
    for label_text in all_labels:
        desired = label_text in normalized
        applied = await _set_checkbox_state(page, label_text, desired, required=desired)
        if applied and desired:
            selected.append(label_text)
    return selected


async def _configure_publish_options(page: Page, config: dict[str, Any], cover_paths: list[Path]) -> dict[str, Any]:
    options = config.get("publish_options") or {}
    cover_mode, desired_cover_count, chosen_cover_paths = _cover_mode_from_paths(cover_paths)
    await _ensure_radio_selected(page, cover_mode)
    cover_uploaded = False
    cover_count = await _wait_for_cover_ready(page, timeout_seconds=5, expected_min_count=desired_cover_count)
    if desired_cover_count > 0 and cover_count < desired_cover_count:
        cover_uploaded = await _upload_cover(
            page,
            chosen_cover_paths,
            expected_count=desired_cover_count,
        )
        cover_count = await _wait_for_cover_ready(
            page,
            timeout_seconds=18,
            expected_min_count=desired_cover_count,
        )
    if desired_cover_count == 0:
        await _dismiss_masks(page)

    await _ensure_radio_selected(page, "投放广告赚收益" if options.get("ad_enabled", True) else "不投放广告")
    claim_exclusive_checked = await _ensure_toutiao_first_publish(
        page,
        bool(options.get("claim_exclusive", False)),
        required=bool(options.get("claim_exclusive", False)),
    )
    await _set_checkbox_state(page, "发布得更多收益", bool(options.get("publish_more_income", False)))

    selected_collection = ""
    if options.get("collection_name"):
        try:
            selected_collection = await _configure_collection(page, str(options["collection_name"]))
        except Exception as exc:
            # 头条合集可能处于审核中、下架、不可新增文章或当前账号暂无权限。
            # 这类情况不应中断整篇文章发布，自动跳过合集即可。
            print(f"[toutiao] skip collection: {options.get('collection_name')}｜{exc}")
            selected_collection = ""

    selected_statements = await _configure_statement_labels(page, options.get("statement_labels") or [])

    if options.get("disable_auto_rights_protection", True):
        for label_text in ("授权平台自动维权", "自动维权", "原创保护"):
            try:
                await _set_checkbox_state(page, label_text, False)
            except Exception:
                continue

    await _dismiss_masks(page)
    return {
        "cover_mode": cover_mode,
        "cover_uploaded": cover_uploaded,
        "cover_count": cover_count,
        "cover_paths": [str(path) for path in chosen_cover_paths],
        "ad_enabled": bool(options.get("ad_enabled", True)),
        "claim_exclusive": bool(options.get("claim_exclusive", False)),
        "claim_exclusive_checked": claim_exclusive_checked,
        "collection_name": selected_collection,
        "statement_labels": selected_statements,
        "publish_more_income": bool(options.get("publish_more_income", False)),
    }


def _find_first_value_by_keys(value: Any, keys: tuple[str, ...]) -> str | int | None:
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, (str, int)) and str(candidate).strip():
                return candidate
        for nested in value.values():
            found = _find_first_value_by_keys(nested, keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_first_value_by_keys(item, keys)
            if found is not None:
                return found
    return None


def _extract_titles_from_payload(payload: Any) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    def walk(value: Any):
        if isinstance(value, dict):
            title = value.get("title") or value.get("article_title") or value.get("content")
            if isinstance(title, str) and title.strip():
                results.append(
                    {
                        "title": title.strip(),
                        "id": value.get("id") or value.get("article_id") or value.get("group_id"),
                        "raw": value,
                    }
                )
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)
    return results


async def _query_article_list(page: Page, *, status: str, page_size: int) -> dict[str, Any]:
    return await page.evaluate(
        """async ({ apiUrl, status, pageSize }) => {
            const url = `${apiUrl}?page=1&page_size=${pageSize}&status=${encodeURIComponent(status)}&from=pc`;
            try {
                const resp = await fetch(url, { credentials: 'include' });
                const text = await resp.text();
                let data = null;
                try {
                    data = JSON.parse(text);
                } catch {
                    data = { raw: text };
                }
                return { ok: resp.ok, statusCode: resp.status, url, data };
            } catch (error) {
                return { ok: false, statusCode: 0, url, error: String(error) };
            }
        }""",
        {
            "apiUrl": TOUTIAO_LIST_API,
            "status": status,
            "pageSize": page_size,
        },
    )


def _classify_article_submission(article: dict[str, Any]) -> dict[str, Any]:
    status_desc = str(article.get("status_desc") or "")
    status = article.get("status")
    pgc_status = article.get("pgc_status")
    is_draft = bool(article.get("is_draft"))
    is_passed = bool(article.get("is_passed"))
    published = (
        ("发布" in status_desc and "草稿" not in status_desc)
        or status == 3
        or pgc_status == 3
        or (not is_draft and is_passed)
    )
    submitted = published or (
        not is_draft
        and (
            status in {6}
            or pgc_status in {6}
            or any(token in status_desc for token in ("审核中", "待审核", "待发布", "提交"))
        )
    )
    if published:
        submission_state = "published"
    elif submitted:
        submission_state = "submitted"
    else:
        submission_state = "draft"
    return {
        "published": published,
        "submitted": submitted,
        "submission_state": submission_state,
        "status_desc": status_desc,
        "status": status,
        "pgc_status": pgc_status,
        "is_draft": is_draft,
        "is_passed": is_passed,
    }


def _is_published_article(article: dict[str, Any]) -> bool:
    return bool(_classify_article_submission(article)["published"])


def _article_time_value(article: dict[str, Any]) -> int:
    candidates = (
        "create_time",
        "modify_time",
        "publish_time",
        "pop_create_time",
        "pop_create_time_ms",
    )
    for key in candidates:
        raw = article.get(key)
        try:
            value = int(float(raw))
        except Exception:
            continue
        if key.endswith("_ms") or value > 10_000_000_000:
            value = value // 1000
        if value > 0:
            return value
    return 0


async def _verify_article_published(
    page: Page,
    title: str,
    verify_list_limit: int,
    *,
    min_create_time: int | None = None,
    require_recent: bool = False,
) -> dict[str, Any]:
    requests_to_try = [
        {"status": "all", "page_size": verify_list_limit},
        {"status": "draft", "page_size": verify_list_limit},
    ]
    for req in requests_to_try:
        try:
            payload = await _query_article_list(
                page,
                status=req["status"],
                page_size=req["page_size"],
            )
            outer = (payload or {}).get("data") or {}
            articles = ((outer.get("data") or {}).get("articles") or []) if isinstance(outer, dict) else []
            for article in articles:
                article_title = (article.get("title") or "").strip()
                if article_title != title:
                    continue
                article_time = _article_time_value(article)
                if min_create_time:
                    if require_recent and article_time < min_create_time:
                        continue
                    if not require_recent and article_time and article_time < min_create_time:
                        continue
                article_state = _classify_article_submission(article)
                article_id = (
                    article.get("group_id")
                    or article.get("article_id")
                    or article.get("pgc_id")
                    or article.get("item_id")
                    or article.get("id")
                )
                return {
                    "verified": bool(article_state["submitted"]),
                    "published": bool(article_state["published"]),
                    "submitted": bool(article_state["submitted"]),
                    "submission_state": article_state["submission_state"],
                    "status": req["status"],
                    "article_id": article_id,
                    "matched_title": article_title,
                    "status_desc": article_state["status_desc"],
                    "is_draft": bool(article_state["is_draft"]),
                    "article_time": article_time,
                    "min_create_time": min_create_time,
                    "api_url": payload.get("url"),
                }
        except Exception:
            continue
    return {"verified": False}


async def _verify_article_published_with_retries(
    page: Page,
    title: str,
    config: dict[str, Any],
    *,
    min_create_time: int | None = None,
    require_recent: bool = False,
) -> dict[str, Any]:
    attempts = max(1, int(config.get("publish_verify_retries") or 1))
    interval_seconds = max(1, int(config.get("publish_verify_retry_interval_seconds") or 1))
    per_attempt_timeout = min(30, max(10, int(config.get("publish_timeout_seconds") or 240) // 4))
    verify_list_limit = int(config.get("verify_list_limit") or DEFAULT_VERIFY_LIST_LIMIT)
    last_result: dict[str, Any] = {"verified": False}

    for attempt in range(1, attempts + 1):
        try:
            result = await asyncio.wait_for(
                _verify_article_published(
                    page,
                    title,
                    verify_list_limit,
                    min_create_time=min_create_time,
                    require_recent=require_recent,
                ),
                timeout=per_attempt_timeout,
            )
        except asyncio.TimeoutError:
            result = {
                "verified": False,
                "status": "verify_timeout",
                "status_desc": "发布后列表校验超时",
            }
        except Exception as exc:
            result = {
                "verified": False,
                "status": "verify_error",
                "status_desc": str(exc)[:300],
            }

        result["verify_attempt"] = attempt
        result["verify_attempts"] = attempts
        last_result = result
        if result.get("submitted") or result.get("verified"):
            return result
        if attempt < attempts:
            await asyncio.sleep(interval_seconds)

    return last_result


def _result_from_verified_article(
    settings: Settings,
    config: dict[str, Any],
    draft: dict[str, Any],
    draft_id: int,
    title: str,
    verify_result: dict[str, Any],
    *,
    recovered_after_error: bool = False,
    error_text: str = "",
) -> dict[str, Any]:
    article_id = verify_result.get("article_id")
    mark_draft_toutiao_uploaded(settings, draft_id, article_id)
    return {
        "draft_id": draft_id,
        "title": draft.get("title"),
        "toutiao_title": title,
        "inline_image_count": 0,
        "content_mode": "recovered_after_error" if recovered_after_error else "verified",
        "content_length": len(draft.get("content_md") or ""),
        "editor_image_count": 0,
        "cover_count": 0,
        "cover_uploaded": False,
        "cover_mode": "",
        "success_toast": "",
        "publish_events": [],
        "publish_mode": "recovered_after_error" if recovered_after_error else "verified",
        "verified": True,
        "verified_status": verify_result.get("status"),
        "published": bool(verify_result.get("published")),
        "submitted": bool(verify_result.get("submitted") or verify_result.get("verified")),
        "submission_state": verify_result.get("submission_state") or "submitted",
        "status_desc": verify_result.get("status_desc") or "",
        "article_id": article_id,
        "ad_enabled": bool((config.get("publish_options") or {}).get("ad_enabled", True)),
        "claim_exclusive": bool((config.get("publish_options") or {}).get("claim_exclusive", False)),
        "collection_name": str((config.get("publish_options") or {}).get("collection_name") or ""),
        "statement_labels": (config.get("publish_options") or {}).get("statement_labels") or [],
        "publish_more_income": bool((config.get("publish_options") or {}).get("publish_more_income", False)),
        "browser_profile_dir": config["browser_profile_dir"],
        "already_uploaded": False,
        "recovered_after_error": recovered_after_error,
        "recovered_error": error_text,
    }


async def _click_locator_with_fallback(page: Page, locator, *, label: str) -> None:
    await locator.scroll_into_view_if_needed(timeout=5000)
    try:
        await locator.click(timeout=15000)
    except Exception:
        try:
            await locator.evaluate("(el) => el.click()")
        except Exception:
            box = await locator.bounding_box()
            if not box:
                raise RuntimeError(f"按钮“{label}”可见但无法获取可点击区域")
            click_x = box["x"] + box["width"] / 2
            click_y = box["y"] + box["height"] / 2
            await page.mouse.move(click_x, click_y)
            await page.mouse.down()
            await page.mouse.up()
    await page.wait_for_timeout(2500)


async def _click_publish_button(page: Page, selectors: list[str], *, label: str) -> bool:
    last_error: Exception | None = None
    for selector in selectors:
        try:
            button = page.locator(selector).first
            await button.wait_for(state="visible", timeout=15000)
            await _click_locator_with_fallback(page, button, label=label)
            return True
        except Exception as exc:
            last_error = exc
            continue
    if last_error:
        raise RuntimeError(f"未找到“{label}”按钮：{last_error}")
    return False


async def _trigger_article_publish(page: Page) -> str:
    confirm_selectors = [
        "button.publish-btn.publish-btn-last:has-text('确认发布')",
        "button:has-text('确认发布')",
        "[role='button']:has-text('确认发布')",
        "text=确认发布",
    ]
    primary_publish_selectors = [
        ".publish-footer button.publish-btn.publish-btn-last",
        "button.publish-btn.publish-btn-last:has-text('发布')",
        "button.publish-btn.publish-btn-last:has-text('预览并发布')",
    ]
    compatibility_selectors = [
        "button:has-text('预览并发布')",
        "[role='button']:has-text('预览并发布')",
        "button:has-text('发布')",
        "[role='button']:has-text('发布')",
        "text=预览并发布",
    ]

    try:
        confirm_button = page.locator(confirm_selectors[0]).first
        if await confirm_button.count() and await confirm_button.is_visible():
            await _click_publish_button(page, confirm_selectors, label="确认发布")
            return "confirm_only"
    except Exception:
        pass

    try:
        await _click_publish_button(page, primary_publish_selectors, label="底部发布")
    except Exception:
        await _click_publish_button(page, compatibility_selectors, label="预览并发布")

    for _ in range(16):
        try:
            confirm_button = page.locator(confirm_selectors[0]).first
            if await confirm_button.count() and await confirm_button.is_visible():
                await _click_publish_button(page, confirm_selectors, label="确认发布")
                return "publish_then_confirm"
        except Exception:
            pass
        await page.wait_for_timeout(600)

    return "publish_only"


async def _check_success_toast(page: Page) -> str:
    candidates = ["发布成功", "已发布", "确认发布成功", "提交成功", "发布完成"]
    deadline = asyncio.get_running_loop().time() + 15
    while asyncio.get_running_loop().time() < deadline:
        try:
            body_text = await page.locator("body").inner_text(timeout=3000)
        except Exception:
            body_text = ""
        for item in candidates:
            if item in body_text:
                return item
        await page.wait_for_timeout(1200)
    return ""


def _summarize_publish_request(raw_body: str) -> dict[str, Any]:
    parsed = parse_qs(raw_body or "", keep_blank_values=True)
    values = {key: (items[-1] if items else "") for key, items in parsed.items()}

    def load_json(name: str) -> Any:
        raw = values.get(name)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return raw

    content = values.get("content") or ""
    extra_json = load_json("extra")
    covers_json = load_json("pgc_feed_covers")
    draft_form_json = load_json("draft_form_data")
    return {
        "title": values.get("title") or "",
        "content_length": len(content),
        "claim_exclusive": values.get("claim_exclusive"),
        "article_ad_type": values.get("article_ad_type"),
        "source": values.get("source"),
        "tuwen_wtt_transfer_switch": (
            extra_json.get("tuwen_wtt_transfer_switch") if isinstance(extra_json, dict) else None
        ),
        "content_word_cnt": extra_json.get("content_word_cnt") if isinstance(extra_json, dict) else None,
        "cover_type": draft_form_json.get("coverType") if isinstance(draft_form_json, dict) else None,
        "cover_count": len(covers_json or []) if isinstance(covers_json, list) else 0,
        "source_statement_list": values.get("source_statement_list") or "",
    }


def _attach_publish_probe(page: Page) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []

    async def on_response(response):
        try:
            request = response.request
            if TOUTIAO_PUBLISH_API not in response.url or request.method.upper() != "POST":
                return
            try:
                response_text = await response.text()
            except Exception as exc:
                response_text = f"<response read error: {exc}>"
            events.append(
                {
                    "status": response.status,
                    "request": _summarize_publish_request(request.post_data or ""),
                    "response_text": response_text,
                }
            )
        except Exception as exc:
            events.append({"status": 0, "request": {}, "response_text": f"<probe error: {exc}>"})

    page.on("response", lambda response: asyncio.create_task(on_response(response)))
    return events


async def _wait_for_publish_probe_events(
    page: Page,
    events: list[dict[str, Any]],
    *,
    timeout_seconds: int,
    stable_seconds: float = 3.0,
) -> list[dict[str, Any]]:
    start_count = len(events)
    last_count = start_count
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    last_change_at = loop.time()
    while loop.time() < deadline:
        current_count = len(events)
        if current_count != last_count:
            last_count = current_count
            last_change_at = loop.time()
        if current_count > start_count and (loop.time() - last_change_at) >= stable_seconds:
            break
        await page.wait_for_timeout(400)
    return events[start_count:]


async def _collect_publish_page_hints(page: Page) -> dict[str, Any]:
    return await page.evaluate(
        """() => {
            const findChecked = (text) => {
                const label = Array.from(document.querySelectorAll('label')).find(
                    (el) => (el.innerText || '').includes(text)
                );
                if (!label) return null;
                return label.className.includes('checked') || !!label.querySelector('input:checked');
            };
            const findFlexibleChecked = (texts) => {
                const textOf = (el) => (el?.innerText || el?.textContent || '').trim();
                const textNode = Array.from(document.querySelectorAll('label, span, div, button')).find(
                    (el) => texts.some((text) => textOf(el).includes(text))
                );
                if (!textNode) return null;
                const candidates = [
                    textNode.closest('label'),
                    textNode.closest('[role="checkbox"]'),
                    textNode.closest('[aria-checked]'),
                    textNode.closest('.byte-checkbox, .semi-checkbox, .arco-checkbox, .switch, .byte-switch, .semi-switch, .arco-switch'),
                    textNode.parentElement,
                    textNode.parentElement?.parentElement,
                ].filter(Boolean);
                return candidates.some((el) => {
                    const nodes = [el, ...Array.from(el.querySelectorAll('*'))];
                    return nodes.some((node) => {
                        const cls = typeof node.className === 'string' ? node.className : '';
                        return (
                            cls.includes('checked') ||
                            cls.includes('active') ||
                            cls.includes('is-checked') ||
                            node.getAttribute?.('aria-checked') === 'true' ||
                            node.getAttribute?.('aria-selected') === 'true' ||
                            (node.tagName === 'INPUT' && node.checked)
                        );
                    });
                });
            };
            const titleBox = document.querySelector("textarea[placeholder*='标题'], textarea[placeholder*='文章标题']");
            const titleTip = document.querySelector('.title-tip');
            const bodyText = document.body.innerText || '';
            const lines = bodyText
                .split(/\\r?\\n/)
                .map((item) => item.trim())
                .filter(Boolean)
                .filter(
                    (item) =>
                        item.includes('还需输入') ||
                        item.includes('保存') ||
                        item.includes('失败') ||
                        item.includes('封面') ||
                        item.includes('声明') ||
                        item.includes('作品')
                )
                .slice(0, 20);
            const selectedStatements = Array.from(document.querySelectorAll('.source-info-wrap label'))
                .filter((el) => el.className.includes('checked') || el.querySelector('input:checked'))
                .map((el) => (el.innerText || '').trim());
            const collectionBtn = document.querySelector('button.collection-btn');
            return {
                title_value: titleBox ? titleBox.value : '',
                title_tip: titleTip ? titleTip.innerText.trim() : '',
                editor_text_length: (document.querySelector('.ProseMirror')?.innerText || '').trim().length,
                editor_image_count: document.querySelectorAll('.ProseMirror img').length,
                cover_count: document.querySelectorAll('.article-cover-images-wrap img').length,
                no_cover_checked: findChecked('无封面'),
                single_cover_checked: findChecked('单图'),
                triple_cover_checked: findChecked('三图'),
                no_ad_checked: findChecked('不投放广告'),
                ad_checked: findChecked('投放广告赚收益'),
                wtt_checked: findChecked('发布得更多收益'),
                claim_exclusive_checked: findFlexibleChecked(['头条首发', '原创首发', '首发']),
                collection_text: collectionBtn ? collectionBtn.innerText.trim() : '',
                selected_statements: selectedStatements,
                body_hints: lines,
            };
        }"""
    )


async def _collect_publish_account_diagnostics(page: Page) -> dict[str, Any]:
    return await page.evaluate(
        """async () => {
            const pgc = window.Garr?.pgc_info || {};
            const user = pgc.user || {};
            const media = pgc.media || {};
            const result = {
                user_id: user.id || 0,
                media_id: media.id || 0,
                media_status: media.media_status_str || '',
                has_already_authentication: !!media.has_already_authentication,
                is_new_register: !!media.is_new_register,
                show_creation_btn: media.show_creation_btn,
                manual_publish_ad: media.manual_publish_ad,
                claim_origin_permission: media.claim_origin_permission,
                claim_exclusive_permission: media.claim_origin_permission,
            };
            try {
                const resp = await fetch('/mp/agw/mass_profit/jingxuan_account_check', {
                    method: 'POST',
                    credentials: 'include',
                });
                result.jingxuan_account_check = await resp.json();
            } catch (error) {
                result.jingxuan_account_check_error = String(error);
            }
            if (user.id) {
                try {
                    const resp = await fetch(`/mp/agw/media/m_get_proj?app_id=13&user_id=${user.id}`, {
                        credentials: 'include',
                    });
                    result.media_project = await resp.json();
                } catch (error) {
                    result.media_project_error = String(error);
                }
            }
            return result;
        }"""
    )


def _parse_publish_response_code(event: dict[str, Any]) -> int | None:
    try:
        payload = json.loads(event.get("response_text") or "{}")
    except Exception:
        return None
    for key in ("code", "err_no"):
        value = payload.get(key)
        if isinstance(value, int):
            return value
    return None


def _extract_article_id_from_publish_event(event: dict[str, Any] | None) -> str | int | None:
    if not event:
        return None
    try:
        payload = json.loads(event.get("response_text") or "{}")
    except Exception:
        return None
    return _find_first_value_by_keys(payload, ("group_id", "article_id", "pgc_id", "item_id", "id"))


def _safe_json_loads(raw: str) -> Any:
    try:
        return json.loads(raw or "")
    except Exception:
        return None


def _compact_account_diag(account_diag: dict[str, Any]) -> dict[str, Any]:
    return {
        "media_status": account_diag.get("media_status"),
        "has_already_authentication": account_diag.get("has_already_authentication"),
        "is_new_register": account_diag.get("is_new_register"),
        "show_creation_btn": account_diag.get("show_creation_btn"),
        "jingxuan_account_check": account_diag.get("jingxuan_account_check"),
        "media_project": account_diag.get("media_project"),
    }


def _account_readiness_issue(account_diag: dict[str, Any], *, include_soft: bool = True) -> str:
    jingxuan = account_diag.get("jingxuan_account_check") or {}
    jingxuan_data = jingxuan.get("data") if isinstance(jingxuan, dict) else {}
    verified = jingxuan_data.get("verified") if isinstance(jingxuan_data, dict) else {}
    account_info = jingxuan_data.get("account") if isinstance(jingxuan_data, dict) else {}
    media_project = account_diag.get("media_project") or {}

    verified_finish = verified.get("finish") if isinstance(verified, dict) else None
    account_finish = account_info.get("finish") if isinstance(account_info, dict) else None
    project_code = media_project.get("code") if isinstance(media_project, dict) else None
    project_message = ""
    if isinstance(media_project, dict):
        project_message = str(media_project.get("message") or media_project.get("reason") or "").strip()
    project_message_lower = project_message.lower()

    if account_diag.get("has_already_authentication") is False or verified_finish is False:
        return "头条号实名认证状态未完全就绪，请先在头条号后台完成认证后再重试。"
    if include_soft and account_finish is False:
        return "头条号创作项目未初始化完成（account.finish=false），请先在头条号后台手动完成一次创作项目开通后再重试。"
    if include_soft and (
        project_code not in (None, 0)
        or "proj_id" in project_message_lower
        or ("project" in project_message_lower and "nil" in project_message_lower)
    ):
        return "头条号创作项目未初始化完成（get proj_id nil / media_project 异常），请先在头条号后台手动完成一次创作项目开通后再重试。"
    return ""


def _response_summary_from_event(event: dict[str, Any]) -> dict[str, Any]:
    payload = _safe_json_loads(event.get("response_text") or "")
    if isinstance(payload, dict):
        return {
            "code": payload.get("code", payload.get("err_no")),
            "message": payload.get("message") or payload.get("errmsg") or "",
            "reason": payload.get("reason") or payload.get("detail") or "",
        }
    return {"raw": (event.get("response_text") or "")[:240]}


def _title_over_limit_reason(page_hints: dict[str, Any], request_summary: dict[str, Any]) -> str:
    title_tip = str(page_hints.get("title_tip") or "").strip()
    title_value = str(page_hints.get("title_value") or request_summary.get("title") or "").strip()
    match = re.search(r"(\d+)\s*/\s*(\d+)", title_tip)
    if match:
        used = int(match.group(1))
        limit = int(match.group(2))
        if used > limit:
            return f"头条标题超出平台限制：当前 {used} / {limit}，已触发保存失败。"
    if len(title_value) > DEFAULT_TITLE_CHAR_LIMIT:
        return f"头条标题超出平台限制：当前 {len(title_value)} / {DEFAULT_TITLE_CHAR_LIMIT}，已触发保存失败。"
    return ""


def _write_publish_diagnostics(config: dict[str, Any], prefix: str, diagnostics: dict[str, Any]) -> str:
    path = _debug_json_path(config, prefix)
    path.write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


async def _build_publish_timeout_details(
    page: Page | None,
    *,
    stage: str,
    timeout_seconds: int,
    publish_events: list[dict[str, Any]],
) -> dict[str, Any]:
    details: dict[str, Any] = {
        "stage": stage,
        "timeout_seconds": timeout_seconds,
        "likely_reason": f"今日头条发布链路超过总超时 {timeout_seconds} 秒，已停止本次发布并保留现场诊断。",
        "publish_events": publish_events[-5:],
    }
    if page is None:
        return details
    try:
        details["url"] = page.url
    except Exception:
        pass
    try:
        details["page"] = await asyncio.wait_for(_collect_publish_page_hints(page), timeout=5)
    except Exception as exc:
        details["page_error"] = str(exc)
    try:
        account_diag = await asyncio.wait_for(_collect_publish_account_diagnostics(page), timeout=8)
        details["account"] = _compact_account_diag(account_diag)
    except Exception as exc:
        details["account_error"] = str(exc)
    return details


async def _recover_published_after_error(
    settings: Settings,
    config: dict[str, Any],
    page: Page | None,
    draft: dict[str, Any],
    draft_id: int,
    title: str,
    error_text: str,
    min_create_time: int | None = None,
    require_recent: bool = True,
) -> dict[str, Any] | None:
    if page is None:
        return None
    try:
        verify_result = await _verify_article_published_with_retries(
            page,
            title,
            config,
            min_create_time=min_create_time,
            require_recent=require_recent,
        )
    except Exception:
        return None
    if verify_result.get("submitted") or verify_result.get("verified"):
        return _result_from_verified_article(
            settings,
            config,
            draft,
            draft_id,
            title,
            verify_result,
            recovered_after_error=True,
            error_text=error_text[:500],
        )
    return None


async def _ensure_publish_account_ready(page: Page, config: dict[str, Any], draft_id: int) -> dict[str, Any]:
    account_diag = await _collect_publish_account_diagnostics(page)
    issue = _account_readiness_issue(account_diag, include_soft=False)
    if not issue:
        return account_diag
    diagnostics = {
        "stage": "precheck",
        "likely_reason": issue,
        "account": _compact_account_diag(account_diag),
    }
    diagnostic_path = _write_publish_diagnostics(config, f"publish_draft_{draft_id}_precheck", diagnostics)
    raise ToutiaoPublishError(
        f"今日头条发布前校验未通过：{issue}",
        diagnostics=diagnostics,
        diagnostic_path=diagnostic_path,
        retryable=False,
    )


async def _build_publish_failure_details(
    page: Page,
    publish_events: list[dict[str, Any]],
) -> dict[str, Any]:
    page_hints = await _collect_publish_page_hints(page)
    account_diag = await _collect_publish_account_diagnostics(page)
    last_event = publish_events[-1] if publish_events else {}
    request_summary = last_event.get("request") or {}
    response_summary = _response_summary_from_event(last_event)
    compact_diag = {
        "page": page_hints,
        "request": request_summary,
        "response": response_summary,
        "likely_reason": "",
        "account": _compact_account_diag(account_diag),
    }
    title_issue = _title_over_limit_reason(page_hints, request_summary)
    account_issue = _account_readiness_issue(account_diag)
    if title_issue:
        compact_diag["likely_reason"] = title_issue
    elif request_summary.get("title") == "":
        compact_diag["likely_reason"] = "标题没有真正写入头条前端状态。"
    elif request_summary.get("cover_type") in (1, 2, 3) and not request_summary.get("cover_count"):
        compact_diag["likely_reason"] = "封面模式已开启，但请求里没有有效封面数据。"
    elif account_issue:
        compact_diag["likely_reason"] = account_issue
    elif response_summary.get("code") == 7050:
        compact_diag["likely_reason"] = (
            "头条接口返回 7050（草稿自动保存失败），但本次没有观察到最终确认发布成功请求；"
            "通常是发布按钮未真正触发、确认发布未完成，或平台当前状态阻断了最终提交。"
        )
    else:
        response_message = str(response_summary.get("message") or response_summary.get("reason") or "").strip()
        if response_message:
            compact_diag["likely_reason"] = f"头条接口返回异常：{response_message}"
    return compact_diag


async def _publish_draft_to_toutiao_core(
    settings: Settings,
    draft_id: int,
    *,
    allow_login_retry: bool,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config = _toutiao_config(settings)
    state = state if state is not None else {}
    state.setdefault("stage", "init")
    state.setdefault("publish_events", [])
    draft = fetch_draft_by_id(settings, draft_id)
    if not draft:
        raise RuntimeError(f"稿件不存在：draft_id={draft_id}")
    if draft.get("toutiao_uploaded_at"):
        return _existing_uploaded_payload(config, draft_id, draft)

    archive_path = Path(draft.get("archive_path") or "")
    if not archive_path.exists():
        raise RuntimeError(f"稿件归档文件不存在：{archive_path}")

    title = _toutiao_title(
        draft.get("title") or draft.get("canonical_title") or archive_path.stem,
        char_limit=config["title_char_limit"],
    )
    content_md = _read_markdown(archive_path)
    content_html, cover_paths, inline_image_count = _render_markdown_to_toutiao_html(
        content_md=content_md,
        title=title,
        base_dir=archive_path.parent,
        max_inline_images=config["max_inline_images"],
    )
    plain_text = _html_to_plain_text(content_html)

    context: BrowserContext | None = None
    page: Page | None = None
    try:
        state["stage"] = "open_browser"
        context = await _new_context(config)
        page = context.pages[0] if context.pages else await context.new_page()
        state["page"] = page
        publish_probe = _attach_publish_probe(page)
        state["publish_events"] = publish_probe

        state["stage"] = "open_publish_page"
        await _ensure_publish_page(page, config["publish_timeout_seconds"])
        if _is_login_url(page.url):
            if allow_login_retry and config["auto_open_login_on_publish"]:
                state["stage"] = "login"
                login_result = await _wait_until_logged_in(context, config)
                if not login_result.get("logged_in"):
                    raise RuntimeError(login_result.get("message") or "头条号未登录")
                page = context.pages[0] if context.pages else page
                state["page"] = page
                state["stage"] = "reopen_publish_page"
                await _ensure_publish_page(page, config["publish_timeout_seconds"])
                await page.wait_for_timeout(2000)
            else:
                raise RuntimeError("头条号未登录，请先初始化登录")

        state["stage"] = "editor_ready"
        await _dismiss_masks(page)
        await _wait_publish_editor_ready(page, timeout_seconds=30)
        state["stage"] = "fill_title"
        actual_title = await _fill_title(page, title)
        state["stage"] = "fill_content"
        content_result = await _fill_content(page, content_html, plain_text)
        state["stage"] = "wait_auto_save"
        await _wait_after_initial_edit(page, timeout_seconds=12)
        state["stage"] = "account_precheck"
        await _ensure_publish_account_ready(page, config, draft_id)
        state["stage"] = "configure_options"
        publish_options_result = await _configure_publish_options(page, config, cover_paths)
        state["stage"] = "click_publish"
        state["publish_started_at"] = int(time.time()) - 10
        publish_mode = await _trigger_article_publish(page)
        state["publish_clicked"] = True
        state["stage"] = "wait_publish_response"
        publish_events = await _wait_for_publish_probe_events(
            page,
            publish_probe,
            timeout_seconds=min(45, config["publish_timeout_seconds"]),
        )
        state["publish_events"] = publish_probe
        state["stage"] = "check_success_toast"
        success_toast = await _check_success_toast(page)
        state["stage"] = "verify_article_list"
        verify_result = await _verify_article_published_with_retries(
            page,
            actual_title,
            config,
            min_create_time=state.get("publish_started_at"),
            require_recent=True,
        )
        success_event = next((event for event in reversed(publish_events) if _parse_publish_response_code(event) == 0), None)
        success = bool(success_toast or verify_result.get("submitted") or verify_result.get("verified") or success_event)
        if not success:
            state["stage"] = "build_failure_diagnostic"
            failure_details = await _build_publish_failure_details(page, publish_events or publish_probe)
            diagnostic_path = _write_publish_diagnostics(
                config,
                f"publish_draft_{draft_id}_diagnostic",
                failure_details,
            )
            issue = str(failure_details.get("likely_reason") or "").strip() or "接口未返回成功"
            retryable = failure_details.get("response", {}).get("code") not in {7050}
            raise ToutiaoPublishError(
                f"今日头条发布失败：{issue}",
                diagnostics=failure_details,
                diagnostic_path=diagnostic_path,
                retryable=retryable,
            )
        article_id = verify_result.get("article_id") or _extract_article_id_from_publish_event(success_event)
        submission_state = verify_result.get("submission_state") or ("submitted" if success_event else "draft")
        status_desc = verify_result.get("status_desc") or ("已提交到头条平台" if success_event else "")
        state["stage"] = "mark_uploaded"
        mark_draft_toutiao_uploaded(settings, draft_id, article_id)
        state["stage"] = "done"
        return {
            "draft_id": draft_id,
            "title": draft.get("title"),
            "toutiao_title": actual_title,
            "inline_image_count": inline_image_count,
            "content_mode": content_result["mode"],
            "content_length": content_result["content_length"],
            "editor_image_count": content_result["actual_image_count"],
            "cover_count": publish_options_result["cover_count"],
            "cover_uploaded": publish_options_result["cover_uploaded"],
            "cover_mode": publish_options_result["cover_mode"],
            "success_toast": success_toast,
            "publish_events": publish_events,
            "publish_mode": publish_mode,
            "verified": bool(verify_result.get("submitted") or verify_result.get("verified") or success_event),
            "verified_status": verify_result.get("status"),
            "verify_attempt": verify_result.get("verify_attempt"),
            "verify_attempts": verify_result.get("verify_attempts"),
            "published": bool(verify_result.get("published")),
            "submitted": bool(verify_result.get("submitted") or success_event),
            "submission_state": submission_state,
            "status_desc": status_desc,
            "article_id": article_id,
            "ad_enabled": publish_options_result["ad_enabled"],
            "claim_exclusive": publish_options_result["claim_exclusive"],
            "collection_name": publish_options_result["collection_name"],
            "statement_labels": publish_options_result["statement_labels"],
            "publish_more_income": publish_options_result["publish_more_income"],
            "browser_profile_dir": config["browser_profile_dir"],
            "already_uploaded": False,
        }
    except Exception as exc:
        recover_title = locals().get("actual_title") or title
        if _can_recover_after_publish(state):
            recovered = await _recover_published_after_error(
                settings,
                config,
                page,
                draft,
                draft_id,
                recover_title,
                str(exc),
                min_create_time=state.get("publish_started_at"),
                require_recent=True,
            )
            if recovered:
                return recovered
        screenshot = await _capture_debug_screenshot(page, config, f"publish_draft_{draft_id}_error")
        if isinstance(exc, ToutiaoPublishError):
            raise exc.with_screenshot(screenshot)
        raise RuntimeError(f"{exc}｜截图：{screenshot}") from exc
    finally:
        await _close_context(context)


async def _publish_draft_to_toutiao_once(
    settings: Settings,
    draft_id: int,
    *,
    allow_login_retry: bool,
) -> dict[str, Any]:
    config = _toutiao_config(settings)
    state: dict[str, Any] = {"stage": "init", "publish_events": []}
    task = asyncio.create_task(
        _publish_draft_to_toutiao_core(
            settings,
            draft_id,
            allow_login_retry=allow_login_retry,
            state=state,
        )
    )
    done, _pending = await asyncio.wait(
        {task},
        timeout=config["publish_total_timeout_seconds"],
    )
    if task in done:
        return await task

    page = state.get("page")
    publish_events = state.get("publish_events") or []
    diagnostics = await _build_publish_timeout_details(
        page,
        stage=str(state.get("stage") or "unknown"),
        timeout_seconds=int(config["publish_total_timeout_seconds"]),
        publish_events=publish_events,
    )
    draft = fetch_draft_by_id(settings, draft_id)
    if draft and _can_recover_after_publish(state):
        recover_title = str(diagnostics.get("page", {}).get("title_value") or "").strip()
        if recover_title:
            recovered = await _recover_published_after_error(
                settings,
                config,
                page,
                draft,
                draft_id,
                recover_title,
                diagnostics.get("likely_reason") or "今日头条发布超时",
                min_create_time=state.get("publish_started_at"),
                require_recent=True,
            )
            if recovered:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                return recovered
    diagnostic_path = _write_publish_diagnostics(config, f"publish_draft_{draft_id}_timeout", diagnostics)
    screenshot = await _capture_debug_screenshot(page, config, f"publish_draft_{draft_id}_timeout")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    raise ToutiaoPublishError(
        diagnostics.get("likely_reason") or "今日头条发布超时",
        diagnostics=diagnostics,
        diagnostic_path=diagnostic_path,
        screenshot_path=screenshot,
        retryable=True,
    )


def publish_draft_to_toutiao(settings: Settings, draft_id: int) -> dict[str, Any]:
    config = _toutiao_config(settings)
    draft = fetch_draft_by_id(settings, draft_id)
    if not draft:
        raise RuntimeError(f"稿件不存在：draft_id={draft_id}")
    if draft.get("toutiao_uploaded_at"):
        return _existing_uploaded_payload(config, draft_id, draft)
    with _ToutiaoPublishFileLock(config):
        # 拿到锁之后再查一次，避免等待期间其他入口已经发布并打标。
        latest = fetch_draft_by_id(settings, draft_id)
        if latest and latest.get("toutiao_uploaded_at"):
            return _existing_uploaded_payload(config, draft_id, latest)
        return asyncio.run(_publish_draft_to_toutiao_once(settings, draft_id, allow_login_retry=True))


def publish_recent_drafts_to_toutiao(settings: Settings, limit: int = 10) -> dict[str, Any]:
    candidates = fetch_recent_drafts(settings, limit=max(1, min(limit * 6, 200)))
    published = []
    failed = []
    skipped = []
    for draft in candidates:
        if len(published) >= limit:
            break
        try:
            result = publish_draft_to_toutiao(settings, int(draft["id"]))
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


def _safe_json_loads(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str) or not value.strip():
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


async def _fetch_toutiao_article_stats_async(config: dict[str, Any], limit: int) -> dict[str, Any]:
    context = await _new_context(config, headless=True)
    page = context.pages[0] if context.pages else await context.new_page()
    try:
        await page.goto("https://mp.toutiao.com/profile_v4/content/manage", wait_until="domcontentloaded", timeout=60000)
        if _is_login_url(page.url):
            return {"ok": False, "error": "not_login", "url": page.url, "articles": []}
        payload = await _query_article_list(page, status="all", page_size=max(5, min(int(limit), 100)))
        outer = (payload or {}).get("data") or {}
        articles = ((outer.get("data") or {}).get("articles") or []) if isinstance(outer, dict) else []
        normalized: list[dict[str, Any]] = []
        for article in articles:
            copy_info = _safe_json_loads(article.get("copy_info"), {})
            covers = _safe_json_loads(article.get("pgc_feed_covers"), [])
            read_count = article.get("go_detail_count_v2")
            if read_count is None:
                read_count = article.get("go_detail_count") or article.get("read_count") or 0
            impression = article.get("impression_count") or 0
            try:
                ctr = round(float(read_count or 0) / max(float(impression or 0), 1.0) * 100, 2)
            except Exception:
                ctr = 0.0
            normalized.append(
                {
                    "title": article.get("title") or "",
                    "article_id": article.get("group_id") or article.get("article_id") or article.get("id") or "",
                    "article_url": article.get("article_url") or "",
                    "status_desc": article.get("status_desc") or "",
                    "create_time": _article_time_value(article),
                    "word_count": copy_info.get("word_count") or article.get("content_word_cnt"),
                    "image_count": copy_info.get("image_count"),
                    "cover_count": len(covers) if isinstance(covers, list) else 0,
                    "impression_count": impression,
                    "read_count": read_count,
                    "ctr_percent": ctr,
                    "digg_count": article.get("digg_count") or 0,
                    "comment_count": article.get("comment_count") or 0,
                    "collection": article.get("pgc_collection_title") or "",
                    "claim_exclusive": str(article.get("claim_exclusive") or ""),
                    "large_image_url": article.get("large_image_url") or "",
                }
            )
        return {
            "ok": True,
            "api_url": payload.get("url"),
            "count": len(normalized),
            "articles": normalized,
        }
    finally:
        await _close_context(context)


def fetch_toutiao_article_stats(settings: Settings, limit: int = 50) -> dict[str, Any]:
    config = _toutiao_config(settings)
    return asyncio.run(_fetch_toutiao_article_stats_async(config, limit=max(5, min(int(limit), 100))))
