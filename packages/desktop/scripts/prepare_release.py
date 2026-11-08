#!/usr/bin/env python3
"""Prepare an openadapt-desktop release in the pull request that ships it.

In the OpenAdapt monorepo, Desktop has no bot version commit. The pull request
that should ship sets the version and the changelog itself, and
.github/workflows/desktop-release.yml publishes that version once it merges.
This script makes that one step:

    python scripts/prepare_release.py            # bump from the commits
    python scripts/prepare_release.py --bump minor
    python scripts/prepare_release.py --version 0.21.0

It reads the conventional commits under packages/desktop since the version
last changed (feat -> minor, fix or perf -> patch; a breaking change is minor
while the version is 0.x), then writes the new version to pyproject.toml,
openadapt_desktop/__init__.py, uv.lock and the five native installer version
files, and adds a dated section at the top of CHANGELOG.md.
"""

from __future__ import annotations

import argparse
import datetime
import difflib
import re
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_release_consistency  # noqa: E402
import native_release  # noqa: E402

VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
SUBJECT = re.compile(r"^(?P<type>[a-z]+)(?:\((?P<scope>[^)]*)\))?(?P<bang>!)?: (?P<text>.+)$")
LEVELS = ("patch", "minor", "major")
SECTIONS = (("feat", "Features"), ("fix", "Bug Fixes"), ("perf", "Performance Improvements"))
# Paths (relative to packages/desktop) that never reach the wheel, the sdist, or
# an installer. A feat, fix, or perf commit confined to them needs no release.
# Every other path counts as shipped, so an unlisted one fails closed.
NOT_SHIPPED_PREFIXES = ("tests/", "docs/", ".github/")
NOT_SHIPPED_SUFFIXES = (".md",)


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str
    body: str
    paths: tuple[str, ...] = ()

    @property
    def ships(self) -> bool:
        """Whether the commit changes anything that reaches a release."""

        return not self.paths or any(
            not path.startswith(NOT_SHIPPED_PREFIXES) and not path.endswith(NOT_SHIPPED_SUFFIXES)
            for path in self.paths
        )

    @property
    def parsed(self) -> re.Match[str] | None:
        return SUBJECT.match(self.subject)

    @property
    def breaking(self) -> bool:
        match = self.parsed
        return bool(match and match.group("bang")) or "BREAKING CHANGE:" in self.body


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True).stdout.decode(
        "utf-8"
    )


def parse_version(version: str) -> tuple[int, int, int]:
    match = VERSION.fullmatch(version)
    if match is None:
        raise ValueError(f"version must be X.Y.Z, got {version!r}")
    return int(match[1]), int(match[2]), int(match[3])


def bump(version: str, level: str) -> str:
    major, minor, patch = parse_version(version)
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    if level == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(f"unknown bump level {level!r}")


def commits_between(root: Path, base: str, head: str = "HEAD") -> list[Commit]:
    """Return non-merge commits in ``base..head`` that touch this package."""

    raw = _git(
        root,
        "log",
        "--no-merges",
        "--relative",
        "--name-only",
        "--format=%x1e%H%x00%s%x00%b%x00",
        f"{base}..{head}",
        "--",
        ".",
    )
    commits = []
    for record in raw.split("\x1e"):
        if not record.strip():
            continue
        sha, subject, body, names = (record.split("\x00") + ["", "", ""])[:4]
        paths = tuple(line for line in names.splitlines() if line.strip())
        commits.append(Commit(sha=sha.strip(), subject=subject.strip(), body=body, paths=paths))
    return commits


def _nul_paths(raw: str) -> set[str]:
    """Parse exact Git names, including embedded newlines and tabs."""
    if not raw:
        return set()
    if not raw.endswith("\0"):
        raise ValueError("Git did not return complete NUL-separated paths")
    paths = set(raw[:-1].split("\0"))
    if any(not p or Path(p).is_absolute() or ".." in Path(p).parts for p in paths):
        raise ValueError("Git returned a path outside Desktop")
    return paths


