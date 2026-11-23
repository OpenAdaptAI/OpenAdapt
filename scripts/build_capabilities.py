"""Assemble CAPABILITIES.md from capabilities/, one file per table row.

Almost every pull request adds or edits a capability row, and most add it at
the end of the same table. When CAPABILITIES.md was edited by hand, every
merge to main made the other open pull requests conflict. Now the page is
assembled from files:

    capabilities/<NN>-<section>/section.md      the section's prose and table header
    capabilities/<NN>-<section>/<NNNN>-<row>.md one table row, exactly as printed

A row file holds one line, the Markdown table row. Rows print in the order of
their numeric prefix (then by name), so a new row takes a number after the
last one, or between two rows to sit between them. Two pull requests that
pick the same number both keep their rows. In section.md, the line
``<!-- capability rows -->`` is where the rows go, and
``<!-- generated: scripts/build_command_map.py -->`` is where the command list
from packages/flow/docs/commands/ goes.

A pull request edits only these files. It does not regenerate CAPABILITIES.md
or llms-full.txt: a workflow (.github/workflows/refresh-generated.yml) opens a
pull request that refreshes both after changes land on main.

    python scripts/build_capabilities.py                   # rewrite CAPABILITIES.md
    python scripts/build_capabilities.py --check           # fail unless it is current
    python scripts/build_capabilities.py --check-pr BASE   # the pull-request check
    python scripts/build_capabilities.py --guard           # fail if the page was edited by hand
    python scripts/build_capabilities.py --split           # rebuild capabilities/ from CAPABILITIES.md

``--check-pr BASE`` passes when each generated file (CAPABILITIES.md,
llms-full.txt) is current or was not changed since BASE. It fails when a pull
request edits a generated file by hand, because the next refresh would drop
the edit. ``--guard`` runs before each refresh: it fails when
CAPABILITIES.md no longer matches the hash this script last wrote
(capabilities/CAPABILITIES.md.sha256), for example after a pull request whose
CI passed before this check existed, and lists the lines no source produces.

``--split`` is the one-time migration, kept so it can be re-run on the newest
main before the split lands. After that, never run it: it rebuilds the row
files from the committed page, so it deletes every row that so far exists
only as a row file.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "CAPABILITIES.md"
SOURCES = ROOT / "capabilities"
SECTION = "section.md"
# The sha256 of CAPABILITIES.md as this script last wrote it. A page whose
# hash differs was changed by something other than this script, such as a
# pull request whose CI passed before --check-pr existed; --guard stops the
# refresh there instead of dropping that change.
STAMP = SOURCES / "CAPABILITIES.md.sha256"
ROWS = "<!-- capability rows -->"
COMMANDS = "<!-- generated: scripts/build_command_map.py -->"
_ROW_FILE = re.compile(r"^(\d+)-[a-z0-9][a-z0-9-]*\.md$")
_SECTION_DIR = re.compile(r"^(\d+)-[a-z0-9][a-z0-9-]*$")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rel(path: Path) -> Path:
    return path.relative_to(ROOT) if path.is_relative_to(ROOT) else path


def _visible(path: Path) -> bool:
    """Skip what editors and the OS leave behind (.DS_Store, foo.md~, .swp)."""
    return not path.name.startswith((".", "__")) and not path.name.endswith("~")


def _numbered(paths, pattern: re.Pattern[str]) -> list[Path]:
    found = []
    for path in paths:
        match = pattern.match(path.name)
        if match is None:
            raise SystemExit(
                f"{_rel(path)}: name it <number>-<lowercase-slug>"
                + (".md" if pattern is _ROW_FILE else "")
            )
        found.append((int(match.group(1)), path.name, path))
    return [path for _, _, path in sorted(found)]


def sections(root: Path = SOURCES) -> list[Path]:
    return _numbered(
        (p for p in root.iterdir() if p.is_dir() and _visible(p)), _SECTION_DIR
    )


def rows(section: Path) -> list[str]:
    """Return a section's table rows, in order, each checked to be one row."""
    files = (
        p
        for p in section.iterdir()
        if p.is_file() and p.name != SECTION and _visible(p)
    )
    lines = []
    for path in _numbered(files, _ROW_FILE):
        text = path.read_text("utf-8")
        line = text[:-1] if text.endswith("\n") else text
        if "\n" in line or "\r" in line or not line.startswith("|"):
            raise SystemExit(
                f"{_rel(path)} must hold exactly one Markdown table row "
                "(one line starting with '|')"
            )
        lines.append(line)
    return lines


