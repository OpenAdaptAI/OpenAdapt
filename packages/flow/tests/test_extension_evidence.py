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


def test_strip_url_drops_tokens_and_non_web_payloads():
    from openadapt_flow.extension_evidence import strip_url

    assert strip_url("https://u:p@a.example:8443/x/y?token=1#f") == (
        "https://a.example:8443/x/y"
    )
    assert strip_url("HTTP://A.example/p?q") == "http://a.example/p"
    assert strip_url("about:srcdoc") == "about:srcdoc"
    assert strip_url("data:text/html,<b>secret</b>") == "data:"
    assert strip_url("javascript:alert(1)") == "javascript:"
    assert strip_url("") is None
    assert strip_url(None) is None
    assert strip_url(42) is None


def test_redact_strips_frame_urls_and_untyped_frame_keys():
    out = redact_evidence(
        {
            "kind": "frame_focus",
            "frame_url": "https://p.example/app?session=S#x",
            "frame_src": "https://c.example/f?token=T",
            "frame": {
                "tab_id": 1,
                "frame_id": 2,
                "document_id": "D",
                "origin": "https://c.example",
                "url": "https://c.example/f?token=T",
                "frame_id_bogus": True,
            },
        }
    )
    assert out["frame_url"] == "https://p.example/app"
    assert out["frame_src"] == "https://c.example/f"
    assert out["frame"] == {
        "tab_id": 1,
        "frame_id": 2,
        "document_id": "D",
        "origin": "https://c.example",
    }
    assert "token" not in json.dumps(out) and "session" not in json.dumps(out)
    junk = redact_evidence({"kind": "target", "frame": {"tab_id": "1", "x": 1}})
    assert "frame" not in junk


def test_native_message_accepts_frame_focus_and_refuses_unknown_kind():
    ok = validate_native_message(
        {
            "type": "EXTENSION_EVIDENCE",
            "evidence": {"kind": "frame_focus", "frame_src": "https://c.example/f?t=1"},
        }
    )
    assert ok["frame_src"] == "https://c.example/f"
    with pytest.raises(ValueError):
        validate_native_message(
            {"type": "EXTENSION_EVIDENCE", "evidence": {"kind": "network"}}
        )


def test_frame_grouping_and_linking():
    from openadapt_flow.extension_evidence import (
        cross_origin_frame_targets,
        frame_key,
        group_by_frame,
        link_frame_targets,
    )

    focus = {
        "kind": "frame_focus",
        "path": "app-shell#s >>> iframe#pay",
        "frame_src": "https://c.example/f",
        "frame_url": "https://p.example/app",
    }
    child = {
        "kind": "target",
        "path": "input#card",
        "is_iframe": True,
        "cross_origin_parent": True,
        "frame_url": "https://c.example/f?x=1",
        "frame": {"tab_id": 7, "frame_id": 3, "document_id": "D"},
    }
    other = {
        "kind": "target",
        "path": "button#go",
        "frame_url": "https://p.example/app",
    }
    lines = [focus, child, other]
    assert frame_key(child) == ("frame", 7, 3, "D")
    assert frame_key(other) == ("url", "https://p.example/app")
    groups = group_by_frame(lines)
    assert [len(v) for v in groups.values()] == [2, 1]
    assert cross_origin_frame_targets(lines) == [child]
    linked = link_frame_targets(lines)
    assert linked[1]["parent_frame_path"] == "app-shell#s >>> iframe#pay"
    assert linked[1]["parent_frame_url"] == "https://p.example/app"
    assert "parent_frame_path" not in linked[2]
    # A child target with no matching frame_focus is returned unchanged.
    assert link_frame_targets([child]) == [child]
