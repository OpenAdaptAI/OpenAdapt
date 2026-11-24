# vendored from 9b6956be37dd:packages/flow/openadapt_flow/runtime/effects/versions.py, lines 152-386, for the harness self-test only
# ruff: noqa: F401, F841, I001
"""judge_versions before it moved onto the kernel, for the differential
test (test_positive_control_migration.py).

Only the lines between ``# fmt: off`` and ``# fmt: on`` markers that
follow a ``# vendored:`` comment are verbatim; the imports and stubs above
them are not."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any, Callable, Optional

from openadapt_flow.runtime.effects.versions import (
    _WHOLE,
    FrameVersions,
    VersionStatus,
    VersionVerdict,
)


def verdict_site(*_a: Any, **_k: Any) -> Callable[[Any], Any]:
    return lambda fn: fn


# vendored: 9b6956be37dd:packages/flow/openadapt_flow/runtime/effects/versions.py 152-386
# fmt: off
def _get(row: Mapping[str, Any], path: str) -> Any:
    if path in row:
        return row[path]
    node: Any = row
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    return text if text != "" else None


def _mac(key: bytes, label: str, *parts: str) -> str:
    body = "\x1f".join((label, *parts)).encode("utf-8")
    return hashlib.blake2b(body, key=key[:64], digest_size=16).hexdigest()


def seal_versions(
    spec: FrameVersions, rows: Optional[Sequence[Mapping[str, Any]]], key: bytes
) -> dict[str, Any]:
    """The baseline's record of versions: per record MAC, the version MACs
    and (whole numbers only) the highest version."""

    if rows is None:
        return {"reachable": False}
    records: dict[str, dict[str, Any]] = {}
    for row in rows:
        record = _text(_get(row, spec.record_field))
        version = _text(_get(row, spec.version_field))
        if record is None:
            continue  # a row that names no record is no record's version
        entry = records.setdefault(
            _mac(key, "record", record),
            {"versions": [], "max": None, "whole": True, "null": False, "dup": False},
        )
        if version is None:
            # A null version can't tell two saves apart.
            entry["whole"] = False
            entry["null"] = True
            continue
        mac = _mac(key, "version", record, version)
        if mac in entry["versions"]:
            entry["dup"] = True  # two rows, one version: a save could hide
        entry["versions"].append(mac)
        if _WHOLE.match(version):
            entry["max"] = max(int(version), entry["max"] or 0)
        else:
            entry["whole"] = False
    for entry in records.values():
        entry["versions"] = sorted(set(entry["versions"]))
    return {"reachable": True, "records": records}


def _resolve_self(
    spec: FrameVersions, params: Mapping[str, object]
) -> Optional[set[str]]:
    out: set[str] = set()
    for expr in spec.self_actors:
        value = expr.resolve(params)
        if value is None or value == "":
            return None
        out.add(str(value))
    return out


@verdict_site("versions.judge", classes="C1 C5")
def judge_versions(
    spec: FrameVersions,
    stored: Mapping[str, Any],
    rows: Optional[Sequence[Mapping[str, Any]]],
    targets: Sequence[tuple[Optional[str], bool, bool]],
    key: bytes,
    params: Mapping[str, object] = {},
) -> VersionVerdict:
    """Judge the targets' versions in the window.

    ``stored``: :func:`seal_versions` of the baseline read. ``rows``: the read
    after the save (None when it failed). ``targets``: per record a confirmed
    write effect matched, its ``target_field`` value (None when the record
    has none), whether it existed before the write, and whether the record
    changed in the window.
    """

    if not stored.get("reachable") or rows is None:
        return VersionVerdict(
            "not_checked",
            "unreadable",
            reason="the target's versions could not be read, before or after the write",
        )
    mine: set[str] = set()
    if spec.author_field:
        resolved = _resolve_self(spec, params)
        if resolved is None:
            return VersionVerdict(
                "not_checked",
                "self_unresolved",
                reason="frame.versions.self references a parameter the run did not supply",
            )
        mine = resolved
    before: Mapping[str, Any] = stored.get("records") or {}
    out = VersionVerdict("held")
    worst: Optional[VersionVerdict] = None

    def stop(status: VersionStatus, problem: str, reason: str) -> None:
        nonlocal worst
        if worst is None or (worst.status == "not_checked" and status == "refuted"):
            worst = VersionVerdict(status, problem, reason=reason)

    seen: set[Optional[str]] = set()
    for target, existed, changed in targets:
        if target in seen:
            continue
        seen.add(target)
        if target is None:
            stop(
                "not_checked",
                "no_target_id",
                f"a target record has no {spec.target_field or 'identity'} field, "
                "so its versions can't be found",
            )
            continue
        base = before.get(_mac(key, "record", target))
        if base is None:
            if existed:
                stop(
                    "not_checked",
                    "missing",
                    "the target existed before the write but had no version",
                )
            continue  # created in the window: no earlier version to overwrite
        out.targets += 1
        now = [r for r in rows if _text(_get(r, spec.record_field)) == target]
        if not now:
            stop("not_checked", "missing", "the target has no version after the write")
            continue
        old = set(base.get("versions") or [])
        versions = [_text(_get(r, spec.version_field)) for r in now]
        if base.get("null") or any(v is None for v in versions):
            stop(
                "not_checked",
                "null_version",
                "a version of the target reads as null, so saves can't be told apart",
            )
            continue
        macs = [_mac(key, "version", target, str(v)) for v in versions]
        if base.get("dup") or len(set(macs)) != len(macs):
            stop(
                "not_checked",
                "duplicate_version",
                "two rows of the target's versions hold the same version, so a "
                "save could hide behind another",
            )
            continue
        fresh = [(v, r) for v, r, m in zip(versions, now, macs) if m not in old]
        out.new_versions += len(fresh)
        if changed and not fresh:
            stop(
                "not_checked",
                "no_new_version",
                "the target changed in the window but no new version of it was "
                "read, so who saved it can't be told",
            )
            continue
        if spec.author_field and not old <= set(macs):
            stop(
                "not_checked",
                "history_changed",
                "a version of the target read before the write is gone after it: "
                "the read isn't a history that only grows, so a save in the "
                "window could be hidden",
            )
            continue
        if spec.author_field:
            for _version, r in fresh:
                author = _text(_get(r, spec.author_field))
                if author is None:
                    stop(
                        "not_checked",
                        "no_author",
                        "a new version of the target names no author, so it "
                        "can't be told from another user's save",
                    )
                elif author in mine:
                    out.own_versions += 1
                else:
                    stop(
                        "refuted",
                        "another_author",
                        "lost update: another user saved a version of the target "
                        "inside the window, and the agent's save came after it "
                        "or beside it",
                    )
            continue
        # No authors: count whole-number steps.
        values = [_text(_get(r, spec.version_field)) for r in now]
        if not base.get("whole") or any(
            v is None or not _WHOLE.match(v) for v in values
        ):
            stop(
                "not_checked",
                "not_a_number",
                "the target's versions aren't whole numbers, and no author is "
                "read, so the steps in the window can't be counted",
            )
            continue
        steps = max(int(v) for v in values if v is not None) - int(base["max"] or 0)
        if steps < 0 or (steps == 0 and fresh):
            stop(
                "refuted",
                "went_back",
                "the target's version went back or was replaced in the window",
            )
        elif steps > 1 or len(fresh) > 1:
            stop(
                "refuted",
                "extra_steps",
                f"lost update: the target moved {max(steps, len(fresh))} versions "
                "in the window; one save is the agent's, and no author is read "
                "to say whose the others are",
            )
    if worst is not None:
        worst.targets, worst.new_versions, worst.own_versions = (
            out.targets,
            out.new_versions,
            out.own_versions,
        )
        return worst
    out.reason = f"{out.targets} target(s): every new version is the agent's own"
    return out
# fmt: on
