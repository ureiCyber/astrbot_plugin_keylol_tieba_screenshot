from __future__ import annotations

import asyncio
import json
import re
import time
from io import BytesIO
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star
from PIL import Image

from .keylol_browser import (
    KeylolBrowserCaptureError,
    KeylolBrowserCaptureStatus,
    KeylolBrowserTimeoutError,
    capture_keylol_webpage_screenshot,
)
from .keylol_page import (
    DEFAULT_MOBILE_VIEWPORT_WIDTH,
    DEFAULT_URL,
    KeylolPageError,
    MOBILE_PAGE_PADDING,
    build_render_html,
    extract_keylol_thread_urls,
    fetch_article,
    mobile_viewport_height,
    normalize_mobile_viewport_width,
    trim_rendered_screenshot,
)
from .tieba_page import (
    TiebaPageError,
    check_tieba_cookie,
    extract_tieba_thread_urls,
    fetch_tieba_article,
)
from .tieba_browser import (
    DEVICE_SCALE_FACTOR,
    TiebaBrowserCaptureError,
    TiebaBrowserCaptureStatus,
    TiebaBrowserTimeoutError,
    capture_tieba_webpage_screenshot,
    normalize_tieba_browser_url,
)
from .screenshot_safety import _normalize_for_qq, _validate_qq_image


class KeylolScreenshotPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        concurrency = max(1, min(4, int(config.get("max_concurrency", 2))))
        self._render_slots = asyncio.Semaphore(concurrency)
        self._recent_links: dict[tuple[str, str], float] = {}
        # Only paths allocated by our browser captures belong to this plugin.
        # HTML renderer files remain owned by AstrBot and its temp cleaner.
        self._owned_capture_paths: set[str] = set()
        self._encoding_tasks: set[asyncio.Task] = set()

    def _cookie(self) -> str:
        return str(self.config.get("keylol_cookie", "")).strip()

    def _tieba_cookie(self) -> str:
        return str(self.config.get("tieba_cookie", "")).strip()

    def _mobile_render_size(self) -> tuple[int, int]:
        width = normalize_mobile_viewport_width(
            self.config.get("content_width", DEFAULT_MOBILE_VIEWPORT_WIDTH)
        )
        return width, mobile_viewport_height(width)

    def _mobile_screenshot_options(
        self, viewport_width: int, viewport_height: int
    ) -> dict[str, object]:
        return {
            "type": "png",
            "full_page": True,
            "animations": "disabled",
            "caret": "hide",
            # Preserve Keylol's existing CSS-pixel HTML output.
            "scale": "css",
            "viewport_width": viewport_width,
            "viewport_height": viewport_height,
            "timeout": max(
                5000,
                min(
                    120000,
                    int(self.config.get("render_timeout_ms", 30000)),
                ),
            ),
        }

    def _tieba_html_screenshot_options(
        self, viewport_width: int, viewport_height: int
    ) -> dict[str, object]:
        # Ask AstrBot's renderer for native DPR 1.8 pixels; never upscale PNGs.
        return {
            **self._mobile_screenshot_options(viewport_width, viewport_height),
            "scale": "device",
            "device_scale_factor_level": "ultra",
        }

    def _keylol_render_engine(self) -> str:
        value = str(self.config.get("keylol_render_engine", "auto")).strip().lower()
        return value if value in {"auto", "playwright", "html"} else "auto"

    def _browser_capture_timeout_ms(self) -> int:
        try:
            value = int(self.config.get("browser_capture_timeout_ms", 120000))
        except (TypeError, ValueError):
            value = 120000
        return min(120000, max(15000, value))

    @staticmethod
    def _check_keylol_access(article: object, cookie: str) -> None:
        if bool(getattr(article, "has_locked_resources", False)) and cookie:
            raise KeylolPageError(
                "1 楼仍有受限资源；Cookie 可能已过期，或当前账号权限不足。"
            )

    async def _render_keylol_html_screenshot(
        self, target_url: str, cookie: str
    ) -> str:
        article = await fetch_article(
            target_url,
            cookie=cookie,
            proxy_url=str(self.config.get("proxy_url", "")),
            request_timeout_seconds=int(
                self.config.get("request_timeout_seconds", 25)
            ),
            inline_keylol_images=bool(
                self.config.get("inline_keylol_images", True)
            ),
            require_authentication=False,
        )
        self._check_keylol_access(article, cookie)
        if article.unresolved_image_count:
            logger.warning(
                f"其乐主楼有 {article.unresolved_image_count} 张图片未能内嵌；"
                "截图中会显示失败提示。"
            )
        viewport_width, viewport_height = self._mobile_render_size()
        document = build_render_html(article, content_width=viewport_width)
        image_path = await self.html_render(
            document,
            {},
            return_url=False,
            options=self._mobile_screenshot_options(
                viewport_width, viewport_height
            ),
        )
        if bool(self.config.get("adaptive_height", True)):
            try:
                await asyncio.to_thread(
                    trim_rendered_screenshot,
                    image_path,
                    bottom_padding=MOBILE_PAGE_PADDING,
                )
            except Exception:
                logger.warning(
                    "其乐截图自适应裁剪失败，将返回未经裁剪的完整截图。",
                    exc_info=True,
                )
        return image_path

    def _split_keylol_toc_sections(self) -> bool:
        return bool(self.config.get("split_toc_sections", True))

    def _max_keylol_toc_sections(self) -> int:
        try:
            value = int(self.config.get("max_toc_sections", 12))
        except (TypeError, ValueError):
            value = 12
        return min(20, max(1, value))

    async def _render_keylol_browser_screenshots(
        self, target_url: str, cookie: str
    ) -> list[str]:
        article = await fetch_article(
            target_url,
            cookie=cookie,
            proxy_url=str(self.config.get("proxy_url", "")),
            request_timeout_seconds=int(
                self.config.get("request_timeout_seconds", 25)
            ),
            inline_keylol_images=False,
            require_authentication=False,
        )
        self._check_keylol_access(article, cookie)
        if article.has_locked_resources:
            logger.warning(
                "其乐帖子包含登录后资源，但未配置 Cookie；将继续使用网页截图，"
                "受限附件会保持网页当前显示状态。"
            )

        viewport_width, viewport_height = self._mobile_render_size()
        timeout_ms = self._browser_capture_timeout_ms()
        # A directory post can produce several independent full-page captures.
        # Keep the per-page browser timeout bounded, but give the outer task a
        # bounded allowance for each additional directory item.
        toc_count = self._max_keylol_toc_sections()
        extra_toc_ms = (
            max(0, toc_count - 1) * 30_000
            if self._split_keylol_toc_sections()
            else 0
        )
        total_timeout_ms = min(600_000, timeout_ms + extra_toc_ms)
        try:
            result = await asyncio.wait_for(
                capture_keylol_webpage_screenshot(
                    target_url,
                    cookie=cookie,
                    viewport_width=viewport_width,
                    viewport_height=viewport_height,
                    timeout_ms=timeout_ms,
                    proxy_url=str(self.config.get("proxy_url", "")),
                    title=article.title,
                    author=article.author,
                    published_at=article.published_at,
                    split_toc_sections=self._split_keylol_toc_sections(),
                    max_toc_sections=self._max_keylol_toc_sections(),
                ),
                timeout=total_timeout_ms / 1000 + 10,
            )
        except asyncio.TimeoutError as exc:
            raise KeylolBrowserTimeoutError(
                "网页截图超过总等待时间。"
            ) from exc
        if result.status is KeylolBrowserCaptureStatus.PARTIAL:
            logger.warning(
                f"其乐网页截图不完整：{result.failed_image_count} 张图片未能显示，"
                f"{getattr(result, 'fallback_embed_count', 0)} 处嵌入内容使用静态回退提示。"
            )
        image_paths = tuple(
            str(path)
            for path in (getattr(result, "image_paths", None) or ())
            if str(path).strip()
        )
        if not image_paths:
            image_path = str(getattr(result, "image_path", "")).strip()
            if image_path:
                image_paths = (image_path,)
        if not image_paths:
            raise KeylolBrowserCaptureError("网页截图没有生成有效图片。")
        self._owned_capture_paths.update(image_paths)
        logger.info(
            f"其乐网页截图完成：共生成 {len(image_paths)} 张，"
            f"目录拆分={'开启' if self._split_keylol_toc_sections() else '关闭'}，"
            f"普通折叠展开 {getattr(result, 'auto_expanded_collapse_count', 0)} 处，"
            f"外链图片 {getattr(result, 'external_image_count', 0)} 张/"
            f"失败 {getattr(result, 'failed_external_image_count', 0)} 张，"
            f"嵌入成功 {getattr(result, 'loaded_embed_count', 0)} 处/"
            f"回退 {getattr(result, 'fallback_embed_count', 0)} 处。"
        )
        return list(image_paths)

    async def _render_keylol_browser_screenshot(
        self, target_url: str, cookie: str
    ) -> str:
        """Compatibility helper returning the first Keylol browser image."""

        paths = await self._render_keylol_browser_screenshots(target_url, cookie)
        self._cleanup_capture_paths(paths[1:])
        return paths[0]

    async def _render_keylol_html_screenshots(
        self, target_url: str, cookie: str
    ) -> list[str]:
        return [await self._render_keylol_html_screenshot(target_url, cookie)]

    async def _render_screenshots_unlocked(
        self, target_url: str, cookie: str, *, multiple: bool
    ) -> list[str]:
        """Render Keylol output after the caller has acquired the semaphore."""

        engine = self._keylol_render_engine()
        browser_renderer = (
            self._render_keylol_browser_screenshots
            if multiple
            else self._render_keylol_browser_screenshot
        )
        html_renderer = (
            self._render_keylol_html_screenshots
            if multiple
            else self._render_keylol_html_screenshot
        )
        if engine != "html":
            try:
                rendered = await browser_renderer(target_url, cookie)
                if multiple:
                    return list(rendered)
                return [str(rendered)]
            except KeylolBrowserCaptureError as exc:
                if engine == "playwright":
                    raise KeylolPageError(str(exc)) from exc
                logger.warning(f"其乐网页截图不可用，已回退兼容模式：{exc}")
        rendered = await html_renderer(target_url, cookie)
        if multiple:
            return list(rendered)
        return [str(rendered)]

    async def _render_screenshots(self, target_url: str, cookie: str) -> list[str]:
        """Render one or more Keylol images, splitting configured TOC entries."""

        logger.info(
            f"其乐截图开始：engine={self._keylol_render_engine()}, "
            f"split_toc={self._split_keylol_toc_sections()}, "
            f"max_toc={self._max_keylol_toc_sections()}, "
            f"has_cookie={bool(cookie)}。"
        )
        async with self._render_slots:
            return await self._render_screenshots_unlocked(
                target_url, cookie, multiple=True
            )

    def _cleanup_capture_paths(self, image_paths: list[str]) -> None:
        for path in image_paths:
            if path not in self._owned_capture_paths:
                continue
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                # Retry on unload without logging paths or thread content.
                continue
            self._owned_capture_paths.discard(path)

    def _image_chain(self, image_paths: list[str]) -> list[object]:
        """Validate every image before exposing one complete message chain.

        Bytes-backed components outlive the handler without temporary JPEGs.
        If any directory image fails validation, none of this chain is sent.
        """
        if not image_paths:
            raise ValueError("截图没有生成有效图片。")
        try:
            chain = []
            for path in image_paths:
                source_renderer = (
                    "playwright" if path in self._owned_capture_paths else "html"
                )
                with Image.open(path) as source_image:
                    source_width, source_height = source_image.size
                encoded = _normalize_for_qq(Path(path).read_bytes())
                _validate_qq_image(encoded)
                with Image.open(BytesIO(encoded)) as final_image:
                    final_width, final_height = final_image.size
                logger.info(
                    "截图发送前："
                    f"source_renderer={source_renderer}, "
                    f"source_width={source_width}, source_height={source_height}, "
                    f"final_width={final_width}, final_height={final_height}, "
                    f"safety_resize={(source_width, source_height) != (final_width, final_height)}。"
                )
                chain.append(Comp.Image.fromBytes(encoded))
            return chain
        finally:
            self._cleanup_capture_paths(image_paths)

    async def _prepare_image_chain(self, image_paths: list[str]) -> list[object]:
        # JPEG searches can be CPU-heavy; share the existing concurrency bound
        # without blocking AstrBot's event loop during encoding.
        started = False
        try:
            async with self._render_slots:
                worker = asyncio.create_task(asyncio.to_thread(self._image_chain, image_paths))
                self._encoding_tasks.add(worker)
                worker.add_done_callback(self._encoding_tasks.discard)
                started = True
                try:
                    return await asyncio.shield(worker)
                except asyncio.CancelledError:
                    # Let the worker release its images/files before propagating.
                    try:
                        await worker
                    except Exception:
                        pass
                    raise
        finally:
            if not started:
                self._cleanup_capture_paths(image_paths)

    @staticmethod
    def _screenshot_result(event: AstrMessageEvent, chain: list[object]):
        """Quote the triggering message once, keeping all screenshots together."""
        message_id = getattr(getattr(event, "message_obj", None), "message_id", None)
        if (
            isinstance(message_id, (str, int))
            and str(message_id).strip()
            and any(isinstance(component, Comp.Image) for component in chain)
        ):
            chain = [Comp.Reply(id=str(message_id).strip()), *chain]
        return event.chain_result(chain)

    async def _render_screenshot(self, target_url: str, cookie: str) -> str:
        # Keep the historical private helper for callers/tests that expect a
        # single path; share engine/fallback logic without nesting semaphores.
        async with self._render_slots:
            paths = await self._render_screenshots_unlocked(
                target_url, cookie, multiple=False
            )
        if not paths or not paths[0].strip():
            raise KeylolPageError("截图没有生成有效图片。")
        return paths[0]

    def _tieba_render_engine(self) -> str:
        value = str(self.config.get("tieba_render_engine", "html")).strip().lower()
        # Legacy auto configurations now use the API directly, without first
        # visiting the real thread page and triggering Baidu verification.
        return "playwright" if value == "playwright" else "html"

    async def _render_tieba_html_screenshot(
        self, target_url: str, cookie: str
    ) -> str:
        article = await fetch_tieba_article(
            target_url,
            cookie=cookie,
            request_timeout_seconds=int(
                self.config.get("request_timeout_seconds", 25)
            ),
            inline_images=bool(self.config.get("inline_tieba_images", True)),
        )
        viewport_width, viewport_height = self._mobile_render_size()
        document = build_render_html(article, content_width=viewport_width)
        image_path = await self.html_render(
            document,
            {},
            return_url=False,
            options=self._tieba_html_screenshot_options(
                viewport_width, viewport_height
            ),
        )
        if bool(self.config.get("adaptive_height", True)):
            try:
                raw_width, _ = self._png_dimensions(image_path)
                # Cropping accepts physical pixels and never changes width.
                renderer_dpr = raw_width / viewport_width if raw_width else 1.8
                await asyncio.to_thread(
                    trim_rendered_screenshot,
                    image_path,
                    bottom_padding=round(MOBILE_PAGE_PADDING * renderer_dpr),
                )
            except Exception:
                logger.warning(
                    "贴吧截图自适应裁剪失败，将返回未经裁剪的完整截图。",
                    exc_info=True,
                )
        return image_path

    @staticmethod
    def _diagnostic_token(value: object, default: str) -> str:
        token = str(value or "").strip().lower()
        return token if re.fullmatch(r"[a-z0-9_-]{1,64}", token) else default

    @classmethod
    def _tieba_browser_failure_details(
        cls, error: TiebaBrowserCaptureError, cookie: str
    ) -> str:
        stage = cls._diagnostic_token(getattr(error, "stage", ""), "unknown")
        reason = cls._diagnostic_token(
            getattr(error, "reason", ""), "capture_failed"
        )
        cookie_present = getattr(error, "cookie_present", None)
        if cookie_present is None:
            cookie_present = bool(str(cookie or "").strip())
        segment_index = getattr(error, "segment_index", None)
        details = [
            f"stage={stage}",
            f"reason={reason}",
            f"cookie_present={bool(cookie_present)}",
            f"bduss_found={bool(getattr(error, 'bduss_found', False))}",
            f"stoken_found={bool(getattr(error, 'stoken_found', False))}",
        ]
        if isinstance(segment_index, int) and not isinstance(segment_index, bool):
            details.append(f"segment_index={segment_index}")
        if getattr(error, "diagnostics", None):
            details.append("page_diagnostics=" + json.dumps(error.diagnostics, ensure_ascii=False))
        return ", ".join(details)

    @staticmethod
    def _normalized_tieba_log_url(result: object, target_url: str) -> str:
        raw_url = str(getattr(result, "source_url", "") or target_url)
        try:
            return normalize_tieba_browser_url(raw_url)
        except Exception:
            return ""

    @staticmethod
    def _png_dimensions(path: str) -> tuple[int, int]:
        try:
            with Image.open(path) as image:
                return image.size
        except Exception:
            return 0, 0

    async def _render_tieba_browser_screenshot(
        self, target_url: str, cookie: str
    ) -> str:
        viewport_width, viewport_height = self._mobile_render_size()
        timeout_ms = self._browser_capture_timeout_ms()
        try:
            result = await asyncio.wait_for(
                capture_tieba_webpage_screenshot(
                    target_url,
                    cookie=cookie,
                    viewport_width=viewport_width,
                    viewport_height=viewport_height,
                    timeout_ms=timeout_ms,
                    proxy_url=str(self.config.get("proxy_url", "")),
                ),
                timeout=timeout_ms / 1000 + 10,
            )
        except asyncio.TimeoutError as exc:
            raise TiebaBrowserTimeoutError(
                "网页截图超过总等待时间。",
                stage="capture",
                reason="timeout",
                cookie_present=bool(str(cookie or "").strip()),
            ) from exc

        if result.status is TiebaBrowserCaptureStatus.PARTIAL:
            logger.warning(
                f"贴吧网页截图有 {result.failed_image_count} 张图片未能显示；"
                "截图中已插入失败提示。"
            )
        image_path = str(getattr(result, "image_path", "")).strip()
        if not image_path:
            raise TiebaBrowserCaptureError(
                "网页截图没有生成有效图片。",
                stage="capture",
                reason="missing_image",
                cookie_present=bool(str(cookie or "").strip()),
            )
        self._owned_capture_paths.add(image_path)
        raw_width = int(getattr(result, "raw_png_width", 0) or 0)
        raw_height = int(getattr(result, "raw_png_height", 0) or 0)
        if raw_width <= 0 or raw_height <= 0:
            raw_width, raw_height = self._png_dimensions(image_path)
        viewport_css_width = int(
            getattr(result, "viewport_css_width", 0) or viewport_width
        )
        device_pixel_ratio = int(
            getattr(result, "device_pixel_ratio", 0) or DEVICE_SCALE_FACTOR
        )
        result_cookie_present = getattr(result, "cookie_present", None)
        if result_cookie_present is None:
            result_cookie_present = bool(str(cookie or "").strip())
        logger.info(
            "贴吧 Playwright 截图完成："
            "engine=playwright, "
            f"final_url={self._normalized_tieba_log_url(result, target_url)}, "
            f"cookie_present={bool(result_cookie_present)}, "
            f"bduss_found={bool(getattr(result, 'bduss_found', False))}, "
            f"stoken_found={bool(getattr(result, 'stoken_found', False))}, "
            f"viewport_css_width={viewport_css_width}, "
            f"device_pixel_ratio={device_pixel_ratio}, "
            f"raw_png_width={raw_width}, raw_png_height={raw_height}。"
        )
        return image_path

    async def _render_tieba_screenshot(self, target_url: str, cookie: str) -> str:
        engine = self._tieba_render_engine()
        logger.info(
            "贴吧截图开始："
            f"engine={engine}, cookie_present={bool(str(cookie or '').strip())}。"
        )
        async with self._render_slots:
            if engine == "playwright":
                try:
                    return await self._render_tieba_browser_screenshot(
                        target_url, cookie
                    )
                except TiebaBrowserCaptureError as exc:
                    details = self._tieba_browser_failure_details(exc, cookie)
                    logger.warning(
                        "贴吧 Playwright 截图失败："
                        f"{details}；engine=playwright，不回退。"
                    )
                    raise TiebaPageError(str(exc)) from exc
            rendered = await self._render_tieba_html_screenshot(target_url, cookie)
            viewport_width, _ = self._mobile_render_size()
            raw_width, raw_height = self._png_dimensions(rendered)
            renderer_dpr = f"{raw_width / viewport_width:.3f}" if raw_width else "unknown"
            logger.info(
                "贴吧截图完成：engine=html, source_renderer=tieba_api_html, "
                f"cookie_present={bool(str(cookie or '').strip())}, "
                f"render_scale=1.8, renderer_dpr={renderer_dpr}, "
                f"viewport_css_width={viewport_width}, "
                f"raw_png_width={raw_width}, raw_png_height={raw_height}。"
            )
            return rendered

    @staticmethod
    def _message_payloads(event: AstrMessageEvent) -> list[object]:
        """Return rich message payloads without logging their potentially secret data."""
        message_obj = getattr(event, "message_obj", None)
        payloads: list[object] = []
        for component in list(getattr(message_obj, "message", None) or []):
            data = getattr(component, "data", None)
            if data is not None:
                payloads.append(data)
        raw_message = getattr(message_obj, "raw_message", None)
        if raw_message is not None:
            payloads.append(raw_message)
        return payloads

    def _is_recent_group_link(self, group_id: str, url: str) -> bool:
        ttl = max(0, min(3600, int(self.config.get("dedupe_seconds", 60))))
        if ttl == 0:
            return False

        now = time.monotonic()
        expired = [
            key for key, timestamp in self._recent_links.items() if now - timestamp >= ttl
        ]
        for key in expired:
            self._recent_links.pop(key, None)

        key = (group_id, url.lower())
        if key in self._recent_links:
            return True
        self._recent_links[key] = now
        return False

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def detect_keylol_link(self, event: AstrMessageEvent):
        """检测群消息中的其乐或贴吧链接并自动返回 1 楼截图。"""
        if not bool(self.config.get("auto_detect_enabled", True)):
            return

        message = event.message_str or ""
        if re.match(
            r"^\s*[/!！#]?(?:keylol|tieba)(?:_check)?(?:\s|$)",
            message,
            re.I,
        ):
            return
        keylol_urls = extract_keylol_thread_urls(message)
        tieba_urls = extract_tieba_thread_urls(
            message,
            *self._message_payloads(event),
        )
        if not keylol_urls and not tieba_urls:
            return

        message_obj = getattr(event, "message_obj", None)
        group_id = str(getattr(message_obj, "group_id", ""))
        limit = max(1, min(3, int(self.config.get("max_links_per_message", 1))))
        candidates = [("keylol", url) for url in keylol_urls]
        candidates.extend(("tieba", url) for url in tieba_urls)
        targets = [
            (site, url)
            for site, url in candidates[:limit]
            if not self._is_recent_group_link(group_id, url)
        ]
        if not targets:
            return

        event.stop_event()
        result_chain: list[object] = []
        for site, target_url in targets:
            if site == "keylol":
                cookie = self._cookie()
                try:
                    image_paths = await self._render_screenshots(target_url, cookie)
                    result_chain.extend(await self._prepare_image_chain(image_paths))
                    logger.info(
                        f"其乐自动回复：已把 {len(image_paths)} 张截图加入同一消息链。"
                    )
                except KeylolPageError as exc:
                    result_chain.append(Comp.Plain(f"其乐截图失败：{exc}"))
                except Exception as exc:
                    logger.exception("自动处理 Keylol 链接失败")
                    result_chain.append(
                        Comp.Plain(
                            f"其乐截图失败：{type(exc).__name__}。请查看 AstrBot 日志。"
                        )
                    )
            else:
                cookie = self._tieba_cookie()
                if not cookie:
                    result_chain.append(
                        Comp.Plain("检测到百度贴吧帖子链接，但尚未配置 tieba_cookie。")
                    )
                    continue
                try:
                    image_url = await self._render_tieba_screenshot(target_url, cookie)
                    result_chain.extend(await self._prepare_image_chain([image_url]))
                except TiebaPageError as exc:
                    result_chain.append(Comp.Plain(f"贴吧截图失败：{exc}"))
                except Exception as exc:
                    logger.exception("自动处理百度贴吧链接失败")
                    result_chain.append(
                        Comp.Plain(
                            f"贴吧截图失败：{type(exc).__name__}。请查看 AstrBot 日志。"
                        )
                    )
        if result_chain:
            yield self._screenshot_result(event, result_chain)

    @filter.command("keylol")
    async def keylol(self, event: AstrMessageEvent, url: str = ""):
        """截取其乐帖子的 1 楼正文。目录帖子会按目录逐张返回。用法：/keylol [帖子链接]"""
        target_url = url.strip() or str(
            self.config.get("default_url", DEFAULT_URL)
        ).strip()
        if not target_url:
            yield event.plain_result("用法：/keylol https://keylol.com/t1046223-1-1")
            return
        cookie = self._cookie()
        try:
            image_paths = await self._render_screenshots(target_url, cookie)
            logger.info(
                f"其乐命令回复：已把 {len(image_paths)} 张截图加入同一消息链。"
            )
            yield self._screenshot_result(event, await self._prepare_image_chain(image_paths))
        except KeylolPageError as exc:
            yield event.plain_result(f"截图失败：{exc}")
        except Exception as exc:
            logger.exception("Keylol 主楼截图失败")
            yield event.plain_result(f"截图失败：{type(exc).__name__}。请查看 AstrBot 日志。")

    @filter.command("keylol_check")
    async def keylol_check(self, event: AstrMessageEvent):
        """仅允许 AstrBot 管理员在私聊中验证其乐 Cookie。"""
        if not event.is_admin() or not event.is_private_chat():
            yield event.plain_result("Cookie 验证仅限 AstrBot 管理员在私聊中执行。")
            return
        cookie = self._cookie()
        if not cookie:
            yield event.plain_result("尚未配置 keylol_cookie。")
            return

        try:
            article = await fetch_article(
                str(self.config.get("default_url", DEFAULT_URL)),
                cookie=cookie,
                proxy_url=str(self.config.get("proxy_url", "")),
                request_timeout_seconds=int(
                    self.config.get("request_timeout_seconds", 25)
                ),
                inline_keylol_images=False,
                require_authentication=True,
            )
            if article.has_locked_resources:
                yield event.plain_result("Cookie 已登录，但 1 楼仍有账号权限不足的资源。")
            else:
                yield event.plain_result(f"Cookie 有效，已登录并读取到 1 楼：{article.title}")
        except KeylolPageError as exc:
            yield event.plain_result(f"Cookie 验证失败：{exc}")
        except Exception:
            logger.exception("Keylol Cookie 验证失败")
            yield event.plain_result("Cookie 验证失败，请查看 AstrBot 日志。")

    @filter.command("tieba")
    async def tieba(self, event: AstrMessageEvent, url: str = ""):
        """截取百度贴吧帖子的主楼。用法：/tieba [帖子链接]"""
        target_url = url.strip() or str(
            self.config.get("tieba_default_url", "")
        ).strip()
        if not target_url:
            yield event.plain_result("用法：/tieba https://tieba.baidu.com/p/帖子ID")
            return
        cookie = self._tieba_cookie()
        if not cookie:
            yield event.plain_result("请先在插件配置中填写 tieba_cookie，再执行截图。")
            return

        try:
            image_url = await self._render_tieba_screenshot(target_url, cookie)
            yield self._screenshot_result(event, await self._prepare_image_chain([image_url]))
        except TiebaPageError as exc:
            yield event.plain_result(f"截图失败：{exc}")
        except Exception as exc:
            logger.exception("百度贴吧主楼截图失败")
            yield event.plain_result(f"截图失败：{type(exc).__name__}。请查看 AstrBot 日志。")

    @filter.command("tieba_check")
    async def tieba_check(self, event: AstrMessageEvent):
        """仅允许 AstrBot 管理员在私聊中验证百度贴吧 Cookie。"""
        if not event.is_admin() or not event.is_private_chat():
            yield event.plain_result("Cookie 验证仅限 AstrBot 管理员在私聊中执行。")
            return
        cookie = self._tieba_cookie()
        if not cookie:
            yield event.plain_result("尚未配置 tieba_cookie。")
            return

        try:
            account_name = await check_tieba_cookie(
                cookie,
                request_timeout_seconds=int(
                    self.config.get("request_timeout_seconds", 25)
                ),
            )
            yield event.plain_result(f"贴吧 Cookie 有效，当前账号：{account_name}")
        except TiebaPageError as exc:
            yield event.plain_result(f"贴吧 Cookie 验证失败：{exc}")
        except Exception:
            logger.exception("百度贴吧 Cookie 验证失败")
            yield event.plain_result("贴吧 Cookie 验证失败，请查看 AstrBot 日志。")

    async def terminate(self):
        """Release browser PNGs left by interrupted or legacy single captures."""
        if self._encoding_tasks:
            await asyncio.gather(*tuple(self._encoding_tasks), return_exceptions=True)
        self._cleanup_capture_paths(list(self._owned_capture_paths))
