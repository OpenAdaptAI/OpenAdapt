"""Tests for the L1 acquisition-artifact emitter."""

import csv
import json

import pytest

from openadapt_flow.emit.l1_artifact import MANIFEST_FIELDS, emit_l1_artifact


@pytest.fixture()
def payload(tmp_path):
    p = tmp_path / "referral.txt"
    p.write_text("synthetic referral document body")
    return p


def test_emit_canonical_filename_and_manifest(payload, tmp_path):
    out = tmp_path / "extractions"
    ref = emit_l1_artifact(
        payload,
        out,
        file_number="P1",
        date="2026-07-06",
        doctype="referral",
        session_id="run-001",
    )

    assert ref.path == out / "P1_2026-07-06_referral.txt"
    assert ref.path.read_text() == payload.read_text()

    rows = list(csv.DictReader(ref.manifest_path.open()))
    assert len(rows) == 1
    row = rows[0]
    assert list(row.keys()) == MANIFEST_FIELDS
    assert row["filename"] == "P1_2026-07-06_referral.txt"
    assert row["session_id"] == "run-001"
    assert len(row["sha256"]) == 64

    prov = json.loads(ref.provenance_path.read_text())
    assert prov["sha256"] == row["sha256"]
    assert prov["tool_name"] == "openadapt-flow"


def test_reemit_identical_is_idempotent_but_content_conflict_raises(payload, tmp_path):
    out = tmp_path / "extractions"
    kwargs = dict(file_number="P1", date="2026-07-06", doctype="referral")
    emit_l1_artifact(payload, out, **kwargs)
    emit_l1_artifact(payload, out, **kwargs)  # same bytes: no error

    conflicting = tmp_path / "other.txt"
    conflicting.write_text("different body")
    with pytest.raises(FileExistsError):
        emit_l1_artifact(conflicting, out, **kwargs)


def test_fields_sanitized_and_date_validated(payload, tmp_path):
    out = tmp_path / "extractions"
    ref = emit_l1_artifact(
        payload, out, file_number="P/2 07", date="2026-07-06", doctype="op note"
    )
    assert ref.path.name == "P-2-07_2026-07-06_op-note.txt"

    with pytest.raises(ValueError):
        emit_l1_artifact(
            payload, out, file_number="P1", date="07/06/2026", doctype="referral"
        )

    with pytest.raises(FileNotFoundError):
        emit_l1_artifact(
            tmp_path / "missing.pdf",
            out,
            file_number="P1",
            date="2026-07-06",
            doctype="referral",
        )


def test_manifest_appends_across_artifacts(payload, tmp_path):
    out = tmp_path / "extractions"
    emit_l1_artifact(
        payload, out, file_number="P1", date="2026-07-06", doctype="referral"
    )
    second = tmp_path / "note.txt"
    second.write_text("op note body")
    emit_l1_artifact(second, out, file_number="P2", date="2026-07-05", doctype="opnote")

    rows = list(csv.DictReader((out / "manifest.csv").open()))
    assert [r["filename"] for r in rows] == [
        "P1_2026-07-06_referral.txt",
        "P2_2026-07-05_opnote.txt",
    ]
