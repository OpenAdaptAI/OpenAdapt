"""Regenerate the interchange fixtures: python tests/fixtures/interchange/make_fixtures.py

All data is synthetic. Valid cases are written first; each invalid case is a
valid one with a single change, so it shows exactly one rule.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from openadapt_flow.interchange import with_checksum

HERE = Path(__file__).resolve().parent
V = "openadapt.records-interchange/v0"
EXECUTOR = {"kind": "executor", "id": "openadapt-flow@site-runner-1"}
DOC_SIDE = {"kind": "system", "id": "document-side@site"}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_payload(rel: str, text: str) -> dict:
    data = text.encode()
    (HERE / rel).parent.mkdir(parents=True, exist_ok=True)
    (HERE / rel).write_bytes(data)
    return {"path": rel, "sha256": sha(data), "mime": "text/plain", "bytes": len(data)}


def prov(producer: dict) -> dict:
    return {
        "tool_name": "fixture-generator",
        "tool_version": "0",
        "producer": producer,
        "captured_at": "2026-10-29T14:00:00+00:00",
    }


def sessions(doc: dict) -> None:
    """Mark which session each application read came from, unless the case
    already says (a ``reopened`` field names its reopened read explicitly)."""
    for f in doc["fields"]:
        for r in f.get("reads", []):
            if "session" in r:
                continue
            if r["channel"] == "export" and r.get("source_kind") == "live_session":
                r["session"] = "acting"
            elif r["channel"] in ("export", "database", "api"):
                r["session"] = "independent"
            elif r["channel"] in ("clipboard", "ocr", "structure", "accessibility"):
                r["session"] = "acting"


def seal(doc: dict) -> dict:
    sessions(doc)
    doc = with_checksum(doc)
    doc["seals"] = [
        {
            "kind": "openadapt.seal/offline-ed25519",
            "covers": doc["provenance"]["checksum"],
            "key_id": "site-key-1",
            "uri": "seals/run-0001.seal.json",
        }
    ]
    return doc


inv_payload = write_payload(
    "payloads/invoice-INV-20417.txt",
    "SYNTHETIC INVOICE\nVendor ID: V-0042\nInvoice No: INV-20417\n"
    "Amount: 1249.50\nDue date: 2026-11-15\n",
)
invoice = {
    "schema_version": V,
    "exchange_id": "ex-inv-0001",
    "direction": "to_executor",
    "workflow": "ap.post_invoice",
    "session_id": "batch-2026-10-29",
    "artifacts": [
        {
            "artifact_id": "a1",
            "kind": "source_document",
            "source_type": "pdf_scan",
            "file_number": "V-0042",
            "date": "2026-10-29",
            "doctype": "invoice",
            "payload": inv_payload,
        }
    ],
    "fields": [
        {
            "name": "vendor_id",
            "type": "code",
            "value": "V-0042",
            "confidence": 0.97,
            "reads": [
                {
                    "channel": "document_parse",
                    "value": "V-0042",
                    "artifact_id": "a1",
                    "locator": {"page": 1, "line": 2},
                }
            ],
        },
        {
            "name": "invoice_number",
            "type": "code",
            "value": "INV-20417",
            "confidence": 0.95,
            "reads": [
                {
                    "channel": "document_parse",
                    "value": "INV-20417",
                    "artifact_id": "a1",
                    "locator": {"page": 1, "line": 3},
                }
            ],
        },
        {
            "name": "amount",
            "type": "decimal",
            "unit": "CAD",
            "value": "1249.50",
            "confidence": 0.62,
            "status": "corrected",
            "confirmed_by": {"kind": "human", "id": "clerk:ap-2", "role": "ap_clerk"},
            "reads": [
                {
                    "channel": "document_parse",
                    "value": "1249.5O",
                    "artifact_id": "a1",
                    "locator": {"page": 1, "line": 4},
                },
                {"channel": "human", "value": "1249.50"},
            ],
        },
        {
            "name": "due_date",
            "type": "date",
            "value": "2026-11-15",
            "confidence": 0.93,
            "reads": [
                {
                    "channel": "document_parse",
                    "value": "2026-11-15",
                    "artifact_id": "a1",
                }
            ],
        },
        {
            "name": "approval",
            "type": "enum",
            "value": "approve",
            "confidence": 1.0,
            "reads": [{"channel": "human", "value": "approve"}],
        },
    ],
    "identities": [
        {
            "identity_id": "vendor",
            "role": "counterparty",
            "fields": ["vendor_id"],
            "match": {"status": "existing", "record_ref": "V-0042", "candidates": 1},
        }
    ],
    "decisions": [
        {
            "decision_id": "d1",
            "question": "Approve this invoice for payment?",
            "options": ["approve", "hold", "reject"],
            "choice": "approve",
            "output_field": "approval",
            "authorized_roles": ["controller"],
            "decided_by": {
                "kind": "human",
                "id": "controller:c-1",
                "role": "controller",
            },
            "decided_at": "2026-10-29T14:05:00+00:00",
        }
    ],
    "outcome": "pending",
    "provenance": prov(DOC_SIDE),
}
invoice = with_checksum(invoice)

invoice_result = {
    "schema_version": V,
    "exchange_id": "ex-inv-0001-r",
    "direction": "from_executor",
    "workflow": "ap.post_invoice",
    "session_id": "batch-2026-10-29",
    "in_reply_to": "ex-inv-0001",
    "artifacts": [
        {
            "artifact_id": "r1",
            "kind": "structured_observation",
            "source_type": "rdp_screen",
            "file_number": "V-0042",
            "date": "2026-10-29",
            "doctype": "invoice-posting",
            "metadata": {"screen": "Bill detail"},
        }
    ],
    "fields": [
        {
            "name": "bill_id",
            "type": "code",
            "value": "B-000318",
            "confidence": 0.99,
            "grade": "reopened",
            "reads": [
                {
                    "channel": "clipboard",
                    "value": "B-000318",
                    "artifact_id": "r1",
                    "session": "acting",
                },
                {
                    "channel": "ocr",
                    "value": "B-000318",
                    "artifact_id": "r1",
                    "session": "reopened",
                    "locator": {
                        "screen": "Bill detail",
                        "region": {
                            "bbox": [120, 64, 140, 22],
                            "frame_sha256": sha(b"frame-1"),
                            "crop_sha256": sha(b"crop-1"),
                        },
                    },
                },
            ],
        },
        {
            "name": "amount",
            "type": "decimal",
            "unit": "CAD",
            "value": "1249.50",
            "confidence": 0.99,
            "grade": "system_of_record",
            "reads": [
                {"channel": "ocr", "value": "1249.50", "artifact_id": "r1"},
                {
                    "channel": "export",
                    "value": "1249.50",
                    "source_kind": "committed_store",
                },
            ],
        },
        {
            # An export started from the same live session grades like a
            # screen read, not as system_of_record.
            "name": "due_date",
            "type": "date",
            "value": "2026-11-15",
            "confidence": 0.98,
            "grade": "same_screen",
            "reads": [
                {"channel": "ocr", "value": "2026-11-15", "artifact_id": "r1"},
                {
                    "channel": "export",
                    "value": "2026-11-15",
                    "source_kind": "live_session",
                },
            ],
        },
        {
            "name": "vendor_id",
            "type": "code",
            "value": "V-0042",
            "confidence": 0.99,
            "reads": [{"channel": "ocr", "value": "V-0042", "artifact_id": "r1"}],
        },
    ],
    "identities": [
        {
            "identity_id": "vendor",
            "role": "counterparty",
            "fields": ["vendor_id"],
            "match": {
                "status": "existing",
                "record_ref": "V-0042",
                "candidates": 1,
                "checked_by": EXECUTOR,
            },
        }
    ],
    "outcome": "committed",
    "provenance": prov(EXECUTOR),
}
invoice_result = seal(invoice_result)

ref_payload = write_payload(
    "payloads/referral-0007.txt",
    "SYNTHETIC REFERRAL (test data)\nPatient: TESTPATIENT, ALPHA\nDOB: 1970-01-01\n"
    "Health card: TEST-0000-0007\nReferring clinic: Example Family Practice\n"
    "Reason: sudden change in vision, test referral\n",
)
referral = {
    "schema_version": V,
    "exchange_id": "ex-ref-0007",
    "direction": "to_executor",
    "workflow": "example.referral_triage",
    "session_id": "fax-2026-10-29",
    "artifacts": [
        {
            "artifact_id": "fax1",
            "kind": "source_document",
            "source_type": "fax_folder",
            "file_number": "TEST-0007",
            "date": "2026-10-29",
            "doctype": "referral",
            "payload": ref_payload,
        }
    ],
    "fields": [
        {
            "name": "subject_name",
            "type": "text",
            "value": "TESTPATIENT, ALPHA",
            "confidence": 0.94,
            "reads": [
                {
                    "channel": "document_parse",
                    "value": "TESTPATIENT, ALPHA",
                    "artifact_id": "fax1",
                    "locator": {"page": 1, "line": 2},
                }
            ],
        },
        {
            "name": "date_of_birth",
            "type": "date",
            "value": "1970-01-01",
            "confidence": 0.95,
            "reads": [
                {
                    "channel": "document_parse",
                    "value": "1970-01-01",
                    "artifact_id": "fax1",
                }
            ],
        },
        {
            "name": "subject_id",
            "type": "code",
            "value": "TEST-0000-0007",
            "confidence": 0.71,
            "status": "confirmed",
            "confirmed_by": {
                "kind": "human",
                "id": "staff:s-3",
                "role": "intake_staff",
            },
            "reads": [
                {
                    "channel": "document_parse",
                    "value": "TEST-OOOO-0007",
                    "artifact_id": "fax1",
                },
                {"channel": "human", "value": "TEST-0000-0007"},
            ],
        },
        {
            "name": "priority",
            "type": "enum",
            "value": "urgent",
            "confidence": 1.0,
            "reads": [{"channel": "human", "value": "urgent"}],
        },
    ],
    "identities": [
        {
            "identity_id": "subject",
            "role": "subject",
            "fields": ["subject_name", "date_of_birth", "subject_id"],
            "match": {"status": "new", "candidates": 0},
        }
    ],
    "decisions": [
        {
            "decision_id": "triage",
            "question": "What priority should this referral get?",
            "options": ["urgent", "soon", "routine", "decline"],
            "choice": "urgent",
            "output_field": "priority",
            "authorized_roles": ["physician"],
            "decided_by": {"kind": "human", "id": "physician:p-1", "role": "physician"},
            "decided_at": "2026-10-29T14:10:00+00:00",
            "advisory": [
                {
                    "source": {"kind": "model", "id": "local:qwen-small"},
                    "option": "soon",
                    "rationale": "advisory only",
                }
            ],
        }
    ],
    "outcome": "pending",
    "provenance": prov(DOC_SIDE),
    "extensions": {
        "org.openadapt.example.referral": {
            "referring_clinic": "Example Family Practice",
            "reason_code": "TEST-R1",
        }
    },
}
referral = with_checksum(referral)

referral_result = {
    "schema_version": V,
    "exchange_id": "ex-ref-0007-r",
    "direction": "from_executor",
    "workflow": "example.referral_triage",
    "session_id": "fax-2026-10-29",
    "in_reply_to": "ex-ref-0007",
    "artifacts": [
        {
            "artifact_id": "chart",
            "kind": "structured_observation",
            "source_type": "rdp_screen",
            "file_number": "TEST-0007",
            "date": "2026-10-29",
            "doctype": "triage-entry",
        }
    ],
    "fields": [
        {
            "name": "record_id",
            "type": "code",
            "value": "REC-100045",
            "confidence": 0.99,
            "grade": "reopened",
            "reads": [
                {
                    "channel": "clipboard",
                    "value": "REC-100045",
                    "artifact_id": "chart",
                    "session": "acting",
                },
                {
                    "channel": "ocr",
                    "value": "REC-100045",
                    "artifact_id": "chart",
                    "session": "reopened",
                },
            ],
        },
        {
            "name": "priority",
            "type": "enum",
            "value": "urgent",
            "confidence": 0.99,
            "grade": "reopened",
            "reads": [
                {
                    "channel": "clipboard",
                    "value": "urgent",
                    "artifact_id": "chart",
                    "session": "acting",
                },
                {
                    "channel": "clipboard",
                    "value": "urgent",
                    "artifact_id": "chart",
                    "session": "reopened",
                },
            ],
        },
    ],
    "identities": [
        {
            "identity_id": "subject",
            "role": "subject",
            "fields": ["record_id"],
            "match": {
                "status": "new",
                "record_ref": "REC-100045",
                "candidates": 0,
                "checked_by": EXECUTOR,
            },
        }
    ],
    "outcome": "committed",
    "provenance": prov(EXECUTOR),
}
referral_result = seal(referral_result)

halted = copy.deepcopy(referral_result)
halted.update(
    exchange_id="ex-ref-0008-r",
    in_reply_to="ex-ref-0008",
    outcome="halted",
    halt_reason="two charts matched the name and date of birth; stopped before writing",
)
halted["fields"] = []
halted["identities"][0]["fields"] = ["subject_id_echo"]
halted["fields"] = [
    {
        "name": "subject_id_echo",
        "type": "code",
        "value": "TEST-0000-0008",
        "confidence": 1.0,
    }
]
halted["identities"][0]["match"] = {
    "status": "ambiguous",
    "candidates": 2,
    "checked_by": EXECUTOR,
}
halted.pop("seals")
halted = seal(halted)

# A declined referral: the physician decided, and nothing was meant to be
# written. The chart's triage entry count is read before and after reopening
# and has not changed.
no_action = {
    "schema_version": V,
    "exchange_id": "ex-ref-0009-r",
    "direction": "from_executor",
    "workflow": "example.referral_triage",
    "session_id": "fax-2026-10-29",
    "in_reply_to": "ex-ref-0009",
    "artifacts": [
        {
            "artifact_id": "chart",
            "kind": "structured_observation",
            "source_type": "rdp_screen",
            "file_number": "TEST-0009",
            "date": "2026-10-29",
            "doctype": "triage-entry",
        }
    ],
    "fields": [
        {
            "name": "chart_id",
            "type": "code",
            "value": "REC-100001",
            "confidence": 1.0,
            "reads": [
                {
                    "channel": "clipboard",
                    "value": "REC-100001",
                    "artifact_id": "chart",
                    "session": "acting",
                }
            ],
        },
        {
            "name": "triage_entries",
            "type": "integer",
            "value": 1,
            "confidence": 1.0,
            "grade": "reopened",
            "reads": [
                {
                    "channel": "clipboard",
                    "value": "1",
                    "artifact_id": "chart",
                    "session": "acting",
                    "observed_at": "2026-10-29T14:11:00+00:00",
                },
                {
                    "channel": "clipboard",
                    "value": "1",
                    "artifact_id": "chart",
                    "session": "reopened",
                    "observed_at": "2026-10-29T14:13:00+00:00",
                },
                {
                    "channel": "ocr",
                    "value": "1",
                    "artifact_id": "chart",
                    "session": "reopened",
                    "observed_at": "2026-10-29T14:13:00+00:00",
                },
            ],
        },
    ],
    "identities": [
        {
            "identity_id": "subject",
            "role": "subject",
            "fields": ["chart_id"],
            "match": {
                "status": "existing",
                "record_ref": "REC-100001",
                "candidates": 1,
                "checked_by": EXECUTOR,
            },
        }
    ],
    "outcome": "no_action",
    "no_action": {
        "decided_by": {"kind": "human", "id": "physician:p-1", "role": "physician"},
        "reason_code": "declined",
        "decision_id": "triage",
        "decided_at": "2026-10-29T14:12:00+00:00",
        "required_grade": "same_screen",
        "evidence": ["triage_entries"],
    },
    "provenance": prov(EXECUTOR),
}
no_action = seal(no_action)

valid = {
    "valid/invoice_posting.json": invoice,
    "valid/invoice_posting_result.json": invoice_result,
    "valid/referral_intake.json": referral,
    "valid/referral_result.json": referral_result,
    "valid/referral_halted.json": halted,
    "valid/referral_no_action.json": no_action,
}


def mutate(base: dict, fn, *, rechecksum: bool = True) -> dict:
    doc = copy.deepcopy(base)
    fn(doc)
    if rechecksum:
        doc = with_checksum(doc)
        for s in doc.get("seals", []):
            s["covers"] = doc["provenance"]["checksum"]
    return doc


def _set(path, value):
    def f(doc):
        cur = doc
        for k in path[:-1]:
            cur = cur[k]
        cur[path[-1]] = value

    return f


invalid = {
    "invalid/system_of_record_without_independent_read.json": (
        mutate(invoice_result, lambda d: d["fields"][1]["reads"].pop()),
        ["IX030"],
    ),
    "invalid/system_of_record_from_live_session_export.json": (
        mutate(
            invoice_result,
            _set(["fields", 1, "reads", 1, "source_kind"], "live_session"),
        ),
        ["IX030"],
    ),
    "invalid/export_without_source_kind.json": (
        mutate(invoice_result, lambda d: d["fields"][1]["reads"][1].pop("source_kind")),
        ["IX030", "IX035"],
    ),
    "invalid/readback_without_screen_read.json": (
        mutate(referral_result, _set(["fields", 1, "reads"], [])),
        ["IX031"],
    ),
    "invalid/reopened_without_reopened_read.json": (
        mutate(
            referral_result,
            lambda d: [r.update(session="acting") for r in d["fields"][0]["reads"]],
        ),
        ["IX033"],
    ),
    "invalid/reopened_without_acting_read.json": (
        mutate(
            referral_result,
            lambda d: [r.update(session="reopened") for r in d["fields"][0]["reads"]],
        ),
        ["IX033"],
    ),
    "invalid/reopened_read_not_on_screen.json": (
        mutate(
            referral_result,
            _set(
                ["fields", 0, "reads", 1],
                {
                    "channel": "export",
                    "value": "REC-100045",
                    "source_kind": "live_session",
                    "session": "reopened",
                },
            ),
        ),
        ["IX033"],
    ),
    "invalid/reopened_read_differs.json": (
        mutate(referral_result, _set(["fields", 1, "reads", 1, "value"], "routine")),
        ["IX036"],
    ),
    "invalid/committed_without_evidence.json": (
        mutate(
            referral_result,
            lambda d: [f.update(grade="unchecked", reads=[]) for f in d["fields"]],
        ),
        ["IX064"],
    ),
    "invalid/result_without_in_reply_to.json": (
        mutate(referral_result, lambda d: d.pop("in_reply_to")),
        ["IX065"],
    ),
    "invalid/committed_to_executor.json": (
        mutate(referral_result, _set(["direction"], "to_executor")),
        ["IX066"],
    ),
    "invalid/record_ref_mismatch.json": (
        mutate(invoice, _set(["identities", 0, "match", "record_ref"], "V-0099")),
        ["IX026"],
    ),
    "invalid/choice_without_decider.json": (
        mutate(invoice, lambda d: d["decisions"][0].pop("decided_by")),
        ["IX043"],
    ),
    "invalid/choice_differs_from_output_field.json": (
        mutate(invoice, _set(["fields", 4, "value"], "hold")),
        ["IX044"],
    ),
    "invalid/self_confirmation.json": (
        mutate(
            referral,
            _set(
                ["fields", 2, "confirmed_by"],
                {"kind": "human", "id": DOC_SIDE["id"], "role": "intake_staff"},
            ),
        ),
        ["IX045"],
    ),
    "invalid/review_without_reason.json": (
        mutate(referral_result, _set(["fields", 1, "needs_review"], True)),
        ["IX034"],
    ),
    "invalid/model_decided.json": (
        mutate(
            referral,
            _set(
                ["decisions", 0, "decided_by"],
                {"kind": "model", "id": "local:qwen-small", "role": "physician"},
            ),
        ),
        ["IX040"],
    ),
    "invalid/self_approval.json": (
        mutate(
            referral,
            _set(
                ["decisions", 0, "decided_by"],
                {"kind": "human", "id": DOC_SIDE["id"], "role": "physician"},
            ),
        ),
        ["IX041"],
    ),
    "invalid/unauthorized_role.json": (
        mutate(referral, _set(["decisions", 0, "decided_by", "role"], "intake_staff")),
        ["IX042"],
    ),
    "invalid/choice_not_option.json": (
        mutate(
            invoice,
            lambda d: [
                _set(["decisions", 0, "choice"], "pay_twice")(d),
                _set(["fields", 4, "value"], "pay_twice")(d),
            ],
        ),
        ["IX023"],
    ),
    "invalid/duplicate_field.json": (
        mutate(invoice, lambda d: d["fields"].append(copy.deepcopy(d["fields"][0]))),
        ["IX021"],
    ),
    "invalid/unknown_output_field.json": (
        mutate(invoice, _set(["decisions", 0, "output_field"], "payment_status")),
        ["IX024"],
    ),
    "invalid/confirmed_without_person.json": (
        mutate(referral, lambda d: d["fields"][2].pop("confirmed_by")),
        ["IX032"],
    ),
    "invalid/dangling_artifact.json": (
        mutate(invoice, _set(["fields", 0, "reads", 0, "artifact_id"], "a9")),
        ["IX020"],
    ),
    "invalid/unknown_identity_field.json": (
        mutate(invoice, _set(["identities", 0, "fields"], ["vendor_name"])),
        ["IX022"],
    ),
    "invalid/wrong_value_type.json": (
        mutate(invoice, _set(["fields", 2, "value"], 1249.5)),
        ["IX025"],
    ),
    "invalid/unnamespaced_extension.json": (
        mutate(referral, lambda d: d["extensions"].update({"referral": {"x": 1}})),
        ["IX050"],
    ),
    "invalid/domain_field_in_core.json": (
        mutate(referral, _set(["patient_name"], "TESTPATIENT, ALPHA")),
        ["IX001"],
    ),
    "invalid/checksum_tampered.json": (
        mutate(invoice, _set(["fields", 2, "value"], "9249.50"), rechecksum=False),
        ["IX010"],
    ),
    "invalid/seal_covers_other.json": (
        mutate(
            invoice_result,
            _set(["seals", 0, "covers"], "sha256:" + "0" * 64),
            rechecksum=False,
        ),
        ["IX011"],
    ),
    "invalid/ambiguous_identity_to_executor.json": (
        mutate(
            referral,
            _set(["identities", 0, "match"], {"status": "ambiguous", "candidates": 2}),
        ),
        ["IX060"],
    ),
    "invalid/existing_without_record.json": (
        mutate(invoice, lambda d: d["identities"][0]["match"].pop("record_ref")),
        ["IX061"],
    ),
    "invalid/halted_without_reason.json": (
        mutate(halted, _set(["halt_reason"], " ")),
        ["IX062"],
    ),
    "invalid/committed_unresolved_identity.json": (
        mutate(
            referral_result, _set(["identities", 0, "match"], {"status": "not_checked"})
        ),
        ["IX063"],
    ),
    "invalid/payload_hash_mismatch.json": (
        mutate(invoice, _set(["artifacts", 0, "payload", "sha256"], "f" * 64)),
        ["IX070"],
    ),
    "invalid/no_action_without_evidence.json": (
        mutate(no_action, _set(["no_action", "evidence"], [])),
        ["IX082"],
    ),
    "invalid/no_action_write_detected.json": (
        mutate(no_action, _set(["fields", 1, "reads", 1, "value"], "2")),
        ["IX083"],
    ),
    "invalid/no_action_without_before_read.json": (
        mutate(
            no_action,
            lambda d: [
                d["fields"][1]["reads"].pop(0),
                d["fields"][1].update(grade="same_screen"),
            ],
        ),
        ["IX085"],
    ),
    "invalid/payload_nul_path.json": (
        mutate(
            invoice, _set(["artifacts", 0, "payload", "path"], "payloads/x\x00.txt")
        ),
        ["IX071"],
    ),
    "invalid/no_action_below_commit_level.json": (
        mutate(no_action, _set(["no_action", "required_grade"], "system_of_record")),
        ["IX082"],
    ),
    "invalid/no_action_without_record.json": (
        mutate(no_action, lambda d: d.pop("no_action")),
        ["IX080"],
    ),
    "invalid/no_action_decided_by_model.json": (
        mutate(
            no_action,
            _set(
                ["no_action", "decided_by"],
                {"kind": "model", "id": "local:qwen-small", "role": "physician"},
            ),
        ),
        ["IX081"],
    ),
    "invalid/no_action_to_executor.json": (
        mutate(no_action, _set(["direction"], "to_executor")),
        ["IX084"],
    ),
    "invalid/payload_escapes_dir.json": (
        mutate(invoice, _set(["artifacts", 0, "payload", "path"], "../../etc/hosts")),
        ["IX071"],
    ),
    "invalid/payload_backslash_path.json": (
        mutate(invoice, _set(["artifacts", 0, "payload", "path"], "..\\..\\x")),
        ["IX071"],
    ),
    "invalid/payload_drive_letter_path.json": (
        mutate(invoice, _set(["artifacts", 0, "payload", "path"], "C:/x")),
        ["IX071"],
    ),
}

for rel, doc in valid.items():
    (HERE / rel).parent.mkdir(parents=True, exist_ok=True)
    (HERE / rel).write_text(json.dumps(doc, indent=2) + "\n")
for rel, (doc, _) in invalid.items():
    (HERE / rel).parent.mkdir(parents=True, exist_ok=True)
    (HERE / rel).write_text(json.dumps(doc, indent=2) + "\n")
# Invalid cases share the valid cases' payload files.
(HERE / "invalid" / "payloads").mkdir(exist_ok=True)
for p in (HERE / "payloads").iterdir():
    (HERE / "invalid" / "payloads" / p.name).write_bytes(p.read_bytes())
    (HERE / "valid" / "payloads").mkdir(exist_ok=True)
    (HERE / "valid" / "payloads" / p.name).write_bytes(p.read_bytes())
for p in (HERE / "payloads").iterdir():
    p.unlink()
(HERE / "payloads").rmdir()

l1_rows = {
    "valid/invoice_posting.json": [
        {
            "filename": "V-0042_2026-10-29_invoice.txt",
            "doctype": "invoice",
            "source_type": "pdf_scan",
            "sha256": inv_payload["sha256"],
        }
    ],
    "valid/invoice_posting_result.json": [
        {
            "filename": "V-0042_2026-10-29_invoice-posting.json",
            "source_type": "rdp_screen",
        }
    ],
    "valid/referral_intake.json": [
        {
            "filename": "TEST-0007_2026-10-29_referral.txt",
            "sha256": ref_payload["sha256"],
        }
    ],
}
cases = [
    {
        "file": rel,
        "expect": [],
        **({"expect_l1_rows": l1_rows[rel]} if rel in l1_rows else {}),
    }
    for rel in valid
]
cases += [{"file": rel, "expect": codes} for rel, (_, codes) in invalid.items()]
(HERE / "cases.json").write_text(json.dumps({"cases": cases}, indent=2) + "\n")
print(f"wrote {len(valid)} valid and {len(invalid)} invalid cases")
