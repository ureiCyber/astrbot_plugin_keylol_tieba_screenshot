"""Verify anonymous GitHub access and the exact AstrBot distribution contents."""

import argparse
import io
import json
from pathlib import PurePosixPath
import re
from urllib.request import Request, urlopen
from zipfile import ZipFile


REPOSITORY = "ureiCyber/astrbot_plugin_keylol_tieba_screenshot"
REPOSITORY_URL = f"https://github.com/{REPOSITORY}"
EXPECTED_FILES = {
    "LICENSE",
    "README.md",
    "__init__.py",
    "_conf_schema.json",
    "keylol_browser.py",
    "keylol_page.py",
    "main.py",
    "metadata.yaml",
    "requirements.txt",
    "tieba_browser.py",
    "tieba_page.py",
}


def download(url, limit=5 * 1024 * 1024):
    # Deliberately do not read gh credentials, tokens, or an Authorization header.
    request = Request(url, headers={"User-Agent": "AstrBot-public-release-check"})
    with urlopen(request, timeout=30) as response:
        content = response.read(limit + 1)
    if len(content) > limit:
        raise ValueError("Distribution response exceeds the validation size limit")
    return content


def require(condition, message):
    if not condition:
        raise ValueError(message)


def scalar(text, key):
    matches = re.findall(rf"^{re.escape(key)}: ([^\r\n]+)$", text, re.MULTILINE)
    require(len(matches) == 1, f"Missing or repeated metadata field: {key}")
    return matches[0].strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", required=True, help="Expected full main commit SHA")
    parser.add_argument("--version", required=True, help="Expected plugin version")
    args = parser.parse_args()
    require(bool(re.fullmatch(r"[0-9a-f]{40}", args.commit)), "Invalid commit SHA")

    repo = json.loads(download(f"https://api.github.com/repos/{REPOSITORY}"))
    require(repo["private"] is False, "Repository is not public")
    require(repo["default_branch"] == "main", "Default branch is not main")
    remote = json.loads(download(f"https://api.github.com/repos/{REPOSITORY}/commits/main"))
    require(remote["sha"] == args.commit, "Remote main differs from the tested commit")

    raw_metadata = download(
        f"https://raw.githubusercontent.com/{REPOSITORY}/main/metadata.yaml"
    ).decode("utf-8")
    require(scalar(raw_metadata, "repo") == REPOSITORY_URL, "Incorrect update source")
    require(scalar(raw_metadata, "version") == args.version, "Incorrect version")
    require(
        scalar(raw_metadata, "name") == "astrbot_plugin_keylol_screenshot",
        "Existing plugin identity was changed",
    )
    require(scalar(raw_metadata, "author") == "キツネの嫁入り", "Incorrect author")

    archive = download(f"{REPOSITORY_URL}/archive/refs/heads/main.zip")
    with ZipFile(io.BytesIO(archive)) as package:
        members = {}
        roots = set()
        for entry in package.infolist():
            path = PurePosixPath(entry.filename)
            require(
                not path.is_absolute() and ".." not in path.parts
                and "\\" not in entry.filename,
                "Unsafe archive member path",
            )
            if entry.is_dir():
                continue
            require(len(path.parts) == 2, "Unexpected nested distribution file")
            roots.add(path.parts[0])
            name = path.parts[1]
            require(name not in members, "Duplicate distribution file")
            members[name] = entry
        require(len(roots) == 1, "Unexpected archive root")
        require(set(members) == EXPECTED_FILES, "Unexpected or missing distribution files")
        require(
            package.read(members["metadata.yaml"]).decode("utf-8") == raw_metadata,
            "Archive and raw metadata differ",
        )
        schema = json.loads(package.read(members["_conf_schema.json"]))
        for field in ("keylol_cookie", "tieba_cookie"):
            require(schema[field]["default"] == "", f"Nonempty {field} default")
        license_text = package.read(members["LICENSE"]).decode("utf-8")
        require(license_text.startswith("MIT License\n"), "MIT license is missing")

    print(json.dumps({
        "repository": REPOSITORY_URL,
        "visibility": "public",
        "commit": args.commit,
        "version": args.version,
        "anonymous_metadata": "ok",
        "anonymous_archive": "ok",
        "archive_files": len(members),
        "cookie_defaults": "empty",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
