"""State transitions over the public program-dispatch execution handoff."""

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from openadapt_flow.jit import hosted_authority as ha
from openadapt_flow.runner.dispatch_envelope import with_runner_local_authorization
from openadapt_flow.runtime.durable import authority as durable_authority
from openadapt_flow.runtime.replayer import Replayer
from tests.test_execution_profiles import _KEY, _ReadyVision
from tests.test_jit_hosted_managed_replay import (
    PARAMS,
    Issuer,
    _binding,
    _make_managed,
    _replay,
)
from tests.test_jit_hosted_managed_replay import (
    issuer as issuer,
)
from tests.test_jit_hosted_managed_replay import (
    managed as managed,
)
from tests.test_replayer import FakeBackend


@settings(max_examples=14, deadline=None)
@given(
    changes=st.lists(
        st.sampled_from(
            [
                "session",
                "registry",
                "admission",
                "readback",
                "workflow",
                "inputs",
                "authorization",
            ]
        ),
        max_size=4,
    ),
    entry=st.sampled_from(["runtime", "deployment"]),
)
def test_no_old_capability_survives_a_change_in_its_scope(
    changes: list[str], entry: str
) -> None:
    """An unchanged signed scope runs; any invalidating transition emits no input."""
    with TemporaryDirectory() as temporary, pytest.MonkeyPatch.context() as patch:
        root = Path(temporary).resolve()
        managed = _make_managed(root, patch)
        binding = _binding(managed)
        issuer = Issuer()
        issuer.admission_digest = managed["digest"]
        real_init = durable_authority.DurableAuthority.__init__

        def init(self: Any, run_dir: Any, store: Any, *, remote_transport: Any = None):
            real_init(self, run_dir, store, remote_transport=remote_transport or issuer)

        patch.setattr(durable_authority.DurableAuthority, "__init__", init)
        patch.setenv(
            durable_authority.REMOTE_AUTHORITY_TOKEN_ENV, "synthetic-run-token"
        )
        patch.setenv(durable_authority.AUTHORITY_DB_ENV, str(root / "authority.db"))
        params = PARAMS
        for change in changes:
            if change == "session":
                patch.setenv(
                    durable_authority.REMOTE_DISPATCH_SESSION_ID_ENV,
                    "30000000-0000-4000-8000-000000000002",
                )
            elif change == "registry":
                (managed["keys"] / "principals.json").write_text(
                    '{"schema":"openadapt-flow/jit-principals/v1","admitter_keys":{"rotated":"'
                    + "1" * 64
                    + '"}}'
                )
            elif change == "admission":
                (managed["bundle"] / "admission.json").unlink(missing_ok=True)
            elif change == "readback":
                with (managed["bundle"] / ha.READBACK_PATH).open("a") as file:
                    file.write("\n# a different signed check\n")
            elif change == "workflow":
                managed["workflow"].steps[0].intent = "different declared task"
            elif change == "inputs":
                params = {"lname": "Another"}
            else:
                # This supported local-field update invalidates the old proof;
                # another execution must explicitly verify its resolved mode.
                updated = binding.authorization.model_copy(
                    update={"approval_mode": "problems_only"}
                )
                binding = with_runner_local_authorization(binding, updated)
        if changes:
            with pytest.raises(ValueError):
                _replay(managed, binding, entry=entry, params=params)
            assert issuer.permits == 0
            assert all(not backend.actions for backend in managed["backends"])
        else:
            result = _replay(managed, binding, entry=entry)
            assert result.success
            assert issuer.permits >= 2 and issuer.receipts == issuer.permits
            assert any(backend.actions for backend in managed["backends"])


@pytest.mark.parametrize("authority", ["fresh", "consumed", "unverified"])
@pytest.mark.parametrize(
    "origin", ["https://app.example.com", None, "https://another.example.com"]
)
def test_a_managed_resume_requires_fresh_scope_and_the_actual_live_target(
    managed: dict[str, Any],
    issuer: Issuer,
    authority: str,
    origin: str | None,
) -> None:
    from openadapt_flow.runtime.durable import resume
    from tests.test_durable_runtime import _approval

    class StopBeforeInput(Replayer):
        def _fresh_actuation_authorization_refusal(self, workflow, params, step):
            refusal = super()._fresh_actuation_authorization_refusal(
                workflow, params, step
            )
            return refusal or "synthetic operator stop before input"

    binding = _binding(managed)
    first_backend = FakeBackend()
    first = StopBeforeInput(
        first_backend,
        vision=_ReadyVision(),
        effect_verifier=managed["verifier"],
        managed_dispatch_binding=binding,
        durable=True,
        checkpoint_key=_KEY,
        require_settled=True,
    ).run(
        managed["workflow"],
        params=PARAMS,
        bundle_dir=managed["bundle"],
        run_dir=managed["run_dir"],
        execution_origin="https://app.example.com",
    )
    assert not first.success and not first_backend.actions and issuer.permits == 0
    approval = _approval(managed["bundle"], managed["run_dir"], checkpoint_key=_KEY)
    if authority == "fresh":
        binding = _binding(managed)
    elif authority == "unverified":
        binding = _binding(managed, verified=False)
    backend = FakeBackend()
    replayer = Replayer(
        backend,
        vision=_ReadyVision(),
        effect_verifier=managed["verifier"],
        managed_dispatch_binding=binding,
        durable=True,
        checkpoint_key=_KEY,
        require_settled=True,
    )
    if authority != "fresh" or origin != "https://app.example.com":
        with pytest.raises((ValueError, RuntimeError)):
            resume(
                managed["run_dir"],
                replayer,
                approval=approval,
                key=_KEY,
                execution_origin=origin,
            )
        assert backend.actions == [] and issuer.permits == 0
    else:
        result = resume(
            managed["run_dir"],
            replayer,
            approval=approval,
            key=_KEY,
            execution_origin=origin,
        )
        assert result.success
        assert backend.actions and issuer.permits >= 2
