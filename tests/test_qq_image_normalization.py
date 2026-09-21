"""Tests for fixed QQ image safety limits and final JPEG validation."""

from __future__ import annotations

import io
import random
import unittest
from unittest.mock import patch

from PIL import Image

import screenshot_safety as safety


class _SizedPayload(bytes):
    """A bytes payload with simulated encoded size for search-only tests."""

    def __new__(cls, payload: bytes, reported_length: int):
        value = super().__new__(cls, payload)
        value.reported_length = int(reported_length)
        return value

    def __len__(self):
        return self.reported_length


def _image_bytes(format_name: str, width: int = 32, height: int = 24) -> bytes:
    image = Image.new("RGB", (width, height), (80, 130, 190))
    output = io.BytesIO()
    try:
        image.save(output, format=format_name)
        return output.getvalue()
    finally:
        image.close()
        output.close()


def _jpeg_with_declared_size(width: int, height: int) -> bytes:
    """Rewrite a real JPEG SOF header to test locks without large allocations."""
    data = bytearray(_image_bytes("JPEG", 16, 12))
    start_of_frame = {
        0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
        0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
    }
    index = 2
    while index < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        while index < len(data) and data[index] == 0xFF:
            index += 1
        if index >= len(data):
            break
        marker = data[index]
        index += 1
        if marker == 0xDA:
            break
        if marker in {0xD8, 0xD9, 0x01} or 0xD0 <= marker <= 0xD7:
            continue
        if index + 2 > len(data):
            break
        segment_length = int.from_bytes(data[index:index + 2], "big")
        if marker in start_of_frame:
            data[index + 3:index + 5] = height.to_bytes(2, "big")
            data[index + 5:index + 7] = width.to_bytes(2, "big")
            return bytes(data)
        index += segment_length
    raise AssertionError("Pillow JPEG did not contain a supported SOF marker")


class ImageSizeSafetyTests(unittest.TestCase):
    def test_hard_limits_and_dpr_are_fixed(self):
        self.assertEqual(safety.DEVICE_SCALE_FACTOR, 2)
        self.assertEqual(safety.MAX_IMAGE_DIMENSION, 16_384)
        self.assertEqual(safety.MAX_IMAGE_PIXELS, 20_000_000)
        self.assertEqual(safety.MAX_IMAGE_BYTES, 10 * 1024 * 1024)
        self.assertEqual(safety.MIN_JPEG_QUALITY, 50)

    def test_dpr2_dimensions_are_preserved_when_they_fit(self):
        css_width = 390
        physical_size = (css_width * safety.DEVICE_SCALE_FACTOR, 5_000 * 2)

        self.assertEqual(physical_size[0], 780)
        self.assertEqual(safety._safe_image_size(*physical_size), physical_size)

    def test_longest_edge_lock_scales_only_as_far_as_needed(self):
        actual = safety._safe_image_size(20_000, 500)

        self.assertEqual(actual, (16_384, 409))
        self.assertLessEqual(max(actual), safety.MAX_IMAGE_DIMENSION)
        self.assertLessEqual(actual[0] * actual[1], safety.MAX_IMAGE_PIXELS)

    def test_pixel_lock_scales_down_with_flooring(self):
        actual = safety._safe_image_size(5_000, 5_000)

        self.assertEqual(actual, (4_472, 4_472))
        self.assertLessEqual(actual[0] * actual[1], safety.MAX_IMAGE_PIXELS)

    def test_rounding_cannot_recross_the_pixel_lock(self):
        actual = safety._safe_image_size(20_000, 2_000)

        self.assertEqual(actual, (14_142, 1_414))
        self.assertLessEqual(actual[0] * actual[1], safety.MAX_IMAGE_PIXELS)

    def test_boundary_and_deterministic_sizes_always_fit(self):
        rng = random.Random(20260920)
        sizes = [
            (1, 1),
            (safety.MAX_IMAGE_DIMENSION, safety.MAX_IMAGE_DIMENSION),
            (safety.MAX_IMAGE_DIMENSION + 1, 1),
            (1, safety.MAX_IMAGE_DIMENSION + 1),
            (5_000, 5_000),
            (20_000, 2_000),
        ]
        sizes.extend((rng.randint(1, 50_000), rng.randint(1, 50_000)) for _ in range(100))

        for width, height in sizes:
            with self.subTest(width=width, height=height):
                actual = safety._safe_image_size(width, height)
                self.assertGreaterEqual(actual[0], 1)
                self.assertGreaterEqual(actual[1], 1)
                self.assertLessEqual(actual[0], width)
                self.assertLessEqual(actual[1], height)
                self.assertLessEqual(max(actual), safety.MAX_IMAGE_DIMENSION)
                self.assertLessEqual(actual[0] * actual[1], safety.MAX_IMAGE_PIXELS)

    def test_non_positive_image_dimensions_are_rejected(self):
        for size in ((0, 1), (1, 0), (-1, 3)):
            with self.subTest(size=size), self.assertRaises(ValueError):
                safety._safe_image_size(*size)


