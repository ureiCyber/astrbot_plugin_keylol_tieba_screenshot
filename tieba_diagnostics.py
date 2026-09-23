"""Safe diagnostics and failure-page classification for Tieba captures.

The helpers in this module are intentionally independent of Playwright's
types, so the classification and redaction rules can be tested without a
browser.  Artifact capture is bounded and must only be called on a failed
native-renderer path.
"""

from __future__ import annotations

import asyncio
import html
import re
import uuid
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, quote, quote_plus, urlencode, urlsplit, urlunsplit


_SHORT_HTML_LIMIT = 18_000
_SHORT_BODY_LIMIT = 2_500
_MAX_TITLE_LENGTH = 180
_MAX_SELECTOR_LABEL_LENGTH = 160
_MAX_SELECTOR_COUNTS = 32
_ARTIFACT_TIMEOUT_SECONDS = 5.0
_CONTENT_TIMEOUT_SECONDS = 2.35
_SCREENSHOT_TIMEOUT_MS = 2_350
_SCREENSHOT_TIMEOUT_SECONDS = _SCREENSHOT_TIMEOUT_MS / 1_000

_COOKIE_ASSIGNMENT_RE = re.compile(
    r"(?i)(\b(?:BDUSS|STOKEN)\b\s*[\"']?\s*[:=]\s*[\"']?)([^\"'<>\s;,&#]+)"
)
_COOKIE_ATTRIBUTE_RE = re.compile(
    r"(?i)(\b(?:BDUSS|STOKEN)\b\s*=\s*)([^\"'<>\s;,&#]+)"
)
_RISK_RE = re.compile(r"风控|异常访问|异常请求|访问过于频繁|操作频繁|安全风险|请求异常")
_VERIFY_RE = re.compile(r"安全验证|验证码|请完成验证|验证后继续|安全校验|人机验证")
_LOGIN_RE = re.compile(r"登录|登陆|请先登录|登录后")
_APP_RE = re.compile(r"(?:打开|下载)(?:(?:百度)?贴吧)?客户端|(?:打开|下载)\s*APP", re.IGNORECASE)
_DIAGNOSTIC_VERIFY_RE = re.compile(r"验证")
_DIAGNOSTIC_APP_RE = re.compile(r"打开.{0,4}客户端|APP", re.IGNORECASE)
_NOT_FOUND_RE = re.compile(r"帖子不存在|贴子不存在|帖子已删除|贴子已删除|内容已被删除|帖子被删除|404 Not Found")
_PERMISSION_RE = re.compile(r"无权查看|没有权限|权限不足|仅吧主可见|仅楼主可见|访问受限|禁止访问")


def _credential_values(pairs: object) -> list[str]:
    """Return non-empty credential values from common pair/container shapes."""

    if pairs is None:
        return []
    if isinstance(pairs, Mapping):
        entries = pairs.items()
    elif isinstance(pairs, (str, bytes)):
        entries = []
        raw = pairs.decode("utf-8", "ignore") if isinstance(pairs, bytes) else pairs
        for piece in raw.split(";"):
            if "=" in piece:
                entries.append(tuple(piece.split("=", 1)))
    else:
        try:
            entries = iter(pairs)  # type: ignore[arg-type]
        except TypeError:
            entries = []

    secrets: list[str] = []
    for entry in entries:
        try:
            _name, value = entry
        except (TypeError, ValueError):
            continue
        if value is None:
            continue
        secret = str(value)
        if secret:
            secrets.append(secret)
    return sorted(set(secrets), key=len, reverse=True)


def _redact_text(value: object, pairs: object = ()) -> str:
    text = str(value or "")
    for secret in _credential_values(pairs):
        variants = {
            secret,
            html.escape(secret, quote=True),
            html.escape(secret, quote=False),
            quote(secret, safe=""),
            quote_plus(secret, safe=""),
        }
        for variant in sorted(variants, key=len, reverse=True):
            if variant:
                text = text.replace(variant, "[REDACTED]")
    text = _COOKIE_ASSIGNMENT_RE.sub(r"\1[REDACTED]", text)
    text = _COOKIE_ATTRIBUTE_RE.sub(r"\1[REDACTED]", text)
    return text


