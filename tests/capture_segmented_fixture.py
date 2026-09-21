"""Offline Chromium smoke fixture for both DPR2 screenshot wrappers.

Run from the repository root with:

    python tests/capture_segmented_fixture.py

It writes a JSON summary and JPEGs under ``.test-tmp/capture-segmented-fixture``
by default. All pages are assembled locally and every browser request is
aborted, so no Keylol/Tieba page or external resource is contacted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from io import BytesIO
from pathlib import Path

from PIL import Image


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

import keylol_browser
import screenshot_safety
import tieba_browser


async def _block_network(route) -> None:
    await route.abort(error_code="blockedbyclient")


def _fixture_html(kind: str, bands: list[tuple[str, str]], height: int) -> str:
    band_markup = "\n".join(
        f"""<section class="band" data-band="{index}" style="height:{height}px;background:{colour}">
          <strong>{label}</strong>
          <p>这是一张离线截图样例，用来检查中文正文、细小字体和分段拼接后的清晰度。</p>
          <p class="small">Keylol / Tieba · DPR 2 · CSS {height}px · band {index}</p>
          <div class="hairlines" aria-hidden="true"></div>
        </section>"""
        for index, (label, colour) in enumerate(bands, start=1)
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>本地 DPR2 截图验证 · {kind}</title>
  <style>
    * {{ box-sizing: border-box; }}
    html, body {{ margin: 0; padding: 0; width: 100%; background: #fff; }}
    body {{ color: #20242a; font-family: Arial, "Microsoft YaHei", sans-serif; }}
    .topbar {{ position: sticky; top: 0; z-index: 5; height: 44px; padding: 12px 16px;
      background: #fff; border-bottom: 1px solid #ccd3dc; font-size: 13px; }}
    .band {{ width: 100%; padding: 18px 20px; border-bottom: 1px solid #c6cbd1; }}
    .band strong {{ display: block; margin-bottom: 12px; font-size: 18px; }}
    .band p {{ margin: 0 0 8px; font-size: 14px; line-height: 1.7; }}
    .band p.small {{ font-size: 11px; line-height: 1.5; color: #4b535d; }}
    .hairlines {{ height: 1px; margin-top: 14px; background: #56616e; box-shadow: 0 2px #fff, 0 4px #56616e; }}
  </style>
</head>
<body>
  <div class="topbar">离线 DPR2 截图验证 · {kind}</div>
  <main id="fixture">{band_markup}</main>
</body>
</html>"""


async def _measure_page(page) -> dict:
    return await page.evaluate(
        """() => ({
          width: window.innerWidth,
          dpr: window.devicePixelRatio,
          pageHeight: Math.ceil(Math.max(
            document.body.scrollHeight,
            document.documentElement.scrollHeight
          )),
          captureHeight: Math.ceil(document.querySelector('#fixture').getBoundingClientRect().bottom + window.scrollY),
          bands: [...document.querySelectorAll('.band')].map((band) => ({
            label: band.querySelector('strong').textContent,
            color: getComputedStyle(band).backgroundColor,
            top: band.getBoundingClientRect().top + window.scrollY,
            height: band.getBoundingClientRect().height,
          })),
        })"""
    )


def _parse_rgb(css_color: str) -> tuple[int, int, int]:
    numbers = [int(value.strip()) for value in css_color[css_color.index("(") + 1 : -1].split(",")]
    return numbers[:3]


def _assert_band_positions(image: Image.Image, bands: list[dict], source_height: int) -> None:
    scale_y = image.height / source_height
    for band in bands:
        y = round((band["top"] + band["height"] / 2) * scale_y)
        y = min(image.height - 1, max(0, y))
        actual = image.getpixel((min(2, image.width - 1), y))
        expected = _parse_rgb(band["color"])
        if max(abs(left - right) for left, right in zip(actual, expected)) > 12:
            raise AssertionError(
                f"color band {band['label']!r} shifted or changed at output row {y}: "
                f"expected {expected}, got {actual}"
            )


