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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from openadapt_flow.runtime.effects.effect import ValueExpr
from openadapt_flow.verdict import (
    Obligations,
    Observed,
    Result,
    Status,
    Subject,
    settle,
)
from openadapt_flow.verdict_sites import verdict_site

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
    "null_version",
    "duplicate_version",
    "no_new_version",
    "history_changed",
    "unknown_surface",
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
    #: Only records matched on this surface are targets (the main record's
    #: surface). Unset: every record a confirmed write effect matched.
    target_surface: Optional[str] = None
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

    @model_serializer(mode="wrap")
    def _compact(self, handler: Any) -> dict[str, Any]:
        # Unset, ``target_surface`` is left out, so a contract that doesn't
        # use it keeps its digests.
        data: dict[str, Any] = handler(self)
        if data.get("target_surface") is None:
            data.pop("target_surface", None)
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
    # The obligations come from the plan: that the reads succeeded (the
    # early return above refused an unreadable one), then one per target.
    # With no target (all creates) that one still has to hold, so the set is
    # never empty.
    read = Subject(surface="versions", key="reads")
    subjects = [read]
    results = [
        Result.held(
            read,
            Observed(len(rows), complete=True),
            "the versions were read before and after the write",
        )
    ]
    #: Per target, in order: the problems found, each (status, code, reason).
    problems: dict[int, list[tuple[VersionStatus, str, str]]] = {}

    seen: set[Optional[str]] = set()
    for target, existed, changed in targets:
        if target in seen:
            continue
        seen.add(target)
        n = len(subjects)
        subject = Subject(surface="versions", key=f"target:{n}")
        subjects.append(subject)
        found = problems.setdefault(n, [])

        def stop(status: VersionStatus, problem: str, reason: str) -> None:
            found.append((status, problem, reason))

        _judge_target(
            spec, before, rows, key, mine, out, target, existed, changed, stop
        )
        if not found:
            results.append(Result.held(subject, Observed(target), "the agent's own"))
        elif any(status == "refuted" for status, _, _ in found):
            # A refutation stands even when another read of the same target
            # can't be explained: adding a duplicate or a null row never hides
            # a version by someone else.
            results.append(
                Result.refuted(subject, Observed(target, complete=False), "refuted")
            )
        else:
            results.append(Result.not_checked(subject, found[0][1]))
    settled = settle(Obligations.fix(subjects, source="frame.versions"), results)
    label = settled.label(precedence=(Status.REFUTED, Status.NOT_CHECKED))
    if label is Status.HELD:
        out.reason = f"{out.targets} target(s): every new version is the agent's own"
        return out
    wanted: VersionStatus = "refuted" if label is Status.REFUTED else "not_checked"
    # The first problem of the worst kind, in target order, names the verdict.
    status, problem, reason = next(
        (p for n in sorted(problems) for p in problems[n] if p[0] == wanted),
        ("not_checked", "missing", "the version check could not be settled"),
    )
    return VersionVerdict(
        status,
        problem,
        targets=out.targets,
        new_versions=out.new_versions,
        own_versions=out.own_versions,
        reason=reason,
    )


def _judge_target(
    spec: FrameVersions,
    before: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    key: bytes,
    mine: set[str],
    out: VersionVerdict,
    target: Optional[str],
    existed: bool,
    changed: bool,
    stop: Callable[[VersionStatus, str, str], None],
) -> None:
    """Every problem with one target, through ``stop``. A problem that only
    leaves the target unexplained doesn't end the search for one that
    refutes it."""

    if target is None:
        stop(
            "not_checked",
            "no_target_id",
            f"a target record has no {spec.target_field or 'identity'} field, "
            "so its versions can't be found",
        )
        return
    base = before.get(_mac(key, "record", target))
    if base is None:
        if existed:
            stop(
                "not_checked",
                "missing",
                "the target existed before the write but had no version",
            )
        return  # created in the window: no earlier version to overwrite
    out.targets += 1
    now = [r for r in rows if _text(_get(r, spec.record_field)) == target]
    if not now:
        stop("not_checked", "missing", "the target has no version after the write")
        return
    old = set(base.get("versions") or [])
    versions = [_text(_get(r, spec.version_field)) for r in now]
    if base.get("null") or any(v is None for v in versions):
        stop(
            "not_checked",
            "null_version",
            "a version of the target reads as null, so saves can't be told apart",
        )
        return
    macs = [_mac(key, "version", target, str(v)) for v in versions]
    if base.get("dup") or len(set(macs)) != len(macs):
        stop(
            "not_checked",
            "duplicate_version",
            "two rows of the target's versions hold the same version, so a "
            "save could hide behind another",
        )
    # One row per new version: a version read twice is one save.
    fresh: list[tuple[Optional[str], Mapping[str, Any]]] = []
    fresh_macs: set[str] = set()
    for v, r, m in zip(versions, now, macs):
        if m not in old and m not in fresh_macs:
            fresh_macs.add(m)
            fresh.append((v, r))
    out.new_versions += len(fresh)
    if changed and not fresh:
        stop(
            "not_checked",
            "no_new_version",
            "the target changed in the window but no new version of it was "
            "read, so who saved it can't be told",
        )
    if spec.author_field and not old <= set(macs):
        stop(
            "not_checked",
            "history_changed",
            "a version of the target read before the write is gone after it: "
            "the read isn't a history that only grows, so a save in the "
            "window could be hidden",
        )
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
        return
    # No authors: count whole-number steps.
    if not base.get("whole") or any(not _WHOLE.match(str(v)) for v in versions):
        stop(
            "not_checked",
            "not_a_number",
            "the target's versions aren't whole numbers, and no author is "
            "read, so the steps in the window can't be counted",
        )
        return
    steps = max(int(str(v)) for v in versions) - int(base["max"] or 0)
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
