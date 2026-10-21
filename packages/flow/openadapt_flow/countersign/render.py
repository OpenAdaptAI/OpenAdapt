"""Plain-text rendering of a :class:`CountersignReport`.

Written so an agent receipt can never read like a Flow replay: the executor
line is always printed, always second, and says who declared it.
"""

from __future__ import annotations

from typing import Any

from openadapt_flow.countersign.document import (
    EFFECT_RECEIPT_SCHEMA,
    EXECUTE_RECEIPT_SCHEMA,
    CountersignReport,
)
from openadapt_flow.readback_ladder import CHECKED_HOW_LABELS

__all__ = ["executor_line", "headline", "render_report", "tier_line"]

_SEAL_TEXT = {
    "verified": "VERIFIED against a pinned key",
    "unsigned": "ABSENT: this receipt is unsigned and is not a Seal",
    "malformed": "MALFORMED",
    "receipt_mismatch": "FAILED: the receipt changed after it was signed",
    "unknown_key": "FAILED: signed by a key you have not pinned",
    "revoked_key": "FAILED: signed by a key revoked before this Seal was issued",
    "bad_signature": "FAILED: the signature does not match",
    "no_pinned_keys": "NOT CHECKED: no Seal keys are pinned",
    "verifier_unavailable": "NOT CHECKED: install cryptography",
    "no_receipt": "no receipt found in this document",
}

_TS_TEXT = {
    "verified": "VERIFIED",
    "absent": "absent",
    "not_checked": "NOT CHECKED: no TSA certificate pinned (--tsa-cert)",
    "imprint_mismatch": "FAILED: the token covers a different statement",
    "untrusted_tsa": "FAILED: the TSA does not chain to a pinned certificate",
    "bad_signature": "FAILED: the TSA signature does not verify",
    "content_mismatch": "FAILED: the token content was altered",
    "signer_certificate_mismatch": "FAILED: the token names a different TSA certificate",
    "not_a_tsa_certificate": "FAILED: the signer certificate is not a TSA certificate",
    "certificate_not_valid_at_gen_time": "FAILED: the TSA certificate was not valid at that time",
    "binding_mismatch": "FAILED: the baseline token names another run or baseline",
    "after_receipt": "FAILED: the baseline was stamped after the receipt",
    "malformed": "MALFORMED",
    "unsupported_algorithm": "FAILED: unsupported signature algorithm",
    "unsupported_critical_extension": "FAILED: the token has a critical extension this verifier does not know",
    "verifier_unavailable": "NOT CHECKED: install openadapt-flow[countersign]",
}

_EXECUTORS = {
    "agent": "AGENT: a computer-use agent acted. This is not a Flow replay",
    "script": "SCRIPT: a script acted. This is not a Flow replay",
    "human": "HUMAN: a person acted. This is not a Flow replay",
    "unknown": "UNKNOWN: the run record did not say what acted",
}


def executor_line(report: CountersignReport) -> str:
    kind = report.executor_kind
    if report.receipt_schema == EXECUTE_RECEIPT_SCHEMA:
        return (
            "flow_replay (implied by openadapt.execute-evidence-receipt/v1, which "
            "Cloud issues only for a Flow runner it verified)"
        )
    if report.receipt_schema != EFFECT_RECEIPT_SCHEMA:
        return "not stated"
    if kind == "flow_replay":
        return (
            "flow_replay, DECLARED by the run record and not attested by an "
            "issuer; treat as unknown"
        )
    return _EXECUTORS.get(str(kind), f"{kind} (unrecognized; treat as unknown)") + (
        " (declared by the run record)"
    )


_CHECKED_HOW_TEXT = {
    code: f"{code} ({label})" for code, label in CHECKED_HOW_LABELS.items()
}


_PART_TEXT = {
    "delta": "the declared change",
    "frame": "nothing else in the slice changed",
    "source": "the inputs match their source",
}


def checked_line(checked: dict[str, Any]) -> str:
    """One clause per part of the claim, in the receipt's own words."""

    out = []
    for part in ("delta", "frame", "source"):
        state = str(checked.get(part))
        if part == "source" and state == "instruction":
            out.append("source: the inputs were the authorized instruction")
        else:
            out.append(f"{part} ({_PART_TEXT[part]}): {state}")
    cover = checked.get("coverage")
    if isinstance(cover, dict):
        out.append(
            f"fault coverage: {cover.get('killed')} of {cover.get('applicable')} "
            f"faults caught, admission policy {cover.get('policy')}"
        )
    return "; ".join(out)


def tier_line(report: CountersignReport) -> str:
    if report.receipt_schema == EXECUTE_RECEIPT_SCHEMA:
        return f"not stated by Execute v1 (oracle_tier {report.oracle_tier})"
    tier = report.verification_tier
    meaning = {
        "independent_readback": "the verifier re-read the system of record or a separate session",
        "screen_readback": "only the acting session or screen was re-read; cannot yield verified",
        "unchecked": "the effect was not read; cannot yield verified",
    }.get(str(tier), "unrecognized")
    return f"{tier} ({meaning}; oracle_tier {report.oracle_tier})"


def headline(report: CountersignReport) -> str:
    if report.seal.get("status") != "verified":
        return "NOT VERIFIED: Seal signature " + str(report.seal.get("status"))
    if report.problems:
        return "NOT VERIFIED: " + ", ".join(report.problems)
    if not report.outcome_verified:
        return f"SIGNATURE GENUINE, OUTCOME NOT VERIFIED ({report.outcome})"
    if report.receipt_schema == EFFECT_RECEIPT_SCHEMA and report.non_flow:
        kind = str(report.executor_kind).upper()
        if not report.accept_non_flow:
            return (
                f"SIGNATURE GENUINE, NOT A FLOW REPLAY ({kind}). "
                "Pass --accept-non-flow to accept a receipt from another executor"
            )
        return f"SEAL VERIFIED: {kind} RUN (not a Flow replay)"
    return "SEAL VERIFIED (Flow replay)"


