"""Checked later: a save confirmed by a report that arrives after the run.

Some saves can't be read back right away. A claim is confirmed by the payer's
acknowledgment (X12 999, 277CA) hours later; an order shows up in tonight's
export or end-of-day file; a status page changes overnight. For those, the
deployment declares a deferred check (``effects.kind: deferred_report``):

- During the run, after the save, the verifier writes a pending entry to a
  local ledger: the effect it waits for, the item's key (stored only as a keyed
  digest), the report format, and a deadline. Its verdict is INDETERMINATE
  with ``deferred_check`` set, so the run stops there and ends
  ``SAVED_AWAITING_CHECK`` (:class:`~openadapt_flow.transaction.TransactionOutcome`):
  saved, waiting for its check. That outcome is never VERIFIED and never
  billable, and it is never safe to retry.
- Later, ``openadapt-flow deferred settle`` reads a report and settles each
  waiting entry whose key the report mentions. An accepting row settles it
  ``VERIFIED`` (billable). A rejecting or conflicting row settles it
  ``RECONCILIATION_REQUIRED``. An entry still unmatched at its deadline settles
  ``RECONCILIATION_REQUIRED``. A row that says the item is still pending
  (``A1``, received) changes nothing.
- ``openadapt-flow deferred status RUN`` gives a run's settled outcome. The
  run's own ``report.json`` (and any seal over it) is never rewritten: the
  settlement is a separate record in the ledger.

What the report proves. It says what the counterparty or the export says
about the item with that key: accepted, or not. It doesn't compare field
values the way a read-back does. The check declares
:attr:`~openadapt_flow.verification.VerificationTier.INDEPENDENT_SYSTEM`,
because the report comes from outside the application's screens.

No model calls. The ledger holds no record values: keys are HMAC-SHA256
digests under a salt kept in the ledger directory (mode 0600).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Literal, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openadapt_flow.assurance import UNCHECKED
from openadapt_flow.reports.parse import (
    ReportError,
    ReportFormat,
    ReportRow,
    parse_report,
    x12_issued_at,
)
from openadapt_flow.runtime.effects.adapter import ConnectionProbe, VerifierAdapterBase
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectState,
    EffectVerdict,
    ValueExpr,
    Verdict,
)
from openadapt_flow.verification import VerificationTier

SUBSTRATE = "deferred-report"
PENDING_SCHEMA: Literal["openadapt.deferred-check/v1"] = "openadapt.deferred-check/v1"
_SALT_FILE = ".salt"
_LOCK_FILE = ".lock"

EntryState = Literal["waiting", "verified", "reconciliation_required"]


class DeferredReportSpec(BaseModel):
    """Deployment config for ``effects.kind: deferred_report``."""

    model_config = ConfigDict(extra="forbid")

    #: How to read the report that confirms the save.
    report: ReportFormat
    #: The item's key in that report (a claim's patient control number, an
    #: order ID), usually a run parameter.
    key: ValueExpr
    #: Hours after the save by which a matching row must arrive.
    deadline_hours: float = Field(gt=0, le=24 * 90)
    #: Directory of the pending-check ledger (created 0700).
    ledger: str
    #: What the report is, for people ("payer 277CA", "nightly export").
    describe: str = Field(default="the report", max_length=120)

    @model_validator(mode="before")
    @classmethod
    def _coerce_literal(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("key"), str):
            data = {**data, "key": {"literal": data["key"]}}
        return data


class PendingCheck(BaseModel):
    """One save waiting for its report. No record values."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["openadapt.deferred-check/v1"] = PENDING_SCHEMA
    id: str = Field(pattern=r"^deferred:[0-9a-f]{32}$")
    created_at: str
    deadline: str
    key_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    effect_contract_hash: str
    report_format: str
    #: SHA-256 of the exact report format (columns, accept and reject values,
    #: pattern): only a report read the same way can settle the entry.
    report_format_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    describe: str
    state: EntryState = "waiting"
    settled_at: Optional[str] = None
    #: SHA-256 of the report that settled it.
    settled_by: Optional[str] = None
    #: The report's status code for the row (``A2``, ``R``), or ``deadline``.
    code: Optional[str] = None
    reason: Optional[str] = None


