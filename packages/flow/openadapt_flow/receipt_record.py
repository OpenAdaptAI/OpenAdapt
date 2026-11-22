"""Write and verify a run's receipt (``record/run/v1``) in its run folder.

After every run, with any outcome, Flow writes:

* ``receipt.json``: a DSSE envelope over a ``record/run/v1`` statement,
  signed with this computer's key (the same key the older offline Seal used);
* ``receipt.private.json``: the salts and values behind the statement's
  commitments (mode 0600; never uploaded).

The statement holds digests, enums, counts and salted commitments only. It
never holds a screenshot, a typed value, a URL or page text; the report and
bundle appear only by SHA-256. Each write the run made is one line, with its
check, the check's strength and what it didn't check. Signing isn't grading:
a stopped run gets a receipt, and its outcome is copied, never upgraded.

The schema, verifier and vectors live in :mod:`openadapt_types.receipt`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Optional, Sequence

if TYPE_CHECKING:
    from openadapt_types.receipt import ReceiptVerification, RoleKey

    from openadapt_flow.ir import RunReport
    from openadapt_flow.seal_keys import SealSigner

RECEIPT_FILE = "receipt.json"
PRIVATE_FILE = "receipt.private.json"
CARD_FILE = "receipt.card.json"
ENV_MODE = "OPENADAPT_RECEIPT"
_SHA256 = re.compile(r"[a-f0-9]{64}")


class ReceiptRecordError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def mode_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """On by default; ``OPENADAPT_RECEIPT=off`` turns it off."""
    env = os.environ if env is None else env
    return env.get(ENV_MODE, "on").strip().lower() not in ("0", "off", "false", "no")


def _line(
    index: int, result: Mapping[str, Any], ledger: Mapping[str, Any] | None
) -> dict[str, Any]:
    from openadapt_flow.check_level import CheckLevel, from_verification_tier

    evidence = result.get("effect_evidence") or []
    declared = Counter(result.get("effect_contract_hashes") or [])
    ledger_effects = ledger["effects"] if ledger is not None else []
    # A declared effect can lack a bound hash when the run stopped before the
    # reader ran. Keep the attempted write and withhold its verification.
    ledger_matches = (
        ledger is None
        or Counter(effect.get("contract_hash") for effect in ledger_effects) == declared
    )
    observed = Counter(item["effect_contract_hash"] for item in evidence)
    check = "not_checked"
    strength = "same_screen" if result.get("postconditions_ok") is True else "none"
    # A negative aggregate flag covers both a contradiction and an unavailable
    # reader. Only complete, contract-bound read evidence distinguishes them.
    if declared and ledger_matches and declared == observed:
        verdicts = {item["final_verdict"] for item in evidence}
        levels = [
            from_verification_tier(item["verification_tier"])
            if type(item.get("verification_tier")) is int
            else CheckLevel.UNCHECKED
            for item in evidence
        ]
        if verdicts <= {"confirmed", "refuted"} and min(levels) > CheckLevel.UNCHECKED:
            if "refuted" in verdicts:
                check = "refuted"
            elif (
                verdicts == {"confirmed"}
                and result.get("effect_verified") is True
                and all(item.get("observed_effect") == "present" for item in evidence)
                and (ledger is None or ledger.get("status") == "confirmed")
            ):
                check = "confirmed"
            if check != "not_checked":
                strength = (
                    "read_back"
                    if min(levels) >= CheckLevel.SYSTEM_OF_RECORD
                    else "same_screen"
                )
    not_checked: list[str] = []
    if result.get("effect_approved_unverified") is True:
        not_checked.append("approved_unverified")
    if not declared and not ledger_effects:
        not_checked.append("effect_not_declared")
    if not result.get("effect_field_checks"):
        not_checked.append("field_values")
    if result.get("postconditions_ok") is None:
        not_checked.append("postconditions")
    return {
        "step": index,
        "target_kind": "api" if result.get("actuation") == "api" else "gui",
        "operation": "irreversible_write"
        if result.get("risk") == "irreversible"
        else "write",
        "check": check,
        "strength": strength,
        "not_checked": not_checked,
    }


def _approval(
    report: Mapping[str, Any],
) -> tuple[Optional[dict[str, Any]], dict[str, Optional[str]]]:
    """``predicate.approval`` from the report's item-approval block, and its values.

    The source is Flow's approval binding (``report.approval``,
    ``openadapt.approval-mode-item/v1``): its mode, and each approved
    ``ItemApprovalRef``. The approver's id is committed, never shown.
    """
    block = report.get("approval")
    if not isinstance(block, Mapping):
        return None, {}
    mode = block.get("mode")
    if mode not in ("each_item", "problems_only", "not_gated"):
        return None, {}
    approved = [
        a
        for a in block.get("approvals") or []
        if isinstance(a, Mapping) and a.get("decision") == "approved"
    ]
    # The item's final decision decides: earlier "approved" answers never
    # make a declined, waiting or unanswered item read as approved.
    if mode == "not_gated" or block.get("decision") != "approved" or not approved:
        return {"mode": mode, "approver_kind": "none", "commitments": {}}, {}
    # The weakest approver counts: one delegate's answer among people's makes
    # the whole run "approved by an agent delegate", never "by a person".
    kind = (
        "person" if all(a.get("decided_by") == "human" for a in approved) else "agent"
    )
    ids = sorted(
        str(
            a.get("approver_id")
            or (a.get("approver") or {}).get("id")
            or a.get("approver_kind")
            or "unknown"
        )
        for a in approved
    )
    scope = sorted(str(a.get("request_sha256", "")) for a in approved)
    values: dict[str, Optional[str]] = {
        "approval.principal": f"{kind}:pr-"
        + hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()[:16],
        "approval.scope": hashlib.sha256("\n".join(scope).encode("utf-8")).hexdigest(),
    }
    return {"mode": mode, "approver_kind": kind, "commitments": {}}, values


def _write_lines(report: Mapping[str, Any], results: list[Any]) -> list[dict[str, Any]]:
    from openadapt_types.receipt import MAX_LINES

    from openadapt_flow.ir import StepResult
    from openadapt_flow.transaction import _attempt_state, _is_consequential_result

    # ChangeLedgerEntry.position counts executed (non-skipped) results. A
    # position, unlike the step ID alone, also identifies repeated loop steps.
    ledger = report.get("change_ledger", [])
    if not isinstance(ledger, list) or not all(
        isinstance(row, Mapping)
        and type(row.get("position")) is int
        and row["position"] > 0
        for row in ledger
    ):
        raise ReceiptRecordError("invalid_change_ledger", "invalid position")
    rows = {row["position"]: row for row in ledger}
    if len(rows) != len(ledger):
        raise ReceiptRecordError("invalid_change_ledger", "duplicate position")
    position = 0
    lines = []
    for index, result in enumerate(results):
        if result.get("skipped") is True:
            continue
        position += 1
        row = rows.pop(position, None)
        if row is not None and row["step_id"] != result.get("step_id"):
            raise ReceiptRecordError("invalid_change_ledger", "step mismatch")
        step = StepResult.model_validate(result)
        if _attempt_state(step) == "not_actuated":
            if row is not None:
                raise ReceiptRecordError("invalid_change_ledger", "undelivered step")
            continue
        if row is not None or _is_consequential_result(step):
            lines.append(_line(index, result, row))
    if rows:
        raise ReceiptRecordError("invalid_change_ledger", "missing result")
    if len(lines) > MAX_LINES:
        raise ReceiptRecordError(
            "too_many_writes", "the complete write list exceeds the receipt limit"
        )
    return lines


def build_statement(
    report: Mapping[str, Any],
    report_sha256: str,
    *,
    flow_version: Optional[str] = None,
    salt: Any = None,
) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    """The ``record/run/v1`` statement for a run report, and its private openings."""
    from openadapt_types import receipt as r

    from openadapt_flow.public_seal import _checks, _hour, _int

    results = report.get("results")
    if not isinstance(results, list) or not all(
        isinstance(x, Mapping) for x in results
    ):
        raise ReceiptRecordError("invalid_results", "expected the ordered run results")
    try:
        lines = _write_lines(report, results)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ReceiptRecordError("invalid_write_evidence") from exc
    tally = {
        k: sum(1 for line in lines if line["check"] == k)
        for k in ("confirmed", "refuted", "not_checked")
    }
    outcome, reason = r.receipt_outcome(report.get("execution_outcome"))
    if outcome == "VERIFIED" and results and "change_ledger" not in report:
        outcome, reason = "UNKNOWN", "incomplete_write_coverage"
    if outcome == "VERIFIED" and (tally["refuted"] or tally["not_checked"]):
        outcome, reason = "UNKNOWN", "unverified_write"
    bundle = report.get("bundle_content_digest")
    bundle = bundle if isinstance(bundle, str) and _SHA256.fullmatch(bundle) else None
    version = bundle or report_sha256
    subject = [{"name": "report.json", "digest": {"sha256": report_sha256}}]
    try:
        subject.append(
            {
                "name": "report.canonical",
                "digest": {"sha256": r.report_canonical_sha256(report)},
            }
        )
    except ValueError:  # a report JSON can't express canonically: file digest only
        pass
    if bundle:
        subject.append({"name": "bundle", "digest": {"sha256": bundle}})
    name = report.get("workflow_name")
    values: dict[str, Optional[str]] = {
        "program_name": name
        if isinstance(name, str) and r.DISCLOSURE_FIELDS["program_name"].fullmatch(name)
        else None,
        "flow_version": flow_version
        if flow_version and r.DISCLOSURE_FIELDS["flow_version"].fullmatch(flow_version)
        else None,
        "run_id": None,
    }
    run_id = report.get("run_id")
    if isinstance(run_id, str) and r.DISCLOSURE_FIELDS["run_id"].fullmatch(run_id):
        values["run_id"] = run_id
    approval, approval_values = _approval(report)
    commitments, openings = r.make_openings({**values, **approval_values}, salt=salt)
    if approval is not None:
        approval["commitments"] = {
            path.split(".", 1)[1]: commitments.pop(path) for path in approval_values
        }
    checks = _checks(report)
    statement: dict[str, Any] = {
        "_type": r.STATEMENT_TYPE,
        "subject": subject,
        "predicateType": r.PREDICATE_TYPE,
        "predicate": {
            "program": {"id": r.program_id(name, version), "version_sha256": version},
            "started_hour": _hour(report.get("started_at")) or "1970-01-01T00:00:00Z",
            "outcome": outcome,
            "reason_code": reason,
            "steps": {
                "total": len(results),
                "ok": sum(1 for x in results if x.get("ok") is True),
            },
            "lines": lines,
            "checks": {
                **{
                    k: {
                        "required": v["required"],
                        "confirmed": min(v["confirmed"], v["required"]),
                    }
                    for k, v in checks.items()
                },
                "lines_confirmed": tally["confirmed"],
                "lines_refuted": tally["refuted"],
                "lines_not_checked": tally["not_checked"],
            },
            "model_calls": _int(report.get("model_calls")),
            "disclosure": {
                "version": r.PUBLIC_DISCLOSURE_VERSION,
                "commitments": commitments,
            },
        },
    }
    if approval is not None:
        statement["predicate"]["approval"] = approval
    return statement, openings


def _write(path: Path, payload: Mapping[str, Any], *, private: bool = False) -> None:
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if not private:
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def write_receipt_record(
    run_dir: Path,
    *,
    signer: Optional["SealSigner"] = None,
    home: Optional[Path] = None,
    flow_version: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    expected_report: Optional["RunReport"] = None,
    handoff_ledgers: Sequence[Any] = (),
) -> dict[str, Any]:
    """Write ``receipt.json`` and ``receipt.private.json`` for a finished run.

    ``handoff_ledgers`` (``openadapt_flow.handoffs.HandoffLedger``): the run's
    handoffs in them go on the receipt as counterparty confirmation lines.

    Idempotent: an existing receipt over the same report bytes is kept. A
    receipt over different bytes is never silently replaced.
    """
    from openadapt_flow.sealed_maintenance import mutation_lock

    with mutation_lock(run_dir):
        return _write_locked(
            Path(run_dir),
            signer,
            home,
            flow_version,
            env,
            expected_report,
            handoff_ledgers,
        )


def _write_locked(
    run_dir: Path,
    signer: Optional["SealSigner"],
    home: Optional[Path],
    flow_version: Optional[str],
    env: Optional[Mapping[str, str]],
    expected_report: Optional["RunReport"],
    handoff_ledgers: Sequence[Any] = (),
) -> dict[str, Any]:
    from openadapt_types import receipt as r

    from openadapt_flow import sealed_set
    from openadapt_flow.offline_seal import signer_for
    from openadapt_flow.runtime.durable.checkpoint import CheckpointStore
    from openadapt_flow.seal_keys import KeyUnavailable

    # Private openings need encrypted-set support before they can be persisted
    # beside an encrypted report. A direct caller has the same boundary as CLI.
    if sealed_set.is_sealed(run_dir):
        raise ReceiptRecordError(
            "encrypted_run", "private openings require encrypted storage"
        )
    try:
        pending = CheckpointStore(run_dir).read_pending()
    except Exception as exc:
        raise ReceiptRecordError(
            "settlement_unknown", "cannot read durable run state"
        ) from exc
    if pending is not None and pending.status in {"pending", "approved", "continuing"}:
        raise ReceiptRecordError(
            "restartable_run", "the durable run can still continue"
        )
    path = run_dir / "report.json"
    try:
        raw = sealed_set.read_bytes(path)
        report = r.loads_strict(raw)
    except (OSError, ValueError) as exc:
        raise ReceiptRecordError("report_unreadable", str(path)) from exc
    if expected_report is not None and expected_report.report_bytes() != raw:
        raise ReceiptRecordError(
            "report_changed", "the saved report differs from this run"
        )
    if not isinstance(report, dict):
        raise ReceiptRecordError("report_unreadable", "report.json is not an object")
    awaiting = report.get("transaction_outcome") == "SAVED_AWAITING_CHECK"
    if awaiting and not handoff_ledgers:
        raise ReceiptRecordError("readback_pending")
    digest = hashlib.sha256(raw).hexdigest()
    statement, openings = build_statement(report, digest, flow_version=flow_version)
    placed: list[Any] = []
    if handoff_ledgers:
        from openadapt_flow.handoffs.line import (
            attach_confirmations,
            handoffs_for_run,
            place,
        )

        handoffs = handoffs_for_run(report, list(handoff_ledgers), run_dir)
        if handoffs:
            try:
                placed = place(statement, handoffs)
                statement, openings = attach_confirmations(
                    statement, openings, handoffs
                )
            except ValueError as exc:
                raise ReceiptRecordError("handoff_unplaceable", str(exc)) from exc
    if awaiting:
        statement = _awaiting_counterparty(statement, placed)
    chosen = signer or signer_for(home, env=env)
    kid = r.key_id(chosen.public_raw())
    entry = chosen.registry_entry()
    registry = [r.RoleKey(kid, entry["public_key"], "this_computer")]
    out = run_dir / RECEIPT_FILE
    private = run_dir / PRIVATE_FILE
    legacy = None
    if out.exists():
        try:
            existing = r.loads_strict(out.read_bytes())
            if r.is_receipt_envelope(existing):
                checked = r.verify_receipt(
                    existing, registry, openings=r.loads_strict(private.read_bytes())
                )
                if (
                    not checked.ok
                    or len(checked.signers) != 1
                    or checked.statement is None
                ):
                    raise ReceiptRecordError("existing_unverified", str(checked.reason))
                if {s["name"]: s["digest"] for s in checked.statement["subject"]} != {
                    s["name"]: s["digest"] for s in statement["subject"]
                }:
                    raise ReceiptRecordError(
                        "already_signed",
                        "receipt subjects differ from the settled report",
                    )
                previous = checked.statement["predicate"]
                expected = statement["predicate"]
                # The verifier already bound every supplied opening to the
                # signed commitments. New random salts cannot be compared.
                for facts in (previous, expected):
                    facts["disclosure"].pop("commitments")
                    if facts.get("approval") is not None:
                        facts["approval"].pop("commitments")
                expected_values = {
                    name: value["value"] for name, value in openings.items()
                }
                if previous != expected or checked.disclosed != expected_values:
                    raise ReceiptRecordError(
                        "already_signed", "receipt facts differ from the settled report"
                    )
                return existing
        except (OSError, ValueError) as exc:
            raise ReceiptRecordError("existing_unreadable", str(out)) from exc
        if (
            not isinstance(existing, dict)
            or existing.get("schema_version") != "openadapt.run-receipt/v2"
        ):
            raise ReceiptRecordError("existing_unreadable", "unrecognized receipt file")
        legacy = run_dir / "run-summary.json"
        if legacy.exists() and legacy.read_bytes() != out.read_bytes():
            raise ReceiptRecordError(
                "legacy_summary_conflict", "both summary files exist and differ"
            )
    try:
        envelope = r.sign_statement(statement, [(kid, chosen.sign)])
    except KeyUnavailable as exc:
        raise ReceiptRecordError(exc.code, str(exc)) from exc
    if sealed_set.read_bytes(path) != raw:
        raise ReceiptRecordError("report_changed", "report changed while signing")
    if legacy is not None and not legacy.exists():
        # Preserve the original bytes; an error before envelope installation
        # leaves the legacy file and any earlier summary readable.
        os.link(out, legacy)
    _write(private, r.private_openings(envelope, openings), private=True)
    _write(out, envelope)
    if placed:
        from openadapt_flow.handoffs.followup import record_signed

        record_signed(run_dir, RECEIPT_FILE, placed)
    return envelope


AWAITING_REASON = "awaiting_counterparty"


def _awaiting_counterparty(
    statement: dict[str, Any], placed: Sequence[Any]
) -> dict[str, Any]:
    """A receipt for a run whose only unsettled writes are handoffs that went
    out and await the other party (permissive-default guarded).

    Allowed only when every unverified write line is a declared handoff line
    still in the ``sent`` state; anything else refuses ``readback_pending``.
    The outcome is never VERIFIED: it is UNKNOWN with reason
    ``awaiting_counterparty``, and follow-ups record the settlement."""
    lines = statement["predicate"].get("lines", [])
    pending = [line for line in lines if line["check"] != "confirmed"]
    sent_steps = {h.step for h in placed if h.step is not None and h.state == "sent"}
    if (
        not pending
        or any(line["check"] == "refuted" for line in pending)
        or any(line["step"] not in sent_steps for line in pending)
    ):
        raise ReceiptRecordError("readback_pending")
    predicate = statement["predicate"]
    return {
        **statement,
        "predicate": {
            **predicate,
            "outcome": "UNKNOWN",
            "reason_code": AWAITING_REASON,
        },
    }


def save_settled_report(run_dir: Path, report: "RunReport") -> "RunReport":
    """Save the final read-back before issuing the pooled item's opted-in receipt."""
    report.save(run_dir)
    receipt_after_run(run_dir, require_opt_in=True, expected_report=report)
    return report


