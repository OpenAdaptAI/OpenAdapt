"""Regenerate the record/run/v1 test vectors: ``python generate.py``.

The keys are derived from public labels (``sha256("openadapt receipt test
vector: <role>")``), so they are test keys anyone can recompute, never
secrets. Salts are derived the same way, so the output is byte-stable.
Run it from any directory; it writes next to itself.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))

from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)
from cryptography.hazmat.primitives.serialization import (  # noqa: E402
    Encoding,
    PublicFormat,
)

from openadapt_types import receipt as r  # noqa: E402


def _key(label: str) -> Ed25519PrivateKey:
    seed = hashlib.sha256(f"openadapt receipt test vector: {label}".encode()).digest()
    return Ed25519PrivateKey.from_private_bytes(seed)


def _raw(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def _salts():
    counter = [0]

    def make() -> str:
        counter[0] += 1
        return hashlib.sha256(f"vector salt {counter[0]}".encode()).hexdigest()

    return make


KEYS = {
    label: _key(label)
    for label in ("this_computer", "customer_key", "openadapt_cloud", "demo", "stranger")
}
IDS = {label: r.key_id(_raw(key)) for label, key in KEYS.items()}


def signer(label: str):
    return IDS[label], KEYS[label].sign


REGISTRY = {
    "schema": r.REGISTRY_SCHEMA,
    "keys": [
        {
            "key_id": IDS[label],
            "public_key": base64.b64encode(_raw(KEYS[label])).decode(),
            "role": label,
            "status": "active",
            **({"org": "Example Clinic"} if label == "customer_key" else {}),
        }
        for label in ("this_computer", "customer_key", "openadapt_cloud", "demo")
    ],
}


def statement(outcome: str = "VERIFIED", commitments=None) -> dict:
    lines = [
        {
            "step": 2,
            "target_kind": "gui",
            "operation": "write",
            "check": "confirmed",
            "strength": "read_back",
            "not_checked": [],
        },
        {
            "step": 4,
            "target_kind": "gui",
            "operation": "irreversible_write",
            "check": "confirmed" if outcome == "VERIFIED" else "refuted",
            "strength": "read_back",
            "not_checked": ["field_values"],
        },
    ]
    tally = {k: sum(1 for x in lines if x["check"] == k) for k in ("confirmed", "refuted", "not_checked")}
    return {
        "_type": r.STATEMENT_TYPE,
        "subject": [
            {"name": "report.json", "digest": {"sha256": "1" * 64}},
            {"name": "bundle", "digest": {"sha256": "2" * 64}},
        ],
        "predicateType": r.PREDICATE_TYPE,
        "predicate": {
            "program": {"id": r.program_id("MockMed referral", "2" * 64), "version_sha256": "2" * 64},
            "started_hour": "2026-11-20T23:00:00Z",
            "outcome": outcome,
            "reason_code": "verified" if outcome == "VERIFIED" else "halted",
            "steps": {"total": 5, "ok": 5 if outcome == "VERIFIED" else 4},
            "lines": lines,
            "checks": {
                "identity": {"required": 5, "confirmed": 5},
                "postconditions": {"required": 3, "confirmed": 3},
                "effects": {"required": 2, "confirmed": tally["confirmed"]},
                "authorization": {"required": 0, "confirmed": 0},
                "lines_confirmed": tally["confirmed"],
                "lines_refuted": tally["refuted"],
                "lines_not_checked": tally["not_checked"],
            },
            "model_calls": 0,
            "disclosure": {
                "version": r.PUBLIC_DISCLOSURE_VERSION,
                "commitments": commitments or {},
            },
        },
    }


def _tamper(envelope: dict) -> dict:
    payload = base64.b64decode(envelope["payload"])
    changed = payload.replace(b'"outcome":"STOPPED"', b'"outcome":"VERIFIED"', 1)
    if changed == payload:
        changed = payload.replace(b'"model_calls":0', b'"model_calls":1', 1)
    out = copy.deepcopy(envelope)
    out["payload"] = base64.b64encode(changed).decode()
    return out


def build() -> dict[str, dict]:
    values = {
        "program_name": "MockMed referral",
        "flow_version": "3.5.0",
        "github_login": "example-user",
        "run_id": "run-20261120-2300",
    }
    commitments, openings = r.make_openings(values, salt=_salts())
    full = statement("VERIFIED", commitments)
    env = r.sign_statement(full, [signer("this_computer")])
    private = r.private_openings(env, openings)
    stopped = r.sign_statement(statement("STOPPED"), [signer("this_computer")])
    vectors: dict[str, dict] = {}

    def add(name, description, document, expected, openings=None):
        vectors[name] = {
            "description": description,
            "registry": "registry.json",
            "document": document,
            **({"openings": openings} if openings is not None else {}),
            "expected": expected,
        }

    ok_local = {
        "ok": True,
        "reason": None,
        "signer_lines": ["Signed by this computer"],
        "demo": False,
    }
    add("valid", "A run signed by this computer, with its private openings.", env,
        {**ok_local, "outcome": "VERIFIED", "disclosed": values}, private)
    add("valid-stopped", "A stopped run gets a receipt too; its outcome stays STOPPED.",
        stopped, {**ok_local, "outcome": "STOPPED", "disclosed": {}})
    add("tampered", "The stopped run's payload edited to say VERIFIED after signing.",
        _tamper(stopped), {"ok": False, "reason": "bad_signature",
                           "signer_lines": [r.refusal_line("bad_signature")]})
    add("unknown-signer", "Signed by a key the registry doesn't pin.",
        r.sign_statement(statement("VERIFIED"), [signer("stranger")]),
        {"ok": False, "reason": "unknown_signer", "signer_lines": [r.UNKNOWN_SIGNER_LINE]})
    add("unknown-signer-beside-valid", "A pinned signature plus an unpinned one: refused.",
        r.sign_statement(statement("VERIFIED"), [signer("this_computer"), signer("stranger")]),
        {"ok": False, "reason": "unknown_signer", "signer_lines": [r.UNKNOWN_SIGNER_LINE]})
    add("demo-key", "Signed only by the demo key: verifies, always marked demo.",
        r.sign_statement(statement("VERIFIED"), [signer("demo")]),
        {"ok": True, "reason": None, "outcome": "VERIFIED", "demo": True,
         "signer_lines": ["Demo key: checks only in this demo"], "disclosed": {}})
    add("demo-beside-real", "A demo key next to a real key: refused.",
        r.sign_statement(statement("VERIFIED"), [signer("this_computer"), signer("demo")]),
        {"ok": False, "reason": "mixed_demo", "signer_lines": [r.refusal_line("mixed_demo")]})
    add("customer-key", "Signed by the organization's own key.",
        r.sign_statement(statement("VERIFIED"), [signer("customer_key")]),
        {"ok": True, "reason": None, "outcome": "VERIFIED", "demo": False,
         "signer_lines": ["Signed by Example Clinic's key"], "disclosed": {}})
    counter = r.add_signature(env, *signer("openadapt_cloud"))
    add("cloud-countersign", "This computer's receipt, countersigned by Cloud in the same envelope.",
        counter, {"ok": True, "reason": None, "outcome": "VERIFIED", "demo": False,
                  "signer_lines": ["Signed by this computer", "Countersigned by OpenAdapt Cloud"],
                  "disclosed": {}})
    card = r.public_card(counter, openings, reveal=["program_name", "flow_version"])
    add("public-redaction", "The public card: the same signed envelope, two allow-listed openings.",
        card, {"ok": True, "reason": None, "outcome": "VERIFIED", "demo": False,
               "signer_lines": ["Signed by this computer", "Countersigned by OpenAdapt Cloud"],
               "disclosed": {"program_name": "MockMed referral", "flow_version": "3.5.0"}})
    leaky = copy.deepcopy(card)
    leaky["openings"]["run_id"] = openings["run_id"]
    add("public-redaction-not-allowed", "A card that also opens run_id, which isn't allow-listed.",
        leaky, {"ok": False, "reason": "disclosure_not_allowed",
                "signer_lines": [r.refusal_line("disclosure_not_allowed")]})
    wrong = copy.deepcopy(card)
    wrong["openings"]["program_name"]["value"] = "Another program"
    add("public-redaction-mismatch", "A card whose opened value doesn't match its commitment.",
        wrong, {"ok": False, "reason": "opening_mismatch",
                "signer_lines": [r.refusal_line("opening_mismatch")]})
    blob = copy.deepcopy(card)
    blob["openings"]["program_name"]["value"] = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    add("embedded-image-in-card", "A card that tries to carry a screenshot: refused.",
        blob, {"ok": False, "reason": "embedded_content",
               "signer_lines": [r.refusal_line("embedded_content")]})
    # A statement carrying a screenshot, signed anyway (bypassing the builder).
    bad = statement("VERIFIED")
    bad["predicate"]["reason_code"] = "iVBORw0KGgo" + "A" * 120
    payload = r.canonical_json(bad).encode()
    raw_env = {"payloadType": r.PAYLOAD_TYPE, "payload": base64.b64encode(payload).decode(),
               "signatures": [{"keyid": IDS["this_computer"],
                               "sig": base64.b64encode(KEYS["this_computer"].sign(r.pae(r.PAYLOAD_TYPE, payload))).decode()}]}
    add("embedded-image-in-record", "A signed record whose payload carries base64 image bytes: refused.",
        raw_env, {"ok": False, "reason": "embedded_content",
                  "signer_lines": [r.refusal_line("embedded_content")]})
    other = statement("VERIFIED")
    other["predicateType"] = "https://openadapt.ai/record/run/v9"
    payload = r.canonical_json(other).encode()
    unknown_schema = {"payloadType": r.PAYLOAD_TYPE, "payload": base64.b64encode(payload).decode(),
                      "signatures": [{"keyid": IDS["this_computer"],
                                      "sig": base64.b64encode(KEYS["this_computer"].sign(r.pae(r.PAYLOAD_TYPE, payload))).decode()}]}
    add("unknown-schema", "A correctly signed statement of a predicate type this verifier doesn't know.",
        unknown_schema, {"ok": False, "reason": "unknown_schema",
                         "signer_lines": [r.refusal_line("unknown_schema")]})
    _confirmation_vectors(add)
    _handoff_vectors(vectors)
    return vectors


HANDOFF = b'{"referral":"synthetic","status":"sent"}'


def _handoff_vectors(vectors) -> None:
    def put(name, description, document, expected, openings=None, received=None):
        vectors[name] = {
            "description": description,
            "registry": "registry.json",
            "document": document,
            **({"openings": openings} if openings is not None else {}),
            **({"received": base64.b64encode(received).decode()} if received is not None else {}),
            "expected": expected,
        }

    values = {
        "approval.principal": "person:pr-" + "cd" * 8,
        "approval.scope": hashlib.sha256(b"synthetic approval scope").hexdigest(),
        "approval.approved_at": "2026-11-20T23:30:00Z",
    }
    commitments, openings = r.make_openings(values, salt=_salts())
    st = statement("VERIFIED")
    st["subject"].append(
        {
            "name": "payload",
            "digest": {"sha256": hashlib.sha256(HANDOFF).hexdigest()},
            "media_type": "application/json",
        }
    )
    st["predicate"]["approval"] = {
        "mode": "each_item",
        "approver_kind": "person",
        "commitments": {k.split(".", 1)[1]: v for k, v in commitments.items()},
    }
    env = r.sign_statement(st, [signer("this_computer")])
    ok = {
        "ok": True,
        "reason": None,
        "outcome": "VERIFIED",
        "demo": False,
        "signer_lines": ["Signed by this computer"],
        "approval_line": "Approved by a person: each item",
    }
    put("payload-bound", "A handoff receipt bound to the bytes the receiver got, with a person's approval.",
        env, {**ok, "payload_bound": True, "disclosed": {k: v["value"] for k, v in openings.items()}},
        r.private_openings(env, openings), HANDOFF)
    # The producer hashes both the original report file and the callback encoding.
    report = '{\n  "z": 2,\n  "a": 1\n}\n'
    canonical = r.canonical_json(json.loads(report)).encode("utf-8")
    paired = copy.deepcopy(st)
    next(s for s in paired["subject"] if s["name"] == "report.json")["digest"] = {
        "sha256": hashlib.sha256(report.encode("utf-8")).hexdigest()
    }
    paired["subject"].extend([
        {"name": "report.canonical.json", "digest": {"sha256": hashlib.sha256(canonical).hexdigest()}},
        {"name": "evidence", "digest": {"sha256": hashlib.sha256(b"synthetic evidence").hexdigest()}},
    ])
    put("report-subjects", "A signed receipt binds the exact report file and its compact callback encoding.",
        r.sign_statement(paired, [signer("this_computer")]),
        {**ok, "payload_bound": True, "disclosed": {}}, received=HANDOFF)
    vectors["report-subjects"]["report_utf8"] = report
    put("payload-mismatch", "The receiver got different bytes from the payload the receipt signed.",
        env, {"ok": False, "reason": "payload_mismatch", "signer_lines": [r.refusal_line("payload_mismatch")]},
        None, HANDOFF.replace(b"sent", b"done"))
    plain = r.sign_statement(statement("VERIFIED"), [signer("this_computer")])
    put("payload-missing", "Handed-off bytes checked against a receipt that names no payload.",
        plain, {"ok": False, "reason": "payload_not_bound", "signer_lines": [r.refusal_line("payload_not_bound")]},
        None, HANDOFF)
    put("approval-public-card", "The public card of an approved handoff: the mode and approver kind show; who, what and when stay committed.",
        r.public_card(env, openings, reveal=[]), {**ok, "disclosed": {}})
    put("approval-none", "A receipt with no approval block says so.",
        plain, {**ok, "approval_line": "No approval recorded", "disclosed": {}})
    gate = statement("VERIFIED")
    gate["predicate"]["approval"] = {"mode": "not_gated", "approver_kind": "none", "commitments": {}}
    put("approval-not-gated", "A run started from the customer's own software, recorded as not gated.",
        r.sign_statement(gate, [signer("this_computer")]),
        {**ok, "approval_line": "Not gated: no approval was required for these writes", "disclosed": {}})


_STATES = {
    "recorded_as_intended": ("portal_read_back", "read_back", 0),
    "recorded_differently": ("api", "read_back", 1),
    "acknowledged": ("acknowledgment_message", "acknowledgment", 0),
    "sent": ("acknowledgment_message", "none", 0),
}


def _confirmed(state: str, reason=None, salt=None):
    """A VERIFIED statement whose step-4 write has one counterparty confirmation."""
    method, strength, differing = _STATES[state]
    field = "dob:" + hashlib.sha256(b"synthetic field value").hexdigest()
    values = {
        "confirmations.0.counterparty": "ehr:cp-" + "ab" * 8,
        "confirmations.0.observed_at": "2026-11-20T23:41:07Z",
        "confirmations.0.field.0": field + (":differs" if differing else ""),
    }
    if reason is not None:
        values["confirmations.0.reason"] = reason
    commitments, openings = r.make_openings(values, salt=salt or _salts())
    st = statement("VERIFIED")
    st["predicate"]["confirmations"] = [
        {
            "kind": "counterparty_confirmation",
            "for_step": 4,
            "system_kind": "ehr",
            "read_method": method,
            "state": state,
            "strength": strength,
            "differing_count": differing,
            "commitments": {k.split(".", 2)[2]: v for k, v in commitments.items()},
        }
    ]
    return st, openings


def _confirmation_vectors(add) -> None:
    lines = {
        "recorded_as_intended": "Confirmed in the other party's system at 2026-11-20T23:41:07Z",
        "recorded_differently": "Recorded differently in the other party's system at 2026-11-20T23:41:07Z: 1 field differs",
        "acknowledged": "Acknowledged by the other party at 2026-11-20T23:41:07Z",
        "sent": "Sent to the other party at 2026-11-20T23:41:07Z, not confirmed",
    }
    for state, line in lines.items():
        st, openings = _confirmed(state)
        env = r.sign_statement(st, [signer("this_computer")])
        add(
            f"confirmation-{state.replace('_', '-')}",
            f"A counterparty confirmation in state {state}, with its private openings.",
            env,
            {
                "ok": True,
                "reason": None,
                "outcome": "VERIFIED",
                "demo": False,
                "signer_lines": ["Signed by this computer"],
                "confirmation_lines": [line],
                "independent": [_STATES[state][0] in r.INDEPENDENT_READS],
                "disclosed": {k: v["value"] for k, v in openings.items()},
            },
            r.private_openings(env, openings),
        )
    st, openings = _confirmed("recorded_as_intended")
    env = r.sign_statement(st, [signer("this_computer")])
    payload = base64.b64decode(env["payload"]).replace(
        b'"state":"recorded_as_intended"', b'"state":"recorded_differently"', 1
    )
    tampered = dict(env, payload=base64.b64encode(payload).decode())
    add(
        "confirmation-tampered",
        "A confirmation's state edited after signing.",
        tampered,
        {"ok": False, "reason": "bad_signature", "signer_lines": [r.refusal_line("bad_signature")]},
    )
    card = r.public_card(env, {}, reveal=[])
    add(
        "confirmation-public-redaction",
        "The public card of a confirmed receipt: state and strength show; the counterparty, time, fields and reason stay committed.",
        card,
        {
            "ok": True,
            "reason": None,
            "outcome": "VERIFIED",
            "demo": False,
            "signer_lines": ["Signed by this computer"],
            "confirmation_lines": ["Confirmed in the other party's system"],
            "independent": [True],
            "disclosed": {},
        },
    )
    leaky = copy.deepcopy(card)
    leaky["openings"]["confirmations.0.counterparty"] = openings["confirmations.0.counterparty"]
    add(
        "confirmation-public-not-allowed",
        "A card that opens the counterparty, which isn't on the allow-list.",
        leaky,
        {"ok": False, "reason": "disclosure_not_allowed", "signer_lines": [r.refusal_line("disclosure_not_allowed")]},
    )
    st, openings = _confirmed("sent", reason="no_answer_by_deadline")
    env = r.sign_statement(st, [signer("this_computer")])
    add(
        "confirmation-reason-no-answer",
        "A sent handoff with reason no_answer_by_deadline (committed; shown only from the private openings).",
        env,
        {
            "ok": True,
            "reason": None,
            "outcome": "VERIFIED",
            "demo": False,
            "signer_lines": ["Signed by this computer"],
            "confirmation_lines": ["Sent to the other party at 2026-11-20T23:41:07Z, not confirmed"],
            "independent": [False],
            "disclosed": {k: v["value"] for k, v in openings.items()},
        },
        r.private_openings(env, openings),
    )
    st, openings = _confirmed("recorded_as_intended", reason="receiver_unreachable")
    env = r.sign_statement(st, [signer("this_computer")])
    add(
        "confirmation-reason-inconsistent",
        "recorded_as_intended with reason receiver_unreachable: refused when the openings are read.",
        env,
        {"ok": False, "reason": "inconsistent", "signer_lines": [r.refusal_line("inconsistent")]},
        r.private_openings(env, openings),
    )


def main() -> None:
    (HERE / "registry.json").write_text(json.dumps(REGISTRY, indent=2, sort_keys=True) + "\n")
    unknown_role = copy.deepcopy(REGISTRY)
    unknown_role["keys"][0]["role"] = "notary"
    (HERE / "registry-unknown-role.json").write_text(
        json.dumps(unknown_role, indent=2, sort_keys=True) + "\n"
    )
    for name, vector in build().items():
        (HERE / f"{name}.json").write_text(json.dumps(vector, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
