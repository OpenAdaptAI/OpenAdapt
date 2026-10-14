"""The local readiness-drill campaign (ledger B276). Made-up values only.

Desktop's Check it runs its safety drills through ``qualify run-case``. Flow
used to refuse every drill ("fault qualification cases require the configured
fault-driver campaign"). These tests pin the local campaign that replaces
that refusal without weakening what a drill proves: the drills are generated
from the recorded program and the customer's own practice case, the project
names the only key whose fault receipts it trusts, and the driver changes
real inputs and signs receipts it can't turn into a verdict.
"""

from __future__ import annotations

import base64
import json
import os
import stat
from types import SimpleNamespace

import pytest

from openadapt_flow.qualification import (
    QualificationActionTarget,
    QualificationCase,
    QualificationCaseKind,
    QualificationOutcome,
    add_case,
    set_case_scope,
)
from openadapt_flow.qualification_drills import (
    DRILLS,
    LOCAL_DRILL_CONTRACT_SHA256,
    LOCAL_DRILL_DRIVER_ID,
    LocalDrillDriver,
    LocalDrillError,
    _rotate,
    local_drill_key,
    prepare_local_drill_campaign,
    project_trusts_local_key,
    write_drill_evidence,
)
from openadapt_flow.qualification_faults import (
    QualificationFaultContext,
    effect_verifier_input_sha256,
    sha256_bytes,
    verify_fault_mutation_receipt,
)
from openadapt_flow.verification import VerificationTier
from tests.test_run_gate import _KEY, _representative_qualification_bundle


def _practice(workflow):
    return next(
        case
        for case in workflow.qualification.cases
        if case.kind is QualificationCaseKind.REPRESENTATIVE
    )


@pytest.fixture
def state(tmp_path, monkeypatch):
    root = tmp_path / "state"
    monkeypatch.setenv("OPENADAPT_HOME", str(root))
    return root


# -- the key --------------------------------------------------------------------------


def test_the_drill_key_is_created_once_and_kept_private(state):
    assert local_drill_key(create=False) is None
    key = local_drill_key(create=True)
    assert key is not None
    assert key.key_id.startswith("local-drill-")
    again = local_drill_key(create=False)
    assert again == key
    path = state / "qualification-drills" / "driver-key.json"
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert "private_key_base64" in json.loads(path.read_text())
    assert "private" not in repr(key) or "private_key=" not in repr(key)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_a_readable_or_redirected_key_is_refused(state, tmp_path):
    key = local_drill_key(create=True)
    assert key is not None
    path = state / "qualification-drills" / "driver-key.json"
    path.chmod(0o644)
    with pytest.raises(LocalDrillError):
        local_drill_key(create=False)
    path.chmod(0o600)
    moved = tmp_path / "elsewhere.json"
    path.rename(moved)
    os.symlink(moved, path)
    with pytest.raises(OSError):
        local_drill_key(create=False)


def test_a_key_file_that_doesnt_match_its_key_is_refused(state):
    key = local_drill_key(create=True)
    assert key is not None
    path = state / "qualification-drills" / "driver-key.json"
    payload = json.loads(path.read_text())
    payload["key_id"] = "local-drill-" + "0" * 24
    path.write_text(json.dumps(payload))
    with pytest.raises(LocalDrillError):
        local_drill_key(create=False)


# -- the campaign ---------------------------------------------------------------------