async def _capture_case(page, wrapper, site: str, css_width: int, kind: str,
                        bands: list[tuple[str, str]], band_height: int,
                        output_dir: Path) -> dict:
    await page.set_content(
        _fixture_html(kind, bands, band_height),
        wait_until="load",
    )
    measured = await _measure_page(page)
    if measured["width"] != css_width or measured["dpr"] != 2:
        raise AssertionError(f"unexpected viewport/DPR: {measured}")

    source_height = measured["captureHeight"]
    png_path = output_dir / f".{site}-{kind}-w{css_width}.png"
    jpeg_path = output_dir / f"{site}-{kind}-w{css_width}.jpg"
    await wrapper(
        page,
        str(png_path),
        width=css_width,
        viewport_height=2000,
        page_height=source_height,
        timeout_ms=5000,
    )
    png_bytes = png_path.read_bytes()
    with Image.open(BytesIO(png_bytes)) as png_image:
        expected_png_size = screenshot_safety._safe_image_size(
            css_width * screenshot_safety.DEVICE_SCALE_FACTOR,
            source_height * screenshot_safety.DEVICE_SCALE_FACTOR,
        )
        if png_image.size != expected_png_size:
            raise AssertionError(
                f"{site} {kind} PNG size {png_image.size} != {expected_png_size}"
            )

    jpeg_bytes = screenshot_safety._normalize_for_qq(png_bytes)
    jpeg_path.write_bytes(jpeg_bytes)
    png_path.unlink(missing_ok=True)

    with Image.open(jpeg_path) as image:
        expected_jpeg_size = screenshot_safety._safe_image_size(
            css_width * screenshot_safety.DEVICE_SCALE_FACTOR,
            source_height * screenshot_safety.DEVICE_SCALE_FACTOR,
        )
        if image.format != "JPEG" or image.size != expected_jpeg_size:
            raise AssertionError(
                f"{site} {kind} JPEG is {image.format} {image.size}, expected {expected_jpeg_size}"
            )
        if max(image.size) > screenshot_safety.MAX_IMAGE_DIMENSION:
            raise AssertionError(f"dimension safety lock exceeded: {image.size}")
        if image.width * image.height > screenshot_safety.MAX_IMAGE_PIXELS:
            raise AssertionError(f"pixel safety lock exceeded: {image.size}")
        _assert_band_positions(
            image,
            measured["bands"],
            source_height,
        )

    if kind == "short" and css_width == 390:
        with Image.open(jpeg_path) as image:
            if image.width != 780:
                raise AssertionError(f"short 390 CSS px output should stay 780 px wide: {image.size}")

    return {
        "site": site,
        "fixture": kind,
        "css_width": css_width,
        "device_scale_factor": measured["dpr"],
        "css_height": source_height,
        "png_size": list(expected_png_size),
        "jpeg_size": list(expected_jpeg_size),
        "jpeg_bytes": len(jpeg_bytes),
        "jpeg_path": str(jpeg_path.resolve()),
        "band_count": len(measured["bands"]),
        "band_positions_verified": True,
    }


async def run(output_dir: Path, chrome: Path) -> dict:
    if not chrome.is_file():
        raise FileNotFoundError(f"Chrome executable not found: {chrome}")
    output_dir.mkdir(parents=True, exist_ok=True)
    results = []
    wrappers = (
        ("keylol", keylol_browser._capture_mobile_page_tiles),
        ("tieba", tieba_browser._capture_mobile_page_tiles),
    )
    fixtures = (
        (
            "short",
            180,
            [
                ("短帖：中文正文与小字号细节", "#e8f4ff"),
                ("原生 DPR2 字体栅格", "#f2f7e8"),
                ("安全范围内保持完整像素", "#fff0e7"),
            ],
        ),
        (
            "long",
            1300,
            [
                ("长帖分段 01 · 蓝色", "#d8e8fa"),
                ("长帖分段 02 · 绿色", "#dff2df"),
                ("长帖分段 03 · 米色", "#f5edcf"),
                ("长帖分段 04 · 粉色", "#f8dfe4"),
                ("长帖分段 05 · 紫色", "#e9def8"),
                ("长帖分段 06 · 青色", "#d7f0ee"),
                ("长帖分段 07 · 蓝灰色", "#dce5ed"),
            ],
        ),
    )

    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            executable_path=str(chrome),
            args=["--disable-background-networking", "--no-first-run"],
        )
        try:
            for css_width in (320, 390, 440):
                context = await browser.new_context(
                    viewport={"width": css_width, "height": 2000},
                    screen={"width": css_width, "height": 2000},
                    device_scale_factor=2,
                    is_mobile=True,
                    has_touch=True,
                    service_workers="block",
                )
                try:
                    page = await context.new_page()
                    await page.route("**/*", _block_network)
                    for site, wrapper in wrappers:
                        for kind, band_height, bands in fixtures:
                            results.append(
                                await _capture_case(
                                    page,
                                    wrapper,
                                    site,
                                    css_width,
                                    kind,
                                    bands,
                                    band_height,
                                    output_dir,
                                )
                            )
                finally:
                    await context.close()
        finally:
            await browser.close()

    summary = {
        "browser": str(chrome.resolve()),
        "network": "blocked for all page requests",
        "device_scale_factor": 2,
        "css_widths": [320, 390, 440],
        "captures": results,
    }
    summary_path = output_dir / "summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPOSITORY_ROOT / ".test-tmp" / "capture-segmented-fixture",
    )
    parser.add_argument(
        "--chrome",
        type=Path,
        default=Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    )
    args = parser.parse_args()
    summary = asyncio.run(run(args.output_dir.resolve(), args.chrome.resolve()))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
