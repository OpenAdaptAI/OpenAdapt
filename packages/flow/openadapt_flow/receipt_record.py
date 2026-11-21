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
    if mode == "not_gated" or not approved:
        return {"mode": mode, "approver_kind": "none", "commitments": {}}, {}
    kind = "person" if any(a.get("decided_by") != "automation" for a in approved) else "agent"
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
    values = {
        "approval.principal": f"{kind}:pr-"
        + hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()[:16],
        "approval.scope": hashlib.sha256("\n".join(scope).encode("utf-8")).hexdigest(),
    }
    return {"mode": mode, "approver_kind": kind, "commitments": {}}, values


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
    results = (
        [x for x in results if isinstance(x, Mapping)]
        if isinstance(results, list)
        else []
    )
    lines = [_line(i, x) for i, x in enumerate(results) if _is_write(x)][: r.MAX_LINES]
    tally = {
        k: sum(1 for line in lines if line["check"] == k)
        for k in ("confirmed", "refuted", "not_checked")
    }
    outcome, reason = r.receipt_outcome(report.get("execution_outcome"))
    if outcome == "VERIFIED" and tally["refuted"]:
        outcome, reason = "UNKNOWN", "refuted_write"
    bundle = report.get("bundle_content_digest")
    bundle = bundle if isinstance(bundle, str) and _SHA256.fullmatch(bundle) else None
    version = bundle or report_sha256
    subject = [{"name": "report.json", "digest": {"sha256": report_sha256}}]
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
    fd = os.open(
        tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644
    )
    try:
        os.write(fd, data)
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
    statement, openings = build_statement(report, digest, flow_version=flow_version)
    chosen = signer or signer_for(home)
    try:
        kid = r.key_id(chosen.public_raw())
        envelope = r.sign_statement(statement, [(kid, chosen.sign)])
    except KeyUnavailable as exc:
        raise ReceiptRecordError(exc.code, str(exc)) from exc
    _write(run_dir / PRIVATE_FILE, r.private_openings(envelope, openings), private=True)
    _write(out, envelope)
    return envelope


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
    keys += [
        k for k in _pinned_cloud_keys(home) if k.key_id not in {x.key_id for x in keys}
    ]
    return keys


def _pinned_cloud_keys(home: Optional[Path]) -> list["RoleKey"]:
    from openadapt_types.receipt import RoleKey

    path = Path(home or Path.home() / ".openadapt" / "seal") / "cloud-keys.json"
    try:
        from openadapt_flow.offline_seal import load_registry

        return [
            RoleKey(k.key_id, k.public_key, "openadapt_cloud", k.status, k.revoked_at)
            for k in load_registry(path)
        ]
    except (OSError, ValueError):
        return []


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
    subjects = {s["name"]: s["digest"]["sha256"] for s in result.statement["subject"]}
    # ``attach-seal`` keeps the signed bytes as report.pre-seal.json.
    for name in ("report.json", "report.pre-seal.json"):
        path = run_dir / name
        if path.is_file() and (
            hashlib.sha256(path.read_bytes()).hexdigest() == subjects.get("report.json")
        ):
            return result
    if not (run_dir / "report.json").is_file():
        return result
    return r._refuse("bad_signature", "report.json changed after it was signed")