def test_the_campaign_scopes_the_projects_five_drills_to_the_practice_case(
    tmp_path, state
):
    workflow, _bundle, _inputs = _representative_qualification_bundle(tmp_path)
    key = local_drill_key(create=True)
    assert key is not None
    practice = _practice(workflow)
    defaults = {
        case.id
        for case in workflow.qualification.cases
        if case.kind is not QualificationCaseKind.REPRESENTATIVE
    }
    assert len(defaults) == 5, "a project starts with one fault case per kind"

    campaign = prepare_local_drill_campaign(workflow, key=key)

    assert campaign.save_step_id == "s1"
    assert campaign.from_case_id == practice.id
    assert [drill.kind for drill in campaign.drills] == [
        kind for kind, _s, _n in DRILLS
    ]
    # The project's own required drills, not five more.
    assert {drill.case_id for drill in campaign.drills} == defaults
    assert {drill.state for drill in campaign.drills} == {"scoped"}
    assert project_trusts_local_key(workflow, key)
    by_id = {case.id: case for case in workflow.qualification.cases}
    for drill in campaign.drills:
        case = by_id[drill.case_id]
        assert case.kind.value == drill.kind
        assert case.expected_outcome is QualificationOutcome.HALTED
        # The customer's own practice inputs and action scope; only the
        # fault differs.
        assert case.runtime_input_sha256 == practice.runtime_input_sha256
        assert case.action_targets == practice.action_targets
        assert case.resolved_fault_target() == QualificationActionTarget(
            step_id="s1", actuation_path="gui"
        )
    summary = campaign.as_json()
    assert summary["driver_id"] == LOCAL_DRILL_DRIVER_ID
    assert summary["driver_contract_sha256"] == LOCAL_DRILL_CONTRACT_SHA256

    # Running it again changes nothing.
    revision = workflow.qualification.revision
    again = prepare_local_drill_campaign(workflow, key=key)
    assert {drill.state for drill in again.drills} == {"kept"}
    assert workflow.qualification.revision == revision


def test_a_drill_scoped_by_desktop_is_never_changed(tmp_path, state):
    workflow, _bundle, _inputs = _representative_qualification_bundle(tmp_path)
    key = local_drill_key(create=True)
    assert key is not None
    practice = _practice(workflow)
    set_case_scope(
        workflow,
        case_id="fault-wrong-identity",
        runtime_input_sha256="9" * 64,
        action_targets=practice.action_targets,
    )
    before = next(
        case
        for case in workflow.qualification.cases
        if case.id == "fault-wrong-identity"
    )
    campaign = prepare_local_drill_campaign(workflow, key=key)
    kept = next(drill for drill in campaign.drills if drill.case_id == before.id)
    assert kept.state == "kept"
    after = next(case for case in workflow.qualification.cases if case.id == before.id)
    assert after == before


def test_a_missing_drill_kind_is_added_and_a_clashing_id_is_refused(tmp_path, state):
    workflow, _bundle, _inputs = _representative_qualification_bundle(tmp_path)
    key = local_drill_key(create=True)
    assert key is not None
    workflow.qualification.cases = [
        case
        for case in workflow.qualification.cases
        if case.kind is not QualificationCaseKind.WRONG_IDENTITY
    ]
    campaign = prepare_local_drill_campaign(workflow, key=key)
    added = [drill for drill in campaign.drills if drill.state == "added"]
    assert [drill.case_id for drill in added] == ["drill-wrong-record-s1"]

    other, _bundle, _inputs = _representative_qualification_bundle(tmp_path / "other")
    other.qualification.cases = [
        case
        for case in other.qualification.cases
        if case.kind is not QualificationCaseKind.WRONG_IDENTITY
    ]
    add_case(
        other,
        QualificationCase(
            id="drill-wrong-record-s1",
            kind=QualificationCaseKind.REPRESENTATIVE,
            expected_outcome=QualificationOutcome.VERIFIED,
        ),
    )
    with pytest.raises(LocalDrillError):
        prepare_local_drill_campaign(other, key=key, from_case_id="representative-1")


def test_the_campaign_needs_exactly_one_named_practice_case(tmp_path, state):
    workflow, _bundle, _inputs = _representative_qualification_bundle(tmp_path)
    key = local_drill_key(create=True)
    assert key is not None
    practice = _practice(workflow)
    add_case(
        workflow,
        QualificationCase(
            id="representative-2",
            kind=QualificationCaseKind.REPRESENTATIVE,
            expected_outcome=QualificationOutcome.VERIFIED,
        ),
    )
    set_case_scope(
        workflow,
        case_id="representative-2",
        runtime_input_sha256=practice.runtime_input_sha256,
        action_targets=practice.action_targets,
    )
    with pytest.raises(LocalDrillError, match="name the practice case"):
        prepare_local_drill_campaign(workflow, key=key)
    campaign = prepare_local_drill_campaign(
        workflow, key=key, from_case_id="representative-2"
    )
    assert campaign.from_case_id == "representative-2"
    with pytest.raises(LocalDrillError):
        prepare_local_drill_campaign(workflow, key=key, from_case_id="nope")
    with pytest.raises(LocalDrillError, match="isn't a save"):
        prepare_local_drill_campaign(
            workflow, key=key, from_case_id="representative-2", save_step_id="s0"
        )


