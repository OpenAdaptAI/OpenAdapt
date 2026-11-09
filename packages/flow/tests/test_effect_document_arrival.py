"""Document/report arrival adapter: qualification fixtures.

Adversarial coverage: a corrupt (unparseable) report REFUTEs a
``parseable: True`` contract instead of vanishing; a report for the WRONG
entity never matches; duplicate reports REFUTE; a missing outbox is
UNAVAILABLE; stale reports fail a fresh contract; text extraction binds named
groups.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from openadapt_flow.deployment import EffectsConfig, build_effect_verifier
from openadapt_flow.runtime.effects import (
    AdapterResult,
    DocumentArrivalVerifier,
    Effect,
    EffectKind,
    Verdict,
    classify_adapter_result,
)
from openadapt_flow.runtime.effects.document_arrival import extract_json_path


def write_report(root, name, claim_id="c-77", status="submitted"):
    (root / name).write_text(json.dumps({"claim": {"id": claim_id, "status": status}}))


def make_verifier(root, **kwargs):
    kwargs.setdefault("field_paths", {"claim_id": "claim.id", "status": "claim.status"})
    kwargs.setdefault("pattern", "*.json")
    kwargs.setdefault("poll_interval_s", 0.0)
    return DocumentArrivalVerifier(str(root), **kwargs)


def report_effect(**kwargs):
    kwargs.setdefault("match", {"parseable": "True", "claim_id": "c-77"})
    kwargs.setdefault("count_new_only", True)
    kwargs.setdefault("timeout_s", 0.0)
    return Effect(kind=EffectKind.RECORD_WRITTEN, **kwargs)


class TestJsonReports:
    def test_parseable_report_with_declared_content_confirms(self, tmp_path):
        verifier = make_verifier(tmp_path)
        before = verifier.capture_pre_state()
        write_report(tmp_path, "r1.json")
        verdict = verifier.verify(report_effect(), before)
        assert verdict.verdict is Verdict.CONFIRMED
        assert verdict.matched_records[0]["status"] == "submitted"

    def test_corrupt_report_refutes_instead_of_vanishing(self, tmp_path):
        verifier = make_verifier(tmp_path)
        before = verifier.capture_pre_state()
        (tmp_path / "r1.json").write_text("{not json")
        verdict = verifier.verify(report_effect(), before)
        # The store is READABLE (not unavailable); the candidate exists but
        # does not parse, so the parseable contract affirmatively refutes.
        assert verdict.verdict is Verdict.REFUTED
        current = verifier._scan()
        assert current[0]["parseable"] == "False"

    def test_wrong_entity_report_never_matches(self, tmp_path):
        verifier = make_verifier(tmp_path)
        before = verifier.capture_pre_state()
        write_report(tmp_path, "r1.json", claim_id="c-99")
        verdict = verifier.verify(report_effect(), before)
        assert verdict.verdict is Verdict.REFUTED
        assert verdict.observed_count == 0

    def test_duplicate_reports_refute(self, tmp_path):
        verifier = make_verifier(tmp_path)
        before = verifier.capture_pre_state()
        write_report(tmp_path, "r1.json")
        write_report(tmp_path, "r2.json")
        verdict = verifier.verify(report_effect(), before)
        assert verdict.observed_count == 2
        assert classify_adapter_result(verdict) is AdapterResult.CONFLICTING

    def test_missing_outbox_is_unavailable(self, tmp_path):
        verifier = make_verifier(tmp_path / "nope")
        before = verifier.capture_pre_state()
        verdict = verifier.verify(report_effect(), before)
        assert classify_adapter_result(verdict) is AdapterResult.UNAVAILABLE
        assert verifier.test_connection().ok is False

    def test_stale_report_fails_fresh_contract(self, tmp_path):
        write_report(tmp_path, "r1.json")
        os.utime(tmp_path / "r1.json", (10_000.0, 10_000.0))
        verifier = make_verifier(tmp_path, mtime_window_s=60.0)
        before = verifier.capture_pre_state().model_copy(update={"records": []})
        effect = report_effect(
            match={"parseable": "True", "claim_id": "c-77", "fresh": "True"}
        )
        verdict = verifier.verify(effect, before)
        assert verdict.should_halt is True

    def test_far_future_report_fails_fresh_contract(self, tmp_path):
        write_report(tmp_path, "r1.json")
        future = time.time() + 3600
        os.utime(tmp_path / "r1.json", (future, future))
        verifier = make_verifier(tmp_path, mtime_window_s=60.0)
        before = verifier.capture_pre_state().model_copy(update={"records": []})
        effect = report_effect(
            match={"parseable": "True", "claim_id": "c-77", "fresh": "True"}
        )
        assert verifier.verify(effect, before).should_halt is True

    def test_field_equals_readback(self, tmp_path):
        write_report(tmp_path, "r1.json")
        verifier = make_verifier(tmp_path)
        before = verifier.capture_pre_state().model_copy(update={"records": []})
        effect = Effect(
            kind=EffectKind.FIELD_EQUALS,
            match={"claim_id": "c-77"},
            field="status",
            value="submitted",
            timeout_s=0.0,
        )
        verdict = verifier.verify(effect, before)
        assert verdict.verdict is Verdict.CONFIRMED
        # And the wrong value is contradicted, not glossed.
        effect_bad = effect.model_copy(
            update={"value": effect.value.model_copy(update={"literal": "approved"})}
        )
        verdict = verifier.verify(effect_bad, before)
        assert verdict.verdict is Verdict.REFUTED


class TestTextReports:
    def test_named_groups_become_fields(self, tmp_path):
        (tmp_path / "r1.txt").write_text("CLAIM RECEIPT\nclaim=c-77 status=OK\n")
        verifier = DocumentArrivalVerifier(
            str(tmp_path),
            pattern="*.txt",
            format="text",
            text_pattern=r"claim=(?P<claim_id>\S+) status=(?P<status>\S+)",
            poll_interval_s=0.0,
        )
        [record] = verifier._scan()
        assert record["claim_id"] == "c-77"
        assert record["status"] == "OK"
        assert record["parseable"] == "True"

    def test_unmatched_text_is_unparseable(self, tmp_path):
        (tmp_path / "r1.txt").write_text("garbled")
        verifier = DocumentArrivalVerifier(
            str(tmp_path),
            pattern="*.txt",
            format="text",
            text_pattern=r"claim=(?P<claim_id>\S+)",
        )
        [record] = verifier._scan()
        assert record["parseable"] == "False"
        assert "claim_id" not in record

    def test_text_format_requires_pattern(self):
        with pytest.raises(ValueError, match="text_pattern"):
            DocumentArrivalVerifier("/tmp/x", format="text")


class TestExtraction:
    def test_json_path_list_takes_first(self):
        doc = {"rows": [{"id": "a"}, {"id": "b"}]}
        assert extract_json_path(doc, "rows.id") == "a"

    def test_json_path_missing_is_none(self):
        assert extract_json_path({"a": 1}, "a.b.c") is None


class TestConfig:
    def test_config_builds_document_verifier(self, tmp_path):
        cfg = EffectsConfig(
            kind="document",
            root=str(tmp_path),
            file_pattern="*.json",
            document_field_paths={"claim_id": "claim.id"},
        )
        verifier = build_effect_verifier(cfg)
        assert isinstance(verifier, DocumentArrivalVerifier)

    def test_bad_format_fails_loud(self, tmp_path):
        cfg = EffectsConfig(
            kind="document", root=str(tmp_path), document_format="yamlish"
        )
        with pytest.raises(ValueError, match="document_format"):
            build_effect_verifier(cfg)
