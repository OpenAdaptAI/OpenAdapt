"""A certification sealed before a reviewed additive field still reproduces.

The fixture is the certified OpenEMR referral program (content digest
``ec096afa...``), sealed on 2026-10-20, before ``Anchor.form_identity`` and
``TokenTemplate.bearing`` existed. Its certification's workflow-contract digest
leaves both fields out. The current digest writes them as ``null``.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
from pathlib import Path

import pytest

from openadapt_flow import bundle_validation as bv
from openadapt_flow.ir import Workflow
from openadapt_flow.qualification import (
    QualificationError,
    build_governed_authorization_template,
    certified_contract_form,
    workflow_contract_digest,
)

FIXTURE = Path(__file__).parent / "fixtures" / "pre_form_identity_certified_openemr"
DIGEST = "ec096afa2b614326b00ec3155f256041c19d86f3998761ce9d772cfd911460b6"


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    target = tmp_path / "bundle"
    shutil.copytree(FIXTURE, target)
    return target


def _first_token(raw: dict) -> dict:
    for step in raw["steps"]:
        anchor = step.get("anchor") or {}
        template = anchor.get("identity_template") or {}
        if template.get("tokens"):
            return template["tokens"][0]
    raise AssertionError("fixture has no identity token")


def _rewrite(bundle: Path, edit) -> None:
    path = bundle / "workflow.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    edit(raw)
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")


def _without_rule(owner: type, field_name: str) -> dict:
    return {
        version: tuple(
            rule
            for rule in rules
            if not (rule.owner is owner and rule.field_name == field_name)
        )
        for version, rules in bv._SEALED_CANONICAL_OMISSIONS_BY_VERSION.items()
    }


def test_the_pre_field_certified_bundle_loads(bundle):
    workflow = Workflow.load(bundle)
    assert workflow.manifest.content_digest == DIGEST
    assert certified_contract_form(workflow) == "sealed_absent_defaults"
    certification = workflow.qualification.last_certification
    assert workflow_contract_digest(workflow) == (
        certification.workflow_contract_sha256,
        "sealed_absent_defaults",
    )


def test_a_non_null_bearing_is_refused(bundle):
    _rewrite(bundle, lambda raw: _first_token(raw).__setitem__("bearing", True))
    with pytest.raises(bv.BundleIntegrityError):
        Workflow.load(bundle)


def test_a_non_null_bearing_set_in_memory_breaks_the_certification(bundle):
    workflow = Workflow.load(bundle)
    token = next(
        step.anchor.identity_template.tokens[0]
        for step in workflow.steps
        if step.anchor is not None
        and step.anchor.identity_template is not None
        and step.anchor.identity_template.tokens
    )
    token.bearing = True
    assert workflow_contract_digest(workflow)[1] == "current"
    with pytest.raises(QualificationError, match="current certification"):
        build_governed_authorization_template(workflow)


def test_an_explicit_null_the_sealed_file_never_held_is_refused(bundle):
    # The content digest drops a null ``bearing`` either way, so only the
    # certification check can see that the file now writes the field.
    _rewrite(bundle, lambda raw: _first_token(raw).__setitem__("bearing", None))
    with pytest.raises(bv.BundleIntegrityError, match="cannot be reproduced"):
        Workflow.load(bundle)


def test_a_field_not_on_the_list_is_refused(bundle, monkeypatch):
    from openadapt_flow.ir import TokenTemplate

    monkeypatch.setattr(
        bv,
        "_SEALED_CANONICAL_OMISSIONS_BY_VERSION",
        _without_rule(TokenTemplate, "bearing"),
    )
    workflow = Workflow.load(bundle, verify_integrity=False)
    assert workflow_contract_digest(workflow)[1] == "current"
    with pytest.raises(QualificationError, match="current certification"):
        build_governed_authorization_template(workflow)


def test_a_rule_that_does_not_accept_none_is_not_used(monkeypatch):
    from openadapt_flow.ir import Anchor

    rules = bv._SEALED_CANONICAL_OMISSIONS_BY_VERSION
    narrowed = {
        version: tuple(
            dataclasses.replace(rule, omitted_values=("x",))
            if rule.owner is Anchor and rule.field_name == "form_identity"
            else rule
            for rule in version_rules
        )
        for version, version_rules in rules.items()
    }
    monkeypatch.setattr(bv, "_SEALED_CANONICAL_OMISSIONS_BY_VERSION", narrowed)
    workflow = Workflow.load(FIXTURE, verify_integrity=False)
    assert workflow_contract_digest(workflow)[1] == "current"


def test_a_version_without_rules_gets_no_fallback(bundle, monkeypatch):
    monkeypatch.setattr(bv, "_SEALED_CANONICAL_OMISSIONS_BY_VERSION", {})
    workflow = Workflow.load(bundle, verify_integrity=False)
    assert workflow_contract_digest(workflow)[1] == "current"
    with pytest.raises(QualificationError, match="current certification"):
        build_governed_authorization_template(workflow)


def test_a_bundle_bound_to_the_current_form_gets_no_fallback(bundle):
    workflow = Workflow.load(bundle, verify_integrity=False)
    sealed = workflow.qualification.last_certification.workflow_contract_sha256
    # Writing every field out, as today's save does, removes the fallback.
    raw = json.loads((bundle / "workflow.json").read_text(encoding="utf-8"))
    rewritten = Workflow.model_validate(
        {**workflow.model_dump(mode="json"), "manifest": raw["manifest"]}
    )
    current, form = workflow_contract_digest(rewritten)
    assert form == "current" and current != sealed
    # A qualification bound to the current digest takes it without trying
    # the older form.
    for case in rewritten.qualification.cases:
        for result in case.results:
            result.workflow_contract_sha256 = current
    rewritten.qualification.last_certification.workflow_contract_sha256 = current
    assert workflow_contract_digest(rewritten) == (current, "current")


def test_an_unqualified_workflow_uses_the_current_form(bundle):
    workflow = Workflow.load(bundle, verify_integrity=False)
    workflow.qualification = None
    assert workflow_contract_digest(workflow)[1] == "current"
