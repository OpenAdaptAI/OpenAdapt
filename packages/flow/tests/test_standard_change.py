"""Standard changes: self-repairs a person approved in advance, on MockMed.

A person signs a repair policy once. After that:

* a relabelled link (MockMed ``?drift=relink``) is repaired without anyone,
  after a verified run on the test record, and every item then verifies;
* a repair that touches the save, its identity evidence or its record check,
  or a class the policy does not allow, goes to a person;
* the pre-flight on the test record catches drift before item 1;
* one approval on the phone releases every item halted at the same step;
* a revert puts the prior bundle back and holds the step for a person.

MockMed's tutorial bundle writes one fixed synthetic record and checks it is
written exactly once, so each session here starts from an empty store: every
item's record check then reads its own save back. The test record is the same
synthetic patient with the note ``TEST-001``. The agent is scripted, so CI
makes no paid calls.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional

import pytest

from openadapt_flow.ir import RunReport, Workflow
from openadapt_flow.jit.authority import (
    AdmissionPolicy,
    DialogPattern,
    StandardRepairs,
    generate_encrypted_key,
    load_encrypted_key,
    register_principal,
    sign_policy,
)
from openadapt_flow.repair.lifecycle import RepairLifecycleError, RepairStore
from openadapt_flow.repair.preflight_queue import QueueItem, WorklistQueue
from openadapt_flow.repair.self_repair import (
    AppSession,
    ReplayTarget,
    ScreenAccess,
    SelfRepair,
)
from openadapt_flow.repair.standard_change import (
    Ledger,
    StandardChanges,
    TestRecord,
    classify_change,
    daily_digest,
    revert,
)

pytest.importorskip("playwright.sync_api")

from tests.test_self_repair import (  # noqa: E402 - after the playwright skip
    AGENT_ID,
    QUERY,
    ScriptedAgent,
    _browser,
    _center_of,
    _phone,
    _reset,
)

ENV = {"OPENADAPT_JIT_KEY_PASSPHRASE": "synthetic-test-passphrase"}
SIGNER = "dana-ops"


@pytest.fixture(scope="module")
def mockmed(tmp_path_factory) -> Iterator[tuple[str, Path]]:
    """MockMed's persistence boundary and the compiled triage bundle."""

    from openadapt_flow.backends._browser_process import shared_browser
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.mockmed.fault_server import serve
    from openadapt_flow.tutorial import TutorialError, certify_tutorial, record_tutorial

    root = tmp_path_factory.mktemp("standard-change")
    base_url, _db, stop = serve()
    try:
        with shared_browser(headless=True):
            for attempt in range(3):
                take = root / f"take-{attempt}"
                record_tutorial(base_url, take / "recording")
                workflow = compile_recording(
                    take / "recording",
                    take / "bundle",
                    name="triage",
                    mine_effects=True,
                )
                try:
                    certify_tutorial(workflow)
                    break
                except TutorialError:
                    if attempt == 2:
                        raise
            yield base_url, take / "bundle"
    finally:
        stop()


# -- policy ----------------------------------------------------------------------


@pytest.fixture
def keys(tmp_path) -> tuple[Path, Path]:
    key_dir = tmp_path / "keys"
    key = key_dir / "dana.pem"
    pub = generate_encrypted_key(key, env=ENV)
    register_principal(
        key_dir, role="policy_signer", principal_id=SIGNER, public_key_hex=pub
    )
    return key_dir, key


def _policy(key: Path, *, expires_in: timedelta = timedelta(days=30), **repairs):
    now = datetime.now(timezone.utc)
    repairs.setdefault("repair_agents", (AGENT_ID,))
    policy = AdmissionPolicy(
        policy_id="ui-drift-standard",
        signer=SIGNER,
        signed_at=(now - timedelta(hours=1)).isoformat(),
        expires_at=(now + expires_in).isoformat(),
        allowed_hosts=("127.0.0.1",),
        allowed_actions=("click",),
        agent_principals=("agent:jit-admitter",),
        standard_repairs=StandardRepairs(**repairs),
    )
    return sign_policy(policy, load_encrypted_key(key, env=ENV))


# -- targets ---------------------------------------------------------------------