def _path_state(root: Path, ref: str, path: str) -> tuple[str, str] | None:
    # Read only tree metadata. A mode change matters even with the same blob.
    raw = _git(root, "--literal-pathspecs", "ls-tree", "-z", ref, "--", path)
    if not raw:
        return None
    records = raw.split("\0")
    if len(records) != 2 or records[-1]:
        raise ValueError("a net Desktop path has ambiguous tree metadata")
    metadata, name = records[0].split("\t", 1)
    mode, _kind, oid = metadata.split()
    if name != path:
        raise ValueError("Git returned tree metadata for another path")
    return mode, oid


def _clean_path_merge(root: Path, path: str, parents: tuple[str, ...]) -> tuple[str, str] | None:
    """Reconstruct one path using Git's text three-way merge, without attributes."""
    if len(parents) != 2:
        raise ValueError("a net Desktop merge has unsupported parent metadata")
    bases = _git(root, "merge-base", "--all", *parents).splitlines()
    if len(bases) != 1:
        raise ValueError("a net Desktop merge has no unique merge base")
    ancestor = _path_state(root, bases[0], path)
    ours, theirs = (_path_state(root, parent, path) for parent in parents)
    if ours == theirs or theirs == ancestor:
        return ours
    if ours == ancestor:
        return theirs
    if any(
        value is None or value[0] not in {"100644", "100755"} for value in (ancestor, ours, theirs)
    ):
        raise ValueError("a net Desktop merge cannot be reconstructed from regular files")
    assert ancestor is not None and ours is not None and theirs is not None
    modes = (ancestor[0], ours[0], theirs[0])
    if modes[1] == modes[2] or modes[2] == modes[0]:
        mode = modes[1]
    elif modes[1] == modes[0]:
        mode = modes[2]
    else:
        raise ValueError("a net Desktop merge has unexplained file modes")
    with tempfile.TemporaryDirectory(prefix="desktop-release-merge-") as directory:
        files = []
        for label, value in (("ours", ours), ("base", ancestor), ("theirs", theirs)):
            size = int(_git(root, "cat-file", "-s", value[1]).strip())
            if not 0 <= size <= 8 * 1024 * 1024:
                raise ValueError("a net Desktop merge blob exceeds its reconstruction bound")
            content = subprocess.run(
                ["git", "cat-file", "blob", value[1]], cwd=root, check=True, capture_output=True
            ).stdout
            if len(content) != size or b"\0" in content:
                raise ValueError("a net Desktop merge has non-text or incomplete blob data")
            target = Path(directory) / label
            target.write_bytes(content)
            files.append(str(target))
        merged = subprocess.run(
            ["git", "merge-file", "--stdout", *files], cwd=root, capture_output=True
        )
        if merged.returncode != 0:
            raise ValueError("a net Desktop merge resolution has no clean combination")
    oid = (
        subprocess.run(
            ["git", "hash-object", "--stdin"],
            cwd=root,
            input=merged.stdout,
            check=True,
            capture_output=True,
        )
        .stdout.decode("ascii")
        .strip()
    )
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid) is None:
        raise ValueError("a net Desktop merge result has incomplete object metadata")
    return mode, oid