class JpegQualitySearchTests(unittest.TestCase):
    def test_quality_100_is_kept_at_or_below_the_byte_lock(self):
        with Image.new("RGB", (32, 24)) as image:
            for payload_size in (safety.MAX_IMAGE_BYTES - 1, safety.MAX_IMAGE_BYTES):
                with self.subTest(payload_size=payload_size):
                    calls = []

                    def encode(_image, quality):
                        calls.append(quality)
                        return _SizedPayload(bytes([quality]), payload_size)

                    with patch.object(safety, "_encode_jpeg", side_effect=encode):
                        encoded, quality = safety._highest_quality_jpeg(image)

                    self.assertEqual(bytes(encoded), bytes([100]))
                    self.assertEqual(len(encoded), payload_size)
                    self.assertEqual(quality, 100)
                    self.assertEqual(calls, [100])

    def test_binary_search_returns_the_highest_fitting_quality(self):
        calls = []

        def encode(_image, quality):
            calls.append(quality)
            size = 7_000_000 + (quality - safety.MIN_JPEG_QUALITY) * 100_000
            return _SizedPayload(bytes([quality]), size)

        with Image.new("RGB", (32, 24)) as image:
            with patch.object(safety, "_encode_jpeg", side_effect=encode):
                encoded, quality = safety._highest_quality_jpeg(image)

        self.assertEqual(bytes(encoded), bytes([84]))
        self.assertEqual(quality, 84)
        self.assertLessEqual(len(encoded), safety.MAX_IMAGE_BYTES)
        self.assertIn(100, calls)
        self.assertIn(safety.MIN_JPEG_QUALITY, calls)
        self.assertEqual(len(calls), len(set(calls)))

    def test_non_monotonic_sizes_do_not_hide_a_higher_quality(self):
        calls = []

        def encode(_image, quality):
            calls.append(quality)
            fits = quality <= 84 or quality == 97
            size = safety.MAX_IMAGE_BYTES if fits else safety.MAX_IMAGE_BYTES + 1
            return _SizedPayload(bytes([quality]), size)

        with Image.new("RGB", (32, 24)) as image:
            with patch.object(safety, "_encode_jpeg", side_effect=encode):
                encoded, quality = safety._highest_quality_jpeg(image)

        self.assertEqual(bytes(encoded), bytes([97]))
        self.assertEqual(quality, 97)
        self.assertEqual(len(calls), len(set(calls)))
        self.assertLessEqual(len(calls), 51)

    def test_oversized_quality_50_checks_higher_qualities_before_resizing(self):
        calls = []

        def encode(_image, quality):
            calls.append(quality)
            fits = quality == 70
            size = safety.MAX_IMAGE_BYTES if fits else safety.MAX_IMAGE_BYTES + 1
            return _SizedPayload(bytes([quality]), size)

        with Image.new("RGB", (32, 24)) as image:
            with patch.object(safety, "_encode_jpeg", side_effect=encode):
                encoded, quality = safety._highest_quality_jpeg(image)

        self.assertEqual(bytes(encoded), bytes([70]))
        self.assertEqual(quality, 70)
        self.assertIn(70, calls)

    def test_no_quality_fit_is_reported_without_returning_oversized_bytes(self):
        calls = []

        def encode(_image, quality):
            calls.append(quality)
            return _SizedPayload(bytes([quality]), safety.MAX_IMAGE_BYTES + 1)

        with Image.new("RGB", (32, 24)) as image:
            with patch.object(safety, "_encode_jpeg", side_effect=encode):
                encoded, quality = safety._highest_quality_jpeg(image)

        self.assertIsNone(encoded)
        self.assertEqual(quality, safety.MIN_JPEG_QUALITY)
        self.assertEqual(sorted(calls), list(range(50, 101)))

    def test_optimize_buffer_error_retries_at_the_same_quality(self):
        class SaveProbe:
            def __init__(self):
                self.calls = []

            def save(self, output, **kwargs):
                self.calls.append(kwargs)
                if kwargs["optimize"]:
                    raise OSError("optimized buffer too small")
                output.write(b"same-quality jpeg")

        image = SaveProbe()
        encoded = safety._encode_jpeg(image, 100)

        self.assertEqual(encoded, b"same-quality jpeg")
        self.assertEqual([call["quality"] for call in image.calls], [100, 100])
        self.assertEqual([call["subsampling"] for call in image.calls], [0, 0])
        self.assertEqual([call["optimize"] for call in image.calls], [True, False])

    def test_real_pillow_encoder_outputs_a_decodable_jpeg(self):
        with Image.new("RGB", (64, 48), (20, 90, 180)) as image:
            encoded = safety._encode_jpeg(image, 100)

        with Image.open(io.BytesIO(encoded)) as decoded:
            self.assertEqual(decoded.format, "JPEG")
            self.assertEqual(decoded.size, (64, 48))


