"""Build llms-full.txt: one current review brief for OpenAdapt.

Reviews of OpenAdapt by people and AI models keep quoting pages from older
versions. llms-full.txt concatenates the pages that describe the current
release (README, CAPABILITIES, surfaces, limits, the latest changelog entry)
into one file to hand a reviewer, named after the release it describes.

    python scripts/build_llms_full.py          # rewrite llms-full.txt
    python scripts/build_llms_full.py --check  # fail if it is out of date
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "llms-full.txt"
REPO = "https://github.com/OpenAdaptAI/OpenAdapt/blob/main/"

SOURCES = [
    ("README.md", "README"),
    ("CAPABILITIES.md", "Capabilities"),
    ("packages/flow/docs/SURFACES.md", "Surfaces"),
    ("packages/flow/docs/LIMITS.md", "Limits"),
]


def flow_version() -> str:
    # A regex, not tomllib, so this runs on Python 3.10 too.
    text = (ROOT / "packages/flow/pyproject.toml").read_text("utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if match is None:
        raise SystemExit("packages/flow/pyproject.toml has no version")
    return match.group(1)


def latest_changelog(version: str) -> str:
    text = (ROOT / "packages/flow/CHANGELOG.md").read_text("utf-8")
    match = re.search(
        rf"^## v{re.escape(version)} \(.*?\)\n(.*?)(?=^## v|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    return match.group(1).strip() if match else ""


def absolute_links(text: str, source: str) -> str:
    """Make relative Markdown links absolute, so the brief reads on its own."""

    base = REPO + str(Path(source).parent) + "/" if "/" in source else REPO

    def fix(match: re.Match[str]) -> str:
        label, url = match.group(1), match.group(2)
        if re.match(r"^[a-z]+:|^#", url):
            return match.group(0)
        return f"[{label}]({base}{url})"

    return re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", fix, text)


def build() -> str:
    version = flow_version()
    parts = [
        f"# OpenAdapt: review brief for openadapt-flow {version}",
        "",
        "This file describes the current OpenAdapt release. It is generated from "
        "the repository on every change (scripts/build_llms_full.py). Pages, "
        "posts and repositories that describe earlier versions are outdated; "
        "where they disagree with this file, this file is current.",
        "",
        "Source: https://github.com/OpenAdaptAI/OpenAdapt",
    ]
    for path, title in SOURCES:
        body = (ROOT / path).read_text("utf-8").strip()
        parts += ["", "---", "", f"<!-- {title}: {path} -->", "", absolute_links(body, path)]
    notes = latest_changelog(version)
    if notes:
        parts += [
            "",
            "---",
            "",
            f"<!-- Changelog: openadapt-flow {version} -->",
            "",
            f"## What changed in openadapt-flow {version}",
            "",
            absolute_links(notes, "packages/flow/CHANGELOG.md"),
        ]
    return "\n".join(parts) + "\n"


def main(argv: list[str]) -> int:
    text = build()
    if "--check" in argv:
        current = OUT.read_text("utf-8") if OUT.exists() else ""
        if current != text:
            print(
                "llms-full.txt is out of date; run python scripts/build_llms_full.py",
                file=sys.stderr,
            )
            return 1
        return 0
    OUT.write_text(text, "utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(text.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
