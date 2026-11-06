"""Engine-side optional extension evidence: schema, redaction, absent-ok."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openadapt_flow.extension_evidence import (
    SCHEMA,
    REDACTED_VALUE,
    ExtensionEvidenceBuffer,
    load_extension_evidence,
    merge_target_path,
    redact_evidence,
    shadow_targets,
    validate_native_message,
)


def test_absent_file_is_empty_optional_capture(tmp_path: Path):
    assert load_extension_evidence(tmp_path) == []


def test_write_and_load_round_trip(tmp_path: Path):
    buf = ExtensionEvidenceBuffer()
    buf.append(
        {
            "kind": "target",
            "event": "click",
            "path": "my-field#wrap >>> input#cid",
            "in_shadow": True,
            "tag": "input",
            "name": "claim_id",
            "secret_field": False,
        }
    )
    path = buf.write(tmp_path)
    assert path.name == "extension_evidence.jsonl"
    lines = load_extension_evidence(tmp_path)
    assert len(lines) == 1
    assert lines[0]["in_shadow"] is True
    assert lines[0]["path"].startswith("my-field#wrap")
    header = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert header["schema"] == SCHEMA
    assert header["events"] == 1


def test_redact_withholds_password_value():
    line = redact_evidence(
        {
            "kind": "target",
            "tag": "input",
            "name": "password",
            "value": "should-not-persist",
            "secret_field": False,
        }
    )
    assert line["secret_field"] is True
    assert line["value"] == REDACTED_VALUE


def test_redact_honours_declared_secret_names():
    line = redact_evidence(
        {"kind": "target", "name": "mrn", "value": "999"},
        secret_names=["mrn"],
    )
    assert line["value"] == REDACTED_VALUE


def test_shadow_targets_filters():
    lines = [
        {"kind": "target", "in_shadow": True, "path": "a >>> b"},
        {"kind": "target", "in_shadow": False, "path": "a"},
        {"kind": "other", "in_shadow": True},
    ]
    found = shadow_targets(lines)
    assert len(found) == 1
    assert found[0]["path"] == "a >>> b"


def test_merge_prefers_extension_path_when_present():
    merged = merge_target_path("AX/WebArea/button", "host >>> button#go")
    assert merged["path"] == "host >>> button#go"
    assert merged["from_extension"] is True
    ax_only = merge_target_path("AX/WebArea/button", None)
    assert ax_only["path"] == "AX/WebArea/button"
    assert ax_only["from_extension"] is False


def test_validate_native_message_accepts_target_and_redacts():
    evidence = validate_native_message(
        {
            "type": "EXTENSION_EVIDENCE",
            "schema": SCHEMA,
            "evidence": {
                "kind": "target",
                "name": "password",
                "value": "nope",
            },
        }
    )
    assert evidence["value"] == REDACTED_VALUE


def test_validate_native_message_refuses_wrong_type():
    with pytest.raises(ValueError, match="EXTENSION_EVIDENCE"):
        validate_native_message({"type": "PING", "evidence": {"kind": "target"}})


def test_wrong_schema_header_refuses(tmp_path: Path):
    path = tmp_path / "extension_evidence.jsonl"
    path.write_text(
        json.dumps({"schema": "other/1", "events": 0}) + "\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="extension-evidence"):
        load_extension_evidence(tmp_path)


def test_buffer_appends_seq(tmp_path: Path):
    buf = ExtensionEvidenceBuffer()
    a = buf.append({"kind": "target", "path": "a", "in_shadow": False})
    b = buf.append({"kind": "target", "path": "b", "in_shadow": True})
    assert a["seq"] == 0
    assert b["seq"] == 1
    buf.write(tmp_path)
    assert [line["seq"] for line in load_extension_evidence(tmp_path)] == [0, 1]


def test_native_host_validate_and_write_via_buffer(tmp_path: Path):
    """Host stub path: validated envelope → buffer → file (no Chrome)."""
    msg = {
        "type": "EXTENSION_EVIDENCE",
        "schema": SCHEMA,
        "evidence": {
            "kind": "target",
            "event": "click",
            "path": "host#h >>> input#i",
            "in_shadow": True,
            "name": "password",
            "value": "leak",
        },
    }
    evidence = validate_native_message(msg)
    buf = ExtensionEvidenceBuffer()
    buf.append(evidence)
    buf.write(tmp_path)
    loaded = load_extension_evidence(tmp_path)
    assert loaded[0]["value"] == REDACTED_VALUE
    assert loaded[0]["in_shadow"] is True


def test_declared_secret_names_match_id_even_with_benign_name():
    line = {
        "kind": "target",
        "name": "note",
        "id": "member_ssn",
        "value": "123-45-6789",
    }
    out = redact_evidence(line, secret_names=["member_ssn"])
    assert out["secret_field"] is True
    assert out["value"] == "<secret>"
