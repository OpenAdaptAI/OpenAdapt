"""Local exception queue for Seals on a REFUTED or INDETERMINATE outcome.

``countersign add`` queues an entry for every genuine Seal whose outcome is not
verified. An entry starts ``open`` and ends in one of two terminal states that
never stand in for each other:

* ``resolved_by_seal``: a later receipt carries a genuine Seal on a
  ``verified`` or ``rolled_back_verified`` outcome, checked against the same
  pinned keys, and links to this run (``remediates_receipt_id`` or a shared
  ``correlation_id``) with the same workflow, contract, and committed
  parameters. A verify-run receipt with ``executor_kind`` ``human`` counts.
* ``closed_by_attestation``: a signed ``openadapt.exception-disposition/v1``
  record from a person whose key is pinned, who is not the failed run's agent
  or script executor, with closed-enum codes and HMAC digests only.

There is no flag that marks an entry resolved or closed on someone's word.

The Needs Attention queue (``openadapt_flow.console.attention``) is a read-only
projection over run directories, so it cannot hold a terminal state. This
queue keeps its categories (``effect_refuted``, ``effect_indeterminate``) and
its rule of no workflow names or field values in an entry, and stores entries
the way the countersign vault stores field secrets: 0600 files in 0700
directories. Salts, field values, and notes never leave this machine.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from openadapt_flow.countersign.commitments import FieldSecret, field_commitment
from openadapt_flow.countersign.document import (
    _SEALABLE,
    CountersignError,
    CountersignReport,
    locate,
    verify_countersigned,
)
from openadapt_flow.countersign.vault import (
    CountersignVault,
    VaultError,
    write_private_bytes,
)

__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "DISPOSITION_CODES",
    "DISPOSITION_SCHEMA",
    "ENTRY_SCHEMA",
    "MIN_RETENTION_DAYS",
    "VERIFICATION_METHODS",
    "ExceptionQueue",
    "registry_sha256",
    "retention_days",
]

ENTRY_SCHEMA = "openadapt.countersign-exception/v1"
DISPOSITION_SCHEMA = "openadapt.exception-disposition/v1"
EXPORT_SCHEMA = "openadapt.countersign-exception-export/v1"
DEFAULT_RETENTION_DAYS = 2190  # 6 years
MIN_RETENTION_DAYS = 183  # 6 months
DISPOSITION_CODES = frozenset(
    {
        "remediated_manually_in_sor",
        "cancelled_by_requester",
        "superseded_by_external_workflow",
        "verified_manually_oracle_unavailable",
    }
)
VERIFICATION_METHODS = frozenset(
    {
        "sor_direct_inspection",
        "ehr_audit_log_review",
        "counterparty_confirmation",
        "task_cancelled_no_check_needed",
    }
)
TERMINAL = ("resolved_by_seal", "closed_by_attestation")
_RECEIPT_SHA = re.compile(r"^[0-9a-f]{64}$")
_HMAC = re.compile(r"^hmac-sha256:[0-9a-f]{64}$")
_OPAQUE = re.compile(r"^[A-Za-z0-9._:-]{4,128}$")
_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")
_CATEGORY = {"REFUTED": "effect_refuted", "INDETERMINATE": "effect_indeterminate"}


def retention_days(
    value: Optional[int] = None, env: Optional[Mapping[str, str]] = None
) -> int:
    """``value``, else ``OPENADAPT_EXCEPTION_RETENTION_DAYS``, else 6 years. At least 6 months."""
    env = os.environ if env is None else env
    if value is None:
        raw = env.get("OPENADAPT_EXCEPTION_RETENTION_DAYS", "").strip()
        value = int(raw) if raw else DEFAULT_RETENTION_DAYS
    if value < MIN_RETENTION_DAYS:
        raise CountersignError(
            "retention_too_short", f"retention is at least {MIN_RETENTION_DAYS} days"
        )
    return value


def registry_sha256(registry: Sequence[Mapping[str, Any]]) -> str:
    """Digest of the pinned Seal keys, independent of order and extra fields."""
    from openadapt_types.seal import parse_key_registry

    try:
        ids = {key.key_id for key in parse_key_registry(list(registry))}
    except (ValueError, TypeError, KeyError):
        ids = set()
    rows = sorted(
        json.dumps(
            {k: e.get(k) for k in ("key_id", "public_key", "status", "algorithm")},
            sort_keys=True,
        )
        for e in registry
        if e.get("key_id") in ids
    )
    if not rows:
        raise CountersignError("no_pinned_keys")
    return "sha256:" + hashlib.sha256("\n".join(rows).encode()).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not _UTC.match(value):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _fmt(at: datetime) -> str:
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _unsigned(record: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in record.items() if k != "seal_signature"}


class ExceptionQueue:
    """Entries live at ``<vault>/exceptions/<receipt_sha256>.json``."""

    def __init__(
        self,
        vault: Optional[CountersignVault] = None,
        *,
        clock: Callable[[], str] = _utc_now,
    ) -> None:
        self.vault = vault or CountersignVault()
        self.dir = self.vault.root / "exceptions"
        self._clock = clock

    # -- storage -------------------------------------------------------------

    def _path(self, receipt_sha256: str, suffix: str = ".json") -> Path:
        if not _RECEIPT_SHA.match(receipt_sha256 or ""):
            raise CountersignError(
                "unknown_entry", "an entry is a 64-hex receipt_sha256"
            )
        return self.dir / f"{receipt_sha256}{suffix}"

    def _mkdir(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.dir, 0o700)

    def _write(self, entry: Mapping[str, Any]) -> None:
        self._mkdir()
        write_private_bytes(
            self._path(entry["receipt_sha256"]),
            json.dumps(dict(entry), indent=2, sort_keys=True).encode(),
            overwrite=True,
        )

    def get(self, receipt_sha256: str) -> dict[str, Any]:
        path = self._path(receipt_sha256)
        if not path.is_file():
            raise CountersignError("unknown_entry", receipt_sha256)
        entry = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(entry, dict) or entry.get("schema") != ENTRY_SCHEMA:
            raise CountersignError("unknown_entry", "unrecognized entry file")
        return entry

    def entries(self, status: Optional[str] = None) -> list[dict[str, Any]]:
        if not self.dir.is_dir():
            return []
        out = []
        for path in sorted(self.dir.glob("*.json")):
            try:
                entry = self.get(path.stem)
            except (CountersignError, ValueError, OSError):
                continue
            if status is None or entry.get("status") == status:
                out.append(entry)
        return sorted(
            out, key=lambda e: (e.get("issued_at") or "", e["receipt_sha256"])
        )

    def hmac_digest(self, value: str) -> str:
        """HMAC-SHA256 under a key that never leaves this vault."""
        self._mkdir()
        key_path = self.dir / "hmac.key"
        if not key_path.is_file():
            try:
                write_private_bytes(key_path, secrets.token_bytes(32))
            except (VaultError, FileExistsError):
                pass  # another process created it first
        key = key_path.read_bytes()
        digest = hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"hmac-sha256:{digest}"

    # -- queueing ------------------------------------------------------------

    def enqueue(
        self,
        document: Any,
        report: CountersignReport,
        registry: Sequence[Mapping[str, Any]],
        *,
        retention: Optional[int] = None,
    ) -> Optional[dict[str, Any]]:
        """Queue a genuine Seal on an unverified outcome. Returns None otherwise.

        Idempotent: an existing entry, open or terminal, is returned unchanged.
        """
        if not report.signature_genuine or report.outcome_verified:
            return None
        located = locate(document)
        receipt = located.receipt
        rsha = report.receipt_sha256
        assert rsha is not None
        try:
            return self.get(rsha)
        except CountersignError:
            pass
        verdict = (
            report.effect_verdict
            if report.effect_verdict in _CATEGORY
            else "INDETERMINATE"
        )
        contract = ((located.evidence or {}).get("contract") or {}).get(
            "contract_sha256"
        )
        entry = {
            "schema": ENTRY_SCHEMA,
            "receipt_sha256": rsha,
            "receipt_id": receipt.get("receipt_id"),
            "execution_id": report.execution_id,
            "workflow_digest": receipt.get("workflow_digest"),
            "contract_sha256": contract,
            "correlation_id": receipt.get("correlation_id"),
            "executor_kind": report.executor_kind,
            "executor_ref_sha256": receipt.get("executor_ref_sha256"),
            "outcome": report.outcome,
            "verdict": verdict,
            "category": _CATEGORY[verdict],
            "issued_at": receipt.get("issued_at"),
            "seal_key_id": report.seal.get("key_id"),
            "registry_sha256": registry_sha256(registry),
            "status": "open",
            "queued_at": self._clock(),
            "retention_days": retention_days(retention),
            "retain_until": None,
            "resolution_receipt_id": None,
            "resolution_receipt_sha256": None,
            "disposition": None,
            "terminal_at": None,
        }
        self._write(entry)
        return entry

    def _open(self, receipt_sha256: str) -> dict[str, Any]:
        entry = self.get(receipt_sha256)
        if entry.get("status") != "open":
            raise CountersignError("not_open", str(entry.get("status")))
        return entry

    def _finish(self, entry: dict[str, Any], status: str, **fields: Any) -> None:
        now = self._clock()
        until = _parse_time(now)
        assert until is not None
        entry.update(
            status=status,
            terminal_at=now,
            retain_until=_fmt(until + timedelta(days=int(entry["retention_days"]))),
            **fields,
        )
        self._write(entry)

    # -- resolved_by_seal ----------------------------------------------------

    def resolve_by_seal(
        self,
        receipt_sha256: str,
        resolution: Any,
        registry: Sequence[Mapping[str, Any]],
        *,
        tsa_certificates: Sequence[Any] = (),
    ) -> dict[str, Any]:
        """Resolve an open entry with a later verified Seal. Raises on any gap."""
        entry = self._open(receipt_sha256)
        if registry_sha256(registry) != entry.get("registry_sha256"):
            raise CountersignError(
                "different_pinned_keys", "pin the keys this entry was queued under"
            )
        # Any executor counts here, a person's verify-run included, because a
        # verified outcome already required an independent readback.
        report = verify_countersigned(
            resolution,
            registry=registry,
            tsa_certificates=tsa_certificates,
            accept_non_flow=True,
        )
        if (
            not report.signature_genuine
            or report.outcome not in _SEALABLE
            or report.problems
        ):
            raise CountersignError(
                "resolution_not_verified",
                f"seal {report.seal.get('status')}, outcome {report.outcome}, "
                f"problems {report.problems}",
            )
        located = locate(resolution)
        receipt = located.receipt
        if report.receipt_sha256 == entry["receipt_sha256"]:
            raise CountersignError("same_receipt")
        before = _parse_time(entry.get("issued_at"))
        after = _parse_time(receipt.get("issued_at"))
        if before is None or after is None or after <= before:
            raise CountersignError("not_later")
        self._check_link(entry, receipt)
        contract = ((located.evidence or {}).get("contract") or {}).get(
            "contract_sha256"
        )
        if (
            not entry.get("workflow_digest")
            or receipt.get("workflow_digest") != entry["workflow_digest"]
            or contract != entry.get("contract_sha256")
        ):
            raise CountersignError("different_contract")
        self._check_params(entry, resolution, report, registry, tsa_certificates)
        self._finish(
            entry,
            "resolved_by_seal",
            resolution_receipt_id=receipt.get("receipt_id") or report.receipt_sha256,
            resolution_receipt_sha256=report.receipt_sha256,
        )
        return entry

    @staticmethod
    def _check_link(entry: Mapping[str, Any], receipt: Mapping[str, Any]) -> None:
        """The resolving receipt must name this run. Both fields are Seal-signed."""
        remediates = receipt.get("remediates_receipt_id")
        if remediates is not None:
            if not entry.get("receipt_id") or remediates != entry["receipt_id"]:
                raise CountersignError(
                    "not_linked", "remediates_receipt_id names another receipt"
                )
            return
        cid = receipt.get("correlation_id")
        if (
            isinstance(cid, str)
            and _OPAQUE.match(cid)
            and cid == entry.get("correlation_id")
        ):
            return
        raise CountersignError(
            "not_linked", "no remediates_receipt_id or matching correlation_id"
        )

    def _check_params(
        self,
        entry: Mapping[str, Any],
        resolution: Any,
        report: CountersignReport,
        registry: Sequence[Mapping[str, Any]],
        tsa_certificates: Sequence[Any],
    ) -> None:
        """Both runs' committed parameters, from the vault, must be identical."""
        try:
            original = json.loads(
                (self.vault.root / entry["receipt_sha256"] / "document.json").read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, ValueError):
            raise CountersignError(
                "params_unproven", "the vault lost the original"
            ) from None
        original_report = verify_countersigned(
            original, registry=registry, tsa_certificates=tsa_certificates
        )
        if original_report.receipt_sha256 != entry["receipt_sha256"]:
            raise CountersignError(
                "params_unproven", "the vault copy is another receipt"
            )
        if self._params(original, original_report) != self._params(resolution, report):
            raise CountersignError("different_params")

    def _params(self, document: Any, report: CountersignReport) -> dict[str, str]:
        """Committed name -> value, each matched to a commitment a check binds."""
        if report.commitments.get("status") != "bound":
            raise CountersignError(
                "params_unproven", "the parameter commitments are not bound"
            )
        block = locate(document).block or {}
        fields = (block.get("commitments") or {}).get("fields") or {}
        try:
            kept: list[FieldSecret] = self.vault.get_secrets(
                report.receipt_sha256 or ""
            )
        except (VaultError, ValueError, TypeError, KeyError):
            raise CountersignError("params_unproven", "no vault secrets") from None
        params: dict[str, str] = {}
        for secret in kept:
            try:
                salt = base64.b64decode(secret.salt, validate=True)
                digest = field_commitment(secret.label, secret.name, secret.value, salt)
            except ValueError:
                digest = ""
            if fields.get(secret.label) != digest or secret.name in params:
                raise CountersignError(
                    "params_unproven", "a vault entry does not match its commitment"
                )
            params[secret.name] = secret.value
        if set(fields) != {s.label for s in kept}:
            raise CountersignError("params_unproven", "the vault is missing a field")
        return params

    # -- closed_by_attestation -----------------------------------------------

    def disposition_record(
        self,
        receipt_sha256: str,
        *,
        signer: Any,
        approver_id: str,
        role: str,
        disposition_code: str,
        verification_method: str,
        external_reference: str,
        notes: Optional[str] = None,
    ) -> dict[str, Any]:
        """Build and sign a disposition. The reference and notes go in as HMACs only.

        Notes are kept in plaintext only in this vault, mode 0600.
        """
        entry = self._open(receipt_sha256)
        record: dict[str, Any] = {
            "schema": DISPOSITION_SCHEMA,
            "receipt_sha256": entry["receipt_sha256"],
            "execution_id": entry["execution_id"],
            "approver": {"id": approver_id, "kind": "person", "role": role},
            "dispositioned_at": self._clock(),
            "disposition_code": disposition_code,
            "verification_method": verification_method,
            "external_reference": self.hmac_digest(external_reference),
        }
        if notes:
            record["notes_hmac"] = self.hmac_digest(notes)
            self._mkdir()
            write_private_bytes(
                self._path(entry["receipt_sha256"], ".notes.txt"),
                notes.encode("utf-8"),
                overwrite=True,
            )
        record["seal_signature"] = signer.countersign(record)
        return record

    def close_by_attestation(
        self,
        receipt_sha256: str,
        record: Any,
        approver_registry: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Close an open entry with a disposition signed by a pinned person."""
        from openadapt_types.seal import (
            parse_key_registry,
            verify_seal_signature,
        )
        from openadapt_types.seal import receipt_sha256 as types_sha

        entry = self._open(receipt_sha256)
        if (
            not isinstance(record, Mapping)
            or record.get("schema") != DISPOSITION_SCHEMA
        ):
            raise CountersignError("bad_disposition", "not a disposition record")
        allowed = {
            "schema",
            "receipt_sha256",
            "execution_id",
            "approver",
            "dispositioned_at",
            "disposition_code",
            "verification_method",
            "external_reference",
            "notes_hmac",
            "seal_signature",
        }
        if set(record) - allowed:
            raise CountersignError("bad_disposition", "unknown fields")
        if (
            record.get("receipt_sha256") != entry["receipt_sha256"]
            or record.get("execution_id") != entry["execution_id"]
        ):
            raise CountersignError("bad_disposition", "it names another run")
        approver = record.get("approver")
        if (
            not isinstance(approver, Mapping)
            or set(approver) != {"id", "kind", "role"}
            or approver.get("kind") != "person"
            or not isinstance(approver.get("id"), str)
            or not _OPAQUE.match(approver["id"])
            or not isinstance(approver.get("role"), str)
            or not 1 <= len(approver["role"]) <= 64
        ):
            raise CountersignError(
                "bad_disposition", "approver is an opaque id of kind person with a role"
            )
        at = _parse_time(record.get("dispositioned_at"))
        issued = _parse_time(entry.get("issued_at"))
        if at is None or (issued is not None and at < issued):
            raise CountersignError(
                "bad_disposition", "dispositioned_at is RFC 3339 UTC after the run"
            )
        if record.get("disposition_code") not in DISPOSITION_CODES:
            raise CountersignError("bad_disposition", "unknown disposition_code")
        if record.get("verification_method") not in VERIFICATION_METHODS:
            raise CountersignError("bad_disposition", "unknown verification_method")
        if not isinstance(record.get("external_reference"), str) or not _HMAC.match(
            record["external_reference"]
        ):
            raise CountersignError(
                "bad_disposition", "external_reference is an HMAC-SHA256 digest only"
            )
        if "notes_hmac" in record and not (
            isinstance(record["notes_hmac"], str) and _HMAC.match(record["notes_hmac"])
        ):
            raise CountersignError("bad_disposition", "notes_hmac is a digest only")
        try:
            keys = parse_key_registry(list(approver_registry))
        except (ValueError, TypeError, KeyError):
            keys = []
        if not keys:
            raise CountersignError("no_pinned_approver_keys")
        signature = record.get("seal_signature")
        result = verify_seal_signature(signature, types_sha(_unsigned(record)), keys)
        if not result.ok or (signature or {}).get("run_id") != entry["execution_id"]:
            raise CountersignError(
                "bad_disposition_signature", str(getattr(result, "reason", ""))
            )
        if result.key_id == entry.get("seal_key_id") or (
            entry.get("executor_kind") in ("agent", "script")
            and approver["id"] == entry.get("executor_ref_sha256")
        ):
            raise CountersignError(
                "reviewer_is_executor", "the reviewer must not be the run's executor"
            )
        self._finish(entry, "closed_by_attestation", disposition=dict(record))
        return entry

    # -- retention -----------------------------------------------------------

    def purge(self, export_path: Path, *, now: Optional[str] = None) -> list[str]:
        """Export, then delete, terminal entries past ``retain_until``.

        Open entries are never purged. The export is written (0600) and read
        back before anything is deleted; if it fails, nothing is deleted.
        """
        at = _parse_time(now or self._clock())
        assert at is not None
        due = [
            e
            for e in self.entries()
            if e.get("status") in TERMINAL
            and (until := _parse_time(e.get("retain_until"))) is not None
            and until <= at
        ]
        if not due:
            return []
        body = {"schema": EXPORT_SCHEMA, "exported_at": _fmt(at), "entries": due}
        write_private_bytes(
            Path(export_path), json.dumps(body, indent=2, sort_keys=True).encode()
        )
        check = json.loads(Path(export_path).read_text(encoding="utf-8"))
        if check.get("entries") != due:
            raise CountersignError("export_failed")
        for e in due:
            self._path(e["receipt_sha256"]).unlink()
            self._path(e["receipt_sha256"], ".notes.txt").unlink(missing_ok=True)
        return [e["receipt_sha256"] for e in due]

    # -- ledger for the dossier ----------------------------------------------

    def ledger(
        self,
        registry: Sequence[Mapping[str, Any]],
        tsa_certificates: Sequence[Any] = (),
    ) -> dict[str, Any]:
        """Every Seal in the vault, counted so the four buckets add up.

        A genuine Seal on an unverified outcome that was never queued counts as
        open. Entries carry no field values, notes, or salts.
        """
        counts = {
            "verified": 0,
            "resolved_by_seal": 0,
            "closed_by_attestation": 0,
            "open": 0,
        }
        known = {e["receipt_sha256"]: e for e in self.entries()}
        listed: list[dict[str, Any]] = []
        seen: set[str] = set()
        root = self.vault.root
        for doc_path in sorted(root.glob("*/document.json")) if root.is_dir() else []:
            try:
                document = json.loads(doc_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            report = verify_countersigned(
                document,
                registry=registry,
                tsa_certificates=tsa_certificates,
                accept_non_flow=True,
            )
            rsha = report.receipt_sha256
            if not report.signature_genuine or rsha is None or rsha in seen:
                continue
            seen.add(rsha)
            if report.outcome_verified:
                counts["verified"] += 1
                continue
            entry = known.get(rsha) or {
                "receipt_sha256": rsha,
                "execution_id": report.execution_id,
                "status": "open",
                "verdict": report.effect_verdict or "INDETERMINATE",
                "note": "not queued",
            }
            counts[str(entry["status"])] += 1
            listed.append(entry)
        for rsha, entry in known.items():
            if rsha not in seen:  # its document left the vault; still report it
                counts[str(entry["status"])] += 1
                listed.append(entry)
        return {
            "total_seals": sum(counts.values()),
            "counts": counts,
            "exceptions": sorted(
                listed, key=lambda e: (e.get("issued_at") or "", e["receipt_sha256"])
            ),
        }