def receipt_after_run(
    run_dir: Optional[Path],
    env: Optional[Mapping[str, str]] = None,
    *,
    require_opt_in: bool = False,
    expected_report: Optional["RunReport"] = None,
) -> Optional[str]:
    """CLI hook: write the run's receipt. Never raises, never changes the exit code."""
    env = os.environ if env is None else env
    if require_opt_in and env.get(ENV_MODE, "").strip().lower() not in {
        "1",
        "on",
        "true",
        "yes",
    }:
        return None
    if run_dir is None or not mode_enabled(env):
        return None
    from openadapt_flow import sealed_set

    if sealed_set.is_sealed(Path(run_dir)):
        return "receipt not written: the run directory is encrypted at rest"
    if not (Path(run_dir) / "report.json").is_file():
        return None
    try:
        from openadapt_flow.offline_seal import seal_home

        from openadapt_flow.handoffs.registry import ledgers_for

        write_receipt_record(
            Path(run_dir),
            home=seal_home(env),
            env=env,
            expected_report=expected_report,
            handoff_ledgers=ledgers_for(env),
        )
    except ImportError:
        return "receipt not written: openadapt-types is older than 0.25"
    except ReceiptRecordError as exc:
        return f"receipt not written: {exc.code}"
    except Exception as exc:  # a receipt failure must never change the run's result
        return f"receipt not written: {type(exc).__name__}"
    return f"receipt: {Path(run_dir) / RECEIPT_FILE}"