def _safe_url(value: object, pairs: object = ()) -> str:
    """Keep a URL's host, path, and query names while dropping all values."""

    raw = str(value or "")
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        if not hostname:
            return ""
        host = hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        try:
            port = parsed.port
        except ValueError:
            port = None
        netloc = host + (f":{port}" if port is not None else "")
        path = _redact_text(parsed.path, pairs)
        keys = []
        for key, _value in parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=80):
            safe_key = _redact_text(key, pairs)[:80]
            keys.append((safe_key, "[REDACTED]"))
        query = urlencode(keys, doseq=True)
        return urlunsplit((parsed.scheme[:12], netloc[:255], path[:512], query[:1024], ""))
    except (ValueError, UnicodeError):
        return ""


def _bool(snapshot: Mapping[str, object], key: str) -> bool:
    return bool(snapshot.get(key, False))


def _number(snapshot: Mapping[str, object], key: str, default: int = 0) -> int:
    try:
        return max(0, int(snapshot.get(key, default) or 0))
    except (TypeError, ValueError, OverflowError):
        return default


def _page_text(snapshot: Mapping[str, object]) -> tuple[str, str]:
    body = html.unescape(str(snapshot.get("body_text") or ""))
    title = html.unescape(str(snapshot.get("title") or ""))
    return title, body


def _short_page(snapshot: Mapping[str, object]) -> bool:
    return (
        _number(snapshot, "html_length") <= _SHORT_HTML_LIMIT
        and _number(snapshot, "body_text_length") <= _SHORT_BODY_LIMIT
    )


def _known_blocked_url_reason(value: object) -> str:
    """Classify only known redirect hosts and route paths; ignore query text."""

    try:
        parsed = urlsplit(str(value or ""))
        host = (parsed.hostname or "").casefold().rstrip(".")
        path = (parsed.path or "/").casefold()
    except ValueError:
        return ""

    auth_hosts = {"passport.baidu.com", "wappass.baidu.com"}
    tieba_hosts = {"tieba.baidu.com", "www.tieba.baidu.com"}
    if host in auth_hosts | tieba_hosts and re.search(
        r"(?:^|/)(?:captcha|verify|verification|security-check)(?:/|$)", path
    ):
        return "tieba_verify_required"
    if host in tieba_hosts and re.search(r"(?:^|/)(?:risk|riskcontrol|risk-control)(?:/|$)", path):
        return "tieba_risk_control"
    if host in auth_hosts and re.fullmatch(
        r"/(?:|v[0-9]+/?|passport(?:/.*)?|account(?:/.*)?|login(?:/.*)?)", path
    ):
        return "tieba_login_required"
    if host in tieba_hosts and re.match(r"^/(?:app/redirect(?:/|$)|download/app(?:/|$))", path):
        return "tieba_app_redirect"
    return ""