def _ts(entry: dict[str, Any]) -> str:
    status = str(entry.get("status"))
    text = _TS_TEXT.get(status, status)
    if status == "verified":
        accuracy = entry.get("accuracy_seconds")
        text += f" {entry.get('gen_time')}" + (
            f" (accuracy {accuracy:g} s)"
            if isinstance(accuracy, (int, float))
            else " (the TSA states no accuracy)"
        )
        text += f" by {entry.get('tsa_subject')}"
        text += (
            f"; policy {entry.get('policy') or 'not stated'}"
            f", serial {entry.get('serial_number') or 'not stated'}"
        )
        if entry.get("trust_anchor_subject") != entry.get("tsa_subject"):
            text += f" (chains to pinned {entry.get('trust_anchor_subject')})"
    return text


_TIMING_TEXT = {
    "backdated_receipt": "the timestamp is later than issued_at by more than the limit",
    "late_timestamp": "the timestamp came much later than issued_at",
    "clock_skew": "issued_at is later than the timestamp by more than the limit",
    "issued_at_unreadable": "issued_at is missing or unreadable",
}


def timing_line(report: CountersignReport) -> str:
    t = report.timing
    if not t:
        return "not checked (no verified receipt timestamp)"
    if "delta_seconds" not in t:
        return f"WARNING {t.get('status')}: {_TIMING_TEXT.get(str(t.get('status')))}"
    delta = f"genTime - issued_at = {t['delta_seconds']:+d} s (limit {t['limit_seconds']} s)"
    if t["status"] == "ok":
        return f"ok, {delta}"
    return f"WARNING {t['status']}: {_TIMING_TEXT[t['status']]}; {delta}"


def render_report(report: CountersignReport, *, show_names: bool = True) -> str:
    lines = [headline(report)]
    lines += [f"WARNING: {timing_line(report)}" for _ in report.warnings[:1]]
    lines.append("")
    rows: list[tuple[str, str]] = [
        ("Executor", executor_line(report)),
        ("Verification tier", tier_line(report)),
        *(
            [("Checked how", _CHECKED_HOW_TEXT.get(report.checked_how, "unrecognized"))]
            if report.checked_how is not None
            else []
        ),
        *(
            [("Checked", checked_line(report.checked))]
            if report.checked is not None
            else []
        ),
        ("Effect verdict", str(report.effect_verdict or "not stated (Execute v1)")),
        ("Outcome", str(report.outcome)),
        ("Receipt schema", str(report.receipt_schema)),
        ("Execution", str(report.execution_id)),
        ("Receipt sha256", str(report.receipt_sha256)),
    ]
    seal = report.seal
    seal_text = _SEAL_TEXT.get(str(seal.get("status")), str(seal.get("status")))
    if seal.get("status") == "verified":
        seal_text += f" ({seal.get('key_id')}, issued {seal.get('issued_at')})"
    rows.append(("Seal signature", seal_text))
    ev = report.evidence.get("status")
    rows.append(
        (
            "Evidence manifest",
            {
                "bound": "bound: digest and labels match the receipt",
                "absent": "absent (not checked)",
            }.get(str(ev), "FAILED: the manifest does not match the receipt"),
        )
    )
    st = report.statement.get("status")
    if st == "absent":
        rows.append(
            (
                "Countersign block",
                "absent: no timestamp, countersignature, or commitments",
            )
        )
    else:
        rows.append(
            (
                "Statement",
                "matches the receipt"
                if st == "matches_receipt"
                else "FAILED: does not match the receipt",
            )
        )
        rows.append(("Receipt timestamp", _ts(report.receipt_timestamp)))
        rows.append(("Timestamp skew", timing_line(report)))
        rows.append(("Baseline timestamp", _ts(report.baseline_timestamp)))
        cs = report.countersignature
        cs_text = _SEAL_TEXT.get(str(cs.get("status")), str(cs.get("status")))
        if cs.get("status") == "absent":
            cs_text = "absent"
        elif cs.get("status") == "verified":
            cs_text += f" ({cs.get('key_id')}, issued {cs.get('issued_at')})"
        rows.append(("Countersignature", cs_text))
        cm = report.commitments
        if cm.get("status") == "absent":
            rows.append(("Field commitments", "absent"))
        else:
            how = (
                " and ".join(cm.get("bound_by") or [])
                or "nothing: a disclosure proves nothing"
            )
            rows.append(
                ("Field commitments", f"{cm.get('count')} committed; bound by {how}")
            )
    if report.disclosures:
        for i, disclosure in enumerate(report.disclosures, 1):
            parts = []
            for f in disclosure.get("fields", []):
                name = f" ({f.get('name')})" if show_names and f.get("name") else ""
                parts.append(
                    f"{f.get('label')}{name} {'OK' if f.get('status') == 'matches' else 'FAILED ' + str(f.get('status'))}"
                )
            rows.append(
                (f"Disclosure {i}", "; ".join(parts) or str(disclosure.get("status")))
            )
    else:
        rows.append(("Disclosures", "none checked"))
    width = max(len(k) for k, _ in rows)
    lines += [f"  {k.ljust(width)}  {v}" for k, v in rows]
    lines += [
        "",
        "A signature proves who said it; a timestamp proves when it existed. Neither proves the record is true.",
    ]
    return "\n".join(lines)
