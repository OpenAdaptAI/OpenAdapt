"""Version history of the target: catch a lost update.

Two snapshots can't see a lost update. The agent reads a record, another
user saves a change to it, and the agent saves its stale copy over theirs.
The record after the save looks like "the agent changed one field, the rest
is unchanged", which is what a correct update looks like too. Only the
record's versions show the second writer.

``frame.versions`` names a read-only read that returns one row per version of
a record (FHIR ``_history`` or HAPI's ``hfj_res_ver``; a SQL ``version``
column; the app's audit or action-history table), and which fields of a row
name the record, the version and, where the app records one, its author.
The verifier reads it before the write and after it. For every target record
that already existed (one a confirmed write effect names), the versions that
are new in the window must be the agent's own:

- With ``author_field``: every new version's author must be one of ``self``.
  A version by anyone else is another user's write inside the window: the
  frame is refuted. A version with no author can't be attributed: not
  checked.
- Without it: versions must be whole numbers that step by one per save
  (HAPI's ``res_ver``, a ``version_number`` column), and exactly one step may
  have happened (zero when nothing was written). Two steps mean someone else
  saved too: refuted. A version that isn't a whole number: not checked.

Anything the reads can't explain stops the run: a read that fails, a target
that existed but has no version, or one whose versions vanished. Never a
pass. Versions are stored in the baseline as keyed MACs of the record and the
version; only a whole-number version's value is kept, to count the steps.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openadapt_flow.runtime.effects.effect import ValueExpr

VersionStatus = Literal["held", "refuted", "not_checked"]

#: Why the version check stopped the run (closed values, for the evidence).
PROBLEMS = (
    "unreadable",
    "no_target_id",
    "missing",
    "another_author",
    "no_author",
    "not_a_number",
    "extra_steps",
    "went_back",
    "self_unresolved",
)

_WHOLE = re.compile(r"^(0|[1-9][0-9]*)$")


class FrameVersions(BaseModel):
    """``frame.versions``: where the target's versions are read."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    #: A read-only read, the same shape as an ``effects:`` verifier section,
    #: that returns one record per version (built by ``verify-run``).
    read: dict[str, Any]
    #: The version row's field naming the record it is a version of.
    record_field: str = Field(min_length=1)
    #: The target record's field (in the contract's read set) that equals
    #: ``record_field``. Unset: ``frame.identity_field``, else ``id``.
    target_field: Optional[str] = None
    #: The version row's version id (``meta.versionId``, ``res_ver``, an
    #: audit entry's id).
    version_field: str = Field(min_length=1)
    #: The version row's author, when the app records who saved each
    #: version. Unset: versions must be whole numbers that step by one.
    author_field: Optional[str] = None
    #: The agent's own user ids, as the author field names them. Literals or
    #: run parameters. Required with ``author_field``.
    self_actors: list[ValueExpr] = Field(default_factory=list, alias="self")

    @model_validator(mode="before")
    @classmethod
    def _coerce_self(cls, data: Any) -> Any:
        if isinstance(data, dict):
            key = "self" if "self" in data else "self_actors"
            values = data.get(key)
            if isinstance(values, list):
                data = dict(data)
                data[key] = [
                    ValueExpr(literal=v) if isinstance(v, str) else v for v in values
                ]
        return data

    @model_validator(mode="after")
    def _shape(self) -> "FrameVersions":
        if bool(self.author_field) != bool(self.self_actors):
            raise ValueError(
                "frame.versions: author_field and self go together (whose "
                "versions are the agent's own)"
            )
        return self


@dataclass
class VersionVerdict:
    status: VersionStatus
    #: One of :data:`PROBLEMS`, or None when the check held.
    problem: Optional[str] = None
    targets: int = 0
    #: Versions new in the window, summed over the targets.
    new_versions: int = 0
    #: Of those, the agent's own (by author), when authors are read.
    own_versions: int = 0
    #: In memory only: the reason line.
    reason: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def evidence(self) -> dict[str, Any]:
        """Closed fields only: counts and a problem code, no ids or values."""

        return {
            "status": self.status,
            "problem": self.problem,
            "targets": self.targets,
            "new_versions": self.new_versions,
            "own_versions": self.own_versions,
        }


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
            _mac(key, "record", record), {"versions": [], "max": None, "whole": True}
        )
        if version is None:
            entry["whole"] = False
            entry["versions"].append(_mac(key, "version", record, "\x00null"))
            continue
        entry["versions"].append(_mac(key, "version", record, version))
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


def judge_versions(
    spec: FrameVersions,
    stored: Mapping[str, Any],
    rows: Optional[Sequence[Mapping[str, Any]]],
    targets: Sequence[tuple[Optional[str], bool]],
    key: bytes,
    params: Mapping[str, object] = {},
) -> VersionVerdict:
    """Judge the targets' versions in the window.

    ``stored``: :func:`seal_versions` of the baseline read. ``rows``: the read
    after the save (None when it failed). ``targets``: per record a confirmed
    write effect matched, its ``target_field`` value (None when the record
    has none) and whether it existed before the write.
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
    for target, existed in targets:
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
        fresh = []
        for r in now:
            version = _text(_get(r, spec.version_field))
            mac = _mac(
                key, "version", target, "\x00null" if version is None else version
            )
            if mac not in old:
                fresh.append((version, r))
        out.new_versions += len(fresh)
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