def assemble(root: Path = SOURCES) -> str:
    """Return CAPABILITIES.md as the files under capabilities/ describe it."""
    for path in root.iterdir():
        if (
            path.is_file()
            and _visible(path)
            and path.name not in ("README.md", STAMP.name)
        ):
            # A row here would never print: stop instead of dropping it.
            raise SystemExit(f"{_rel(path)}: put row files in a section directory")
    commands = _load("build_command_map").render()
    parts = []
    for section in sections(root):
        text = (section / SECTION).read_text("utf-8")
        table = rows(section)
        lines = text.split("\n")
        if table and lines.count(ROWS) != 1:
            raise SystemExit(f"{_rel(section)}/{SECTION} needs one '{ROWS}' line")
        if not table and ROWS in lines:
            raise SystemExit(f"{_rel(section)} has '{ROWS}' but no row files")
        out = []
        for line in lines:
            if line == ROWS:
                out.extend(table)
            elif line == COMMANDS:
                out.append(commands)
            else:
                out.append(line)
        parts.append("\n".join(out))
    return "".join(parts)


# --------------------------------------------------------------------------- #
# The migration: CAPABILITIES.md -> capabilities/
# --------------------------------------------------------------------------- #
def _path_tokens() -> list[str]:
    """Words a public path must not hold (source-policy.public.json)."""
    policy = ROOT / "source-policy.public.json"
    if not policy.exists():
        return []
    tokens = json.loads(policy.read_text("utf-8"))["enforcement"]["path_tokens"]
    return [token.replace("_", "-") for token in tokens]


