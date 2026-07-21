#!/usr/bin/env python3
"""Claims-consistency gate: stop the docs from drifting away from the code.

The README once claimed "vision-only", "864 tests", and "adapters to come"
long after each became false. This script makes those drifts a hard CI failure
instead of a thing a reader eventually notices. It enforces four invariants:

* **version** — ``openadapt_flow.__version__`` and the editable root package in
  ``uv.lock`` equal the version in ``pyproject.toml`` (the drift the clean-wheel
  job catches at runtime, caught here at doc/lint speed too).
* **paths** — every local file path referenced by a markdown link or a
  backticked ``a/b.ext`` token in ``README.md`` / ``DESIGN.md`` /
  ``docs/LIMITS.md``, and every such path in a ``.github/workflows/*.yml``
  comment, actually exists.
* **banned phrases** — stale or unbounded claims the README must never carry
  again (including substrate universals and single-demonstration guarantees).
* **test count** — the README deliberately carries NO hardcoded test count (a
  number that rots on every test added). If someone reintroduces one, it is
  checked against ``pytest --collect-only`` and must match within a tolerance.

Run standalone (exit 0 = consistent, 1 = drift found)::

    python scripts/check_consistency.py

The individual checks are importable so ``tests/test_consistency.py`` can drive
them with controlled inputs (and so the expensive test-collection path stays
injectable, never spawning pytest-inside-pytest).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 compatibility
    import tomli as tomllib

REPO_ROOT = Path(__file__).resolve().parent.parent

# Docs whose file references and (for the README) claims are governed here.
DOC_FILES = ["README.md", "DESIGN.md", "docs/LIMITS.md"]
README = "README.md"

# Stale or unbounded claims the README must never carry again. Lowercased
# substring match. Substrate maturity belongs in the evidence matrix; the
# opening must not silently turn intended scope into universal availability.
BANNED_PHRASES = [
    "vision-only",
    "adapters to come",
    "864 tests",
    "any repeated gui task",
    "demonstrated once",
    "replays exactly",
]

# A referenced path is only checked when it ends in one of these (prose slashes
# like "O/0" or "20/20" carry no extension and are ignored), or is a directory
# glob (``a/b/**``) / trailing-slash dir (``a/b/``).
PATH_EXTS = (
    "py md txt rst json jsonl yaml yml toml cfg ini png gif jpg jpeg svg html js css sh"
).split()
_EXT_RE = "|".join(PATH_EXTS)

# A clean intra-repo path token MUST contain a slash (so a bare ``REPORT.md``
# format sketch or a prose word is never mistaken for a repo file) and be built
# only of word chars, dot, dash, slash. Placeholder tokens (``<id>``, ``{i}``,
# ``…``) fail this and are ignored.
_CLEAN_PATH = re.compile(r"^[\w.\-]+(?:/[\w.\-]+)+$")
# A path candidate: ends in a known extension.
_PATH_WITH_EXT = re.compile(r"^.+\.(?:" + _EXT_RE + r")$")

_MD_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
_BACKTICK = re.compile(r"`([^`]+)`")
_URL_SCHEME = re.compile(r"^[a-z][a-z0-9+.\-]*://|^mailto:", re.IGNORECASE)


def _clean_token(raw: str) -> str:
    """Strip surrounding brackets/quotes and trailing punctuation, but keep a
    leading dot so ``.github/dependabot.yml`` survives."""
    tok = raw.strip("()[]{}<>\"'")
    return tok.rstrip(".,:;")


def read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# version
# --------------------------------------------------------------------------- #
def pyproject_version() -> str:
    data = tomllib.loads(read("pyproject.toml"))
    return data["project"]["version"]


def package_version() -> str:
    # Read the literal from source without importing the (heavy) package.
    text = read("openadapt_flow/__init__.py")
    m = re.search(r"""__version__\s*=\s*["']([^"']+)["']""", text)
    if not m:
        raise AssertionError("openadapt_flow/__init__.py has no __version__")
    return m.group(1)


def lock_version() -> str:
    data = tomllib.loads(read("uv.lock"))
    roots = [
        package
        for package in data.get("package", [])
        if package.get("name") == "openadapt-flow"
        and package.get("source") == {"editable": "."}
    ]
    if len(roots) != 1:
        raise AssertionError(
            "uv.lock must contain exactly one editable openadapt-flow root package"
        )
    return roots[0]["version"]


def check_version(pkg: Optional[str] = None, toml: Optional[str] = None) -> list[str]:
    pkg = package_version() if pkg is None else pkg
    toml = pyproject_version() if toml is None else toml
    if pkg != toml:
        return [
            f"version drift: openadapt_flow.__version__={pkg!r} != "
            f"pyproject [project].version={toml!r}"
        ]
    return []


def check_lock_version(
    lock: Optional[str] = None, toml: Optional[str] = None
) -> list[str]:
    lock = lock_version() if lock is None else lock
    toml = pyproject_version() if toml is None else toml
    if lock != toml:
        return [
            f"version drift: uv.lock editable openadapt-flow={lock!r} != "
            f"pyproject [project].version={toml!r}"
        ]
    return []


# --------------------------------------------------------------------------- #
# banned phrases
# --------------------------------------------------------------------------- #
def check_banned_phrases(readme_text: Optional[str] = None) -> list[str]:
    text = read(README) if readme_text is None else readme_text
    low = text.lower()
    return [
        f"README.md contains banned stale phrase {phrase!r}"
        for phrase in BANNED_PHRASES
        if phrase in low
    ]


# --------------------------------------------------------------------------- #
# test count (README should omit a hard number; if present it must be accurate)
# --------------------------------------------------------------------------- #
_TEST_COUNT = re.compile(r"(\d[\d,]*)\s+tests\b", re.IGNORECASE)


def collected_test_count() -> int:
    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    ).stdout
    m = re.search(r"(\d+)\s+tests? collected", out)
    if not m:
        raise AssertionError(
            "could not parse test count from `pytest --collect-only -q`"
        )
    return int(m.group(1))


def check_test_count(
    readme_text: Optional[str] = None,
    count_fn: Callable[[], int] = collected_test_count,
    tolerance: int = 25,
) -> list[str]:
    text = read(README) if readme_text is None else readme_text
    numbers = [int(m.group(1).replace(",", "")) for m in _TEST_COUNT.finditer(text)]
    if not numbers:
        # The chosen, drift-proof state: no hardcoded count to rot.
        return []
    actual = count_fn()  # only pay for collection when a number is present
    errors = []
    for n in numbers:
        if abs(n - actual) > tolerance:
            errors.append(
                f"README.md claims {n} tests but `pytest --collect-only` "
                f"found {actual} (tolerance {tolerance}). Prefer omitting the "
                f"number entirely."
            )
    return errors


# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #
def _path_candidates_from_text(text: str) -> list[tuple[str, bool]]:
    """Return ``(candidate, from_link)`` pairs. ``from_link`` markdown targets
    may be extension-less dirs; backtick tokens must carry an extension/glob."""
    out: list[tuple[str, bool]] = []
    for m in _MD_LINK.finditer(text):
        target = m.group(1).strip().split()[0]  # drop optional "title"
        target = target.split("#", 1)[0]
        if target and not _URL_SCHEME.match(target) and not target.startswith("#"):
            out.append((target, True))
    for m in _BACKTICK.finditer(text):
        for raw in re.split(r"[\s,;]+", m.group(1)):
            tok = _clean_token(raw)
            if tok:
                out.append((tok, False))
    return out


def _comment_path_candidates(yml_text: str) -> list[tuple[str, bool]]:
    out: list[tuple[str, bool]] = []
    for line in yml_text.splitlines():
        if "#" not in line:
            continue
        comment = line.split("#", 1)[1]
        for raw in re.split(r"[\s,;()]+", comment):
            tok = _clean_token(raw)
            if tok:
                out.append((tok, False))
    return out


def _is_path_like(cand: str, from_link: bool) -> Optional[str]:
    """Return the on-disk path to check, or None if ``cand`` is not a repo path."""
    if _URL_SCHEME.match(cand):
        return None
    is_glob = cand.endswith(("/**", "/*"))
    is_dir_slash = cand.endswith("/")
    norm = cand
    for suffix in ("/**", "/*", "/"):
        if norm.endswith(suffix):
            norm = norm[: -len(suffix)]
            break
    if not norm or not _CLEAN_PATH.match(norm):  # requires an interior slash
        return None
    # Globs (``a/b/**``) are unambiguous path references anywhere. An
    # extension-less or trailing-slash dir is only trusted from a markdown link
    # (intentional), never from backtick prose (``save/submit/create/delete/``).
    # A backtick/comment token must carry a known file extension.
    if is_glob:
        return norm
    if _PATH_WITH_EXT.match(norm):
        return norm
    if from_link and not is_dir_slash:
        return norm  # extension-less dir target of a link, e.g. docs/showcase
    if from_link and is_dir_slash:
        return norm
    return None


def _exists(norm: str, doc_dir: Path) -> bool:
    return (REPO_ROOT / norm).exists() or (doc_dir / norm).exists()


def check_paths() -> list[str]:
    errors: list[str] = []
    for rel in DOC_FILES:
        doc_dir = (REPO_ROOT / rel).parent
        for cand, from_link in _path_candidates_from_text(read(rel)):
            norm = _is_path_like(cand, from_link)
            if norm and not _exists(norm, doc_dir):
                errors.append(f"{rel} references missing path: {cand!r}")
    wf_dir = REPO_ROOT / ".github" / "workflows"
    for yml in sorted(wf_dir.glob("*.yml")):
        for cand, from_link in _comment_path_candidates(
            yml.read_text(encoding="utf-8")
        ):
            norm = _is_path_like(cand, from_link)
            if norm and not _exists(norm, wf_dir):
                errors.append(
                    f".github/workflows/{yml.name} comment references missing "
                    f"path: {cand!r}"
                )
    return errors


# --------------------------------------------------------------------------- #
# aggregate
# --------------------------------------------------------------------------- #
def run_all_checks() -> list[str]:
    errors: list[str] = []
    errors += check_version()
    errors += check_lock_version()
    errors += check_banned_phrases()
    errors += check_test_count()
    errors += check_paths()
    return errors


def main() -> int:
    errors = run_all_checks()
    if errors:
        print("Consistency gate FAILED:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("Consistency gate passed: versions, paths, phrases, and test count OK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