def _target(
    base_url: str, drift: Callable[[], Optional[str]], note: str, *, on_open=None
) -> ReplayTarget:
    """Sessions on MockMed, each starting from an empty system of record."""

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.runtime.effects import RestRecordVerifier
    from openadapt_flow.tutorial import TUTORIAL_NOTE

    def url(fault: Optional[str]) -> str:
        drifts = ",".join(d for d in (drift(), fault) if d)
        return f"{base_url}/{QUERY}" + (f"&drift={drifts}" if drifts else "") + "#tasks"

    def open_session(fault: Optional[str]) -> AppSession:
        if on_open is not None:
            on_open()
        _reset(base_url)
        backend, close = PlaywrightBackend.launch(url(fault), headless=True)
        return AppSession(backend=backend, close=close)

    return ReplayTarget(
        open_session,
        effect_verifier=RestRecordVerifier(
            base_url,
            records_path="/api/db",
            records_key="records",
            timeout_s=2.0,
            poll_interval_s=0.05,
        ),
        policy="clinical-write",
        # The recorded record check pins the demonstrated note, so every
        # session types it; the test record and the items differ by session.
        params={"note": TUTORIAL_NOTE},
        entry_url=url(None),
        execution_origin=base_url,
    )


def _repairs(
    tmp_path: Path,
    agent: ScriptedAgent,
    standard: Optional[StandardChanges],
    **kwargs,
) -> Callable:
    kwargs.setdefault("fault_probes", ("missing",))
    kwargs.setdefault("approval_wait_s", 0)

    def factory(target) -> SelfRepair:
        return SelfRepair(
            agent,
            target,
            store=tmp_path / "repair-store",
            screen_access=ScreenAccess(enabled_by="ops-lead", mode="allow_unscrubbed"),
            standard_changes=standard,
            **kwargs,
        )

    return factory


def _center_on_patient(base_url: str, selector: str) -> tuple[int, int]:
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    backend, close = PlaywrightBackend.launch(
        f"{base_url}/{QUERY}&drift=relink#patient/p1", headless=True
    )
    try:
        locator = backend.page.locator(selector)
        locator.wait_for(state="visible")
        box = locator.bounding_box()
        assert box is not None
        return int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)
    finally:
        close()


# -- the live tests ----------------------------------------------------------------


def test_relabelled_link_is_repaired_without_a_person(mockmed, keys, tmp_path) -> None:
    base_url, bundle = mockmed
    key_dir, key = keys
    relink = lambda: "relink"  # noqa: E731
    with _browser():
        agent = ScriptedAgent(_center_on_patient(base_url, "#start-visit"))
        test_record = TestRecord("TEST-001", _target(base_url, relink, "TEST-001"))
        standard = StandardChanges(
            _policy(key, relabel_or_move=True), key_dir, test_record
        )
        queue = WorklistQueue(
            bundle,
            test_record,
            tmp_path / "queue",
            self_repair=_repairs(tmp_path, agent, standard),
        )
        items = [
            QueueItem(f"item-{i}", _target(base_url, relink, f"item-{i}"))
            for i in range(1, 4)
        ]
        result = queue.run(items)

    assert result.status == "done", result.reason
    # The pre-flight caught the drift on the test record, before any item.
    assert not result.preflight.passed
    assert result.preflight.halted_step_id == "step_001"
    # One repair, on the test record, applied without a person.
    (repair,) = result.repairs
    assert repair.status == "resumed", repair.evidence.reason
    assert repair.verified
    evidence = repair.evidence
    change = evidence.standard_change
    assert change is not None
    assert change.change_class == "relabel_or_move"
    assert change.policy_signed_by == SIGNER
    assert change.test_record == "TEST-001"
    assert change.preflight_outcome == "VERIFIED"
    assert "repair revert" in change.revert_command
    assert evidence.approval_route == "standard_change"
    assert evidence.approved_by.startswith("standard-change:")
    assert not (
        repair.halted_report
        and (tmp_path / "queue" / "repair-1" / "item-approval").exists()
    )
    # The store holds the policy approval, bound to the policy digest.
    store = RepairStore(tmp_path / "repair-store")
    candidate = store.load_candidate(evidence.candidate_id)
    assert candidate.approval.pre_approved_by == SIGNER
    assert candidate.approval.pre_approval_policy_sha256 == change.policy_sha256
    assert candidate.approval.non_interactive
    assert all(t.actor_kind == "automation" for t in candidate.history)
    # Every item then ran with the repaired bundle and verified.
    assert [i.status for i in result.items] == ["verified"] * 3
    assert all(i.candidate_id == candidate.candidate_id for i in result.items)
    assert all(i.report.model_calls == 0 for i in result.items)
    assert (
        store.load_candidate(candidate.candidate_id).canary_metrics.verified_runs >= 3
    )
    # Audit: the run report, REPORT.md, the ledger, the digest, the receipt.
    on_disk = RunReport.model_validate_json(
        (repair.run_dir / "report.json").read_text()
    )
    assert on_disk.self_repair.standard_change == change
    report_md = (repair.run_dir / "REPORT.md").read_text()
    assert "Applied without a person" in report_md
    (row,) = Ledger(store.root).applied()
    assert row["change_class"] == "relabel_or_move" and row["step_id"] == "step_001"
    digest = daily_digest(store.root)
    assert "1 repair(s) applied without a person" in digest
    assert "step_001" in digest
    from openadapt_flow.repair.standard_change import receipt_fields

    fields = receipt_fields(on_disk)
    assert fields == {
        "change_class": "relabel_or_move",
        "policy_sha256": change.policy_sha256,
        "candidate_id": candidate.candidate_id,
    }

    # Revert: the prior bundle is back, and the step waits for a person.
    reverted = revert(store, candidate.candidate_id, by="ops-lead")
    assert reverted.state == "rolled_back"
    assert store.active_pointer().active_digest == candidate.prior_content_digest
    assert Ledger(store.root).held("triage", "step_001")["reverted_by"] == "ops-lead"
    assert "reverted by ops-lead" in daily_digest(store.root)
    with pytest.raises(RepairLifecycleError, match="already reverted"):
        revert(store, candidate.candidate_id, by="ops-lead")


