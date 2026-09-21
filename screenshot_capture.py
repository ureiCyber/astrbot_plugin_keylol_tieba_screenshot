"""Shared DPR2 capture/stitch implementation ported from Xiaoheihe's main.py."""

from __future__ import annotations

import io
import math
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from PIL import Image

try:
    from .screenshot_safety import (
        DEVICE_SCALE_FACTOR,
        MAX_IMAGE_BYTES,
        MAX_IMAGE_DIMENSION,
        MAX_IMAGE_PIXELS,
        _safe_image_size,
    )
except ImportError:  # Direct import from the plugin directory.
    from screenshot_safety import (  # type: ignore[no-redef]
        DEVICE_SCALE_FACTOR,
        MAX_IMAGE_BYTES,
        MAX_IMAGE_DIMENSION,
        MAX_IMAGE_PIXELS,
        _safe_image_size,
    )


_ImageSource = bytes | bytearray | memoryview | str | os.PathLike[str]


class ScreenshotCaptureError(RuntimeError):
    """An incomplete or unsafe segmented screenshot that must not be sent."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@contextmanager
def _open_image(source: _ImageSource) -> Iterator[Image.Image]:
    if isinstance(source, (bytes, bytearray, memoryview)):
        stream = io.BytesIO(source)
        try:
            with Image.open(stream) as image:
                yield image
        finally:
            stream.close()
    else:
        with Image.open(os.fspath(source)) as image:
            yield image


def _close_image(image: object) -> None:
    close = getattr(image, "close", None)
    if callable(close):
        close()


def _plan_screenshot_segments(
    total_height: int,
    segment_height: int = 2000,
    max_total_height: int | None = None,
) -> list[tuple[int, int]]:
    """Return non-overlapping ``(top, height)`` slices in CSS pixels."""
    total = max(0, int(total_height))
    if max_total_height is not None and total > max(0, int(max_total_height)):
        raise ValueError(
            f"Page content height {total} exceeds safety limit {max_total_height}"
        )
    step = max(1, int(segment_height))
    return [(top, min(step, total - top)) for top in range(0, total, step)]


def _crop_screenshot_segment(
    image_bytes: bytes,
    viewport_height: int,
    crop_top: float,
    crop_height: int,
) -> bytes:
    """Crop CSS coordinates from a physical-pixel viewport PNG."""
    with _open_image(image_bytes) as source:
        scale_y = source.height / max(1, viewport_height)
        top = round(crop_top * scale_y)
        bottom = round((crop_top + crop_height) * scale_y)
        if top < 0 or bottom > source.height or bottom <= top:
            raise ValueError("Screenshot segment crop is outside the viewport")
        cropped = source.crop((0, top, source.width, bottom))
        try:
            output = io.BytesIO()
            cropped.save(output, format="PNG")
            return output.getvalue()
        finally:
            _close_image(cropped)


def _stitch_screenshot_segments(
    parts: list[tuple[int, _ImageSource]],
    total_height: int,
    css_width: int = 390,
) -> bytes:
    """Stitch DPR2 regions on a canvas sized before it is allocated."""
    if not parts or total_height <= 0:
        raise ValueError("No screenshot segments to stitch")

    ordered_parts = sorted(parts, key=lambda item: item[0])
    if ordered_parts[0][0] != 0:
        raise ValueError("Screenshot segments must start at CSS y=0")

    physical_width = css_width * DEVICE_SCALE_FACTOR
    physical_height = total_height * DEVICE_SCALE_FACTOR
    output_width, output_height = _safe_image_size(physical_width, physical_height)

    # Validate all DPR2 regions before creating the output canvas. Boundaries
    # are absolute CSS positions so no per-segment rounding can accumulate.
    regions: list[tuple[int, int, _ImageSource]] = []
    for index, (css_y, image_source) in enumerate(ordered_parts):
        next_css_y = (
            ordered_parts[index + 1][0]
            if index + 1 < len(ordered_parts)
            else total_height
        )
        if not 0 <= css_y < next_css_y <= total_height:
            raise ValueError("Invalid screenshot segment boundaries")
        source_height = (next_css_y - css_y) * DEVICE_SCALE_FACTOR
        with _open_image(image_source) as source:
            if source.width != physical_width or source.height < source_height:
                raise ValueError("Screenshot segment does not cover its DPR2 region")
            if source.height > source_height and index != len(ordered_parts) - 1:
                raise ValueError("Screenshot segments overlap")
        regions.append(
            (
                css_y * DEVICE_SCALE_FACTOR,
                next_css_y * DEVICE_SCALE_FACTOR,
                image_source,
            )
        )

    stitched = Image.new("RGB", (output_width, output_height), "white")
    scale_y = output_height / physical_height
    needs_resize = (output_width, output_height) != (physical_width, physical_height)
    # Lanczos samples neighbouring rows across boundaries. The same source
    # padding and absolute output grid are used for every strip.
    padding = math.ceil(3 / scale_y) + 1 if needs_resize else 0
    output_regions = []
    for start, end, _ in regions:
        top = round(start * output_height / physical_height)
        bottom = round(end * output_height / physical_height)
        # Keep fractional resize boxes small enough for Pillow's float box
        # coordinates. These are subdivisions of the same absolute grid,
        # never independently rounded/scaled copies of a segment.
        step = 512 if needs_resize else max(1, bottom - top)
        output_regions.extend(
            (row, min(row + step, bottom)) for row in range(top, bottom, step)
        )
    try:
        for top, bottom in output_regions:
            source_top = max(0, math.floor(top / scale_y) - padding)
            source_bottom = min(physical_height, math.ceil(bottom / scale_y) + padding)
            strip = Image.new(
                "RGB", (physical_width, source_bottom - source_top), "white"
            )
            try:
                for region_top, region_bottom, image_source in regions:
                    overlap_top = max(source_top, region_top)
                    overlap_bottom = min(source_bottom, region_bottom)
                    if overlap_top >= overlap_bottom:
                        continue
                    with _open_image(image_source) as source:
                        cropped = source.crop(
                            (
                                0,
                                overlap_top - region_top,
                                physical_width,
                                overlap_bottom - region_top,
                            )
                        )
                        try:
                            rgb = cropped.convert("RGB")
                            try:
                                strip.paste(rgb, (0, overlap_top - source_top))
                            finally:
                                _close_image(rgb)
                        finally:
                            _close_image(cropped)

                if needs_resize:
                    # Explicit horizontal-then-vertical passes keep the filter
                    # order identical for all strips. Pillow can otherwise
                    # reverse its passes for tall images, making a short last
                    # strip look different despite identical sampling bounds.
                    horizontal = strip.resize(
                        (output_width, strip.height), Image.Resampling.LANCZOS
                    )
                    try:
                        resized = horizontal.resize(
                            (output_width, bottom - top),
                            Image.Resampling.LANCZOS,
                            box=(
                                0,
                                top / scale_y - source_top,
                                output_width,
                                bottom / scale_y - source_top,
                            ),
                        )
                        try:
                            stitched.paste(resized, (0, top))
                        finally:
                            _close_image(resized)
                    finally:
                        _close_image(horizontal)
                else:
                    stitched.paste(strip, (0, top))
            finally:
                _close_image(strip)

        output = io.BytesIO()
        stitched.save(output, format="PNG")
        return output.getvalue()
    finally:
        _close_image(stitched)


async def _capture_segmented_screenshot(
    page: object,
    output_path: str | os.PathLike[str],
    *,
    width: int,
    viewport_height: int,
    page_height: int,
    timeout_ms: int,
    hide_repeated_chrome_script: str,
    restore_repeated_chrome_script: str,
    max_total_height: int = 100_000,
) -> None:
    """Capture a fixed mobile viewport in DPR2 PNG regions and stitch them."""
    if width <= 0 or viewport_height <= 0:
        raise ScreenshotCaptureError("dimensions")
    try:
        plans = _plan_screenshot_segments(
            page_height,
            segment_height=viewport_height,
            max_total_height=max_total_height,
        )
    except ValueError as exc:
        raise ScreenshotCaptureError("page_height") from exc
    if not plans:
        raise ScreenshotCaptureError("page_height")

    total_height = plans[-1][0] + plans[-1][1]
    max_scroll = max(0, total_height - viewport_height)
    temporary_parts: list[tuple[int, Path]] = []
    output_path = os.fspath(output_path)
    try:
        with tempfile.TemporaryDirectory(prefix="astrbot-screenshot-segments-") as temp_dir:
            for index, (css_y, css_height) in enumerate(plans):
                requested_scroll = min(css_y, max_scroll)
                actual_scroll = float(
                    await page.evaluate(  # type: ignore[attr-defined]
                        "(value) => { window.scrollTo(0, value); return window.scrollY; }",
                        requested_scroll,
                    )
                )
                await page.wait_for_timeout(80)  # type: ignore[attr-defined]
                crop_top = css_y - actual_scroll
                if crop_top < 0 or crop_top + css_height > viewport_height:
                    raise ScreenshotCaptureError("scroll")

                viewport_png = await page.screenshot(  # type: ignore[attr-defined]
                    type="png",
                    animations="disabled",
                    caret="hide",
                    scale="device",
                    timeout=timeout_ms,
                )
                with _open_image(viewport_png) as viewport_image:
                    # A smaller physical screenshot cannot supply native DPR2
                    # rows. Reject it before crop/stitch; never upscale it.
                    expected_width = width * DEVICE_SCALE_FACTOR
                    minimum_height = viewport_height * DEVICE_SCALE_FACTOR
                    if (
                        viewport_image.width != expected_width
                        or viewport_image.height < minimum_height
                        or viewport_image.height / viewport_height < DEVICE_SCALE_FACTOR
                    ):
                        raise ScreenshotCaptureError("dimensions")

                try:
                    cropped_png = _crop_screenshot_segment(
                        viewport_png,
                        viewport_height,
                        crop_top,
                        css_height,
                    )
                except ValueError as exc:
                    raise ScreenshotCaptureError("crop") from exc

                segment_path = Path(temp_dir) / f"segment-{index:05d}.png"
                segment_path.write_bytes(cropped_png)
                temporary_parts.append((css_y, segment_path))

                if index == 0 and hide_repeated_chrome_script:
                    await page.evaluate(hide_repeated_chrome_script)  # type: ignore[attr-defined]

            try:
                stitched_png = _stitch_screenshot_segments(
                    temporary_parts,
                    total_height,
                    css_width=width,
                )
            except ValueError as exc:
                raise ScreenshotCaptureError("stitch") from exc

        with open(output_path, "wb") as output:
            output.write(stitched_png)
    finally:
        try:
            await page.evaluate(restore_repeated_chrome_script)  # type: ignore[attr-defined]
        except Exception:
            pass


__all__ = [
    "DEVICE_SCALE_FACTOR",
    "MAX_IMAGE_BYTES",
    "MAX_IMAGE_DIMENSION",
    "MAX_IMAGE_PIXELS",
    "ScreenshotCaptureError",
    "_capture_segmented_screenshot",
    "_crop_screenshot_segment",
    "_plan_screenshot_segments",
    "_safe_image_size",
    "_stitch_screenshot_segments",
]