def commits_for_net_diff(root: Path, base: str, head: str) -> list[Commit]:
    """Classify only changes that remain in the squash-merged tree.

    Exact per-file reversions discard the canceled intent. Copies inherit
    intent from earlier equal tree versions, whether or when the source is
    deleted. A content change without an exact source keeps possible current
    source intent conservatively. A merge may carry the exact clean three-way
    combination of its parents; unexplained resolution bytes still refuse.
    No similarity heuristic is used. Historical release
    preparation stays unchanged.
    """
    net = _nul_paths(
        _git(root, "diff", "--no-renames", "--relative", "--name-only", "-z", base, head, "--", ".")
    )
    if not net:
        return []
    commits = commits_between(root, base, head)
    by_sha = {commit.sha: commit for commit in commits}
    order = _git(
        root, "rev-list", "--no-merges", "--topo-order", "--reverse", f"{base}..{head}", "--", "."
    ).splitlines()
    if len(by_sha) != len(commits) or set(order) != set(by_sha):
        raise ValueError("net Desktop changes have incomplete commit metadata")
    changed = {
        sha: _nul_paths(
            _git(
                root,
                "diff-tree",
                "--no-commit-id",
                "--no-renames",
                "--relative",
                "--name-only",
                "-z",
                "-r",
                sha,
                "--",
                ".",
            )
        )
        for sha in order
    }
    # Keep actual merge edges as well as ordinary commits. Main's side is
    # ambient because base..head already excludes its non-merge commits.
    history = [
        line.split()
        for line in _git(
            root, "rev-list", "--parents", "--topo-order", "--reverse", f"{base}..{head}"
        ).splitlines()
    ]
    # Use one topological event order for ordinary commits and merge sources.
    order = [row[0] for row in history if row[0] in by_sha]
    rank = {sha: index for index, sha in enumerate(order)}
    ancestor_cache: dict[str, set[str]] = {}
    merge_cache: dict[tuple[str, str], tuple[str, str] | None] = {}
    history_rank = {row[0]: index for index, row in enumerate(history)}
    # Combined tree metadata includes novel merge bytes but excludes files
    # merely adopted unchanged from an ambient parent. Git's combined output
    # does not honor --relative consistently; normalize an explicit root path.
    prefix = _git(root, "rev-parse", "--show-prefix").removesuffix("\n")
    merge_changed = {}
    for row in history:
        if len(row) <= 2:
            continue
        names = _nul_paths(
            _git(
                root,
                "diff-tree",
                "--cc",
                "--no-commit-id",
                "--no-renames",
                "--no-relative",
                "--name-only",
                "-z",
                "-r",
                row[0],
                "--",
                ".",
            )
        )
        if any(not name.startswith(prefix) for name in names):
            raise ValueError("Git returned a merge source outside Desktop")
        merge_changed[row[0]] = {name[len(prefix) :] for name in names}
    states_cache: dict[tuple[str, str], tuple[str, str] | None] = {}
    memo: dict[tuple[str, int, str], set[str]] = {}
    blob_cache: dict[str, bytes] = {}

    def state(ref: str, path: str) -> tuple[str, str] | None:
        key = (ref, path)
        if key not in states_cache:
            states_cache[key] = _path_state(root, ref, path)
        return states_cache[key]

    def content(oid: str) -> bytes:
        if oid not in blob_cache:
            size = int(_git(root, "cat-file", "-s", oid).strip())
            if not 0 <= size <= 8 * 1024 * 1024:
                raise ValueError("a Desktop copy source exceeds its content bound")
            raw = subprocess.run(
                ["git", "cat-file", "blob", oid], cwd=root, check=True, capture_output=True
            ).stdout
            if len(raw) != size:
                raise ValueError("a Desktop copy source has incomplete content")
            blob_cache[oid] = raw
        return blob_cache[oid]

    delta_cache: dict[tuple[tuple[str, str] | None, tuple[str, str]], tuple[bytes, ...]] = {}

    def added_lines(before: tuple[str, str] | None, after: tuple[str, str]) -> tuple[bytes, ...]:
        key = (before, after)
        if key not in delta_cache:
            old = content(before[1]).splitlines() if before is not None else []
            new = content(after[1]).splitlines()
            diff = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
            delta_cache[key] = tuple(
                line
                for kind, _i, _j, start, end in diff.get_opcodes()
                if kind in {"insert", "replace"}
                for line in new[start:end]
            )
        return delta_cache[key]

    def related_content(
        source_path: str, source_ref: str, before: tuple[str, str] | None, after: tuple[str, str]
    ) -> bool:
        source = state(source_ref, source_path)
        assert source is not None
        if source[1] == after[1]:
            return True
        source_base = state(historical_base(source_ref), source_path)
        values = (source_base, source, before, after)
        if any(value is not None and value[0] not in {"100644", "100755"} for value in values):
            return True
        if any(value is not None and b"\0" in content(value[1]) for value in values):
            return True
        # Shared ambient text is not evidence that branch intent was copied.
        # Compare the source's contributed bytes with the destination edit.
        destination_delta = b"\n".join(added_lines(before, after))
        if any(
            line.strip() and line in destination_delta for line in added_lines(source_base, source)
        ):
            return True
        if source_base is None:
            return False
        # A removal contributes an absence. Require the complete surviving
        # source component in destination order, not a shared ambient line.
        # The removed span must remain absent, and an existing destination
        # must remove it in its own edit rather than merely lack it already.
        kept = content(source[1]).splitlines()
        source_counts = Counter(content(source_base[1]).splitlines())
        removed = source_counts - Counter(kept)
        if not removed:
            return False
        destination = content(after[1]).splitlines()
        destination_counts = Counter(destination)
        if any(
            destination_counts[line] > source_counts[line] - count
            for line, count in removed.items()
        ):
            return False
        if before is not None:
            before_counts = Counter(content(before[1]).splitlines())
            if any(
                before_counts[line] - destination_counts[line] < count
                for line, count in removed.items()
            ):
                return False

        def contains_ordered(needles: list[bytes], lines: list[bytes]) -> bool:
            remaining = iter(lines)
            return all(any(value == needle for value in remaining) for needle in needles)

        if not any(line.strip() for line in kept):
            # Empty/blank surviving payload cannot disprove a source edge;
            # retain its strict recursive lineage as for opaque binary data.
            return True
        return contains_ordered(kept, destination)

    # All changed paths matter, including a feature's original location that
    # disappears from the final diff. Version references are ordered before a
    # consumer, so recursive lineage cannot cycle or depend on a later commit.
    paths = sorted(set().union(*changed.values(), *merge_changed.values()))
    versions = []
    source_stop = 0
    for row in history:
        sha = row[0]
        if sha in rank:
            source_stop += 1
        for path in sorted(changed.get(sha, merge_changed.get(sha, set()))):
            versions.append((path, source_stop, sha, state(sha, path)))

    def sources(
        path: str, index: int, sha: str, after: tuple[str, str]
    ) -> set[tuple[str, int, str]]:
        current = {
            (source, index, sha + "^")
            for source in paths
            if source != path and state(sha + "^", source) is not None
        }
        exact = {
            (source, stop, ref)
            for source, stop, ref, version in versions
            if source != path
            and stop <= index
            and history_rank[ref] < history_rank[sha]
            and version == after
        }
        exact.update(source for source in current if state(source[2], source[0]) == after)
        # Removing a source in the same commit is a conservative move edge,
        # even when its destination was rewritten completely. Other inexact
        # sources need actual contributed content, not shared ambient text.
        removed = {
            candidate
            for candidate in current
            if candidate[0] in changed[sha] and state(sha, candidate[0]) is None
        }
        if exact:
            return exact | removed
        return removed | {
            candidate
            for candidate in current
            if related_content(candidate[0], candidate[2], state(sha + "^", path), after)
        }

    historical_bases: dict[str, str] = {}

    def historical_base(ref: str) -> str:
        # A recursive copy source can precede a merge from base. Its original
        # ambient tree is then their shared ancestor, not the later base tree.
        # Top-level head still starts at base. Merge-only changes with no
        # non-merge contributor continue to refuse below.
        if ref not in historical_bases:
            ancestors = _git(root, "merge-base", "--all", base, ref).splitlines()
            if len(ancestors) != 1:
                raise ValueError("historical Desktop lineage has no unique merge base")
            historical_bases[ref] = ancestors[0]
        return historical_bases[ref]

    def contributions(path: str, stop: int, final_ref: str) -> set[str]:
        key = (path, stop, final_ref)
        if key in memo:
            return memo[key]
        initial, final = state(historical_base(final_ref), path), state(final_ref, path)
        if initial == final:
            memo[key] = set()
            return set()
        if final_ref not in ancestor_cache:
            ancestor_cache[final_ref] = set(
                _git(root, "rev-list", final_ref, "^" + historical_base(final_ref)).splitlines()
            )
        ancestors = ancestor_cache[final_ref]
        contributors = [
            (i, sha)
            for i, sha in enumerate(order[:stop])
            if sha in ancestors and path in changed[sha]
        ]
        if not contributors:
            raise ValueError("a net Desktop change has no non-merge commit metadata")
        # Branch commits can precede main's ambient edits. Replay their
        # exact first-parent chain from its own starting tree, then validate
        # the merge that adds the ambient side. Starting from newer base
        # would turn ordinary reversions into ambiguous parallel history.
        chain_initial = state(contributors[0][1] + "^", path)
        states = [chain_initial]
        edges: list[set[str]] = []
        observed = [initial, chain_initial]
        possible: set[str] = set()
        ambiguous = False
        for row in history:
            sha, *parents_list = row
            if sha not in ancestors:
                continue
            if sha in rank:
                index = rank[sha]
                if index >= stop or path not in changed[sha]:
                    continue
                before, after = state(sha + "^", path), state(sha, path)
                # A copy carries earlier shipped intent; it must not promote
                # docs/test-only intent into a releasable commit retroactively.
                intent = {sha} if Commit(sha, "", "", (path,)).ships else set()
                if after is not None and (before is None or before[1] != after[1]):
                    for source, source_stop, ref in sources(path, index, sha, after):
                        intent |= contributions(source, source_stop, ref)
            elif len(parents_list) > 1:
                parents = tuple(parents_list)
                before, after = state(parents[0], path), state(sha, path)
                if before == after or after == states[-1]:
                    continue
                # An exact already-observed parent version is attributable,
                # as before. A new combined blob needs independent reconstruction.
                parent_states = {state(parent, path) for parent in parents}
                if after not in parent_states or after not in observed:
                    merge_key = (sha, path)
                    if merge_key not in merge_cache:
                        merge_cache[merge_key] = _clean_path_merge(root, path, parents)
                    if merge_cache[merge_key] != after:
                        raise ValueError("a net Desktop merge resolution has unexplained content")
                intent = set()
            else:
                continue
            observed.append(after)
            possible |= intent
            if before != states[-1]:
                ambiguous = True
                continue
            if after in states:
                index = states.index(after)
                states, edges = states[: index + 1], edges[:index]
            else:
                states.append(after)
                edges.append(intent)
        if (not ambiguous and final != states[-1]) or (ambiguous and final not in observed):
            raise ValueError("a net Desktop merge resolution has no commit intent")
        result = possible if ambiguous else set().union(*edges)
        memo[key] = result
        return result

    surviving: dict[str, set[str]] = {sha: set() for sha in order}
    for path in sorted(net):
        if state(base, path) == state(head, path):
            raise ValueError("a net Desktop path does not match exact tree metadata")
        # Reuse the existing shipped-path policy. Version/changelog guards
        # remain independent, and a non-shipped path used as a copy source
        # still takes the full lineage/reconstruction path above.
        if not Commit("", "", "", (path,)).ships:
            continue
        for sha in contributions(path, len(order), head):
            surviving[sha].add(path)
    return [
        Commit(commit.sha, commit.subject, commit.body, tuple(sorted(surviving[commit.sha])))
        for commit in commits
        if surviving[commit.sha]
    ]