def test_qualify_drills_saves_the_campaign_into_the_bundle(tmp_path, state, capsys):
    import openadapt_flow.__main__ as main
    from openadapt_flow.ir import Workflow

    _workflow, bundle, _inputs = _representative_qualification_bundle(tmp_path)
    os.environ["OPENADAPT_BUNDLE_KEY"] = _KEY
    try:
        assert main.main(["qualify", "drills", str(bundle), "--json"]) == 0
        campaign = json.loads(capsys.readouterr().out)
        reloaded = Workflow.load(bundle, key=_KEY)
    finally:
        del os.environ["OPENADAPT_BUNDLE_KEY"]
    assert len(campaign["drills"]) == 5
    assert {drill["state"] for drill in campaign["drills"]} == {"scoped"}
    ids = {case.id for case in reloaded.qualification.cases}
    assert {drill["case_id"] for drill in campaign["drills"]} <= ids
    key = local_drill_key(create=False)
    assert project_trusts_local_key(reloaded, key)


# -- run-case runs drills ----------------------------------------------------------------


def _drill_run_case_argv(bundle, inputs_path, tmp_path, case_id, *extra):
    return [
        "qualify",
        "run-case",
        str(bundle),
        "--case-id",
        case_id,
        "--inputs",
        str(inputs_path),
        "--campaign-id",
        "local-drills-1",
        "--run-id",
        f"{case_id}-run-1",
        "--run-dir",
        str(tmp_path / f"{case_id}-run"),
        "--effects-kind",
        "rest",
        "--effects-base-url",
        "http://sor.local",
        "--backend",
        "web",
        *extra,
    ]


def test_run_case_refuses_a_drill_with_no_drill_driver_and_says_how_to_fix_it(
    tmp_path, state, monkeypatch, capsys
):
    import openadapt_flow.__main__ as main
    from openadapt_flow.qualification import save_qualified_workflow

    workflow, bundle, inputs = _representative_qualification_bundle(tmp_path)
    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", _KEY)
    # The project's drill scoped by hand (as Desktop does), but no local
    # campaign key on this computer.
    practice = _practice(workflow)
    set_case_scope(
        workflow,
        case_id="fault-wrong-identity",
        runtime_input_sha256=practice.runtime_input_sha256,
        action_targets=practice.action_targets,
    )
    save_qualified_workflow(workflow, bundle, key=_KEY)
    inputs_path = tmp_path / "inputs.json"
    inputs_path.write_bytes(inputs)
    inputs_path.chmod(0o600)
    called = []
    monkeypatch.setattr(main, "_cmd_replay", lambda args: called.append(args) or 0)

    argv = _drill_run_case_argv(bundle, inputs_path, tmp_path, "fault-wrong-identity")
    assert main.main(argv) == 2
    out = capsys.readouterr().out
    assert "openadapt-flow qualify drills" in out
    assert "configured fault-driver campaign" not in out
    assert not called


def test_run_case_runs_a_drill_with_the_local_campaign(tmp_path, state, monkeypatch):
    import openadapt_flow.__main__ as main

    _workflow, bundle, inputs = _representative_qualification_bundle(tmp_path)
    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", _KEY)
    assert main.main(["qualify", "drills", str(bundle)]) == 0
    key = local_drill_key(create=False)
    inputs_path = tmp_path / "inputs.json"
    inputs_path.write_bytes(inputs)
    inputs_path.chmod(0o600)
    captured = {}

    def capture(args):
        captured["authorization"] = args._governed_run_authorization
        captured["driver"] = args._qualification_fault_driver
        return 0

    monkeypatch.setattr(main, "_cmd_replay", capture)
    argv = _drill_run_case_argv(bundle, inputs_path, tmp_path, "fault-weak-effect")
    assert main.main(argv) == 0
    authorization = captured["authorization"]
    assert authorization.qualification_case_kind == "weak_effect"
    assert authorization.qualification_fault_driver_id == LOCAL_DRILL_DRIVER_ID
    assert (
        authorization.qualification_fault_driver_contract_sha256
        == LOCAL_DRILL_CONTRACT_SHA256
    )
    assert authorization.qualification_fault_driver_key_id == key.key_id
    assert authorization.qualification_fault_step_id_sha256 == sha256_bytes(b"s1")
    # Drills run under the same default approval mode as the practice item.
    assert authorization.approval_mode == "each_item"
    assert isinstance(captured["driver"], LocalDrillDriver)
    status = json.loads(
        (
            tmp_path / "fault-weak-effect-run" / "qualification-fault" / "status.json"
        ).read_text()
    )
    assert status["staged"] is False