def test_preflight_catches_drift_before_item_one(mockmed, tmp_path) -> None:
    base_url, bundle = mockmed
    opened: list[str] = []
    redesign = lambda: "redesign"  # noqa: E731
    with _browser():
        test_record = TestRecord("TEST-001", _target(base_url, redesign, "TEST-001"))
        items = [
            QueueItem(
                f"item-{i}",
                _target(
                    base_url,
                    redesign,
                    f"item-{i}",
                    on_open=lambda: opened.append("item"),
                ),
            )
            for i in range(1, 3)
        ]
        result = WorklistQueue(bundle, test_record, tmp_path / "queue").run(items)
    assert result.status == "preflight_failed"
    assert not result.preflight.passed
    assert result.preflight.halted_step_id == "step_005"
    assert result.preflight.report.transaction_outcome == "HALTED_BEFORE_EFFECT"
    assert "no item started" in result.reason
    assert [i.status for i in result.items] == ["not_started", "not_started"]
    assert opened == []
    assert not (tmp_path / "queue" / "items").exists()


def test_one_approval_releases_five_halted_items(mockmed, keys, tmp_path) -> None:
    """The save itself changed, so the policy refuses; one person decides."""

    base_url, bundle = mockmed
    key_dir, key = keys
    vendor = {"drift": None}

    def deploy() -> None:  # the vendor ships between the pre-flight and item 1
        vendor["drift"] = "redesign"

    with _browser():
        submit = _center_of(base_url, "#submit-encounter")
        agent = ScriptedAgent(submit)
        test_record = TestRecord(
            "TEST-001", _target(base_url, lambda: vendor["drift"], "TEST-001")
        )
        standard = StandardChanges(
            _policy(key, relabel_or_move=True, menu_steps=2), key_dir, test_record
        )
        root = tmp_path / "queue"
        asked: list[int] = []
        queue = WorklistQueue(
            bundle,
            test_record,
            root,
            self_repair=_repairs(
                tmp_path,
                agent,
                standard,
                approval_wait_s=60,
                sleep=_phone(root, approve=True, seen=asked, base_url=base_url),
            ),
        )
        items = [
            QueueItem(
                f"item-{i}",
                _target(base_url, lambda: vendor["drift"], f"item-{i}", on_open=deploy),
            )
            for i in range(1, 6)
        ]
        result = queue.run(items)

    assert result.preflight.passed
    assert result.status == "done", result.reason
    (repair,) = result.repairs
    # Not a standard change: the save step changed. A person was asked once.
    refusal = next(
        s for s in repair.evidence.steps if s.stage == "approval" and not s.passed
    )
    assert "not a standard change" in refusal.detail
    assert "the save step" in refusal.detail
    assert repair.evidence.standard_change is None
    assert repair.evidence.approved_by == "decision_portal:person"
    assert len(asked) == 1
    requests = list((root / "repair-1" / "item-approval").glob("*.json"))
    intents = [
        json.loads(p.read_text()).get("step_intent", "")
        for p in requests
        if "step_intent" in p.read_text()
    ]
    assert any("releases 5 queued item(s) halted at step_005" in t for t in intents)
    assert any("TEST-001" in t for t in intents)
    # Every halted item ran twice: halted before any effect, then verified.
    for item in result.items:
        assert item.status == "verified", item.reason
        assert item.runs == 2
    first_runs = [
        RunReport.model_validate_json(
            (root / "items" / f"item-{i}" / "run-1" / "report.json").read_text()
        )
        for i in range(1, 6)
    ]
    assert {r.halt.state_id for r in first_runs} == {"step_005"}
    assert {r.transaction_outcome for r in first_runs} == {"HALTED_BEFORE_EFFECT"}
    # The stop ledger: five stopped items, one repair on the test record, one
    # person's decision, five items resumed and verified.
    from openadapt_flow.repair.auto_repair import RepairLedger, summarize

    summary = summarize(RepairLedger(tmp_path / "repair-store").rows())
    assert summary["stops"] == 5
    assert summary["to_repair"] == 5
    assert summary["repair_runs"] == 1
    assert summary["approved_by_person"] == 1
    assert summary["person_decisions"] == 1
    assert summary["resumed_items"] == 5
    assert summary["repair_success_rate"] == 1.0


