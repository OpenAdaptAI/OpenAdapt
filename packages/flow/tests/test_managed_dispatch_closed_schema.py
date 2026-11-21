"""Raw managed restrictions cannot vanish through permissive historical models."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from openadapt_flow.runner.dispatch_envelope import (
    ManagedDispatchEnvelope,
    ManagedDispatchEnvelopeError,
    _refuse_dropped_authorization_fields,
    read_managed_dispatch_envelope,
)
from openadapt_flow.runner.protocol import dispatch_binding_sha256
from openadapt_flow.runtime.authorization import GovernedRunAuthorization
from tests.test_jit_hosted_authority import (
    _dispatch,
    _run,
)
from tests.test_jit_hosted_authority import (
    program as program,
)
from tests.test_jit_hosted_authority import (
    runner as runner,
)


def _envelope() -> dict[str, Any]:
    authorization = GovernedRunAuthorization(
        bundle_content_digest="a" * 64,
        runtime_inputs_digest="b" * 64,
        admitted_policy_name="permissive",
        approval_source="synthetic-local-authority",
        unverified_write_approvals=[
            {
                "step_id": "save",
                "effect_contract_hashes": ["c" * 64],
            }
        ],
        api_steps=[
            {
                "step_id": "save",
                "admission_digest": "sha256:" + "d" * 64,
                "folded_checks_digest": "sha256:" + "e" * 64,
                "effect_contract_hashes": ["f" * 64],
            }
        ],
    )
    run_id = "00000000-0000-4000-8000-000000000123"
    return ManagedDispatchEnvelope(
        run_id=run_id,
        bundle_content_digest=authorization.bundle_content_digest,
        runtime_inputs_digest=authorization.runtime_inputs_digest,
        authorization=authorization,
        dispatch_binding_sha256=dispatch_binding_sha256(run_id, authorization),
    ).model_dump(mode="json")


@pytest.mark.parametrize("entry", ["model", "json", "private_file"])
@pytest.mark.parametrize("location", ["authorization", "api_step", "write_approval"])
def test_future_restrictions_refuse_before_any_dispatch_binding(
    tmp_path,
    entry: str,
    location: str,
) -> None:
    raw = _envelope()
    target = raw["authorization"]
    if location == "api_step":
        target = target["api_steps"][0]
    elif location == "write_approval":
        target = target["unverified_write_approvals"][0]
    # The historical model ignores these keys. A digest over only the known
    # fields is unchanged, but the managed boundary must refuse the raw tree.
    target["future_maximum_input_edges"] = 0
    if entry == "model":
        with pytest.raises(ValidationError, match="unknown fields"):
            ManagedDispatchEnvelope.model_validate(raw)
    elif entry == "json":
        with pytest.raises(ValidationError, match="unknown fields"):
            ManagedDispatchEnvelope.model_validate_json(json.dumps(raw))
    else:
        path = tmp_path / "dispatch.json"
        path.write_text(json.dumps(raw))
        path.chmod(0o600)
        with pytest.raises(ManagedDispatchEnvelopeError):
            read_managed_dispatch_envelope(path)


def test_current_dispatch_defaults_remain_supported() -> None:
    raw = _envelope()
    for field in [
        "qualification_admission",
        "qualification_admission_sha256",
        "approval_mode",
    ]:
        raw["authorization"].pop(field, None)
    parsed = ManagedDispatchEnvelope.model_validate(raw)
    assert parsed.exact_authorization().approval_mode is None


def test_declared_string_aliases_are_closed_and_unambiguous() -> None:
    class Aliased(BaseModel):
        model_config = ConfigDict(populate_by_name=True)
        input_limit: int = Field(alias="inputLimit")

    for raw in [{"inputLimit": 0}, {"input_limit": 0}]:
        _refuse_dropped_authorization_fields(raw, Aliased.model_validate(raw))
    for raw in [
        {"inputLimits": 0, "inputLimit": 1},
        {"input_limit": 0, "inputLimit": 1},
    ]:
        with pytest.raises(ValueError):
            _refuse_dropped_authorization_fields(raw, Aliased.model_validate(raw))


@pytest.mark.parametrize("field", ["future_maximum_input_edges", "approvalMode"])
def test_actual_cli_refuses_a_dropped_or_misspelled_restriction_before_replay(
    tmp_path,
    program,
    runner,
    monkeypatch,
    capsys,
    field,
) -> None:
    from openadapt_flow.jit import lifecycle

    monkeypatch.setenv(lifecycle.KEY_DIR_ENV, str(program["keys"]))
    path = _dispatch(tmp_path, program)
    raw = json.loads(path.read_text())
    raw["authorization"][field] = "per_item"
    path.write_text(json.dumps(raw))
    assert _run(tmp_path, program, path, "standard") == 2
    assert "authorization" not in runner
    assert "Nothing was executed" in capsys.readouterr().out