def test_a_drill_in_another_computers_campaign_is_refused(tmp_path, state, monkeypatch):
    """A key the project doesn't trust can't run its drills."""
    import openadapt_flow.__main__ as main

    _workflow, bundle, inputs = _representative_qualification_bundle(tmp_path)
    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", _KEY)
    assert main.main(["qualify", "drills", str(bundle)]) == 0
    # Another computer: a different OPENADAPT_HOME with its own key.
    monkeypatch.setenv("OPENADAPT_HOME", str(tmp_path / "other-computer"))
    local_drill_key(create=True)
    inputs_path = tmp_path / "inputs.json"
    inputs_path.write_bytes(inputs)
    inputs_path.chmod(0o600)
    monkeypatch.setattr(main, "_cmd_replay", lambda args: 0)
    argv = _drill_run_case_argv(bundle, inputs_path, tmp_path, "fault-weak-effect")
    assert main.main(argv) == 2


# -- the driver ----------------------------------------------------------------------------


def _context(kind: str, gate: str, *, backend=None, verifier=None, path="gui"):
    return QualificationFaultContext(
        project_id="project-1",
        project_revision=3,
        project_contract_sha256="c" * 64,
        campaign_id_sha256="d" * 64,
        case_id_sha256="e" * 64,
        case_input_sha256="f" * 64,
        run_id_sha256="1" * 64,
        step_id="s1",
        actuation_path=path,
        fault_kind=kind,
        gate=gate,
        before_input_sha256=effect_verifier_input_sha256(verifier, ()),
        backend=backend,
        vision=None,
        effect_verifier=verifier,
        effects=(),
    )


class _Verifier:
    verification_tier = VerificationTier.INDEPENDENT_SYSTEM


def _driver(tmp_path, params=None):
    from openadapt_flow.ir import Workflow

    key = local_drill_key(create=True)
    workflow, _bundle, _inputs = _representative_qualification_bundle(tmp_path)
    return key, LocalDrillDriver(
        key, workflow=Workflow.model_validate(workflow.model_dump()), params=params
    )


@pytest.mark.parametrize(
    ("kind", "gate"),
    [("missing_effect", "effect_verifier"), ("weak_effect", "effect_strength")],
)
def test_record_check_drills_swap_the_verifier_and_sign_a_receipt(
    tmp_path, state, kind, gate
):
    key, driver = _driver(tmp_path)
    context = _context(kind, gate, verifier=_Verifier())
    mutation = driver.mutate(context)
    assert mutation is not None and mutation.replace_effect_verifier
    if kind == "missing_effect":
        assert mutation.effect_verifier is None
    else:
        assert (
            mutation.effect_verifier.verification_tier
            is VerificationTier.IMMEDIATE_SCREEN
        )
    receipt = mutation.receipt
    assert receipt.before_input_sha256 != receipt.after_input_sha256
    assert receipt.after_input_sha256 == effect_verifier_input_sha256(
        mutation.effect_verifier, ()
    )
    assert verify_fault_mutation_receipt(
        receipt, trusted_public_key_base64=key.public_key_base64
    )
    artifact = driver.mutation_bytes(receipt)
    assert artifact is not None
    assert sha256_bytes(artifact) == receipt.mutation_artifact_sha256
    assert json.loads(artifact)["fault_kind"] == kind