# -- refusals at the policy layer, on the recorded bundle ---------------------------


def _copy(bundle: Path, out: Path, edit: Callable[[Workflow], None]) -> Path:
    shutil.copytree(bundle, out, ignore=shutil.ignore_patterns("manifest.json"))
    workflow = Workflow.load(out)
    edit(workflow)
    workflow.save(out)
    return out


def _step(workflow: Workflow, step_id: str):
    return next(s for s in workflow.steps if s.id == step_id)


def _relabel(workflow: Workflow) -> None:
    anchor = _step(workflow, "step_001").anchor
    anchor.ocr_text = "Start visit"
    anchor.region = (anchor.region[0] + 300, *anchor.region[1:])
    if anchor.structural is not None:
        anchor.structural = anchor.structural.model_copy(
            update={"selector": "#start-visit", "name": "Start visit"}
        )


ALL_CLASSES = StandardRepairs(
    repair_agents=(AGENT_ID,),
    relabel_or_move=True,
    menu_steps=1,
    dialogs=(DialogPattern(name="survey", text=r"quick survey", dismiss=("Not now",)),),
)


def test_a_relabel_is_a_standard_change(mockmed, tmp_path) -> None:
    _base, bundle = mockmed
    patched = _copy(bundle, tmp_path / "relabel", _relabel)
    verdict = classify_change(
        Workflow.load(bundle), Workflow.load(patched), bundle, patched, ALL_CLASSES
    )
    assert verdict.allowed, verdict.problems
    assert verdict.change_class == "relabel_or_move"
    assert "Start visit" in verdict.detail


@pytest.mark.parametrize(
    ("edit", "problem"),
    [
        (
            lambda w: setattr(
                _step(w, "step_005").anchor, "ocr_text", "Submit to chart"
            ),
            "step_005: the save step changed",
        ),
        (
            lambda w: setattr(
                _step(w, "step_005").anchor,
                "identity_template",
                _step(w, "step_005").anchor.identity_template.model_copy(
                    update={"salt": "00" * 16}
                ),
            ),
            "step_005: the save step's identity evidence changed",
        ),
        (
            lambda w: (
                _step(w, "step_005")
                .effects[1]
                .value.__setattr__("literal", "a different note")
            ),
            "step_005: the save step's record check changed",
        ),
    ],
    ids=["save-button", "identity-evidence", "record-check"],
)
def test_changes_to_the_save_go_to_a_person(mockmed, tmp_path, edit, problem) -> None:
    _base, bundle = mockmed

    def both(workflow: Workflow) -> None:
        _relabel(workflow)
        edit(workflow)

    patched = _copy(bundle, tmp_path / "patched", both)
    verdict = classify_change(
        Workflow.load(bundle), Workflow.load(patched), bundle, patched, ALL_CLASSES
    )
    assert not verdict.allowed
    assert any(p.startswith(problem) for p in verdict.problems), verdict.problems