@dataclass(frozen=True)
class Settlement:
    id: str
    state: EntryState
    code: str
    reason: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def format_digest(fmt: ReportFormat) -> str:
    return hashlib.sha256(fmt.model_dump_json().encode("utf-8")).hexdigest()


#: X12 dates carry no time zone; a report time is compared with this slack.
_X12_CLOCK_SLACK = timedelta(hours=14)


class PendingLedger:
    """A directory of pending checks, one JSON file each (mode 0600).

    Every change happens under an exclusive lock on the ledger, and an entry
    only ever moves out of ``waiting``, never back or sideways.
    """

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    # -- storage ------------------------------------------------------------------
    def _ensure(self) -> None:
        if not self.directory.exists():
            self.directory.mkdir(parents=True, mode=0o700)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        import fcntl

        self._ensure()
        fd = os.open(self.directory / _LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _salt(self) -> bytes:
        self._ensure()
        path = self.directory / _SALT_FILE
        if not path.exists():
            # Written in full to a temporary file, then linked into place, so
            # no reader ever sees a partial salt.
            tmp = self.directory / f".salt.{secrets.token_hex(8)}"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(secrets.token_hex(32))
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(tmp, path)
            except FileExistsError:
                pass
            finally:
                os.unlink(tmp)
        salt = bytes.fromhex(path.read_text().strip())
        if len(salt) != 32:
            raise ValueError("the ledger salt is damaged")
        return salt

    def key_digest(self, key: str) -> str:
        return hmac.new(
            self._salt(), key.strip().encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def _path(self, entry_id: str) -> Path:
        return self.directory / f"{entry_id.split(':', 1)[1]}.json"

    def _write(self, entry: PendingCheck) -> None:
        self._ensure()
        path = self._path(entry.id)
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(entry.model_dump_json(indent=2) + "\n")
        os.replace(tmp, path)

    def get(self, entry_id: str) -> Optional[PendingCheck]:
        if not re.fullmatch(r"deferred:[0-9a-f]{32}", entry_id or ""):
            return None
        path = self._path(entry_id)
        if not path.is_file():
            return None
        return PendingCheck.model_validate_json(path.read_text())

    def entries(self) -> list[PendingCheck]:
        if not self.directory.is_dir():
            return []
        out = []
        for path in sorted(self.directory.glob("*.json")):
            out.append(PendingCheck.model_validate_json(path.read_text()))
        return out

    # -- the run side ---------------------------------------------------------------
    def add(
        self,
        *,
        key: str,
        effect_contract_hash: str,
        fmt: ReportFormat,
        describe: str,
        deadline_hours: float,
        now: Optional[datetime] = None,
    ) -> PendingCheck:
        moment = now or _now()
        with self._locked():
            entry = PendingCheck(
                id=f"deferred:{secrets.token_hex(16)}",
                created_at=_iso(moment),
                deadline=_iso(moment + timedelta(hours=deadline_hours)),
                key_digest=self.key_digest(key),
                effect_contract_hash=effect_contract_hash,
                report_format=fmt.format,
                report_format_digest=format_digest(fmt),
                describe=describe,
            )
            self._write(entry)
        return entry

    # -- the settle side --------------------------------------------------------------
    def settle(
        self,
        report_text: Optional[str],
        fmt: Optional[ReportFormat],
        *,
        issued_at: Optional[datetime] = None,
        now: Optional[datetime] = None,
    ) -> list[Settlement]:
        """Expire overdue entries, then settle waiting ones against a report.

        An entry past its deadline settles RECONCILIATION_REQUIRED first, so a
        report that arrives late never promotes it. A report settles only
        entries created before it was issued (``issued_at``: the X12 header,
        or the caller's report time for a CSV or text report), and only
        entries waiting on that exact report format. ``report_text`` None only
        expires entries.
        """

        moment = now or _now()
        report_sha = (
            hashlib.sha256(report_text.encode("utf-8")).hexdigest()
            if report_text is not None
            else None
        )
        settled: list[Settlement] = []
        with self._locked():
            waiting = [e for e in self.entries() if e.state == "waiting"]
            still: list[PendingCheck] = []
            for entry in waiting:
                if datetime.fromisoformat(entry.deadline) <= moment:
                    reason = f"no matching row in {entry.describe} by the deadline"
                    self._write(
                        entry.model_copy(
                            update={
                                "state": "reconciliation_required",
                                "settled_at": _iso(moment),
                                "code": "deadline",
                                "reason": reason,
                            }
                        )
                    )
                    settled.append(
                        Settlement(
                            entry.id, "reconciliation_required", "deadline", reason
                        )
                    )
                else:
                    still.append(entry)
            if report_text is None or fmt is None:
                return settled
            stamp = issued_at
            if stamp is None and fmt.format in ("x12-999", "x12-277ca"):
                stamp = x12_issued_at(report_text)
            if stamp is None:
                raise ReportError(
                    "the report's issue time is unknown; give it (--report-time) so "
                    "an older report can't settle a newer save"
                )
            rows: dict[str, ReportRow] = {}
            for key, parsed in parse_report(report_text, fmt).items():
                rows[self.key_digest(key)] = parsed
            wanted = format_digest(fmt)
            by_digest: dict[str, list[PendingCheck]] = {}
            for entry in still:
                if entry.report_format_digest != wanted:
                    continue
                created = datetime.fromisoformat(entry.created_at)
                if not _issued_after(stamp, created):
                    continue  # this report predates the save
                by_digest.setdefault(entry.key_digest, []).append(entry)
            for digest, entries in by_digest.items():
                row = rows.get(digest)
                if row is None or row.status == "pending":
                    continue
                if len(entries) > 1:
                    state: EntryState = "reconciliation_required"
                    reason = (
                        f"{len(entries)} saves wait on the same report key; the "
                        "report can't say which one it means"
                    )
                elif row.status == "accepted":
                    state, reason = "verified", "the report accepted it"
                elif row.status == "rejected":
                    state, reason = "reconciliation_required", "the report rejected it"
                else:
                    state = "reconciliation_required"
                    reason = "the report says two different things about it"
                code = _safe_code(row.code, fmt)
                for entry in entries:
                    current = self.get(entry.id)
                    if current is None or current.state != "waiting":
                        continue
                    self._write(
                        current.model_copy(
                            update={
                                "state": state,
                                "settled_at": _iso(moment),
                                "settled_by": report_sha,
                                "code": code,
                                "reason": reason,
                            }
                        )
                    )
                    settled.append(Settlement(entry.id, state, code, reason))
        return settled


def _issued_after(stamp: datetime, created: datetime) -> bool:
    """Whether a report issued at ``stamp`` can speak about a save made at
    ``created``. A report time without a zone (X12) gets the clock slack."""

    if stamp.tzinfo is None:
        naive_created = created.astimezone(timezone.utc).replace(tzinfo=None)
        return stamp >= naive_created - _X12_CLOCK_SLACK
    return stamp >= created


def _safe_code(code: str, fmt: ReportFormat) -> str:
    """The status code to keep: X12 codes as given, a CSV or text status only
    when it is one of the declared accept or reject values (no free cell)."""

    if fmt.format in ("x12-999", "x12-277ca"):
        return code[:8]
    known = {v.strip().casefold() for v in [*fmt.accept, *fmt.reject]}
    return code.strip()[:32] if code.strip().casefold() in known else "other"


def settled_outcome(
    report: Any, ledger: PendingLedger, *, now: Optional[datetime] = None
) -> str:
    """A run's outcome after its deferred checks settle.

    ``SAVED_AWAITING_CHECK`` becomes ``VERIFIED`` only when every deferred
    check on the run settled verified; any check that settled the other way,
    or is past its deadline, makes it ``RECONCILIATION_REQUIRED``. Any other
    outcome is returned unchanged.
    """

    outcome = getattr(report, "transaction_outcome", None)
    if outcome != "SAVED_AWAITING_CHECK":
        return str(outcome)
    pairs = [
        (evidence.deferred_check, evidence.effect_contract_hash)
        for result in report.results
        for evidence in result.effect_evidence
        if evidence.deferred_check
    ]
    if not pairs:
        return "RECONCILIATION_REQUIRED"
    moment = now or _now()
    states = []
    for entry_id, contract_hash in pairs:
        entry = ledger.get(entry_id)
        if entry is None or entry.effect_contract_hash != contract_hash:
            # A missing entry, or one written for another effect, settles
            # nothing about this run.
            return "RECONCILIATION_REQUIRED"
        if (
            entry.state == "waiting"
            and datetime.fromisoformat(entry.deadline) <= moment
        ):
            return "RECONCILIATION_REQUIRED"
        states.append(entry.state)
    if any(state == "reconciliation_required" for state in states):
        return "RECONCILIATION_REQUIRED"
    if all(state == "verified" for state in states):
        return "VERIFIED"
    return "SAVED_AWAITING_CHECK"


class DeferredReportVerifier(VerifierAdapterBase):
    """Record the save as waiting for a later report; never confirm now."""

    substrate = SUBSTRATE
    #: The report comes from outside the application's screens: the
    #: counterparty's acknowledgment or the system's own export.
    verification_tier = VerificationTier.INDEPENDENT_SYSTEM
    independent_system_of_record = True
    adapter_version = "1"

    def __init__(self, spec: DeferredReportSpec, key: str) -> None:
        self.spec = spec
        self._key = key
        self.ledger = PendingLedger(spec.ledger)

    def capture_pre_state(self, context: Any = None) -> EffectState:
        # Nothing to read before the save: the report doesn't exist yet.
        return EffectState(
            substrate=self.substrate,
            reachable=True,
            records=[],
            detail={"read": f"deferred: {self.spec.describe}"},
        )

    def test_connection(self, context: Any = None) -> ConnectionProbe:
        try:
            self.ledger._salt()
        except OSError as exc:
            return ConnectionProbe(
                ok=False,
                substrate=self.substrate,
                reason=f"the pending-check ledger isn't writable ({type(exc).__name__})",
            )
        return ConnectionProbe(
            ok=True, substrate=self.substrate, reason="pending-check ledger writable"
        )

    def verify(
        self, expected: Effect, before: EffectState, context: Any = None
    ) -> EffectVerdict:
        try:
            entry = self.ledger.add(
                key=self._key,
                effect_contract_hash=expected.contract_hash(),
                fmt=self.spec.report,
                describe=self.spec.describe,
                deadline_hours=self.spec.deadline_hours,
            )
        except (OSError, ValueError) as exc:
            # Without a pending entry, nothing could ever settle this save:
            # an ordinary INDETERMINATE, so the run needs reconciliation.
            return EffectVerdict(
                verdict=Verdict.INDETERMINATE,
                kind=expected.kind,
                substrate=self.substrate,
                reason=(
                    "could not record the save as waiting for "
                    f"{self.spec.describe} ({type(exc).__name__}); HALT"
                ),
                unavailable=True,
                assurance=UNCHECKED,
            )
        return EffectVerdict(
            verdict=Verdict.INDETERMINATE,
            kind=expected.kind,
            substrate=self.substrate,
            reason=(
                f"saved; waiting for {self.spec.describe} to confirm it by "
                f"{entry.deadline}; not counted until a matching row settles it"
            ),
            deferred_check=entry.id,
            assurance=UNCHECKED,
        )


def build_deferred_report_verifier(
    spec: DeferredReportSpec, params: Optional[Mapping[str, object]] = None
) -> DeferredReportVerifier:
    key = spec.key.resolve(dict(params or {}))
    if key is None or not str(key).strip():
        raise ValueError(
            "deferred_report.key references run parameter "
            f"{spec.key.param!r}, which was not supplied"
        )
    return DeferredReportVerifier(spec, str(key))


def settle_report_file(
    ledger: PendingLedger,
    report_path: str | Path,
    fmt: ReportFormat,
    *,
    issued_at: Optional[datetime] = None,
) -> list[Settlement]:
    """Settle against a report file. A CSV or text report needs its issue
    time (``issued_at``); an X12 report carries its own."""

    text = Path(report_path).read_text(encoding="utf-8", errors="strict")
    return ledger.settle(text, fmt, issued_at=issued_at)


__all__ = [
    "DeferredReportSpec",
    "DeferredReportVerifier",
    "PendingCheck",
    "PendingLedger",
    "ReportError",
    "SUBSTRATE",
    "Settlement",
    "build_deferred_report_verifier",
    "settle_report_file",
    "settled_outcome",
]
