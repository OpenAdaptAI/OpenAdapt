"""The approval gate in the commands Desktop calls: ``run``, ``qualify run-case``,
``resume`` and ``item-approval``. Made-up values only.

What these pin:

- ``run`` and ``qualify run-case`` default to ``each_item`` under the
  Standard and Regulated profiles, including for a missing or unknown
  configured mode, and bind the mode into the run's authorization;
- the hosted runner and the BYOC connector say ``not_gated`` explicitly, and
  Cloud's dispatch binding digest is unchanged by the runner-local mode;
- a resume keeps the run's own mode and refuses another;
- ``item-approval`` answers exactly the request the person saw, once, only
  for ``--decided-by human`` from ``--route desktop_run_it``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from openadapt_flow.approval_mode.item_approval import ItemApprovalStore
from openadapt_flow.approval_mode.model import ApprovalMode
from openadapt_flow.runtime.authorization import GovernedRunAuthorization
from tests.test_run_gate import _KEY, _representative_qualification_bundle


def _authorization(**overrides) -> GovernedRunAuthorization:
    fields = dict(
        bundle_content_digest="a" * 64,
        runtime_inputs_digest="b" * 64,
        admitted_policy_name="clinical-write",
        execution_profile="standard",
    )
    fields.update(overrides)
    return GovernedRunAuthorization(**fields)


# -- resolving the mode -------------------------------------------------------------


@pytest.mark.parametrize(
    ("flag", "configured", "profile", "expected"),
    [
        (None, None, "standard", ApprovalMode.EACH_ITEM),
        (None, None, "regulated", ApprovalMode.EACH_ITEM),
        (None, "not_a_mode", "standard", ApprovalMode.EACH_ITEM),
        (None, "not_gated", "standard", ApprovalMode.NOT_GATED),
        ("not_gated", "each_item", "standard", ApprovalMode.NOT_GATED),
        ("problems_only", None, "regulated", ApprovalMode.PROBLEMS_ONLY),
        (None, None, "demo", ApprovalMode.EACH_ITEM),
        (None, "not_gated", None, ApprovalMode.NOT_GATED),
        (None, None, None, ApprovalMode.EACH_ITEM),
        ("each_item", None, "demo", ApprovalMode.EACH_ITEM),
    ],
)
def test_run_resolves_each_item_unless_a_mode_is_named(
    flag, configured, profile, expected
):
    from openadapt_flow.__main__ import _resolve_run_approval_mode

    args = SimpleNamespace(approval_mode=flag)
    cfg = SimpleNamespace(runtime=SimpleNamespace(approval_mode=configured))
    assert _resolve_run_approval_mode(args, cfg, profile) is expected


def test_binding_refuses_an_authorization_that_names_another_mode(capsys):
    from openadapt_flow.__main__ import _bind_approval_mode

    args = SimpleNamespace(
        _approval_mode=ApprovalMode.NOT_GATED,
        _governed_run_authorization=_authorization(approval_mode="each_item"),
    )
    assert _bind_approval_mode(args) == 2
    assert "names the each_item approval mode" in capsys.readouterr().out

    args = SimpleNamespace(
        _approval_mode=ApprovalMode.EACH_ITEM,
        _governed_run_authorization=_authorization(),
    )
    assert _bind_approval_mode(args) is None
    assert args._governed_run_authorization.approval_mode == "each_item"


def test_an_authorization_without_a_mode_serializes_as_before():
    authorization = _authorization()
    assert "approval_mode" not in authorization.model_dump(mode="json")
    bound = authorization.model_copy(update={"approval_mode": "each_item"})
    assert bound.model_dump(mode="json")["approval_mode"] == "each_item"
    assert bound != authorization
    restored = GovernedRunAuthorization.model_validate_json(bound.model_dump_json())
    assert restored.approval_mode == "each_item"
    with pytest.raises(ValueError):
        _authorization(approval_mode="skip_the_person")


def test_the_cloud_binding_digest_ignores_the_runner_local_mode():
    from openadapt_flow.runner.protocol import dispatch_binding_sha256

    run_id = "00000000-0000-4000-8000-000000000001"
    plain = _authorization()
    for mode in ("each_item", "problems_only", "not_gated"):
        bound = plain.model_copy(update={"approval_mode": mode})
        assert dispatch_binding_sha256(run_id, bound) == dispatch_binding_sha256(
            run_id, plain
        )


def test_a_runner_local_update_cant_change_a_cloud_bound_field():
    from openadapt_flow.runner.dispatch_envelope import (
        _BINDING_FACTORY,
        ManagedDispatchBinding,
        ManagedDispatchEnvelopeError,
        with_runner_local_authorization,
    )
    from openadapt_flow.runner.protocol import dispatch_binding_sha256

    run_id = "00000000-0000-4000-8000-000000000002"
    plain = _authorization()
    binding = ManagedDispatchBinding(
        _BINDING_FACTORY,
        run_id=run_id,
        authorization=plain,
        binding_sha256=dispatch_binding_sha256(run_id, plain),
    )
    rebound = with_runner_local_authorization(
        binding, plain.model_copy(update={"approval_mode": "not_gated"})
    )
    assert rebound.binding_sha256 == binding.binding_sha256
    assert rebound.authorization.approval_mode == "not_gated"
    with pytest.raises(ManagedDispatchEnvelopeError):
        with_runner_local_authorization(
            binding, plain.model_copy(update={"execution_profile": "demo"})
        )


# -- resume keeps the run's mode -------------------------------------------------------


def test_resume_keeps_the_retained_mode_and_refuses_another(capsys):
    from openadapt_flow.__main__ import _resume_approval_mode

    retained = _authorization(approval_mode="each_item")
    assert (
        _resume_approval_mode(SimpleNamespace(approval_mode=None), retained)
        is ApprovalMode.EACH_ITEM
    )
    assert (
        _resume_approval_mode(SimpleNamespace(approval_mode="not_gated"), retained) == 3
    )
    assert "admitted in the each_item approval mode" in capsys.readouterr().out

    legacy = _authorization()
    assert (
        _resume_approval_mode(SimpleNamespace(approval_mode=None), legacy)
        is ApprovalMode.EACH_ITEM
    )
    assert (
        _resume_approval_mode(SimpleNamespace(approval_mode="not_gated"), legacy)
        is ApprovalMode.NOT_GATED
    )
    assert _resume_approval_mode(SimpleNamespace(approval_mode=None), None) is None


# -- qualify run-case (Desktop's Check it) ------------------------------------------------


def _run_case_argv(
    bundle: Path, inputs: Path, tmp_path: Path, *extra: str
) -> list[str]:
    return [
        "qualify",
        "run-case",
        str(bundle),
        "--case-id",
        "representative-1",
        "--inputs",
        str(inputs),
        "--campaign-id",
        "campaign-1",
        "--run-id",
        "run-1",
        "--run-dir",
        str(tmp_path / "case-run"),
        "--effects-kind",
        "rest",
        "--effects-base-url",
        "http://sor.local",
        "--backend",
        "web",
        *extra,
    ]


@pytest.mark.parametrize(
    ("extra", "expected"),
    [((), "each_item"), (("--approval-mode", "not_gated"), "not_gated")],
)
def test_run_case_binds_each_item_by_default(tmp_path, monkeypatch, extra, expected):
    import openadapt_flow.__main__ as main

    _workflow, bundle, inputs = _representative_qualification_bundle(tmp_path)
    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", _KEY)
    monkeypatch.setenv("OPENADAPT_HOME", str(tmp_path / "state"))
    inputs_path = tmp_path / "case-inputs.json"
    inputs_path.write_bytes(inputs)
    inputs_path.chmod(0o600)
    captured = {}

    def capture(args):
        captured["authorization"] = args._governed_run_authorization
        captured["gate"] = main._approval_gate_for_run(args, tmp_path / "case-run")
        return 0

    monkeypatch.setattr(main, "_cmd_replay", capture)
    assert main.main(_run_case_argv(bundle, inputs_path, tmp_path, *extra)) == 0
    assert captured["authorization"].approval_mode == expected
    gate = captured["gate"]
    assert gate.mode.value == expected
    assert gate.approval_store is not None
    assert gate.approval_store.run_dir == tmp_path / "case-run"


def test_run_case_builds_a_durable_replayer_with_the_gate(tmp_path, monkeypatch):
    import openadapt_flow.__main__ as main
    import openadapt_flow.backends.factory as factory
    import openadapt_flow.deployment as deployment
    import openadapt_flow.report as report_module

    _workflow, bundle, inputs = _representative_qualification_bundle(
        tmp_path, target_kind="windows"
    )
    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", _KEY)
    monkeypatch.setenv("OPENADAPT_HOME", str(tmp_path / "state"))
    inputs_path = tmp_path / "case-inputs.json"
    inputs_path.write_bytes(inputs)
    inputs_path.chmod(0o600)
    captured = {}

    class Backend:
        def close(self):
            pass

    class Report:
        success = True
        execution_outcome = "VERIFIED"
        screenshots_may_leave_box = False

    class FakeReplayer:
        def run(self, *_args, **_kwargs):
            return Report()

    def build(_backend, **kwargs):
        captured.update(kwargs)
        return FakeReplayer()

    monkeypatch.setattr(factory, "build_backend", lambda _cfg: Backend())
    monkeypatch.setattr(deployment, "build_replayer", build)
    monkeypatch.setattr(report_module, "render_run_report", lambda _run_dir: "R.md")
    argv = _run_case_argv(bundle, inputs_path, tmp_path)
    argv[argv.index("web")] = "windows"
    argv += ["--agent-url", "http://127.0.0.1:5001", "--approval-wait", "42"]
    assert main.main(argv) == 0
    gate = captured["approval_gate"]
    assert gate.mode is ApprovalMode.EACH_ITEM
    assert gate._approval_wait_s == 42
    assert captured["governed_authorization"].approval_mode == "each_item"
    assert captured["qualification_fault_driver"] is None


# -- item-approval (Desktop's Run it) ---------------------------------------------------------


def _request(tmp_path: Path):
    store = ItemApprovalStore(tmp_path / "run")
    request = store.open_request(
        run_id="run-0001",
        workflow_name="referral",
        step_id="save",
        step_index=2,
        step_intent="Click 'Save'",
        mode=ApprovalMode.EACH_ITEM,
        bundle_content_digest="a" * 64,
        runtime_inputs_digest="b" * 64,
        frame_png=b"made-up filled form",
        form_state=("dom", "sha256:" + "1" * 64),
    )
    return store, request


def _answer_argv(store, request, verb="approve", *extra):
    return [
        "item-approval",
        verb,
        str(store.run_dir),
        "--request-id",
        request.request_id,
        "--request-sha256",
        request.digest(),
        *extra,
    ]


PERSON = ("--decided-by", "human", "--route", "desktop_run_it")
SIGNED_IN = ("--approver-id", "cloud-user-0001", "--auth-method", "cloud_aal2")


def _signed(approver) -> tuple[str, ...]:
    """A person in Run it: Desktop's registered approver key signs the answer."""

    key_path, _signer = approver
    return ("--unauthenticated", "--approver-key", str(key_path))