def classify_tieba_failure(
    snapshot: dict,
    *,
    status: int | None = None,
    blocked_document_url: str = "",
    wait_timed_out: bool = False,
) -> str:
    """Classify a failed first-post lookup using bounded page evidence.

    Text keywords are only treated as page-level evidence when the document is
    short and lacks Tieba post structure.  Structural flags and HTTP status
    take priority so ordinary post text or navigation links do not misclassify
    a page.
    """

    data: Mapping[str, object] = snapshot if isinstance(snapshot, Mapping) else {}
    if _bool(data, "has_first_post"):
        return ""

    blocked_reason = _known_blocked_url_reason(blocked_document_url)
    if blocked_reason:
        return blocked_reason

    title, body = _page_text(data)
    url = str(data.get("url") or "").casefold()
    has_posts = _bool(data, "has_post_features")
    short = _short_page(data)
    title_text = title.casefold()
    page_text = (title + "\n" + body).casefold()

    if _bool(data, "has_verify_widget"):
        if _RISK_RE.search(page_text):
            return "tieba_risk_control"
        return "tieba_verify_required"
    if _bool(data, "has_login_form"):
        return "tieba_login_required"
    if _bool(data, "has_app_gate") and not has_posts:
        return "tieba_app_redirect"

    if status == 403 and short and _VERIFY_RE.search(title_text):
        return "tieba_verify_required"

    if status in {404, 410}:
        return "tieba_post_not_found"
    if status == 401:
        return "tieba_login_required"
    if status == 429:
        return "tieba_risk_control"
    if status == 403:
        if _RISK_RE.search(page_text) and short:
            return "tieba_risk_control"
        return "tieba_permission_denied"
    if isinstance(status, int) and status >= 500:
        return "tieba_http_error"

    # Weak text signals require a short document and title evidence.  A
    # keyword in a post body, menu, or download promotion alone is ignored.
    title_verify = bool(_VERIFY_RE.search(title_text))
    title_risk = bool(_RISK_RE.search(title_text))
    title_login = bool(_LOGIN_RE.search(title_text))
    title_app = bool(_APP_RE.search(title_text))
    title_missing = bool(_NOT_FOUND_RE.search(title_text))
    title_permission = bool(_PERMISSION_RE.search(title_text))
    if not has_posts and short:
        if title_risk or (title_verify and _RISK_RE.search(page_text)):
            return "tieba_risk_control"
        if title_verify or (_VERIFY_RE.search(body) and "百度" in title):
            return "tieba_verify_required"
        if title_login or _known_blocked_url_reason(url) == "tieba_login_required":
            return "tieba_login_required"
        if title_app or (_APP_RE.search(body) and re.search(r"(?:请|需要|必须).{0,12}(?:客户端|APP).{0,12}(?:查看|继续|阅读)", body, re.IGNORECASE)):
            return "tieba_app_redirect"
        if title_missing or _NOT_FOUND_RE.search(body):
            return "tieba_post_not_found"
        if title_permission or _PERMISSION_RE.search(body):
            return "tieba_permission_denied"
        if _RISK_RE.search(page_text) and ("安全" in title or "百度" in title):
            return "tieba_risk_control"

    ready_state = str(data.get("ready_state") or "").strip().casefold()
    if ready_state and ready_state not in {"interactive", "complete"}:
        return "tieba_page_not_loaded"
    body_length = _number(data, "body_text_length")
    visibly_empty = (
        body_length == 0
        and not body.strip()
        and not has_posts
        and not _bool(data, "first_floor_seen")
        and not _bool(data, "has_image_features")
        and _number(data, "image_count") == 0
        and _number(data, "iframe_count") == 0
        and not _bool(data, "has_loading_indicator")
    )
    if visibly_empty:
        return "tieba_blank_page" if ready_state == "complete" else "tieba_page_not_loaded"

    if wait_timed_out and (
        _bool(data, "has_loading_indicator") or _bool(data, "first_floor_seen")
    ):
        return "tieba_first_post_timeout"
    return "tieba_dom_changed"