def after_run_notes(
    run_dir: Optional[Path], *, include_receipt: bool = True
) -> list[str]:
    """Return the legacy Seal note and, when requested, the signed receipt note."""
    from openadapt_flow.offline_seal import seal_after_run

    notes = [seal_after_run(run_dir)]
    if include_receipt:
        notes.append(receipt_after_run(run_dir))
    return [note for note in notes if note]


# -- verifying -------------------------------------------------------------------


def local_registry(
    home: Optional[Path] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
    extra: Sequence["RoleKey"] = (),
) -> list["RoleKey"]:
    """This computer's key as ``this_computer``, plus pinned Cloud keys, plus ``extra``.

    Demo keys are never trusted by default.
    """
    from openadapt_types.receipt import RoleKey

    from openadapt_flow.offline_seal import public_key_entry, seal_home, signer_for

    keys: list[RoleKey] = list(extra)
    try:
        home = home or seal_home(env)
        entry = public_key_entry(home, signer=signer_for(home, env=env))
        if entry["key_id"] not in {k.key_id for k in keys}:
            keys.append(RoleKey(entry["key_id"], entry["public_key"], "this_computer"))
    except Exception:  # noqa: BLE001 - no key on this computer: trust nothing local
        pass
    # Lost-revocation class: a key listed twice keeps the revoked entry, so a
    # revocation from any source survives the merge.
    for k in _pinned_cloud_keys(home):
        same = [i for i, x in enumerate(keys) if x.key_id == k.key_id]
        if not same:
            keys.append(k)
        elif k.status == "revoked":
            for i in same:
                if keys[i].status != "revoked":
                    keys[i] = replace(
                        keys[i], status="revoked", revoked_at=k.revoked_at
                    )
    return keys


