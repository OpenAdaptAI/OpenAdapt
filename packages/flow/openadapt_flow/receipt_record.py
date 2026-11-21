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
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Optional, Sequence

if TYPE_CHECKING:
    from openadapt_types.receipt import ReceiptVerification, RoleKey

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


def _count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _is_write(result: Mapping[str, Any]) -> bool:
    return (
        result.get("effect_verified") is not None
        or result.get("effect_approved_unverified") is True
        or result.get("risk") == "irreversible"
        or bool(result.get("effect_contract_hashes"))
    )


def _line(index: int, result: Mapping[str, Any]) -> dict[str, Any]:
    effect = result.get("effect_verified")
    if effect is True:
        check, strength = "confirmed", "read_back"
    elif effect is False:
        check, strength = "refuted", "read_back"
    else:
        check = "not_checked"
        strength = "same_screen" if result.get("postconditions_ok") is True else "none"
    not_checked: list[str] = []
    if effect is None:
        not_checked.append(
            "approved_unverified"
            if result.get("effect_approved_unverified") is True
            else "effect_not_declared"
        )
    if effect is True and not result.get("effect_field_checks"):
        not_checked.append("field_values")
    if result.get("postconditions_ok") is None:
        not_checked.append("postconditions")
    return {
        "step": index,
        "target_kind": "api" if result.get("actuation") == "api" else "gui",
        "operation": (
            "irreversible_write" if result.get("risk") == "irreversible" else "write"
        ),
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


#: Signed per-write receipts a run folder may hold, each one line.
_EVIDENCE_GLOBS = (("tx_receipt", "tx-receipt.cose"), ("write_receipt", "*.receipt.cbor"))
_EVIDENCE_CHECK = {
    "CONFIRMED": ("confirmed", "read_back", []),
    "REFUTED": ("refuted", "read_back", []),
}


def evidence_lines(run_dir: Optional[Path], first_step: int) -> list[dict[str, Any]]:
    """One line per transaction or write receipt in ``run_dir``, by digest.

    The line copies the receipt's own verdict (``CONFIRMED``, ``REFUTED``;
    anything else is ``not_checked``/``uncertain``). Its ``evidence.sha256``
    names the receipt file, which verifies on its own (``openadapt verify``).
    """
    if run_dir is None or not Path(run_dir).is_dir():
        return []
    found: list[tuple[str, Path]] = []
    for kind, pattern in _EVIDENCE_GLOBS:
        found += [(kind, p) for p in sorted(Path(run_dir).rglob(pattern)) if p.is_file()]
    lines: list[dict[str, Any]] = []
    for offset, (kind, path) in enumerate(found[:64]):
        data = path.read_bytes()
        verdict = None
        try:
            from openadapt_flow.receipt_log import decode_sign1

            payload = decode_sign1(data)[3]
            body = json.loads(payload) if isinstance(payload, (bytes, str)) else {}
            verdict = body.get("outcome") if kind == "tx_receipt" else body.get("verdict")
        except Exception:  # noqa: BLE001 - unreadable: the line says not checked
            verdict = None
        check, strength, not_checked = _EVIDENCE_CHECK.get(
            str(verdict), ("not_checked", "none", ["uncertain"])
        )
        lines.append(
            {
                "step": first_step + offset,
                "target_kind": "api",
                "operation": "write",
                "check": check,
                "strength": strength,
                "not_checked": list(not_checked),
                "evidence": {"kind": kind, "sha256": hashlib.sha256(data).hexdigest()},
            }
        )
    return lines


def build_statement(
    report: Mapping[str, Any],
    report_sha256: str,
    *,
    flow_version: Optional[str] = None,
    salt: Any = None,
    run_dir: Optional[Path] = None,
) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    """The ``record/run/v1`` statement for a run report, and its private openings."""
    from openadapt_types import receipt as r

    from openadapt_flow.public_seal import _checks, _hour, _int

    results = report.get("results")
    results = (
        [x for x in results if isinstance(x, Mapping)]
        if isinstance(results, list)
        else []
    )
    every_line = [_line(i, x) for i, x in enumerate(results) if _is_write(x)]
    every_line += evidence_lines(run_dir, len(results))
    lines = every_line[: r.MAX_LINES]
    tally = {
        k: sum(1 for line in lines if line["check"] == k)
        for k in ("confirmed", "refuted", "not_checked")
    }
    outcome, reason = r.receipt_outcome(report.get("execution_outcome"))
    # Judge every write, not only the ones that fit: a refuted write past
    # the line limit still refuses VERIFIED, and so does any write the
    # receipt can't show.
    if outcome == "VERIFIED" and any(x["check"] == "refuted" for x in every_line):
        outcome, reason = "UNKNOWN", "refuted_write"
    elif outcome == "VERIFIED" and len(every_line) > r.MAX_LINES:
        outcome, reason = "UNKNOWN", "too_many_writes"
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
    statement = {
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
    tmp = path.with_name(path.name + ".tmp")
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    from openadapt_flow.private_file import open_binary

    # A raw-byte descriptor (no Windows newline translation), every byte written.
    fd = open_binary(
        tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644
    )
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
    finally:
        os.close(fd)
    os.replace(tmp, path)


def write_receipt_record(
    run_dir: Path,
    *,
    signer: Optional["SealSigner"] = None,
    home: Optional[Path] = None,
    flow_version: Optional[str] = None,
) -> dict[str, Any]:
    """Write ``receipt.json`` and ``receipt.private.json`` for a finished run.

    Idempotent: an existing receipt over the same report bytes is kept. A
    receipt over different bytes is never silently replaced.
    """
    from openadapt_types import receipt as r

    from openadapt_flow.offline_seal import loads_strict, signer_for
    from openadapt_flow.seal_keys import KeyUnavailable

    run_dir = Path(run_dir)
    report_path = run_dir / "report.json"
    try:
        raw = report_path.read_bytes()
        report = loads_strict(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ReceiptRecordError("report_unreadable", str(report_path)) from exc
    if not isinstance(report, dict):
        raise ReceiptRecordError("report_unreadable", "report.json is not an object")
    digest = hashlib.sha256(raw).hexdigest()
    out = run_dir / RECEIPT_FILE
    if out.exists():
        try:
            existing = loads_strict(out.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ReceiptRecordError("existing_unreadable", str(out)) from exc
        if r.is_receipt_envelope(existing):
            payload = loads_strict(base64.b64decode(existing["payload"]))
            subjects = {
                s["name"]: s["digest"]["sha256"] for s in payload.get("subject", [])
            }
            if subjects.get("report.json") == digest:
                private = run_dir / PRIVATE_FILE
                try:
                    opened = loads_strict(private.read_text(encoding="utf-8"))
                    write_public_card(run_dir, existing, opened.get("openings") or {})
                except (OSError, ValueError, AttributeError):
                    pass  # no private openings: the card stays as it was
                return existing
            raise ReceiptRecordError(
                "already_signed", "receipt.json covers different report bytes"
            )
        # An older run-receipt/v2 summary in receipt.json: keep it, renamed.
        if isinstance(existing, dict) and existing.get("schema_version", "").startswith(
            "openadapt.run-receipt/"
        ):
            os.replace(out, run_dir / "run-summary.json")
        else:
            raise ReceiptRecordError(
                "existing_unreadable", "receipt.json is not a receipt"
            )
    if flow_version is None:
        from openadapt_flow import __version__ as flow_version
    statement, openings = build_statement(
        report, digest, flow_version=flow_version, run_dir=run_dir
    )
    chosen = signer or signer_for(home)
    try:
        kid = r.key_id(chosen.public_raw())
        envelope = r.sign_statement(statement, [(kid, chosen.sign)])
    except KeyUnavailable as exc:
        raise ReceiptRecordError(exc.code, str(exc)) from exc
    _write(run_dir / PRIVATE_FILE, r.private_openings(envelope, openings), private=True)
    _write(out, envelope)
    write_public_card(run_dir, envelope, openings)
    return envelope


def public_reveal(run_dir: Path) -> list[str]:
    """What a run's public card opens: the Flow version; the program name only
    for the synthetic tutorial (a program name can be a person's name)."""
    from openadapt_flow.receipt import read_run_summary

    summary = read_run_summary(run_dir) or {}
    reveal = ["flow_version"]
    if summary.get("provenance") == "synthetic-tutorial":
        reveal.append("program_name")
    return reveal


def write_public_card(
    run_dir: Path,
    envelope: Mapping[str, Any],
    openings: Mapping[str, Mapping[str, str]],
) -> Path:
    """Write ``receipt.card.json``: the same signed envelope, allow-listed openings only.

    Never re-signed, so the card can't say anything the receipt doesn't. It
    holds no screenshot, typed value, URL or page text.
    """
    from openadapt_types import receipt as r

    card = r.public_card(envelope, openings, reveal=public_reveal(Path(run_dir)))
    path = Path(run_dir) / CARD_FILE
    _write(path, card)
    return path


def receipt_after_run(
    run_dir: Optional[Path], env: Optional[Mapping[str, str]] = None
) -> Optional[str]:
    """CLI hook: write the run's receipt. Never raises, never changes the exit code."""
    if run_dir is None or not mode_enabled(env):
        return None
    from openadapt_flow import sealed_set

    if sealed_set.is_sealed(Path(run_dir)):
        return "receipt not written: the run directory is encrypted at rest"
    if not (Path(run_dir) / "report.json").is_file():
        return None
    try:
        from openadapt_flow.offline_seal import seal_home, signer_for

        home = seal_home(env)
        write_receipt_record(Path(run_dir), signer=signer_for(home, env=env))
    except ImportError:
        return "receipt not written: openadapt-types is older than 0.25"
    except ReceiptRecordError as exc:
        return f"receipt not written: {exc.code}"
    except Exception as exc:  # a receipt failure must never change the run's result
        return f"receipt not written: {type(exc).__name__}"
    return f"receipt: {Path(run_dir) / RECEIPT_FILE}"


def after_run_notes(run_dir: Optional[Path]) -> list[str]:
    """CLI hook after ``run`` and ``replay``: the older offline Seal, then the receipt.

    ``OPENADAPT_LOCAL_SEAL=off`` and ``OPENADAPT_RECEIPT=off`` turn each off.
    Returns the status lines for stderr; never raises.
    """
    from openadapt_flow.offline_seal import seal_after_run

    notes = [seal_after_run(run_dir), receipt_after_run(run_dir)]
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
        if not path.is_file():
            continue
        data = path.read_bytes()
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
                result = replace(
                    result,
                    detail="covers report.pre-seal.json; report.json changed after signing",
                )
            return _with_anchor(run_dir, envelope, result, registry)
    if not (run_dir / "report.json").is_file():
        # A run folder's receipt is checked against its report: without the
        # signed bytes there is nothing to verify it over.
        return r._refuse("malformed", "the run folder has no report.json")
    return r._refuse("bad_signature", "report.json changed after it was signed")


def _with_anchor(
    run_dir: Path,
    envelope: Mapping[str, Any],
    result: "ReceiptVerification",
    registry: Sequence["RoleKey"],
) -> "ReceiptVerification":
    """Add "Logged publicly at <time>" when ``receipt.anchor.json`` verifies.

    The anchor must be signed by a key that signed the receipt and is pinned
    here. An anchor file that doesn't verify refuses the receipt.
    """
    import dataclasses
    from datetime import datetime, timezone

    from openadapt_types import receipt as r

    from openadapt_flow.seal_anchor import (
        anchor_path,
        load_anchor,
        verify_receipt_anchor,
    )

    path = anchor_path(run_dir / RECEIPT_FILE)
    if not path.is_file():
        return result
    anchor = load_anchor(run_dir / RECEIPT_FILE)
    for check in result.signers:
        key = next((k for k in registry if k.key_id == check.key_id), None)
        if key is None:
            continue
        found = verify_receipt_anchor(envelope, anchor, base64.b64decode(key.public_key))
        if found.ok and found.integrated_time is not None:
            when = datetime.fromtimestamp(found.integrated_time, timezone.utc)
            line = f"Logged publicly at {when.strftime('%Y-%m-%dT%H:%M:%SZ')}"
            return dataclasses.replace(result, signer_lines=(*result.signer_lines, line))
    return r._refuse("inconsistent", f"{path.name} doesn't verify for this receipt")