def test_a_changed_save_template_goes_to_a_person(mockmed, tmp_path) -> None:
    _base, bundle = mockmed
    out = tmp_path / "patched"
    template = _step(Workflow.load(bundle), "step_005").anchor.template

    def edit(workflow: Workflow) -> None:
        _relabel(workflow)
        (out / template).write_bytes((out / template).read_bytes() + b"\0")

    patched = _copy(bundle, out, edit)
    verdict = classify_change(
        Workflow.load(bundle), Workflow.load(patched), bundle, patched, ALL_CLASSES
    )
    assert f"step_005: the save step's file {template} changed" in verdict.problems


def test_a_class_the_policy_does_not_allow_goes_to_a_person(mockmed, tmp_path) -> None:
    _base, bundle = mockmed
    patched = _copy(bundle, tmp_path / "patched", _relabel)
    dialogs_only = StandardRepairs(
        repair_agents=(AGENT_ID,), dialogs=ALL_CLASSES.dialogs
    )
    verdict = classify_change(
        Workflow.load(bundle), Workflow.load(patched), bundle, patched, dialogs_only
    )
    assert not verdict.allowed
    assert verdict.problems == ("the policy does not allow relabel_or_move",)


def test_an_unsigned_expired_or_foreign_policy_refuses(mockmed, keys, tmp_path) -> None:
    from openadapt_flow.repair.candidate import RepairProvenance
    from openadapt_flow.repair.registration import build_candidate
    from openadapt_flow.repair.standard_change import StandardChangeRefused, authorize

    _base, bundle = mockmed
    key_dir, key = keys
    patched = _copy(bundle, tmp_path / "patched", _relabel)
    (tmp_path / "evidence").mkdir()
    candidate = build_candidate(
        bundle,
        patched,
        source="model_suggestion",
        evidence_run_dir=tmp_path / "evidence",
    )
    candidate.provenance = RepairProvenance(
        repaired_by="self_repair",
        agent_id=AGENT_ID,
        halted_run_id="run",
        halted_step_id="step_001",
        repair_token_sha256="0" * 64,
        screen_access_enabled_by="ops-lead",
        screen_share_mode="allow_unscrubbed",
        screens_shared=1,
        screens_scrubbed=False,
    )
    good = _policy(key, relabel_or_move=True)
    assert authorize(candidate, policy=good, key_dir=key_dir).change_class == (
        "relabel_or_move"
    )
    tampered = good.model_copy(
        update={
            "policy": good.policy.model_copy(
                update={
                    "standard_repairs": good.policy.standard_repairs.model_copy(
                        update={"menu_steps": 3}
                    )
                }
            )
        }
    )
    with pytest.raises(StandardChangeRefused, match="does not verify"):
        authorize(candidate, policy=tampered, key_dir=key_dir)
    expired = _policy(key, relabel_or_move=True, expires_in=timedelta(minutes=-30))
    with pytest.raises(StandardChangeRefused, match="expired"):
        authorize(candidate, policy=expired, key_dir=key_dir)
    other_agent = _policy(key, relabel_or_move=True, repair_agents=("agent:other",))
    with pytest.raises(StandardChangeRefused, match="does not name repair agent"):
        authorize(candidate, policy=other_agent, key_dir=key_dir)
    other_workflow = _policy(key, relabel_or_move=True, workflows=("billing",))
    with pytest.raises(StandardChangeRefused, match="does not cover workflow"):
        authorize(candidate, policy=other_workflow, key_dir=key_dir)


# -- every backend: the same rules on pixel-only (RDP) and UIA (WinForms) bundles ---


def _rdp(workflow: Workflow) -> None:
    """The recorded bundle as an RDP kiosk sees it: pixels only, no DOM."""

    for step in workflow.steps:
        if step.anchor is not None:
            step.anchor.structural = None


def _winforms(workflow: Workflow) -> None:
    """The recorded bundle as a WinForms app exposes it: UIA ids, no CSS."""

    from openadapt_flow.ir import StructuralLocator

    for step in workflow.steps:
        if step.anchor is not None and step.anchor.structural is not None:
            name = step.anchor.structural.name or step.id
            step.anchor.structural = StructuralLocator(
                role="button",
                name=name,
                automation_id="btn" + "".join(w.title() for w in name.split()),
                window_name="MockMed Desktop",
            )