def _pinned_cloud_keys(home: Optional[Path]) -> list["RoleKey"]:
    """Cloud keys pinned with ``openadapt receipt keys pin`` (``seal keys pin``).

    The same file the older Cloud Seal check reads
    (:func:`openadapt_flow.agent.seal.load_pinned_registry`).
    """
    from openadapt_types.receipt import RoleKey

    try:
        from openadapt_flow.agent.seal import load_pinned_registry

        entries = load_pinned_registry()
    except Exception:  # noqa: BLE001 - nothing pinned: trust no Cloud key
        return []
    return [
        RoleKey(
            str(k["key_id"]),
            base64.b64encode(k["raw"]).decode("ascii"),
            "openadapt_cloud",
            k["status"],
            k.get("revoked_at"),
        )
        for k in entries
    ]


def load_role_registry(path: Path) -> list["RoleKey"]:
    from openadapt_types.receipt import parse_registry

    from openadapt_flow.offline_seal import loads_strict

    return parse_registry(loads_strict(Path(path).read_text(encoding="utf-8")))


def verify_run_receipt(
    run_dir: Path, registry: Sequence["RoleKey"]
) -> "ReceiptVerification":
    """Verify ``run_dir/receipt.json`` and check it covers the run's report bytes."""
    from openadapt_types import receipt as r

    from openadapt_flow import sealed_set
    from openadapt_flow.offline_seal import loads_strict

    run_dir = Path(run_dir)
    try:
        envelope = loads_strict((run_dir / RECEIPT_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return r._refuse("unsigned", "no receipt.json")
    except (OSError, ValueError):
        return r._refuse("malformed", "receipt.json is not JSON")
    openings = None
    private = run_dir / PRIVATE_FILE
    if private.is_file():
        try:
            openings = loads_strict(private.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return r._refuse("malformed", "receipt.private.json is not JSON")
    result = r.verify_receipt(envelope, registry, openings=openings)
    if not result.ok or result.statement is None:
        return result
    # ``attach-seal`` keeps the signed bytes as report.pre-seal.json. The
    # file digest is checked against ``report.json`` and the canonical digest
    # against ``report.canonical``, never one against the other.
    for name in ("report.json", "report.pre-seal.json"):
        path = run_dir / name
        if not sealed_set.is_file(path):
            continue
        try:
            data = sealed_set.read_bytes(path)
        except (OSError, sealed_set.SealedSetError):
            return r._refuse("malformed", "the covered report is unreadable")
        try:
            canonical = r.report_canonical_sha256(loads_strict(data.decode("utf-8")))
        except (UnicodeDecodeError, ValueError):
            canonical = None
        if r.report_subject_match(
            result.statement, file_bytes=data, canonical_sha256=canonical
        ):
            if name != "report.json" and (run_dir / "report.json").is_file():
                # Say which bytes it covers: never a silent pass for a
                # report.json that changed after signing.
                return replace(
                    result,
                    detail="covers report.pre-seal.json; report.json changed after signing",
                )
            return result
    if not sealed_set.is_file(run_dir / "report.json"):
        # A run folder's receipt is checked against its report: without the
        # signed bytes there is nothing to verify it over.
        return r._refuse("malformed", "the run folder has no report.json")
    return r._refuse("bad_signature", "report.json changed after it was signed")
