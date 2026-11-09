"""Behavior contract for binding reviewed record checks to a compiled program.

The binding is the operator seam for a GUI-only demonstration whose save step
carries no system-of-record contract. These tests pin what it may and may not
do: it publishes a new bundle and never touches the source, it only adds
contracts, it refuses a contract that cannot prove this run's write, it
accounts for every entered field, it keeps secrets out, and it invalidates a
prior certification instead of carrying it over.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openadapt_flow import crypto
from openadapt_flow.__main__ import main
from openadapt_flow.ir import ActionKind, Anchor, Step, Workflow
from openadapt_flow.record_check_binding import (
    PROVENANCE_FILENAME,
    RecordCheckBinding,
    RecordCheckBindingError,
    RecordCheckStep,
    bind_record_checks,
    check_record_checks,
    entered_parameters,
    record_check_coverage,
)
from openadapt_flow.runtime.effects import Effect, EffectKind, ValueExpr

_CROP = b"\x89PNG\r\n\x1a\nsynthetic-save-crop"
_KEY = "customer-controlled-test-key"


def _program(tmp_path: Path, *, save_effects: list[Effect] | None = None) -> Path:
    bundle = tmp_path / "program"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "save.png").write_bytes(_CROP)
    Workflow(
        name="synthetic-intake",
        params={"member_no": "100200300", "note": "first visit", "pin": "0000"},
        secret_params=["pin"],
        steps=[
            Step(
                id="step_000",
                intent="type <member_no>",
                action=ActionKind.TYPE,
                param="member_no",
            ),
            Step(
                id="step_001",
                intent="type <note>",
                action=ActionKind.TYPE,
                param="note",
            ),
            Step(
                id="step_002", intent="type <pin>", action=ActionKind.TYPE, param="pin"
            ),
            Step(
                id="step_003",
                intent="click 'Save'",
                action=ActionKind.CLICK,
                anchor=Anchor(
                    template="templates/save.png",
                    region=(10, 20, 30, 40),
                    click_point=(25, 40),
                ),
                effects=list(save_effects or []),
            ),
        ],
    ).save(bundle)
    return bundle


def _binding(**overrides) -> RecordCheckBinding:
    raw = {
        "schema": "openadapt.record-check-binding/v1",
        "workflow_name": "synthetic-intake",
        "steps": [
            {
                "step": "step_003",
                "intent": "click 'Save'",
                "effects": [
                    {
                        "kind": "record_written",
                        "match": {"member": {"param": "member_no"}},
                        "expected_count": 1,
                        "count_new_only": True,
                    },
                    {
                        "kind": "field_equals",
                        "match": {"member": {"param": "member_no"}},
                        "field": "note",
                        "value": {"param": "note"},
                    },
                ],
            }
        ],
    }
    raw.update(overrides)
    return RecordCheckBinding.model_validate(raw)


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file()
    }


def test_binding_publishes_a_new_sealed_bundle_and_leaves_the_source(tmp_path):
    source = _program(tmp_path)
    before = _snapshot(source)
    out = tmp_path / "bound"

    result = bind_record_checks(source, _binding(), out)

    assert _snapshot(source) == before
    bound = Workflow.load(out, verify_integrity=True)
    save = bound.steps[3]
    assert [e.kind for e in save.effects] == [
        EffectKind.RECORD_WRITTEN,
        EffectKind.FIELD_EQUALS,
    ]
    # Only contracts were added: action, target and risk labels are untouched.
    original = Workflow.load(source).steps[3]
    assert save.action == original.action
    assert save.anchor == original.anchor
    assert save.risk == original.risk
    assert save.risk_review_required == original.risk_review_required
    assert save.expect == original.expect
    assert result.content_digest == bound.manifest.content_digest
    assert result.source_content_digest != result.content_digest
    provenance = json.loads((out / PROVENANCE_FILENAME).read_text())
    assert provenance["binding_sha256"] == _binding().sha256()
    assert provenance["source_content_digest"] == result.source_content_digest
    assert result.coverage.complete


def test_refuses_existing_output_path(tmp_path):
    source = _program(tmp_path)
    out = tmp_path / "bound"
    out.mkdir()
    with pytest.raises(FileExistsError):
        bind_record_checks(source, _binding(), out)


def test_every_entered_field_must_be_compared_or_explained(tmp_path):
    source = _program(tmp_path)
    binding = _binding()
    binding.steps[0].effects.pop()  # drop the note read-back
    with pytest.raises(RecordCheckBindingError, match="entered field 'note'"):
        bind_record_checks(source, binding, tmp_path / "bound")
    assert not (tmp_path / "bound").exists()

    explained = _binding(not_compared={"note": "free text the system truncates"})
    explained.steps[0].effects.pop()
    coverage = check_record_checks(source, explained)
    by_param = {item.param: item for item in coverage.fields}
    assert by_param["member_no"].status == "compared"
    assert by_param["note"].status == "not_compared"
    assert by_param["note"].reason == "free text the system truncates"


def test_secret_parameters_are_neither_required_nor_allowed(tmp_path):
    source = _program(tmp_path)
    workflow = Workflow.load(source)
    assert entered_parameters(workflow) == ["member_no", "note"]

    binding = _binding()
    binding.steps[0].effects.append(
        {
            "kind": "field_equals",
            "match": {"member": {"param": "member_no"}},
            "field": "pin",
            "value": {"param": "pin"},
        }
    )
    with pytest.raises(RecordCheckBindingError, match="secret parameter 'pin'"):
        check_record_checks(source, binding)


def test_a_change_claim_is_required(tmp_path):
    source = _program(tmp_path)
    binding = _binding()
    binding.steps[0].effects[0]["count_new_only"] = False
    with pytest.raises(RecordCheckBindingError, match="no change claim"):
        check_record_checks(source, binding)


def test_the_selector_must_bind_a_run_parameter(tmp_path):
    source = _program(tmp_path)
    binding = _binding()
    binding.steps[0].effects[1]["match"] = {"member": "100200300"}
    with pytest.raises(RecordCheckBindingError, match="through a run parameter"):
        check_record_checks(source, binding)


def test_unknown_parameter_step_and_field_are_refused(tmp_path):
    source = _program(tmp_path)
    binding = _binding()
    binding.steps[0].effects[1]["value"] = {"param": "not_a_param"}
    binding.steps[0].effects[1]["probe_typo"] = "x"
    binding.steps.append(
        RecordCheckStep(step="step_999", effects=[{"kind": "record_written"}])
    )
    with pytest.raises(RecordCheckBindingError) as excinfo:
        check_record_checks(source, binding)
    text = str(excinfo.value)
    assert "unknown field(s) probe_typo" in text
    assert "'step_999' is not in this program" in text


def test_intent_and_digest_pins_guard_against_a_recompiled_program(tmp_path):
    source = _program(tmp_path)
    binding = _binding()
    binding.steps[0].intent = "click 'Delete'"
    with pytest.raises(RecordCheckBindingError, match="intent is"):
        check_record_checks(source, binding)
    with pytest.raises(RecordCheckBindingError, match="pinned to program digest"):
        check_record_checks(source, _binding(source_content_digest="0" * 64))
    with pytest.raises(RecordCheckBindingError, match="binding is for workflow"):
        check_record_checks(source, _binding(workflow_name="another-program"))


def test_confirmed_contracts_are_kept_and_placeholders_replaced(tmp_path):
    kept = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"member": ValueExpr(param="member_no")},
        expected_count=1,
    )
    placeholder = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"__unbound__": ValueExpr(literal="?")},
        needs_operator_confirmation=True,
    )
    source = _program(tmp_path, save_effects=[kept, placeholder])
    result = bind_record_checks(source, _binding(), tmp_path / "bound")
    effects = Workflow.load(tmp_path / "bound").steps[3].effects
    assert effects[0] == kept
    assert not any(e.needs_operator_confirmation for e in effects)
    assert len(effects) == 3
    assert result.replaced_placeholders == {"step_003": 1}

    duplicate = _program(tmp_path / "again", save_effects=[effects[1]])
    with pytest.raises(RecordCheckBindingError, match="duplicates a contract"):
        check_record_checks(duplicate, _binding())


def test_prior_certification_is_invalidated(tmp_path):
    source = _program(tmp_path)
    workflow = Workflow.load(source)
    workflow.stamp_certification("permissive", passed=True)
    workflow.save(source)
    result = bind_record_checks(source, _binding(), tmp_path / "bound")
    assert result.certification_invalidated
    bound = Workflow.load(tmp_path / "bound")
    assert bound.manifest.provenance.certified is False
    assert (
        bound.manifest.provenance.certification_invalidation_reason
        == "record-check binding changed the workflow contract"
    )
    assert Workflow.load(source).manifest.provenance.certified is True


def test_encrypted_bundle_stays_encrypted(tmp_path, monkeypatch):
    monkeypatch.setenv(crypto.ENV_KEY, _KEY)
    plain = _program(tmp_path)
    sealed = tmp_path / "sealed"
    from openadapt_flow.bundle_sealing import seal_bundle

    seal_bundle(plain, sealed)
    bind_record_checks(sealed, _binding(), tmp_path / "bound")
    assert not (tmp_path / "bound" / "workflow.json").exists()
    assert crypto.is_encrypted((tmp_path / "bound" / "workflow.json.enc").read_bytes())
    loaded = Workflow.load(tmp_path / "bound", key=_KEY, verify_integrity=True)
    assert len(loaded.steps[3].effects) == 2


def test_coverage_ignores_placeholders(tmp_path):
    placeholder = Effect(
        kind=EffectKind.FIELD_EQUALS,
        match={"member": ValueExpr(param="member_no")},
        field="note",
        value=ValueExpr(param="note"),
        needs_operator_confirmation=True,
    )
    source = _program(tmp_path, save_effects=[placeholder])
    coverage = record_check_coverage(Workflow.load(source))
    assert not coverage.complete
    assert {item.status for item in coverage.fields} == {"uncovered"}


def test_cli_check_then_bind(tmp_path, capsys):
    source = _program(tmp_path)
    binding_path = tmp_path / "binding.json"
    binding_path.write_text(_binding().model_dump_json(by_alias=True))

    assert (
        main(
            [
                "bind-record-checks",
                str(source),
                "--binding",
                str(binding_path),
                "--check",
            ]
        )
        == 0
    )
    assert "compare 2 of 2 entered field(s)" in capsys.readouterr().out
    assert not (tmp_path / "bound").exists()

    out = tmp_path / "bound"
    assert (
        main(
            [
                "bind-record-checks",
                str(source),
                "--binding",
                str(binding_path),
                "-o",
                str(out),
            ]
        )
        == 0
    )
    printed = capsys.readouterr().out
    assert "step_003: 2 record check(s) bound" in printed
    assert (
        main(
            [
                "bind-record-checks",
                str(source),
                "--binding",
                str(binding_path),
                "-o",
                str(out),
            ]
        )
        == 2
    )
    assert "REFUSED" in capsys.readouterr().out


def test_cli_requires_out_without_check(tmp_path, capsys):
    source = _program(tmp_path)
    binding_path = tmp_path / "binding.yaml"
    binding_path.write_text(
        "schema: openadapt.record-check-binding/v1\n"
        "workflow_name: synthetic-intake\n"
        "steps:\n"
        "  - step: step_003\n"
        "    effects:\n"
        "      - {kind: record_written, match: {member: {param: member_no}}, count_new_only: true}\n"
        "not_compared: {note: free text}\n"
    )
    assert (
        main(["bind-record-checks", str(source), "--binding", str(binding_path)]) == 2
    )
    assert "--out is required" in capsys.readouterr().out


def test_encrypted_bundle_without_key_is_refused_cleanly(tmp_path, monkeypatch):
    monkeypatch.setenv(crypto.ENV_KEY, _KEY)
    plain = _program(tmp_path)
    sealed = tmp_path / "sealed"
    from openadapt_flow.bundle_sealing import seal_bundle

    seal_bundle(plain, sealed)
    monkeypatch.delenv(crypto.ENV_KEY)
    with pytest.raises(RecordCheckBindingError, match="encrypted"):
        check_record_checks(sealed, _binding())
    with pytest.raises(RecordCheckBindingError, match="encrypted"):
        bind_record_checks(sealed, _binding(), tmp_path / "bound")