def _menu(workflow: Workflow, clicks: int) -> None:
    """``clicks`` new menu clicks now lead to the moved New Encounter control."""

    target = _step(workflow, "step_001")
    at = workflow.steps.index(target)
    _relabel(workflow)
    for i in range(clicks, 0, -1):
        new = target.model_copy(deep=True)
        new.id = f"step_001_bridge_{i}"
        new.intent = f"click 'Actions {i}'"
        new.anchor.ocr_text = f"Actions {i}"
        if new.anchor.structural is not None:
            new.anchor.structural = new.anchor.structural.model_copy(
                update={"name": f"Actions {i}"}
            )
        workflow.steps.insert(at, new)


def _dialog(workflow: Workflow, text: str, label: str) -> None:
    """After New Encounter, a new dialog that ``label`` dismisses."""

    from openadapt_flow.ir import Postcondition, PostconditionKind

    halted = _step(workflow, "step_001")
    at = workflow.steps.index(halted)
    dismiss = halted.model_copy(deep=True)
    dismiss.id = "step_001_bridge_1"
    dismiss.intent = f"click {label!r}"
    dismiss.anchor.ocr_text = label
    if dismiss.anchor.structural is not None:
        dismiss.anchor.structural = dismiss.anchor.structural.model_copy(
            update={"name": label}
        )
    dismiss.expect = list(halted.expect)
    halted.expect = [Postcondition(kind=PostconditionKind.TEXT_PRESENT, text=text)]
    workflow.steps.insert(at + 1, dismiss)


@pytest.mark.parametrize("surface", [_rdp, _winforms], ids=["rdp-kiosk", "winforms"])
def test_the_rules_are_the_same_on_non_web_bundles(mockmed, tmp_path, surface) -> None:
    _base, recorded = mockmed
    bundle = _copy(recorded, tmp_path / "admitted", surface)
    prior = Workflow.load(bundle)

    def verdict(name: str, edit: Callable[[Workflow], None], repairs=ALL_CLASSES):
        def both(workflow: Workflow) -> None:
            edit(workflow)

        patched = _copy(bundle, tmp_path / name, both)
        return classify_change(prior, Workflow.load(patched), bundle, patched, repairs)

    assert verdict("relabel", _relabel).change_class == "relabel_or_move"
    menu = verdict("menu", lambda w: _menu(w, 1))
    assert menu.change_class == "menu_path", menu.problems
    deep = verdict("deep-menu", lambda w: _menu(w, 2))
    assert not deep.allowed
    assert "the menu path has 2 new click(s); the policy allows 1" in deep.problems
    dialog = verdict(
        "dialog", lambda w: _dialog(w, "Take a quick survey about MockMed", "Not now")
    )
    assert dialog.change_class == "dismiss_dialog", dialog.problems
    wrong_button = verdict(
        "dialog-ok", lambda w: _dialog(w, "Take a quick survey about MockMed", "Accept")
    )
    assert not wrong_button.allowed
    other_dialog = verdict(
        "dialog-other", lambda w: _dialog(w, "Your session will expire", "Not now")
    )
    assert not other_dialog.allowed
    save = verdict(
        "save",
        lambda w: setattr(_step(w, "step_005").anchor, "ocr_text", "Submit to chart"),
    )
    assert any(p.startswith("step_005: the save step changed") for p in save.problems)


# -- revert from the phone portal -----------------------------------------------------