def test_a_receipt_signed_by_another_key_does_not_verify(tmp_path, state, monkeypatch):
    _key, driver = _driver(tmp_path)
    mutation = driver.mutate(
        _context("missing_effect", "effect_verifier", verifier=_Verifier())
    )
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    other = base64.b64encode(
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode("ascii")
    assert not verify_fault_mutation_receipt(
        mutation.receipt, trusted_public_key_base64=other
    )


@pytest.mark.parametrize("kind", ["ambiguity", "wrong_identity", "stale_identity"])
def test_screen_drills_decline_without_a_browser(tmp_path, state, kind):
    _key, driver = _driver(tmp_path, params={"patient": "Testperson Ana"})
    backend = SimpleNamespace(screenshot=lambda: b"frame")
    gate = {
        "ambiguity": "target_resolution",
        "wrong_identity": "identity_verification",
        "stale_identity": "actuation_revalidation",
    }[kind]
    assert driver.mutate(_context(kind, gate, backend=backend)) is None
    assert driver.declined == "screen_drill_needs_a_browser"


class _Frame:
    def __init__(self, changed: int) -> None:
        self.changed = changed
        self.calls: list = []

    def evaluate(self, script, pairs=None):
        if pairs is None:  # the driver waiting for the page to paint
            return True
        self.calls.append(pairs)
        return self.changed


def test_the_wrong_record_drill_changes_the_cases_own_values_on_the_page(
    tmp_path, state
):
    key, driver = _driver(
        tmp_path, params={"last_name": "Testperson", "mrn": "M-4821", "flag": "x"}
    )
    frame = _Frame(changed=2)
    backend = SimpleNamespace(
        page=SimpleNamespace(frames=[frame]), screenshot=lambda: b"after"
    )
    mutation = driver.mutate(
        _context("wrong_identity", "identity_verification", backend=backend)
    )
    assert mutation is not None
    (pairs,) = frame.calls
    sources = {source for source, _target in pairs}
    assert "Testperson" in sources and "M-4821" in sources
    assert "x" not in sources  # too short to identify anything
    for source, target in pairs:
        assert target != source and len(target) == len(source)
    artifact = driver.mutation_bytes(mutation.receipt)
    assert b"Testperson" not in artifact and b"M-4821" not in artifact
    assert mutation.receipt.after_input_sha256 == sha256_bytes(b"after")

    nothing = _Frame(changed=0)
    backend = SimpleNamespace(
        page=SimpleNamespace(frames=[nothing]), screenshot=lambda: b""
    )
    assert (
        driver.mutate(
            _context("wrong_identity", "identity_verification", backend=backend)
        )
        is None
    )
    assert driver.declined == "record_identity_not_on_page"


def test_a_screen_drill_signs_only_a_frame_that_stayed_the_same(monkeypatch):
    """A transient frame, even twice in a row, is never the signed frame.

    Right after a page change a browser can still re-rasterize an edge. The
    receipt must name the bytes the detector then reads, so the driver waits
    for three identical captures (seen in MockMed: two equal transient frames,
    then the settled one).
    """

    import openadapt_flow.qualification_drills as drills

    monkeypatch.setattr("time.sleep", lambda _s: None)
    frames = iter([b"t1", b"t2", b"t2", b"settled", b"settled", b"settled", b"x"])
    assert drills._settled_screenshot(lambda: next(frames)) == b"settled"
    flicker = iter([b"a", b"b"] * 30)
    assert drills._settled_screenshot(lambda: next(flicker), attempts=20) is None


def test_rotation_keeps_the_shape_and_changes_every_letter_and_digit():
    assert _rotate("Rivera, Ana 1984-03-02") == "Eviren, Nan 6439-58-57"
    assert _rotate("---") == "---"


def test_drill_evidence_is_written_next_to_the_report(tmp_path, state):
    _key, driver = _driver(tmp_path)
    mutation = driver.mutate(
        _context("missing_effect", "effect_verifier", verifier=_Verifier())
    )
    report = SimpleNamespace(qualification_fault_mutations=[mutation.receipt])
    summary = write_drill_evidence(tmp_path / "run", driver, report)
    folder = tmp_path / "run" / "qualification-fault"
    assert summary["staged"] is True
    assert (folder / "receipt.json").read_bytes() == mutation.receipt.artifact_bytes()
    assert sha256_bytes((folder / "mutation.json").read_bytes()) == (
        mutation.receipt.mutation_artifact_sha256
    )
    declined = write_drill_evidence(
        tmp_path / "other", driver, SimpleNamespace(qualification_fault_mutations=[])
    )
    assert declined["staged"] is False
