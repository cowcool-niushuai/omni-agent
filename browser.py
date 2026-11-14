"""Headless browser automation.

The browser is intentionally a thin, policy-friendly layer over Playwright:

* one daemon thread owns the Playwright/browser/page objects (the sync API is
  thread-affine), while agent tool calls submit commands through a queue;
* every navigation and subresource request is checked by the shared SSRF
  policy, and private addresses are rejected unless explicitly enabled;
* no JavaScript evaluation / arbitrary code execution is exposed;
* screenshots are restricted to the workspace ``browser/`` directory and are
  size-capped.

Only safe, read-oriented actions are allowed automatically. Interactions
(click / fill / press) are classified as ``ASK_USER`` by ``permission.py``.
"""

from __future__ import annotations

import atexit
import os
import queue
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any

import structlog

from omni_agent.config import get_settings
from omni_agent.security.limits import truncate_text
from omni_agent.security.paths import PathSecurityError, safe_join
from omni_agent.security.ssrf import (
    UnsafeURLError,
    resolve_and_validate,
    validate_url_static,
)

log = structlog.get_logger("omni.browser")

SUPPORTED_ACTIONS = {
    "goto",
    "click",
    "fill",
    "press",
    "text",
    "screenshot",
    "wait",
    "back",
    "forward",
    "reload",
    "status",
    "close",
}
INTERACTION_ACTIONS = {"click", "fill", "press"}
_TITLE_TIMEOUT_MS = 2_000
_HOST_CACHE_SECONDS = 30.0


class BrowserError(RuntimeError):
    """Raised for invalid browser requests."""


def validate_browser_url(url: str) -> None:
    """Validate a public HTTP(S) URL, including DNS resolution."""
    settings = get_settings()
    validated = validate_url_static(
        url,
        allowed_ports=settings.web_fetch_port_list,
        allow_private=settings.allow_private_network,
    )
    resolve_and_validate(validated, allow_private=settings.allow_private_network)


