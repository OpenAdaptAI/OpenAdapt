"""Build llms-full.txt: one current review brief for OpenAdapt.

Reviews of OpenAdapt by people and AI models keep quoting pages from older
versions. llms-full.txt concatenates the pages that describe the current
release (README, CAPABILITIES, surfaces, limits, the latest changelog entry)
into one file to hand a reviewer, named after the release it describes.

    python scripts/build_llms_full.py          # rewrite llms-full.txt
    python scripts/build_llms_full.py --check  # fail if it is out of date

A pull request does not regenerate llms-full.txt. Every page it quotes changes
often, so a committed copy per pull request made every open pull request
conflict whenever one merged. The refresh workflow
(.github/workflows/refresh-generated.yml) opens a pull request that refreshes
it after its sources change on main; on a pull request, CI only checks that
the file was not edited by hand (scripts/build_capabilities.py --check-pr).
"""

from __future__ import annotations

import importlib.util
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


def source_text(path: str) -> str:
    """A source page as its sources describe it.

    CAPABILITIES.md is assembled from capabilities/, not read from the
    committed file, which can trail its row files until the refresh workflow
    runs.
    """
    if path != "CAPABILITIES.md":
        return (ROOT / path).read_text("utf-8")
    script = ROOT / "scripts" / "build_capabilities.py"
    spec = importlib.util.spec_from_file_location("build_capabilities", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.assemble()


def build() -> str:
    version = flow_version()
    parts = [
        f"# OpenAdapt: review brief for openadapt-flow {version}",
        "",
        "This file describes the current OpenAdapt release. It is generated from "
        "the repository (scripts/build_llms_full.py) and refreshed with every release. Pages, "
        "posts and repositories that describe earlier versions are outdated; "
        "where they disagree with this file, this file is current.",
        "",
        "Source: https://github.com/OpenAdaptAI/OpenAdapt",
    ]
    for path, title in SOURCES:
        body = source_text(path).strip()
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
