"""Safe, server-rendered static cards for approved Keylol embeds.

The only live provider handled here is the Steam Store widget endpoint. The
browser receives generated markup and a validated, inlined image; it never
loads the provider page or executes provider JavaScript.
"""

from __future__ import annotations

import asyncio
import base64
import html
import inspect
import re
from dataclasses import dataclass
from typing import Callable, Mapping
from urllib.parse import parse_qsl, urlsplit

from bs4 import BeautifulSoup, Tag


STEAM_STORE_HOST = "store.steampowered.com"
STEAM_WIDGET_TIMEOUT_SECONDS = 10
STEAM_WIDGET_MAX_HTML_BYTES = 512 * 1024
STEAM_WIDGET_MAX_IMAGE_BYTES = 2 * 1024 * 1024

# These hosts serve Steam artwork. Paths are separately restricted to the
# exact app header image, so the allowlist does not grant general CDN access.
STEAM_CDN_HOSTS = frozenset(
    {
        "cdn.akamai.steamstatic.com",
        "cdn.cloudflare.steamstatic.com",
        "shared.akamai.steamstatic.com",
        "shared.cloudflare.steamstatic.com",
        "shared.fastly.steamstatic.com",
        "steamcdn-a.akamaihd.net",
    }
)
_WIDGET_PATH_RE = re.compile(r"^/widget/([0-9]{1,15})/$")
_STEAM_IMAGE_PATH_RE = re.compile(
    r"^/(?:store_item_assets/)?steam/apps/([0-9]{1,15})/header\.jpg$",
    re.IGNORECASE,
)
_CACHE_QUERY_RE = re.compile(r"^t=[0-9]{1,20}$")
_DISCOUNT_RE = re.compile(r"(?:-|−)\s*\d{1,3}\s*%")


@dataclass(frozen=True)
class EmbedRenderResult:
    """Sanitized HTML and loading state for one embedded resource."""

    html: str
    loaded: bool
    fallback: bool
    provider: str | None


def normalize_steam_widget_url(value: object) -> str | None:
    """Return a canonical Steam widget URL, or None for every other URL.

    The optional utm_source=keylol marker is accepted because older forum
    posts can append it. It is discarded from the canonical URL and never
    sent to Steam.
    """

    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or len(raw) > 2048:
        return None
    try:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").lower()
        # Comparing the complete authority rejects explicit :443, credentials,
        # trailing-dot aliases, and authority tricks.
        if (
            parsed.scheme.lower() != "https"
            or host != STEAM_STORE_HOST
            or parsed.netloc.lower() != STEAM_STORE_HOST
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.fragment
        ):
            return None
        match = _WIDGET_PATH_RE.fullmatch(parsed.path)
        if match is None:
            return None
        if parsed.query and parsed.query != "utm_source=keylol":
            return None
        # Guard against alternate encodings or duplicate parameters.
        if parsed.query and parse_qsl(parsed.query, keep_blank_values=True) != [
            ("utm_source", "keylol")
        ]:
            return None
    except (ValueError, UnicodeError):
        return None
    return f"https://{STEAM_STORE_HOST}/widget/{match.group(1)}/"


def _steam_app_id(canonical_url: str) -> str:
    match = _WIDGET_PATH_RE.fullmatch(urlsplit(canonical_url).path)
    return match.group(1) if match else ""


def _steam_widget_redirect_allowed(url: str, canonical_url: str) -> bool:
    """Enforce the exact widget origin and app path before every redirect."""

    return normalize_steam_widget_url(url) == canonical_url


def _steam_image_redirect_allowed(url: str, app_id: str) -> bool:
    """Allow only one app's header image on an explicit Steam CDN host."""

    if not isinstance(url, str) or len(url) > 2048:
        return False
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme.lower() != "https"
            or host not in STEAM_CDN_HOSTS
            or parsed.netloc.lower() != host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or parsed.fragment
        ):
            return False
        path_match = _STEAM_IMAGE_PATH_RE.fullmatch(parsed.path)
        if path_match is None or path_match.group(1) != app_id:
            return False
        if parsed.query and not _CACHE_QUERY_RE.fullmatch(parsed.query):
            return False
    except (ValueError, UnicodeError):
        return False
    return True


def _mapping_or_attr(value: object, *names: str) -> object | None:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
        return None
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return None


def _media_result_data(result: object) -> tuple[bytes, str, str | None] | None:
    """Read the small common result surface exposed by safe_media."""

    if result is None:
        return None
    raw_data = _mapping_or_attr(result, "data", "body", "content")
    if not isinstance(raw_data, (bytes, bytearray, memoryview)):
        return None
    raw_type = _mapping_or_attr(result, "content_type", "mime_type")
    if not isinstance(raw_type, str):
        headers = _mapping_or_attr(result, "headers")
        raw_type = _mapping_or_attr(headers, "Content-Type", "content-type")
    if not isinstance(raw_type, str):
        return None
    final_url = _mapping_or_attr(result, "final_url", "url")
    return bytes(raw_data), raw_type.split(";", 1)[0].strip().lower(), (
        final_url if isinstance(final_url, str) else None
    )