class BrowserManager:
    """Thread-safe synchronous facade over a single headless browser."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._commands: queue.Queue[Any] | None = None
        self._thread: threading.Thread | None = None
        self._page_ready = threading.Event()
        self._state_lock = threading.Lock()
        self._state: dict[str, str] = {"url": "", "title": ""}

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread and thread.is_alive() and self._page_ready.is_set())

    def status(self) -> dict[str, Any]:
        settings = get_settings()
        with self._state_lock:
            state = dict(self._state)
        return {
            "enabled": settings.browser_enabled,
            "running": self.running,
            "url": state.get("url", ""),
            "title": state.get("title", ""),
        }

    def run(self, action: str, arguments: dict[str, Any] | None = None) -> str:
        """Execute one browser command and return a human-readable result."""
        settings = get_settings()
        action = str(action or "").strip().lower()
        arguments = arguments or {}

        if not settings.browser_enabled:
            return "浏览器自动化已在配置中禁用（BROWSER_ENABLED=false）。"
        if action == "status":
            info = self.status()
            if not info["running"]:
                return "浏览器未启动（首次使用 goto 等操作时自动启动无头浏览器）。"
            return (
                "浏览器运行中\n"
                f"URL: {info['url'] or '(空白页)'}\n"
                f"标题: {info['title'] or '(无标题)'}"
            )
        try:
            self._precheck(action, arguments)
        except (BrowserError, UnsafeURLError, PathSecurityError, ValueError) as exc:
            return f"浏览器操作被拒绝: {exc}"

        self._ensure_worker()
        command: tuple[str, dict[str, Any], Future[str]] = (
            action,
            dict(arguments),
            Future(),
        )
        with self._lock:
            commands = self._commands
        if commands is None:
            return "浏览器暂不可用，请稍后重试。"

        try:
            commands.put(command)
        except Exception as exc:  # pragma: no cover - queue is unbounded
            return f"浏览器操作提交失败: {exc}"

        timeout = (
            settings.browser_timeout_seconds + settings.browser_launch_timeout_seconds
        )
        try:
            return str(command[2].result(timeout=timeout))
        except FutureTimeoutError:
            return (
                f"浏览器操作超时（{timeout:g} 秒）：{action}。"
                "页面可能仍在加载，可稍后使用 status/text 查看。"
            )
        except Exception as exc:
            return f"浏览器操作失败（{action}）: {exc}"

    def stop(self) -> None:
        """Ask the browser worker to close, waiting briefly for cleanup."""
        thread = self._thread
        if not thread or not thread.is_alive():
            return
        with self._lock:
            commands = self._commands
        if commands is not None:
            future: Future[str] = Future()
            try:
                commands.put(("close", {}, future))
                future.result(timeout=5)
            except Exception:
                pass
        thread.join(timeout=5)
        if thread.is_alive():
            log.warning("browser_stop_timeout")

    # ------------------------------------------------------------------
    # validation
    # ------------------------------------------------------------------
    def _precheck(self, action: str, arguments: dict[str, Any]) -> None:
        settings = get_settings()
        if action not in SUPPORTED_ACTIONS:
            raise BrowserError(
                f"不支持的浏览器操作 '{action}'，可用操作: "
                + ", ".join(sorted(SUPPORTED_ACTIONS))
            )

        if action == "goto":
            url = str(arguments.get("url", "")).strip()
            if not url:
                raise BrowserError("goto 需要 url 参数")
            validate_browser_url(url)

        if action in {"click", "fill", "press"}:
            selector = str(arguments.get("selector", "")).strip()
            if not selector and action != "press":
                raise BrowserError(f"{action} 需要 selector 参数")

        if action == "fill" and "value" not in arguments:
            raise BrowserError("fill 需要 value 参数")
        if action == "press" and not str(arguments.get("key", "")).strip():
            raise BrowserError("press 需要 key 参数")

        if action == "screenshot":
            filename = str(arguments.get("filename", "")).strip()
            if filename:
                path = safe_join(
                    Path(settings.workspace_dir).resolve() / "browser", filename
                )
                if path.suffix.lower() != ".png":
                    raise BrowserError("截图文件名必须以 .png 结尾")

        if action == "wait":
            try:
                milliseconds = int(arguments.get("milliseconds", 1000))
            except (TypeError, ValueError) as exc:
                raise BrowserError("wait 的 milliseconds 必须是整数") from exc
            if milliseconds < 0 or milliseconds > settings.browser_max_wait_ms:
                raise BrowserError(
                    f"wait 时长必须在 0-{settings.browser_max_wait_ms} 毫秒之间"
                )

    # ------------------------------------------------------------------
    # worker lifecycle
    # ------------------------------------------------------------------
    def _ensure_worker(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._commands = queue.Queue()
            self._thread = threading.Thread(
                target=self._worker_entry,
                name="omni-browser",
                daemon=True,
            )
            self._thread.start()

    def _worker_entry(self) -> None:
        try:
            self._worker()
        except Exception as exc:
            log.exception("browser_worker_failed")
            self._fail_pending(f"浏览器启动/运行失败: {exc}")
        finally:
            self._page_ready.clear()
            with self._state_lock:
                self._state = {"url": "", "title": ""}
            with self._lock:
                if threading.current_thread() is self._thread:
                    self._thread = None
                    self._commands = None

    def _worker(self) -> None:
        from playwright.sync_api import sync_playwright

        settings = get_settings()
        playwright: Any = None
        browser: Any = None
        context: Any = None
        page: Any = None
        validated_hosts: dict[str, float] = {}

        def route_handler(route: Any) -> None:
            request = route.request
            url = request.url
            if url in {"about:blank"} or url.startswith(("data:", "blob:")):
                route.continue_()
                return
            try:
                validated = validate_url_static(
                    url,
                    allowed_ports=settings.web_fetch_port_list,
                    allow_private=settings.allow_private_network,
                )
                key = f"{validated.host}:{validated.port}"
                now = time.monotonic()
                checked_at = validated_hosts.get(key)
                if checked_at is None or now - checked_at > _HOST_CACHE_SECONDS:
                    resolve_and_validate(
                        validated,
                        allow_private=settings.allow_private_network,
                    )
                    validated_hosts[key] = now
            except UnsafeURLError as exc:
                log.warning(
                    "browser_request_blocked",
                    url=url[:300],
                    reason=str(exc),
                )
                route.abort("blockedbyclient")
                return
            route.continue_()

        try:
            playwright = sync_playwright().start()
            launch_options: dict[str, Any] = {
                "headless": settings.browser_headless,
                "args": ["--disable-dev-shm-usage"],
            }
            proxy_server = settings.browser_proxy.strip() or (
                os.environ.get("HTTPS_PROXY")
                or os.environ.get("https_proxy")
                or os.environ.get("HTTP_PROXY")
                or os.environ.get("http_proxy")
                or os.environ.get("ALL_PROXY")
                or os.environ.get("all_proxy")
                or ""
            ).strip()
            if proxy_server.startswith("socks5h://"):
                proxy_server = "socks5://" + proxy_server[len("socks5h://") :]
            if proxy_server:
                proxy: dict[str, str] = {"server": proxy_server}
                bypass = settings.browser_proxy_bypass.strip() or (
                    os.environ.get("NO_PROXY")
                    or os.environ.get("no_proxy", "")
                ).strip()
                if bypass:
                    proxy["bypass"] = bypass
                launch_options["proxy"] = proxy
            browser = playwright.chromium.launch(**launch_options)
            context_options: dict[str, Any] = {
                "viewport": {
                    "width": settings.browser_viewport_width,
                    "height": settings.browser_viewport_height,
                },
                "accept_downloads": False,
                "service_workers": "block",
            }
            if settings.browser_user_agent.strip():
                context_options["user_agent"] = settings.browser_user_agent.strip()
            context = browser.new_context(**context_options)
            context.route("**/*", route_handler)
            page = context.new_page()
            page.set_default_timeout(int(settings.browser_timeout_seconds * 1000))
            page.set_default_navigation_timeout(
                int(settings.browser_timeout_seconds * 1000)
            )
            self._page_ready.set()
            log.info("browser_started", headless=settings.browser_headless)

            commands = self._commands
            if commands is None:
                return
            while True:
                action, arguments, future = commands.get()
                if action == "close":
                    future.set_result("浏览器已关闭。")
                    break
                try:
                    result = self._perform(page, action, arguments)
                except Exception as exc:
                    log.warning("browser_action_failed", action=action, error=str(exc))
                    result = f"浏览器操作失败（{action}）: {exc}"
                self._refresh_state(page)
                if not future.done():
                    future.set_result(result)
        finally:
            self._page_ready.clear()
            for resource in (context, browser):
                if resource is not None:
                    try:
                        resource.close()
                    except Exception:
                        pass
            if playwright is not None:
                try:
                    playwright.stop()
                except Exception:
                    pass
            log.info("browser_stopped")

    def _fail_pending(self, message: str) -> None:
        commands = self._commands
        if commands is None:
            return
        while True:
            try:
                _, _, future = commands.get_nowait()
            except queue.Empty:
                return
            if not future.done():
                future.set_result(message)

    # ------------------------------------------------------------------
    # page operations (always called on the browser worker thread)
    # ------------------------------------------------------------------
    def _perform(self, page: Any, action: str, arguments: dict[str, Any]) -> str:
        settings = get_settings()
        timeout_ms = int(settings.browser_timeout_seconds * 1000)

        if action == "goto":
            url = str(arguments.get("url", "")).strip()
            response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            self._wait_quietly(page)
            self._refresh_state(page)
            status = response.status if response is not None else "unknown"
            return (
                f"已打开网页\nURL: {page.url}\n"
                f"状态码: {status}\n标题: {self._title(page) or '(无标题)'}"
            )

        if action == "text":
            selector = str(arguments.get("selector", "")).strip() or "body"
            locator = page.locator(selector).first
            body = locator.inner_text(timeout=timeout_ms)
            limit = settings.browser_max_text_chars
            requested = arguments.get("max_chars")
            if requested is not None:
                try:
                    requested_int = int(requested)
                except (TypeError, ValueError):
                    requested_int = 0
                if requested_int > 0:
                    limit = min(requested_int, limit)
            text = truncate_text(body.strip(), limit)
            self._refresh_state(page)
            return (
                f"--- Browser Text ---\nURL: {page.url}\n"
                f"Title: {self._title(page) or '(无标题)'}\n\n{text}"
            )

        if action == "click":
            selector = str(arguments.get("selector", "")).strip()
            page.locator(selector).first.click(timeout=timeout_ms)
            self._wait_quietly(page)
            self._refresh_state(page)
            return (
                f"已点击: {selector}\nURL: {page.url}\n"
                f"标题: {self._title(page) or '(无标题)'}"
            )

        if action == "fill":
            selector = str(arguments.get("selector", "")).strip()
            value = str(arguments.get("value", ""))
            page.locator(selector).first.fill(value, timeout=timeout_ms)
            self._refresh_state(page)
            return f"已填写: {selector}（{len(value)} 字符）"

        if action == "press":
            selector = str(arguments.get("selector", "")).strip()
            key = str(arguments.get("key", "")).strip()
            if selector:
                page.locator(selector).first.press(key, timeout=timeout_ms)
            else:
                page.keyboard.press(key)
            self._wait_quietly(page)
            self._refresh_state(page)
            return (
                f"已按键: {key}\nURL: {page.url}\n"
                f"标题: {self._title(page) or '(无标题)'}"
            )

        if action == "wait":
            milliseconds = int(arguments.get("milliseconds", 1000))
            page.wait_for_timeout(milliseconds)
            return f"已等待 {milliseconds} 毫秒。"

        if action == "back":
            page.go_back(wait_until="domcontentloaded", timeout=timeout_ms)
            self._wait_quietly(page)
            self._refresh_state(page)
            return f"已后退\nURL: {page.url}"

        if action == "forward":
            page.go_forward(wait_until="domcontentloaded", timeout=timeout_ms)
            self._wait_quietly(page)
            self._refresh_state(page)
            return f"已前进\nURL: {page.url}"

        if action == "reload":
            page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
            self._wait_quietly(page)
            self._refresh_state(page)
            return f"已刷新\nURL: {page.url}"

        if action == "screenshot":
            return self._screenshot(page, arguments)

        if action == "status":
            self._refresh_state(page)
            return (
                f"浏览器运行中\nURL: {page.url}\n"
                f"标题: {self._title(page) or '(无标题)'}"
            )

        raise BrowserError(f"不支持的浏览器操作: {action}")

    def _screenshot(self, page: Any, arguments: dict[str, Any]) -> str:
        settings = get_settings()
        root = Path(settings.workspace_dir).resolve() / "browser"
        filename = str(arguments.get("filename", "")).strip()
        if not filename:
            filename = f"shot_{int(time.time() * 1000)}.png"
        path = safe_join(root, filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        full_page = bool(arguments.get("full_page", False))
        page.screenshot(path=str(path), full_page=full_page)
        size = path.stat().st_size
        if size > settings.browser_screenshot_max_bytes:
            path.unlink(missing_ok=True)
            return (
                "截图失败: 图片过大"
                f"（{size} bytes，上限 {settings.browser_screenshot_max_bytes} bytes）"
            )
        relative = os.path.relpath(path, Path(settings.workspace_dir).resolve())
        return (
            f"截图已保存: {relative}\n大小: {size} bytes\nURL: {page.url}"
        )

    @staticmethod
    def _wait_quietly(page: Any) -> None:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=_TITLE_TIMEOUT_MS)
        except Exception:
            pass

    @staticmethod
    def _title(page: Any) -> str:
        try:
            return str(page.title())[:300]
        except Exception:
            return ""

    def _refresh_state(self, page: Any) -> None:
        with self._state_lock:
            self._state = {
                "url": str(getattr(page, "url", ""))[:1000],
                "title": self._title(page),
            }


browser_manager = BrowserManager()
atexit.register(browser_manager.stop)