def test_revert_from_the_phone_portal(mockmed, keys, tmp_path) -> None:
    from fastapi.testclient import TestClient

    from openadapt_flow.console.app import create_app
    from openadapt_flow.repair.candidate import (
        CampaignCaseResult,
        CampaignResult,
        RepairProvenance,
    )
    from openadapt_flow.repair.registration import build_candidate
    from openadapt_flow.repair.self_repair import AUTOMATION

    _base, bundle = mockmed
    key_dir, key = keys
    patched = _copy(bundle, tmp_path / "patched", _relabel)
    (tmp_path / "evidence").mkdir()
    store = RepairStore(tmp_path / "repair-store")
    candidate = build_candidate(
        bundle,
        patched,
        source="model_suggestion",
        evidence_run_dir=tmp_path / "evidence",
    )
    candidate.provenance = RepairProvenance(
        repaired_by="self_repair",
        agent_id=AGENT_ID,
        halted_run_id="run",
        halted_step_id="step_001",
        repair_token_sha256="0" * 64,
        screen_access_enabled_by="ops-lead",
        screen_share_mode="allow_unscrubbed",
        screens_shared=1,
        screens_scrubbed=False,
    )
    candidate = store.add_candidate(candidate)
    for kind in ("replay", "fault"):
        store.record_campaign(
            candidate.candidate_id,
            CampaignResult(
                campaign=kind,
                passed=True,
                total=1,
                failed=0,
                cases=[
                    CampaignCaseResult(label="case", kind="live_probe", passed=True)
                ],
            ),
            AUTOMATION,
        )
    policy = _policy(key, relabel_or_move=True)

    def preflight(digest: str, outcome: str = "VERIFIED") -> Path:
        run = tmp_path / f"preflight-{digest[:8]}-{outcome}"
        run.mkdir()
        RunReport(
            workflow_name="triage",
            started_at="2026-10-18T00:00:00+00:00",
            success=outcome == "VERIFIED",
            execution_outcome=outcome,
            transaction_outcome=(
                outcome if outcome == "VERIFIED" else "HALTED_BEFORE_EFFECT"
            ),
            bundle_content_digest=digest,
        ).save(run)
        return run

    # The store checks the test-record run itself: it must be a VERIFIED run
    # of exactly the proposed bundle.
    for bad in (
        preflight(candidate.prior_content_digest),
        preflight(candidate.proposed_content_digest, "HALTED"),
        tmp_path / "no-run",
    ):
        with pytest.raises(RepairLifecycleError, match="test-record run"):
            store.approve_standard_change(
                candidate.candidate_id,
                policy=policy,
                key_dir=key_dir,
                preflight_run_dir=bad,
            )
    approved, grant = store.approve_standard_change(
        candidate.candidate_id,
        policy=policy,
        key_dir=key_dir,
        preflight_run_dir=preflight(candidate.proposed_content_digest),
    )
    assert approved.approval.pre_approved_by == SIGNER
    store.stage(approved.candidate_id, AUTOMATION)
    store.start_canary(approved.candidate_id, AUTOMATION)
    Ledger(store.root).append(
        {
            "event": "applied",
            "candidate_id": approved.candidate_id,
            "workflow": "triage",
            "step_id": grant.step_id,
            "change_class": grant.change_class,
            "policy_id": grant.policy_id,
            "signed_by": grant.signed_by,
            "test_record": "TEST-001",
            "preflight_outcome": "VERIFIED",
        }
    )
    # A person approving the same candidate is refused: it is already approved.
    with pytest.raises(RepairLifecycleError):
        store.approve_standard_change(
            candidate.candidate_id,
            policy=policy,
            key_dir=key_dir,
            preflight_run_dir=tmp_path / "no-run",
        )

    runs = tmp_path / "runs"
    (runs / "bundles").mkdir(parents=True)

    def client(attend: bool) -> TestClient:
        app = create_app(runs / "bundles", runs, attend=attend, repair_store=store.root)
        return TestClient(
            app,
            base_url="http://127.0.0.1",
            headers={
                "Authorization": f"Bearer {app.state.console_access_token}",
                "Origin": "http://127.0.0.1",
                "X-OpenAdapt-CSRF": app.state.console_csrf_token,
            },
        )

    (row,) = client(attend=True).get("/api/standard-changes").json()
    assert row["candidate_id"] == approved.candidate_id
    assert row["actions"] == ["revert"]
    url = f"/api/standard-changes/{approved.candidate_id}/revert"
    # Not on an unattended console, and not with a stale card.
    assert (
        client(attend=False)
        .post(url, json={"entry_sha256": row["entry_sha256"]})
        .status_code
        == 404
    )
    stale = client(attend=True).post(url, json={"entry_sha256": "0" * 64})
    assert stale.status_code == 409 and stale.json()["detail"] == "changed"
    done = client(attend=True).post(url, json={"entry_sha256": row["entry_sha256"]})
    assert done.status_code == 200, done.text
    assert done.json()["state"] == "rolled_back"
    assert store.active_pointer().active_digest == approved.prior_content_digest
    (after,) = client(attend=True).get("/api/standard-changes").json()
    assert after["reverted_by"] == "decision_portal:person"
    assert after["actions"] == []
    # The step is held: the same kind of repair now needs a person.
    from openadapt_flow.repair.standard_change import StandardChangeRefused, authorize

    with pytest.raises(StandardChangeRefused, match="reverted an automatic repair"):
        authorize(candidate, policy=policy, key_dir=key_dir, store_root=store.root)
    from openadapt_flow.repair.standard_change import clear_hold

    clear_hold(store.root, "triage", "step_001", by="ops-lead")
    assert authorize(candidate, policy=policy, key_dir=key_dir, store_root=store.root)


