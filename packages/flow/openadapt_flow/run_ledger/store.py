"""The ledger store: one row per run, append-only, plus a private sidecar.

Layout under ``$OPENADAPT_HOME/ledger`` (default ``~/.openadapt/ledger``)::

    rows.jsonl                      one entry per line (mode 0600)
    ../exceptions/<run_ref>/        P6's exception cards and answers (read)
    private/<row_id>/ledger.private.json   that row's values (mode 0600)
    .lock                           serialises add

Each ``rows.jsonl`` line is ``{"row": <openadapt.run-ledger/v1>, "run_digest":
..., "run_ref": ..., "added_at": ..., "declared_fields": [...]}``.
``declared_fields`` lists the field names the run's own task, verify or
readiness reference config declares; ``ledger export`` and ``push`` let only
those field names past the scrubber check (an entry without the key exempts
nothing).
``declared_task_ids`` lists the task ids the run's own guard task or
readiness files name, under the same rule. ``run_digest`` hashes the source
files (paths and bytes); ``run_ref`` hashes the run's resolved path, so a run
whose files change later (an approval answered after the first ``add``) gets
a new row and :meth:`LedgerStore.latest` keeps the newest one per run. A
second ``add`` of an unchanged run is a no-op: the store dedupes on
``(task_id, run_digest)``.

The file is rewritten through a temporary file and ``os.replace`` under an
exclusive lock, so a reader never sees half a line and two sessions on one
machine can't interleave.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from openadapt_types.run_ledger import (
    RunLedgerFieldV1,
    RunLedgerHumanV1,
    RunLedgerIntervalV1,
    RunLedgerModelUsageV1,
    RunLedgerOutcome,
    RunLedgerRowV1,
)

from openadapt_flow.fleet._lock import exclusive_lock
from openadapt_flow.run_ledger.collect import (
    _TIER_RANK,
    Collected,
    Interval,
    Usage,
    _worse,
    collect,
)
from openadapt_flow.run_ledger.outcome import Signal, decide

PRIVATE_SCHEMA = "openadapt.run-ledger-private/v1"
#: P6's run reference (``openadapt_types.exception_card.RUN_REF_PATTERN``).
_RUN_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_LABEL_DEFAULT = "unspecified"


def ledger_home(home: Optional[Path | str] = None) -> Path:
    base = (
        Path(home)
        if home
        else Path(os.environ.get("OPENADAPT_HOME") or Path.home() / ".openadapt")
    )
    return base.expanduser() / "ledger"


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    ).encode("utf-8")


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


# -- building a row -------------------------------------------------------------------


@dataclass
class PrivateSidecar:
    """What stays on this computer: field values and the inputs salt."""

    row_id: str
    inputs_salt: Optional[str]
    fields: dict[str, dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": PRIVATE_SCHEMA,
            "row_id": self.row_id,
            "inputs_salt": self.inputs_salt,
            "fields": self.fields,
        }

    def values(self) -> list[str]:
        """Every value string, so an export can be scanned for them."""

        found: list[str] = []
        for entry in self.fields.values():
            for key in ("intended", "saved"):
                value = entry.get(key)
                if value is not None and not isinstance(value, bool):
                    text = str(value)
                    if len(text) >= 3:
                        found.append(text)
        return found


def _diff(intended: Any, saved: Any) -> Optional[str]:
    if intended == saved or saved is None:
        return None
    import difflib

    return "\n".join(
        difflib.unified_diff(
            str(intended).splitlines(),
            str(saved).splitlines(),
            "intended",
            "saved",
            lineterm="",
        )
    )


def _usage(collected: Collected) -> RunLedgerModelUsageV1:
    """Metered usage beats reported usage beats unknown; first source wins
    within a rank. Usage is never summed across sources: two files often
    describe the same calls."""

    rank = {"metered": 0, "reported": 1, "unknown": 2}
    usages: list[Usage] = [c.usage for c in collected.contributions if c.usage]
    if not usages:
        return RunLedgerModelUsageV1()
    best = min(usages, key=lambda u: rank[u.cost_source])
    return RunLedgerModelUsageV1(
        calls=best.calls,
        input_tokens=best.input_tokens,
        output_tokens=best.output_tokens,
        cost_usd=float(best.cost_usd) if best.cost_usd is not None else None,
        cost_source=best.cost_source,  # type: ignore[arg-type]
    )


def _human(collected: Collected) -> RunLedgerHumanV1:
    seen: dict[str, Interval] = {}
    loose: list[Interval] = []
    for contribution in collected.contributions:
        for interval in contribution.intervals:
            if not interval.key:
                loose.append(interval)
                continue
            known = seen.get(interval.key)
            # The first reader wins; one that has the decision fills it in.
            if known is None or (known.decided_at is None and interval.decided_at):
                seen[interval.key] = interval
    intervals: list[RunLedgerIntervalV1] = []
    dedupe: set[tuple[str, str, Optional[str]]] = set()
    for interval in list(seen.values()) + loose:
        key = (interval.kind, interval.opened_at, interval.decided_at)
        if key in dedupe:
            continue
        dedupe.add(key)
        decided = interval.decided_at
        if decided is not None and _parse(decided) < _parse(interval.opened_at):
            decided = None  # a clock that runs backwards names no span
        intervals.append(
            RunLedgerIntervalV1(
                kind=interval.kind,  # type: ignore[arg-type]
                opened_at=interval.opened_at,
                decided_at=decided,
            )
        )
    intervals.sort(key=lambda i: i.opened_at)
    if not intervals:
        # No interval in the run's files is not proof nobody spent time on
        # it: 0 only when a source shows no person was asked, else unknown.
        nobody = any(c.no_person for c in collected.contributions)
        return RunLedgerHumanV1(intervention_s=0.0 if nobody else None)
    spans = [i.seconds() for i in intervals]
    total: Optional[float] = (
        None
        if any(s is None for s in spans)
        else round(sum(s or 0.0 for s in spans), 3)
    )
    return RunLedgerHumanV1(intervention_s=total, intervals=tuple(intervals))


def _times(
    collected: Collected,
) -> tuple[Optional[str], Optional[str], Optional[float]]:
    starts = [c.started_at for c in collected.contributions if c.started_at]
    ends = [c.ended_at for c in collected.contributions if c.ended_at]
    started = min(starts, key=_parse) if starts else None
    ended = max(ends, key=_parse) if ends else None
    if started is not None and ended is not None and _parse(ended) < _parse(started):
        ended = None
    if started is not None and ended is not None:
        duration: Optional[float] = round(
            (_parse(ended) - _parse(started)).total_seconds(), 3
        )
    else:
        durations = [
            c.duration_s for c in collected.contributions if c.duration_s is not None
        ]
        duration = durations[0] if durations else None
    return started, ended, duration


def _first(values: list[Optional[str]]) -> Optional[str]:
    return next((v for v in values if v), None)


def build_row(
    run: Path | str,
    *,
    task_type: Optional[str] = None,
    task_id: Optional[str] = None,
    collected: Optional[Collected] = None,
    exceptions: Optional[Path] = None,
) -> tuple[RunLedgerRowV1, PrivateSidecar, str]:
    """One row for ``run``, its private sidecar, and the run digest.

    ``exceptions`` is the run's P6 folder (``$OPENADAPT_HOME/exceptions/
    <run_ref>``), read beside the run."""

    collected = (
        collected
        if collected is not None
        else collect(Path(run), exceptions=exceptions)
    )
    contributions = collected.contributions
    signals: list[Signal] = [s for c in contributions for s in c.signals]
    ids = list(dict.fromkeys(t for c in contributions for t in c.task_ids))
    digest = collected.digest()
    if task_id is None:
        # No source names the task: name it after the run's place on disk, so
        # a run whose files change later keeps its task id.
        where = hashlib.sha256(str(collected.root.resolve()).encode("utf-8"))
        task_id = ids[0] if ids else f"run-{where.hexdigest()[:16]}"
    if len(ids) > 1:
        signals.append(Signal("task_id_conflict", source="ledger"))
    if contributions and not signals:
        # Sources were read but none decides (a flight log alone, say).
        signals.append(Signal("completed_unverified", source="ledger"))
    if any(s.basis == "deterministic" for s in signals):
        # A model's or agent's own judgement only counts when nothing else
        # speaks: it can't upgrade or downgrade a deterministic finding.
        signals = [s for s in signals if s.basis == "deterministic"]
    outcome, reason = decide(signals)

    fields: dict[str, str] = {}
    private_fields: dict[str, dict[str, Any]] = {}
    differing = 0
    for contribution in contributions:
        for name, check in contribution.fields.items():
            fields[name] = _worse(fields.get(name), check)
        for name, values in contribution.private_fields.items():
            intended, saved = values.get("intended"), values.get("saved")
            private_fields[name] = {
                "intended": intended,
                "saved": saved,
                "diff": _diff(intended, saved),
            }
        differing = max(differing, contribution.fields_differing)
    differing = max(differing, sum(1 for c in fields.values() if c == "differs"))

    tiers = [t for c in contributions for t in c.readback_tiers if t in _TIER_RANK]
    tier = max(tiers, key=lambda t: _TIER_RANK[t]) if tiers else None
    if outcome is RunLedgerOutcome.VERIFIED and tier != "independent_readback":
        # The weakest read decides; a VERIFIED resting on a weaker one isn't.
        outcome, reason = decide(
            signals
            + [
                Signal(
                    "screen_readback_only"
                    if tier == "screen_readback"
                    else "completed_unverified",
                    source="ledger",
                )
            ]
        )

    inputs = next((c.inputs for c in contributions if c.inputs is not None), None)
    salt: Optional[str] = None
    commitment: Optional[str] = None
    if inputs is not None:
        salt = secrets.token_hex(32)
        commitment = (
            "sha256:"
            + hashlib.sha256(bytes.fromhex(salt) + _canonical(inputs)).hexdigest()
        )
    started, ended, duration = _times(collected)
    row_id = str(uuid.uuid4())
    kinds = [c.executor_kind for c in contributions if c.executor_kind]
    row = RunLedgerRowV1(
        row_id=row_id,
        task_id=task_id,
        task_type=task_type or _LABEL_DEFAULT,
        executor_kind=(kinds[0] if kinds else "unknown"),  # type: ignore[arg-type]
        executor_label=_first([c.executor_label for c in contributions])
        or "unlabelled",
        inputs_commitment=commitment,
        started_at=started,
        ended_at=ended,
        duration_s=duration,
        model=_usage(collected),
        human=_human(collected),
        outcome=outcome,
        outcome_reason=reason,  # type: ignore[arg-type]
        fields_written=tuple(
            RunLedgerFieldV1(name=name, check=check)  # type: ignore[arg-type]
            for name, check in sorted(fields.items())
        ),
        fields_differing=differing,
        readback_tier=tier,  # type: ignore[arg-type]
        receipt_digest=_first([c.receipt_digest for c in contributions]),
        sources=tuple(collected.relative(path) for path in collected.files),
    )
    return row, PrivateSidecar(row_id, salt, private_fields), digest


# -- the store ------------------------------------------------------------------------


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        # mkstemp already creates 0600 on POSIX; Windows has no fchmod and
        # keeps the file under the user's profile ACL.
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


@dataclass
class AddResult:
    row: RunLedgerRowV1
    added: bool


class LedgerStore:
    """``$OPENADAPT_HOME/ledger``: rows, sidecars, a lock."""

    def __init__(self, home: Optional[Path | str] = None) -> None:
        self.root = ledger_home(home)
        self.rows_path = self.root / "rows.jsonl"
        self.private_root = self.root / "private"

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        # flock on POSIX, msvcrt.locking on Windows.
        with exclusive_lock(self.root / ".lock"):
            yield

    def entries(self) -> list[dict[str, Any]]:
        if not self.rows_path.is_file():
            return []
        out: list[dict[str, Any]] = []
        for line in self.rows_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out

    def rows(self) -> list[RunLedgerRowV1]:
        return [RunLedgerRowV1.model_validate(e["row"]) for e in self.entries()]

    def latest_entries(self) -> list[dict[str, Any]]:
        """The newest entry per (task_id, run), in the order they were added."""

        keep: dict[tuple[str, str], dict[str, Any]] = {}
        for entry in self.entries():
            keep[(entry["row"]["task_id"], entry.get("run_ref", ""))] = entry
        return list(keep.values())

    def latest(self) -> list[RunLedgerRowV1]:
        """The newest row per (task_id, run), in the order they were added."""

        return [RunLedgerRowV1.model_validate(e["row"]) for e in self.latest_entries()]

    def exceptions_dir(self, run_ref: Optional[str]) -> Optional[Path]:
        """P6's folder for a run: ``exceptions/<run_ref>`` beside the ledger.
        P6's run reference is the operator's ``exceptions serve --run-ref``
        (or a random ``mcp-...``); no run directory names it, so it is only
        ever given explicitly."""

        if run_ref is None:
            return None
        if not _RUN_REF.match(run_ref):
            raise ValueError("a run reference is letters, digits, '.', '_' or '-'")
        return self.root.parent / "exceptions" / run_ref

    def find(self, row_id: str) -> Optional[RunLedgerRowV1]:
        for entry in self.entries():
            if entry["row"]["row_id"] == row_id:
                return RunLedgerRowV1.model_validate(entry["row"])
        return None

    def sidecar_path(self, row_id: str) -> Path:
        uuid.UUID(row_id)  # refuses a path in place of an id
        return self.private_root / row_id / "ledger.private.json"

    def sidecar(self, row_id: str) -> Optional[dict[str, Any]]:
        path = self.sidecar_path(row_id)
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def sidecar_values(self) -> list[str]:
        values: list[str] = []
        for entry in self.entries():
            doc = self.sidecar(entry["row"]["row_id"]) or {}
            values.extend(
                PrivateSidecar(
                    entry["row"]["row_id"], None, doc.get("fields") or {}
                ).values()
            )
        return values

    def add(
        self,
        run: Path | str,
        *,
        task_type: Optional[str] = None,
        task_id: Optional[str] = None,
        run_ref: Optional[str] = None,
        config: tuple[Path | str, ...] = (),
    ) -> AddResult:
        run = Path(run)
        collected = collect(
            run,
            exceptions=self.exceptions_dir(run_ref),
            config=tuple(Path(c) for c in config),
        )
        row, sidecar, digest = build_row(
            run, task_type=task_type, task_id=task_id, collected=collected
        )
        run_ref = hashlib.sha256(str(run.resolve()).encode("utf-8")).hexdigest()
        with self._locked():
            entries = self.entries()
            for entry in entries:
                if (
                    entry["row"]["task_id"] == row.task_id
                    and entry.get("run_digest") == digest
                ):
                    return AddResult(RunLedgerRowV1.model_validate(entry["row"]), False)
            _write_private(
                self.sidecar_path(row.row_id),
                json.dumps(sidecar.to_dict(), indent=2, sort_keys=True).encode("utf-8"),
            )
            entries.append(
                {
                    "row": row.to_json(),
                    "run_digest": digest,
                    "run_ref": run_ref,
                    "added_at": _now(),
                    "declared_fields": sorted(collected.declared_fields),
                    "declared_task_ids": sorted(collected.declared_task_ids),
                }
            )
            body = "".join(json.dumps(e, sort_keys=True) + "\n" for e in entries)
            _write_private(self.rows_path, body.encode("utf-8"))
        return AddResult(row, True)


__all__ = [
    "AddResult",
    "LedgerStore",
    "PRIVATE_SCHEMA",
    "PrivateSidecar",
    "build_row",
    "ledger_home",
]