def _json(capsys) -> dict:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_show_lists_what_the_person_must_answer(tmp_path, capsys):
    import openadapt_flow.__main__ as main

    store, request = _request(tmp_path)
    assert main.main(["item-approval", "show", str(store.run_dir)]) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["schema"] == "openadapt.item-approval-list/v1"
    (row,) = listing["requests"]
    assert row["request_id"] == request.request_id
    assert row["request_sha256"] == request.digest()
    assert row["status"] == "waiting"
    assert Path(row["screenshot"]).is_file()


def test_a_person_approves_once(tmp_path, capsys, approver):
    import openadapt_flow.__main__ as main

    store, request = _request(tmp_path)
    argv = _answer_argv(store, request, "approve", *PERSON, *_signed(approver))
    assert main.main(argv) == 0
    result = _json(capsys)
    assert result["ok"] is True and result["decision"] == "approve"
    assert result["resume_required"] is False
    assert store.status(request) == "approved"
    answer = store.read_answer(request.request_id)
    assert answer.attestation is not None
    assert answer.attestation.signer == "front-desk"

    assert main.main(argv) == 2
    assert _json(capsys)["code"] == "request_approved"


def test_a_signed_out_decline_is_recorded_without_a_name(tmp_path, capsys, approver):
    import openadapt_flow.__main__ as main

    store, request = _request(tmp_path)
    argv = _answer_argv(
        store,
        request,
        "decline",
        *PERSON,
        *_signed(approver),
        "--reason",
        "wrong_values",
    )
    assert main.main(argv) == 0
    answer = store.read_answer(request.request_id)
    assert answer.approver_kind == "unauthenticated" and answer.approver_id is None
    assert answer.decline_reason == "wrong_values"
    assert answer.attestation is not None