# -- review regressions -------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "ocr", "problem"),
    [
        ("", "", "the control has no readable label"),
        ("Void encounter", "Void encounter", "the control reads as 'void'"),
        ("Sign note", "Sign note", "the control reads as 'sign'"),
    ],
    ids=["unlabeled", "void", "sign"],
)
def test_a_relabel_onto_an_unclear_or_risky_control_goes_to_a_person(
    mockmed, tmp_path, name, ocr, problem
) -> None:
    """The new control's own label decides, never the recorded intent."""

    _base, bundle = mockmed

    def edit(workflow: Workflow) -> None:
        _relabel(workflow)
        anchor = _step(workflow, "step_001").anchor
        anchor.ocr_text = ocr or None
        if anchor.structural is not None:
            anchor.structural = anchor.structural.model_copy(update={"name": name})

    patched = _copy(bundle, tmp_path / "patched", edit)
    verdict = classify_change(
        Workflow.load(bundle), Workflow.load(patched), bundle, patched, ALL_CLASSES
    )
    assert not verdict.allowed
    assert any(problem in p for p in verdict.problems), verdict.problems


def test_a_changed_file_of_another_step_goes_to_a_person(mockmed, tmp_path) -> None:
    """Bytes behind an unchanged step's JSON are compared too."""

    _base, bundle = mockmed
    out = tmp_path / "patched"
    template = _step(Workflow.load(bundle), "step_002").anchor.template

    def edit(workflow: Workflow) -> None:
        _relabel(workflow)
        (out / template).write_bytes((out / template).read_bytes() + b"\0")

    patched = _copy(bundle, out, edit)
    verdict = classify_change(
        Workflow.load(bundle), Workflow.load(patched), bundle, patched, ALL_CLASSES
    )
    assert f"the bundle file {template} changed" in verdict.problems


def test_only_the_no_control_refusal_is_repairable(mockmed) -> None:
    """An ambiguous locator or a changed record is never self-repaired."""

    from openadapt_flow.ir import HaltObservation, Resolution, StepResult
    from openadapt_flow.repair.self_repair import _repairable

    _base, bundle = mockmed
    workflow = Workflow.load(bundle)

    def report(error: str) -> RunReport:
        first = StepResult(
            step_id="step_000", intent="click 'Open'", ok=True, delivery_attempted=True
        )
        halted = StepResult(
            step_id="step_001",
            intent="click 'New Encounter'",
            ok=False,
            delivery_attempted=False,
            error=error,
            resolution=Resolution(
                rung="geometry", point=(1, 1), confidence=0.9, elapsed_ms=0
            ),
        )
        return RunReport(
            workflow_name="triage",
            started_at="2026-10-18T00:00:00Z",
            results=[first, halted],
            halt=HaltObservation(state_id="step_001", reason=error),
        )

    prefix = "Structural safety refusal for step 'step_001' (click 'New Encounter'): "
    no_control = (
        "visual point is not an identity-bearing actionable DOM element — "
        "no action was admitted"
    )
    assert not isinstance(_repairable(report(prefix + no_control), workflow), str)
    for other in (
        "DOM locator is ambiguous (2 matches) — no action was admitted",
        "guarded DOM target or record identity changed before delivery — "
        "no action was admitted",
    ):
        assert isinstance(_repairable(report(prefix + other), workflow), str)


def test_a_rollback_by_any_route_holds_the_step(mockmed, keys, tmp_path) -> None:
    from openadapt_flow.repair.standard_change import hold_reason

    store_root = tmp_path / "store"
    Ledger(store_root).append(
        {
            "event": "applied",
            "candidate_id": "0123456789abcdef",
            "workflow": "triage",
            "step_id": "step_001",
        }
    )
    # The candidate is not in the store at all: held, fail closed.
    assert "missing" in hold_reason(store_root, "triage", "step_001")
    assert hold_reason(store_root, "triage", "step_002") is None
    from openadapt_flow.repair.standard_change import clear_hold

    clear_hold(store_root, "triage", "step_001", by="ops-lead")
    assert hold_reason(store_root, "triage", "step_001") is None