def required_level(commits: list[Commit], version: str) -> str | None:
    """Return the bump these commits need, or None when nothing releasable."""

    level = None
    for commit in commits:
        if not commit.ships:
            continue
        match = commit.parsed
        kind = match.group("type") if match else None
        if commit.breaking:
            candidate = "major" if parse_version(version)[0] > 0 else "minor"
        elif kind == "feat":
            candidate = "minor"
        elif kind in {"fix", "perf"}:
            candidate = "patch"
        else:
            continue
        if level is None or LEVELS.index(candidate) > LEVELS.index(level):
            level = candidate
    return level


def last_version_change(root: Path) -> str:
    """Return the newest commit that changed the project version."""

    sha = _git(root, "log", "-1", "--format=%H", "-G", r'^version = "', "--", "pyproject.toml")
    sha = sha.strip()
    if not sha:
        raise ValueError("no commit changed the version in pyproject.toml")
    return sha


def changelog_section(version: str, commits: list[Commit], today: datetime.date) -> str:
    lines = [f"## v{version} ({today.isoformat()})", ""]
    breaking = [commit for commit in commits if commit.breaking]
    if breaking:
        lines += ["### Breaking Changes", ""]
        lines += [f"- {commit.subject}" for commit in breaking]
        lines.append("")
    for kind, title in SECTIONS:
        entries = []
        for commit in commits:
            match = commit.parsed
            if match and match.group("type") == kind:
                scope = match.group("scope")
                text = match.group("text")
                entries.append(f"- **{scope}**: {text}" if scope else f"- {text}")
        if entries:
            lines += [f"### {title}", ""] + entries + [""]
    if len(lines) == 2:
        lines += ["- Maintenance release.", ""]
    return "\n".join(lines) + "\n"