@pytest.mark.parametrize(
    ("argv_tail", "code"),
    [
        (
            (
                "--decided-by",
                "automation",
                "--route",
                "desktop_run_it",
                "--unauthenticated",
            ),
            "not_decided_by_a_person",
        ),
        (
            (
                "--decided-by",
                "unknown",
                "--route",
                "desktop_run_it",
                "--unauthenticated",
            ),
            "not_decided_by_a_person",
        ),
        (
            ("--decided-by", "human", "--route", "phone", "--unauthenticated"),
            "route_not_allowed",
        ),
        ((*PERSON,), "approver_invalid"),
        ((*PERSON, *SIGNED_IN, "--unauthenticated"), "approver_invalid"),
        ((*PERSON, "--approver-id", "cloud-user-0001"), "approver_invalid"),
        (
            (*PERSON, "--unauthenticated", "--auth-method", "cloud_aal2"),
            "approver_invalid",
        ),
    ],
)
def test_anything_but_a_person_in_run_it_is_refused(
    tmp_path, capsys, approver, argv_tail, code
):
    import openadapt_flow.__main__ as main

    key_path, _signer = approver
    store, request = _request(tmp_path)
    argv = _answer_argv(
        store, request, "approve", *argv_tail, "--approver-key", str(key_path)
    )
    assert main.main(argv) == 2
    assert _json(capsys)["code"] == code
    assert store.status(request) == "waiting"


