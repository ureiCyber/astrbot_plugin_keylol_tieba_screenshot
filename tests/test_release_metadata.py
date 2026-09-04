"""Protect the public distribution contract without importing AstrBot."""

import json
from pathlib import Path
import re
import unittest
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "https://github.com/ureiCyber/astrbot_plugin_keylol_tieba_screenshot"


def metadata_scalar(key):
    """Read one required plain scalar from this project's metadata file."""
    text = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
    matches = re.findall(rf"^{re.escape(key)}: ([^\r\n]+)$", text, re.MULTILINE)
    if len(matches) != 1:
        raise AssertionError(f"Expected exactly one top-level {key} scalar")
    return matches[0].strip()


class ReleaseMetadataTests(unittest.TestCase):
    def test_plugin_identity_stays_compatible_with_existing_installations(self):
        self.assertEqual(metadata_scalar("name"), "astrbot_plugin_keylol_screenshot")

    def test_update_repository_is_public_https_without_credentials(self):
        repository = metadata_scalar("repo")
        self.assertEqual(repository, REPOSITORY)
        parts = urlsplit(repository)
        self.assertEqual(parts.scheme, "https")
        self.assertEqual(parts.hostname, "github.com")
        self.assertIsNone(parts.username)
        self.assertIsNone(parts.password)
        self.assertFalse(parts.query)
        self.assertFalse(parts.fragment)

    def test_author_is_the_chosen_display_name(self):
        self.assertEqual(metadata_scalar("author"), "キツネの嫁入り")

    def test_version_is_a_stable_semantic_version(self):
        self.assertRegex(metadata_scalar("version"), r"^\d+\.\d+\.\d+$")

    def test_cookie_defaults_are_empty(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        for field in ("keylol_cookie", "tieba_cookie"):
            with self.subTest(field=field):
                self.assertEqual(schema[field]["default"], "")

    def test_license_is_included(self):
        license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertTrue(license_text.startswith("MIT License\n"))
        self.assertIn("Copyright (c) 2026 キツネの嫁入り", license_text)


if __name__ == "__main__":
    unittest.main()