async def _call_fetch(
    downloader: object,
    method_name: str,
    url: str,
    *,
    allowed_url: Callable[[str], bool],
    max_bytes: int,
) -> object | None:
    """Call safe_media while keeping provider redirects constrained."""

    method = getattr(downloader, method_name, None)
    if not callable(method):
        return None
    try:
        parameters = inspect.signature(method).parameters
    except (TypeError, ValueError):
        return None

    # Fail closed if the downloader cannot enforce the provider rule at every
    # redirect. A final-URL check would happen after an unsafe request.
    if "allowed_url" not in parameters:
        return None
    kwargs: dict[str, object] = {"allowed_url": allowed_url}
    if "max_bytes" in parameters or any(
        item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters.values()
    ):
        kwargs["max_bytes"] = max_bytes

    try:
        call_result = method(url, **kwargs)
        if not inspect.isawaitable(call_result):
            return None
        return await asyncio.wait_for(
            call_result, timeout=STEAM_WIDGET_TIMEOUT_SECONDS
        )
    except Exception:
        return None


def _clean_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:limit]


def _first_text(
    root: BeautifulSoup | Tag, selectors: tuple[str, ...], limit: int
) -> str:
    for selector in selectors:
        node = root.select_one(selector)
        if node is None:
            continue
        value = (
            node.get("content")
            if node.name == "meta"
            else node.get_text(" ", strip=True)
        )
        cleaned = _clean_text(value, limit)
        if cleaned:
            return cleaned
    return ""


def _steam_widget_fields(document: BeautifulSoup, app_id: str) -> dict[str, str]:
    name = _first_text(
        document,
        (
            ".game_name",
            ".game_title",
            "[itemprop='name']",
            "meta[property='og:title']",
        ),
        240,
    )
    description = _first_text(
        document,
        (
            ".game_description_snippet",
            ".game_description",
            "#game_area_description .game_description_snippet",
            "meta[name='description']",
        ),
        1200,
    )
    price = _first_text(
        document,
        (
            ".game_purchase_price",
            ".discount_final_price",
            ".game_purchase_action .price",
            ".game_area_purchase_game .price",
        ),
        120,
    )
    discount = _first_text(document, (".discount_pct",), 60)
    if not discount:
        discount_block = document.select_one(".discount_block")
        block_text = _clean_text(
            discount_block.get_text(" ", strip=True) if discount_block else "", 300
        )
        found = _DISCOUNT_RE.search(block_text)
        if found:
            discount = _clean_text(found.group(0), 60)

    image_url = ""
    for selector in (
        ".game_header_image_ctn img",
        "img.game_header_image",
        ".game_header_image",
    ):
        for node in document.select(selector):
            if node.name != "img":
                continue
            candidate = str(node.get("src", "")).strip()
            if not candidate:
                candidate = str(node.get("data-src", "")).strip()
            if _steam_image_redirect_allowed(candidate, app_id):
                image_url = candidate
                break
        if image_url:
            break
    return {
        "name": name,
        "description": description,
        "price": price,
        "discount": discount,
        "image_url": image_url,
    }


def _sniff_image(data: bytes, content_type: str) -> str | None:
    """Accept only static raster formats whose bytes match their declared MIME."""

    actual: str | None = None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        actual = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        actual = "image/jpeg"
    elif data[:6] in (b"GIF87a", b"GIF89a"):
        actual = "image/gif"
    elif len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        actual = "image/webp"
    elif (
        len(data) >= 12
        and data[4:8] == b"ftyp"
        and data[8:12] in (b"avif", b"avis")
    ):
        actual = "image/avif"
    if actual != content_type:
        return None
    return actual


def _fallback_html(provider: str | None) -> str:
    title = "Steam 商店内容" if provider == "steam" else "外部嵌入内容"
    return (
        '<div class="keylol-browser-media-card keylol-browser-embed-fallback" '
        f'data-keylol-embed-provider="{provider or "unknown"}">'
        f"<strong>{title}</strong>"
        "<span>（静态截图无法完整加载）</span>"
        "</div>"
    )