def _slug(text: str, limit: int = 48) -> str:
    """A file name from a row's first cell, or a section heading."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    words = re.sub(r"[^a-z0-9]+", " ", text.lower()).split()
    slug = ""
    for word in words:
        if slug and len(slug) + 1 + len(word) > limit:
            break
        slug = f"{slug}-{word}" if slug else word[:limit]
    for token in _path_tokens():
        slug = re.sub(r"-+", "-", slug.replace(token, "")).strip("-")
    return slug or "row"


def split(text: str) -> dict[str, str]:
    """Return {relative path: content} for capabilities/, from a CAPABILITIES.md.

    assemble() of the result gives ``text`` back byte for byte.
    """
    command_map = _load("build_command_map")
    begin, end = text.index(command_map.BEGIN), text.index(command_map.END)
    text = text[:begin] + COMMANDS + text[end + len(command_map.END) :]
    lines = text.splitlines(keepends=True)
    chunks: list[list[str]] = [[]]
    for line in lines:
        if line.startswith("## ") and chunks[-1]:
            chunks.append([])
        chunks[-1].append(line)
    files: dict[str, str] = {}
    for number, chunk in enumerate(chunks):
        heading = chunk[0].lstrip("#").strip()
        directory = f"{number:02d}-{_slug(heading, 40)}"
        table = [i for i, line in enumerate(chunk) if line.startswith("|")]
        body = chunk
        if table:
            first, last = table[0], table[-1]
            if table != list(range(first, last + 1)):
                raise SystemExit(f"section '{heading}' has more than one table")
            header, row_lines = chunk[first : first + 2], chunk[first + 2 : last + 1]
            if not row_lines:
                raise SystemExit(f"section '{heading}' has a table with no rows")
            body = chunk[:first] + header + [ROWS + "\n"] + chunk[last + 1 :]
            used: set[str] = set()
            for index, row in enumerate(row_lines, 1):
                cell = row.strip().strip("|").split("|", 1)[0]
                name = _slug(cell)
                unique, n = name, 2
                while unique in used:
                    unique, n = f"{name}-{n}", n + 1
                used.add(unique)
                files[f"{directory}/{index * 10:04d}-{unique}.md"] = (
                    row if row.endswith("\n") else row + "\n"
                )
        files[f"{directory}/{SECTION}"] = "".join(body)
    return files


def write_split(text: str, root: Path = SOURCES) -> None:
    # Replace the section directories; keep capabilities/README.md.
    for directory in (p for p in root.iterdir() if p.is_dir()) if root.exists() else ():
        shutil.rmtree(directory)
    for relative, content in split(text).items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, "utf-8")


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #
def _stamp(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest() + "\n"


def generated() -> dict[Path, str]:
    """Each generated, committed file and what it should hold now."""
    page = assemble()
    return {
        OUT: page,
        STAMP: _stamp(page),
        ROOT / "llms-full.txt": _load("build_llms_full").build(),
    }


def guard() -> int:
    """Fail when CAPABILITIES.md changed since this script last wrote it."""
    page = OUT.read_text("utf-8")
    if STAMP.exists() and STAMP.read_text("utf-8") == _stamp(page):
        return 0
    assembled = assemble()
    if page == assembled:
        # Regenerated outside this script (or the stamp was not updated):
        # the page matches its sources, so the refresh drops nothing.
        return 0
    sources = set(assembled.splitlines())
    extra = [line for line in page.splitlines() if line not in sources]
    print(
        "CAPABILITIES.md was changed by something other than "
        "scripts/build_capabilities.py, so regenerating it would drop that "
        "change. Move each line below into its row file (or section.md), then "
        "regenerate the page in the same pull request. Lines on the page "
        "that no source produces:",
        file=sys.stderr,
    )
    for line in extra or ["(none: the page only trails its sources; regenerate it)"]:
        print(f"  {line[:200]}", file=sys.stderr)
    return 1


def _changed_since(base: str, path: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(ROOT), "diff", "--quiet", base, "--", str(_rel(path))],
        check=False,
    )
    if result.returncode not in (0, 1):
        raise SystemExit(f"git diff {base} failed")
    return result.returncode == 1


def main(argv: list[str]) -> int:
    if "--split" in argv:
        write_split(OUT.read_text("utf-8"))
        if assemble() != OUT.read_text("utf-8"):
            raise SystemExit("the split does not assemble back to CAPABILITIES.md")
        STAMP.write_text(_stamp(assemble()), "utf-8")
        print(f"wrote {SOURCES.relative_to(ROOT)}/ from CAPABILITIES.md")
        return 0
    if "--guard" in argv:
        return guard()
    if "--check-pr" in argv:
        base = argv[argv.index("--check-pr") + 1]
        bad = []
        for path, text in generated().items():
            current = path.read_text("utf-8") if path.exists() else ""
            if current != text and _changed_since(base, path):
                bad.append(_rel(path))
        for path in bad:
            print(
                f"{path} is generated and this change edits it by hand. Make the "
                "change in its sources instead (a row file under capabilities/, "
                "capabilities/*/section.md, README.md, ...), then undo the edit: "
                f"git checkout {base} -- {path}. A branch from before the split "
                "can run python scripts/migrate_branch_to_split_files.py during "
                "its merge of main.",
                file=sys.stderr,
            )
        return 1 if bad else 0
    text = assemble()
    if "--check" in argv:
        current = STAMP.read_text("utf-8") if STAMP.exists() else ""
        if OUT.read_text("utf-8") != text or current != _stamp(text):
            print(
                "CAPABILITIES.md is out of date; run python scripts/build_capabilities.py",
                file=sys.stderr,
            )
            return 1
        return 0
    OUT.write_text(text, "utf-8")
    STAMP.write_text(_stamp(text), "utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