def sanitize_diagnostics(snapshot: dict, pairs: object) -> dict:
    """Return metadata safe for logs; page body text is never returned."""

    data: Mapping[str, object] = snapshot if isinstance(snapshot, Mapping) else {}
    title, body = _page_text(data)
    combined = title + "\n" + body
    selector_counts: dict[str, int] = {}
    raw_counts = data.get("selector_counts")
    if isinstance(raw_counts, Mapping):
        for index, (selector, count) in enumerate(raw_counts.items()):
            if index >= _MAX_SELECTOR_COUNTS:
                break
            label = _redact_text(selector, pairs).replace("\r", " ").replace("\n", " ")
            try:
                count_value = max(0, int(count))
            except (TypeError, ValueError, OverflowError):
                count_value = 0
            selector_counts[label[:_MAX_SELECTOR_LABEL_LENGTH]] = count_value

    safe_title = _redact_text(title, pairs)
    safe_title = re.sub(r"[\x00-\x1f\x7f]", " ", safe_title).strip()
    return {
        "url": _safe_url(data.get("url"), pairs),
        "title": safe_title[:_MAX_TITLE_LENGTH],
        "ready_state": str(data.get("ready_state") or "")[:24],
        "html_length": _number(data, "html_length"),
        "body_text_length": _number(data, "body_text_length"),
        "has_first_post": _bool(data, "has_first_post"),
        "first_floor_seen": _bool(data, "first_floor_seen"),
        "has_post_features": _bool(data, "has_post_features"),
        "has_loading_indicator": _bool(data, "has_loading_indicator"),
        "has_login_form": _bool(data, "has_login_form"),
        "has_verify_widget": _bool(data, "has_verify_widget"),
        "has_app_gate": _bool(data, "has_app_gate"),
        "selected_selector": _redact_text(data.get("selected_selector", ""), pairs)[:_MAX_SELECTOR_LABEL_LENGTH],
        "iframe_count": _number(data, "iframe_count"),
        "contains_login": bool(_LOGIN_RE.search(combined)),
        "contains_verify": bool(_DIAGNOSTIC_VERIFY_RE.search(combined)),
        "contains_risk_control": bool(_RISK_RE.search(combined)),
        "contains_app": bool(_DIAGNOSTIC_APP_RE.search(combined)),
        "selector_counts": selector_counts,
    }


def _redact_html(content: str, pairs: object) -> str:
    # Keep markup intact while covering literal, HTML-escaped, and URL-encoded
    # cookie values plus named BDUSS/STOKEN assignments.
    clean = _redact_text(content, pairs)
    return clean


async def _await_with_timeout(awaitable, timeout: float):
    return await asyncio.wait_for(awaitable, timeout=timeout)


async def save_failure_artifacts(page, *, pairs: object, directory: str | Path) -> dict:
    """Save redacted final HTML and a viewport screenshot after native failure.

    This function is best-effort and bounded to about five seconds.  It never
    reads ``document.cookie`` or browser storage, and errors are represented
    only by safe status fields so callers can preserve the original failure.
    """

    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S_%f")
    token = uuid.uuid4().hex[:12]
    base = f"tieba_failure_{stamp}_{token}"
    result: dict[str, object] = {
        "status": "partial",
        "html_path": "",
        "screenshot_path": "",
        "html_saved": False,
        "screenshot_saved": False,
    }
    try:
        target = Path(directory)
        await asyncio.to_thread(target.mkdir, parents=True, exist_ok=True)
    except Exception:
        return result

    async def save() -> None:
        html_path = target / f"{base}.html"
        screenshot_path = target / f"{base}.png"

        try:
            content = await _await_with_timeout(page.content(), _CONTENT_TIMEOUT_SECONDS)
            safe_content = _redact_html(str(content), pairs)
            await _await_with_timeout(
                asyncio.to_thread(html_path.write_text, safe_content, encoding="utf-8"),
                _CONTENT_TIMEOUT_SECONDS,
            )
            result["html_path"] = str(html_path)
            result["html_saved"] = True
        except Exception:
            try:
                html_path.unlink(missing_ok=True)
            except OSError:
                pass

        try:
            await _await_with_timeout(
                page.screenshot(
                    path=str(screenshot_path),
                    full_page=False,
                    timeout=_SCREENSHOT_TIMEOUT_MS,
                ),
                _SCREENSHOT_TIMEOUT_SECONDS,
            )
            result["screenshot_path"] = str(screenshot_path)
            result["screenshot_saved"] = True
        except Exception:
            try:
                screenshot_path.unlink(missing_ok=True)
            except OSError:
                pass

    try:
        await asyncio.wait_for(save(), timeout=_ARTIFACT_TIMEOUT_SECONDS)
    except Exception:
        # Artifact capture must never replace the original renderer failure.
        pass

    if result["html_saved"] and result["screenshot_saved"]:
        result["status"] = "ok"
    return result


__all__ = [
    "classify_tieba_failure",
    "sanitize_diagnostics",
    "save_failure_artifacts",
]