def write_changelog(root: Path, section: str) -> None:
    path = root / "CHANGELOG.md"
    text = path.read_text(encoding="utf-8")
    header = "# CHANGELOG\n"
    if not text.startswith(header):
        raise ValueError("CHANGELOG.md must start with '# CHANGELOG'")
    rest = text[len(header) :].lstrip("\n")
    path.write_text(f"{header}\n\n{section}\n\n{rest}", encoding="utf-8")


def set_engine_version(root: Path, version: str) -> None:
    pyproject = root / "pyproject.toml"
    text, count = re.subn(
        r'(\[project\]\s+name = "openadapt-desktop"\s+version = ")[^"]+(")',
        rf"\g<1>{version}\g<2>",
        pyproject.read_text(encoding="utf-8"),
        count=1,
    )
    if count != 1:
        raise ValueError("could not set the version in pyproject.toml")
    pyproject.write_text(text, encoding="utf-8")
    module = root / "openadapt_desktop" / "__init__.py"
    text, count = re.subn(
        r'(?m)^__version__ = "[^"]+"$',
        f'__version__ = "{version}"',
        module.read_text(encoding="utf-8"),
        count=1,
    )
    if count != 1:
        raise ValueError("could not set __version__ in openadapt_desktop/__init__.py")
    module.write_text(text, encoding="utf-8")
    check_release_consistency.sync_lock_version(root)
    native_release.set_native_version(version, root)


def prepare(
    root: Path,
    *,
    level: str | None = None,
    version: str | None = None,
    today: datetime.date | None = None,
) -> str:
    current = check_release_consistency.release_versions(root)["pyproject.toml"]
    commits = commits_between(root, last_version_change(root))
    if version is None:
        level = level or required_level(commits, current)
        if level is None:
            raise ValueError(
                "no feat, fix, perf or breaking commit since the last version change; "
                "pass --bump or --version to release anyway"
            )
        version = bump(current, level)
    if parse_version(version) <= parse_version(current):
        raise ValueError(f"version {version} does not advance {current}")
    set_engine_version(root, version)
    write_changelog(root, changelog_section(version, commits, today or datetime.date.today()))
    return version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--bump", choices=LEVELS)
    group.add_argument("--version")
    args = parser.parse_args(argv)
    try:
        version = prepare(ROOT, level=args.bump, version=args.version)
    except ValueError as exc:
        parser.exit(1, f"error: {exc}\n")
    print(f"Prepared openadapt-desktop {version}. Review CHANGELOG.md, then commit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