class NormalizationTests(unittest.TestCase):
    def test_small_image_keeps_quality_100_and_original_physical_dimensions(self):
        source = _image_bytes("PNG", 780, 240)
        original_encoder = safety._encode_jpeg
        calls = []

        def encode(image, quality):
            calls.append((image.size, quality))
            return original_encoder(image, quality)

        with patch.object(safety, "_encode_jpeg", side_effect=encode):
            result = safety._normalize_for_qq(source)

        with Image.open(io.BytesIO(result)) as decoded:
            self.assertEqual(decoded.format, "JPEG")
            self.assertEqual(decoded.size, (780, 240))
        self.assertEqual(calls, [((780, 240), 100)])
        self.assertLessEqual(len(result), safety.MAX_IMAGE_BYTES)

    def test_quality_50_byte_failure_searches_integer_longest_edge_sizes(self):
        source = _image_bytes("PNG", 400, 300)
        original_encoder = safety._encode_jpeg
        calls = []

        def encode(image, quality):
            calls.append((image.size, quality))
            actual_jpeg = original_encoder(image, quality)
            width, height = image.size
            simulated_size = width * height * quality * 10
            return _SizedPayload(actual_jpeg, simulated_size)

        with patch.object(safety, "_encode_jpeg", side_effect=encode):
            result = safety._normalize_for_qq(source)

        with Image.open(io.BytesIO(result)) as decoded:
            self.assertEqual(decoded.format, "JPEG")
            self.assertEqual(decoded.size, (167, 125))
        self.assertGreater(400 * 300 * 50 * 10, safety.MAX_IMAGE_BYTES)
        self.assertGreater(168 * 126 * 50 * 10, safety.MAX_IMAGE_BYTES)
        self.assertLessEqual(167 * 125 * 50 * 10, safety.MAX_IMAGE_BYTES)
        self.assertTrue(any(size != (400, 300) for size, _quality in calls))

    def test_unattainable_byte_lock_raises_instead_of_returning_oversized_jpeg(self):
        source = _image_bytes("PNG", 16, 12)
        original_encoder = safety._encode_jpeg
        calls = []

        def encode(image, quality):
            calls.append((image.size, quality))
            return _SizedPayload(
                original_encoder(image, quality), safety.MAX_IMAGE_BYTES + 1,
            )

        with patch.object(safety, "_encode_jpeg", side_effect=encode):
            with self.assertRaisesRegex(RuntimeError, "within the QQ byte safety lock"):
                safety._normalize_for_qq(source)

        self.assertTrue(any(size != (16, 12) for size, _quality in calls))
        self.assertLessEqual(
            sum(quality == safety.MIN_JPEG_QUALITY for _size, quality in calls),
            16,
        )

    def test_non_image_encoder_output_fails_closed_at_final_validation(self):
        source = _image_bytes("PNG", 32, 24)

        with patch.object(safety, "_encode_jpeg", return_value=b"not an image"):
            with self.assertRaisesRegex(ValueError, "JPEG"):
                safety._normalize_for_qq(source)


class FinalJpegValidationTests(unittest.TestCase):
    def test_accepts_a_real_jpeg_within_all_locks(self):
        encoded = _image_bytes("JPEG", 64, 48)

        self.assertIsNone(safety._validate_qq_image(encoded))

    def test_checks_actual_jpeg_header_longest_edge(self):
        encoded = _jpeg_with_declared_size(safety.MAX_IMAGE_DIMENSION + 1, 1)

        with self.assertRaisesRegex(RuntimeError, "dimension safety lock"):
            safety._validate_qq_image(encoded)

    def test_checks_actual_jpeg_header_total_pixels(self):
        encoded = _jpeg_with_declared_size(5_000, 4_001)

        with self.assertRaisesRegex(RuntimeError, "pixel safety lock"):
            safety._validate_qq_image(encoded)

    def test_checks_actual_buffer_bytes_not_a_reported_length(self):
        jpeg = _image_bytes("JPEG", 4, 3)
        oversized = jpeg + bytes(safety.MAX_IMAGE_BYTES + 1 - len(jpeg))
        misleading = _SizedPayload(oversized, 1)

        with self.assertRaisesRegex(RuntimeError, "byte safety lock"):
            safety._validate_qq_image(misleading)

    def test_rejects_non_jpeg_and_invalid_bytes(self):
        for value in (_image_bytes("PNG", 4, 3), b"not an image"):
            with self.subTest(value=value[:8]), self.assertRaises(ValueError):
                safety._validate_qq_image(value)


if __name__ == "__main__":
    unittest.main()
