"""Fixed QQ image safety helpers ported from astrbot_plugin_xiaoheihescreenshot/main.py."""

from __future__ import annotations

import math
from io import BytesIO

from PIL import Image, UnidentifiedImageError


DEVICE_SCALE_FACTOR = 2
MAX_IMAGE_DIMENSION = 16_384
MAX_IMAGE_PIXELS = 20_000_000
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MIN_JPEG_QUALITY = 50


def _safe_image_size(width: int, height: int) -> tuple[int, int]:
    """Keep the source pixels unless a fixed final-dimension lock is hit."""
    if width <= 0 or height <= 0:
        raise ValueError("Image dimensions must be positive")

    scale = min(
        1.0,
        MAX_IMAGE_DIMENSION / max(width, height),
        math.sqrt(MAX_IMAGE_PIXELS / (width * height)),
    )
    # Flooring both axes prevents rounding from crossing either dimension lock.
    return max(1, math.floor(width * scale)), max(1, math.floor(height * scale))


def _encode_jpeg(image, quality: int) -> bytes:
    """Encode one JPEG trial from the same lossless RGB pixels."""

    def encode(optimize: bool) -> bytes:
        with BytesIO() as output:
            image.save(
                output,
                format="JPEG",
                quality=quality,
                subsampling=0,
                optimize=optimize,
            )
            return output.getvalue()

    try:
        return encode(optimize=True)
    except OSError:
        # Pillow's optimized encoder can under-estimate its buffer for a
        # high-entropy 4:4:4 JPEG. Retry at the same quality and source pixels.
        return encode(optimize=False)


def _highest_quality_jpeg(image) -> tuple[bytes | None, int]:
    """Try quality 100 first, then find the highest integer quality that fits."""
    encoded = _encode_jpeg(image, 100)
    if len(encoded) <= MAX_IMAGE_BYTES:
        return encoded, 100

    best = _encode_jpeg(image, MIN_JPEG_QUALITY)
    if len(best) > MAX_IMAGE_BYTES:
        best = None

    quality = MIN_JPEG_QUALITY
    failed = {100}
    low = MIN_JPEG_QUALITY + 1
    high = 99 if best is not None else MIN_JPEG_QUALITY
    while low <= high:
        middle = (low + high) // 2
        encoded = _encode_jpeg(image, middle)
        if len(encoded) <= MAX_IMAGE_BYTES:
            best, quality = encoded, middle
            low = middle + 1
        else:
            failed.add(middle)
            high = middle - 1

    # JPEG sizes can have local reversals. Check every untested higher quality
    # before accepting a binary-search result or deciding to shrink dimensions.
    for candidate_quality in range(99, quality, -1):
        if candidate_quality in failed:
            continue
        encoded = _encode_jpeg(image, candidate_quality)
        if len(encoded) <= MAX_IMAGE_BYTES:
            return encoded, candidate_quality

    return best, quality


def _close_image(image) -> None:
    """Close Pillow images promptly while allowing small test doubles."""
    close = getattr(image, "close", None)
    if callable(close):
        close()


def _validate_qq_image(encoded: bytes) -> None:
    """Decode and recheck the actual final JPEG bytes against all three locks."""
    try:
        encoded_view = memoryview(encoded)
    except TypeError as error:
        raise ValueError("Screenshot output must be encoded image bytes") from error

    # A bytes subclass can override __len__. Read the actual buffer length so
    # the final byte check cannot be satisfied by a reported or source size.
    if encoded_view.nbytes > MAX_IMAGE_BYTES:
        raise RuntimeError("Screenshot exceeds QQ image byte safety lock")
    encoded_bytes = encoded_view.tobytes()

    try:
        with Image.open(BytesIO(encoded_bytes)) as decoded:
            if decoded.format != "JPEG":
                raise ValueError("Screenshot output must be a JPEG image")

            width, height = decoded.size
            if width <= 0 or height <= 0:
                raise ValueError("JPEG dimensions must be positive")
            if max(width, height) > MAX_IMAGE_DIMENSION:
                raise RuntimeError("Screenshot exceeds QQ image dimension safety lock")
            if width * height > MAX_IMAGE_PIXELS:
                raise RuntimeError("Screenshot exceeds QQ image pixel safety lock")

            # Check that the encoded JPEG can be fully decoded after its header
            # has passed the bounded-dimension checks.
            decoded.load()
    except (OSError, UnidentifiedImageError) as error:
        raise ValueError("Screenshot output is not a decodable JPEG image") from error


def _normalize_for_qq(image_bytes: bytes) -> bytes:
    """Apply dimension, pixel, and byte locks before returning one safe JPEG."""
    with Image.open(BytesIO(image_bytes)) as source:
        image = source.convert("RGB")
        # Pillow returns an independent image for convert; keep this defensive
        # copy for compatible image backends that return the source itself.
        if image is source:
            image = source.copy()

    try:
        size = _safe_image_size(*image.size)
        if size != image.size:
            resized = image.resize(size, Image.Resampling.LANCZOS)
            _close_image(image)
            image = resized

        normalized, _quality = _highest_quality_jpeg(image)
        if normalized is None:
            # Quality 50 still exceeds the byte lock. Search integer longest
            # edges to keep the largest size that fits instead of shrinking by
            # a fixed percentage. Every candidate comes from the same RGB image.
            width, height = image.size
            longest = max(width, height)
            low, high = 1, longest - 1
            best_size = None
            while low <= high:
                edge = (low + high) // 2
                candidate_size = (
                    max(1, width * edge // longest),
                    max(1, height * edge // longest),
                )
                candidate = image.resize(candidate_size, Image.Resampling.LANCZOS)
                try:
                    encoded = _encode_jpeg(candidate, MIN_JPEG_QUALITY)
                finally:
                    _close_image(candidate)

                if len(encoded) <= MAX_IMAGE_BYTES:
                    best_size = candidate_size
                    low = edge + 1
                else:
                    high = edge - 1

            if best_size is None:
                raise RuntimeError("Cannot encode an image within the QQ byte safety lock")

            resized = image.resize(best_size, Image.Resampling.LANCZOS)
            _close_image(image)
            image = resized
            normalized, _quality = _highest_quality_jpeg(image)

        if normalized is None:
            raise RuntimeError("Cannot encode an image within the QQ byte safety lock")

        # Materialize the underlying bytes before validation and return, so
        # neither a custom bytes subclass nor source-image dimensions affect
        # the final checks.
        normalized_bytes = memoryview(normalized).tobytes()
        _validate_qq_image(normalized_bytes)
        return normalized_bytes
    finally:
        _close_image(image)