def _steam_card_html(
    fields: Mapping[str, str], image_data_uri: str = "", *, partial: bool
) -> str:
    name = html.escape(fields.get("name", "") or "Steam 商店内容", quote=True)
    image = ""
    if image_data_uri:
        alt = html.escape(fields.get("name", "Steam 游戏封面"), quote=True)
        image = (
            '<div class="keylol-steam-embed-image">'
            f'<img data-keylol-embed-image="1" src="{image_data_uri}" alt="{alt}" '
            'loading="eager" decoding="async">'
            "</div>"
        )
    details: list[str] = []
    description = html.escape(fields.get("description", ""), quote=True)
    if description:
        details.append(
            f'<p class="keylol-steam-embed-description">{description}</p>'
        )
    price = html.escape(fields.get("price", ""), quote=True)
    discount = html.escape(fields.get("discount", ""), quote=True)
    price_parts: list[str] = []
    if discount:
        price_parts.append(
            f'<span class="keylol-steam-embed-discount">{discount}</span>'
        )
    if price:
        price_parts.append(
            f'<strong class="keylol-steam-embed-price">{price}</strong>'
        )
    if price_parts:
        details.append(
            '<div class="keylol-steam-embed-pricing">'
            + " ".join(price_parts)
            + "</div>"
        )
    if partial:
        details.append(
            '<span class="keylol-steam-embed-partial">部分商店内容无法加载</span>'
        )
    return (
        '<article class="keylol-browser-media-card keylol-browser-steam-embed" '
        'data-keylol-embed-provider="steam">'
        f'{image}<div class="keylol-steam-embed-content"><strong>{name}</strong>'
        + "".join(details)
        + "</div></article>"
    )


async def render_embed(
    url: object,
    downloader: object,
    *,
    fetch: bool = True,
) -> EmbedRenderResult:
    """Render a safe static card for a Steam widget or unknown embed.

    downloader is the shared safe_media downloader. It must provide
    fetch_html/fetch_image methods accepting an allowed_url callback. Returning
    None gives the caller a visible partial card. No cookies or browser
    requests are used here.
    """

    canonical_url = normalize_steam_widget_url(url)
    if canonical_url is None:
        return EmbedRenderResult(
            html=_fallback_html(None), loaded=False, fallback=True, provider=None
        )
    if not fetch or downloader is None:
        return EmbedRenderResult(
            html=_fallback_html("steam"), loaded=False, fallback=True, provider="steam"
        )

    app_id = _steam_app_id(canonical_url)
    raw_page = await _call_fetch(
        downloader,
        "fetch_html",
        canonical_url,
        allowed_url=lambda candidate: _steam_widget_redirect_allowed(
            candidate, canonical_url
        ),
        max_bytes=STEAM_WIDGET_MAX_HTML_BYTES,
    )
    page_result = _media_result_data(raw_page)
    if (
        page_result is None
        or page_result[1] not in {"text/html", "application/xhtml+xml"}
        or len(page_result[0]) > STEAM_WIDGET_MAX_HTML_BYTES
        or (
            page_result[2] is not None
            and not _steam_widget_redirect_allowed(page_result[2], canonical_url)
        )
    ):
        return EmbedRenderResult(
            html=_fallback_html("steam"), loaded=False, fallback=True, provider="steam"
        )

    try:
        document = BeautifulSoup(page_result[0], "html.parser")
        fields = _steam_widget_fields(document, app_id)
    except Exception:
        return EmbedRenderResult(
            html=_fallback_html("steam"), loaded=False, fallback=True, provider="steam"
        )

    image_data_uri = ""
    image_ok = False
    image_url = fields.get("image_url", "")
    if image_url:
        raw_image = await _call_fetch(
            downloader,
            "fetch_image",
            image_url,
            allowed_url=lambda candidate: _steam_image_redirect_allowed(
                candidate, app_id
            ),
            max_bytes=STEAM_WIDGET_MAX_IMAGE_BYTES,
        )
        image_result = _media_result_data(raw_image)
        if (
            image_result is not None
            and image_result[1].startswith("image/")
            and len(image_result[0]) <= STEAM_WIDGET_MAX_IMAGE_BYTES
            and _sniff_image(image_result[0], image_result[1]) is not None
            and (
                image_result[2] is None
                or _steam_image_redirect_allowed(image_result[2], app_id)
            )
        ):
            image_data_uri = (
                f"data:{image_result[1]};base64,"
                + base64.b64encode(image_result[0]).decode("ascii")
            )
            image_ok = True

    # Missing price or description can be a normal Steam product state;
    # missing title or header image leaves the static widget incomplete.
    loaded = bool(fields.get("name")) and image_ok
    markup = _steam_card_html(fields, image_data_uri, partial=not loaded)
    return EmbedRenderResult(
        html=markup,
        loaded=loaded,
        fallback=not loaded,
        provider="steam",
    )


__all__ = [
    "EmbedRenderResult",
    "normalize_steam_widget_url",
    "render_embed",
]