def test_a_person_without_the_approver_key_is_refused(tmp_path, capsys, approver):
    """--decided-by human on its own is a claim any process can make."""

    import openadapt_flow.__main__ as main

    store, request = _request(tmp_path)
    argv = _answer_argv(store, request, "approve", *PERSON, "--unauthenticated")
    assert main.main(argv) == 2
    assert _json(capsys)["code"] == "human_answer_unsigned"
    assert store.status(request) == "waiting"


def test_a_claimed_cloud_sign_in_is_refused_even_with_the_key(
    tmp_path, capsys, approver
):
    """Nothing here can verify a Cloud sign-in, so naming one is refused."""

    import openadapt_flow.__main__ as main

    key_path, _signer = approver
    store, request = _request(tmp_path)
    argv = _answer_argv(
        store,
        request,
        "approve",
        *PERSON,
        *SIGNED_IN,
        "--approver-key",
        str(key_path),
    )
    assert main.main(argv) == 2
    assert _json(capsys)["code"] == "auth_method_unverified"
    assert store.status(request) == "waiting"


def test_an_answer_for_a_request_the_person_didnt_see_is_refused(
    tmp_path, capsys, approver
):
    import openadapt_flow.__main__ as main

    store, request = _request(tmp_path)
    argv = _answer_argv(store, request, "approve", *PERSON, *_signed(approver))
    argv[argv.index("--request-sha256") + 1] = "sha256:" + "9" * 64
    assert main.main(argv) == 2
    assert _json(capsys)["code"] == "request_digest_mismatch"

    argv = _answer_argv(store, request, "approve", *PERSON, *_signed(approver))
    argv[argv.index("--request-id") + 1] = "0" * 32
    assert main.main(argv) == 2
    assert _json(capsys)["code"] == "request_unknown"


def test_decline_requires_a_closed_reason(tmp_path, approver):
    import openadapt_flow.__main__ as main

    store, request = _request(tmp_path)
    with pytest.raises(SystemExit):
        main.main(_answer_argv(store, request, "decline", *PERSON, *_signed(approver)))
    with pytest.raises(SystemExit):
        main.main(
            _answer_argv(
                store,
                request,
                "decline",
                *PERSON,
                *_signed(approver),
                "--reason",
                "because",
            )
        )
