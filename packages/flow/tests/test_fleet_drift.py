"""Fleet drift patches: export, signature chain, import, canary, policy.

The end-to-end tests drive the real Replayer (Standard profile, independent
effect verification) against the simulated payer portal in
``openadapt_flow.fleet.simulator``. No network, no model, no browser.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openadapt_flow.fleet import (
    CloudPatchFeed,
    FilePatchFeed,
    FleetPromotionPolicy,
    FleetTrust,
    PatchExportError,
    PatchImportError,
    PatchRedactionError,
    PatchSignatureError,
    QualificationCase,
    SignedStructuralPatch,
    export_patch,
    import_patch,
    issue_certificate,
    process_imported_patch,
)
from openadapt_flow.fleet import simulator as sim
from openadapt_flow.fleet.canary import _label, judge_fault, judge_healthy
from openadapt_flow.fleet.fingerprint import (
    AppVersionFingerprint,
    dom_skeleton_sha256,
    version_sha256,
    window_hierarchy_sha256,
)
from openadapt_flow.fleet.patch import find_leaks, sanitize_selector, sign_patch
from openadapt_flow.fleet.signing import public_key_b64
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    IdentityTemplate,
    RunReport,
    Step,
    StructuralLocator,
    TokenTemplate,
    Workflow,
)
from openadapt_flow.repair.candidate import (
    Actor,
    CampaignCaseResult,
    CampaignResult,
    value_digest,
)
from openadapt_flow.repair.lifecycle import (
    QUALIFICATION_CASE_KIND,
    RepairLifecycleError,
    RepairStore,
)
from openadapt_flow.repair.registration import build_candidate
from openadapt_flow.runtime.effects import Effect, EffectKind

PERSON = Actor(kind="human", id="ops-lead")
AUTOMATION = Actor(kind="automation", id="ci")


# --------------------------------------------------------------------------- #
# Shared fleet: north repairs the 7.2 drift once and exports a patch.
# --------------------------------------------------------------------------- #


def _source_patch(fleet: sim.Fleet, *, change_identity: bool = False):
    north = fleet.add_tenant(
        "north",
        member_band="Member Ada Lovelace DOB 1985-12-10",
        member_key="north-m-1",
        auto_promote=True,
        portal_version="7.1",
        healthy_cases=1,
        fault_kinds=("wrong_entity",),
    )
    north.portal_version = "7.2"  # the portal upgrades under north
    candidate_id = sim.local_repair(
        north, reviewer="north-ops-lead", change_identity=change_identity
    )
    assert north.certificate is not None
    return (
        export_patch(
            north.store,
            candidate_id,
            workflow_family=sim.WORKFLOW_FAMILY,
            app_before=sim.app_fingerprint("7.1"),
            app_after=sim.app_fingerprint("7.2"),
            signer_key=north.signer_key,
            certificate=north.certificate,
        ),
        north,
        candidate_id,
    )


def _new_fleet(root: Path) -> sim.Fleet:
    return sim.Fleet(
        root=root,
        root_key=Ed25519PrivateKey.generate(),
        feed=FilePatchFeed(root / "feed"),
    )


@pytest.fixture(scope="module")
def fleet_with_patch(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("fleet").resolve()
    with sim._private_durable_authority(root):
        fleet = _new_fleet(root)
        exported, north, candidate_id = _source_patch(fleet)
        assert sim.live_canary(north, candidate_id) == "active"
        fleet.feed.publish(exported.patch)
    return fleet, exported.patch


def _receiver(fleet: sim.Fleet, name: str, **kwargs) -> sim.SimulatedTenant:
    options = dict(
        member_band="Member Grace Hopper DOB 1906-12-09",
        member_key=f"{name}-m-7",
        auto_promote=True,
        portal_version="7.2",
        healthy_cases=1,
        fault_kinds=("wrong_entity", "duplicate_target"),
    )
    options.update(kwargs)
    # Every tenant recorded its bundle on 7.1; ``portal_version`` is what the
    # portal runs now.
    running = options.pop("portal_version")
    tenant = fleet.add_tenant(name, portal_version="7.1", **options)
    tenant.portal_version = running
    return tenant


def _receive(fleet, tenant, patch):
    with sim._private_durable_authority(tenant.root.resolve()):
        return sim.receive(fleet, tenant, patch)


# --------------------------------------------------------------------------- #
# (a) Fingerprint
# --------------------------------------------------------------------------- #


def test_fingerprint_ignores_text_and_record_count_but_sees_structure():
    one = sim.portal_html("7.2", member_rows=1)
    many = sim.portal_html("7.2", member_rows=40)
    renamed = one.replace("Search", "Find").replace("Member 0", "Jane Q Sample")
    assert dom_skeleton_sha256(one) == dom_skeleton_sha256(many)
    assert dom_skeleton_sha256(one) == dom_skeleton_sha256(renamed)
    assert dom_skeleton_sha256(one) != dom_skeleton_sha256(sim.portal_html("7.1"))
    # Attribute values (ids, values) are not structure.
    assert dom_skeleton_sha256(
        '<input id="mrn-1" value="Jane">'
    ) == dom_skeleton_sha256('<input id="mrn-2" value="Bob">')

    tree = {
        "control_type": "Window",
        "class_name": "EHRMain",
        "name": "Chart - Jane Q Sample",
        "children": [
            {"control_type": "Edit", "class_name": "Edit", "automation_id": "mrn"}
        ],
    }
    other = json.loads(
        json.dumps(tree).replace("Jane Q Sample", "Bob").replace('"mrn"', '"x"')
    )
    assert window_hierarchy_sha256(tree) == window_hierarchy_sha256(other)

    a = sim.app_fingerprint("7.2")
    assert a.mismatch(sim.app_fingerprint("7.2", member_rows=12)) is None
    assert a.mismatch(sim.app_fingerprint("7.1")) is not None
    bare = AppVersionFingerprint(
        app_family=a.app_family,
        target_kind="web",
        version_sha256=version_sha256(a.app_family, "7.2"),
    )
    assert a.mismatch(bare) is not None


# --------------------------------------------------------------------------- #
# (b) Export redaction
# --------------------------------------------------------------------------- #

PHI = {
    "name": "Jane Q Sample",
    "dob": "1980-01-01",
    "mrn": "MRN-00012345",
    "member_id": "W123456789",
    "note": "Follow-up for diabetic retinopathy",
    "phone": "416-555-0199",
}
SALT = "9f2c4a1b7e3d5f60aa11bb22cc33dd44"


def _phi_bundle(path: Path, *, search_selector: str, save_selector: str) -> Path:
    ctx = f"{PHI['name']} DOB {PHI['dob']} {PHI['mrn']}"
    template = IdentityTemplate(
        salt=SALT,
        band_len=len(ctx),
        tokens=[
            TokenTemplate(c="c" * 64, r="d" * 64, n=4, alpha=True),
            TokenTemplate(c="e" * 64, r="f" * 64, n=8, digit=True),
        ],
        structured="a1" * 32,
    )
    workflow = Workflow(
        name=f"intake for {PHI['name']}",
        viewport=(400, 240),
        params={"member_name": PHI["name"], "dob": PHI["dob"]},
        steps=[
            Step(
                id="step-1",
                intent="Open the chart",
                action=ActionKind.CLICK,
                anchor=Anchor(
                    template="templates/a.png",
                    region=(10, 10, 40, 20),
                    click_point=(30, 20),
                    ocr_text=PHI["name"],
                    context_text=ctx,
                    structured_identity=f"{PHI['name']} {PHI['dob']} {PHI['mrn']}",
                    structural=StructuralLocator(
                        selector=search_selector,
                        role="link",
                        name=f"Open chart {PHI['name']}",
                        window_name=f"Chart - {PHI['name']}",
                    ),
                ),
            ),
            Step(
                id="note",
                intent="Type the note",
                action=ActionKind.TYPE,
                text=PHI["note"],
            ),
            Step(
                id="save",
                intent="Save",
                action=ActionKind.CLICK,
                risk="irreversible",
                anchor=Anchor(
                    template="templates/b.png",
                    region=(10, 60, 40, 20),
                    click_point=(30, 70),
                    ocr_text="Save",
                    identity_template=template,
                    structural=StructuralLocator(
                        selector=save_selector, role="button", name="Save"
                    ),
                ),
                effects=[
                    Effect(
                        kind=EffectKind.RECORD_WRITTEN,
                        match={
                            "mrn": PHI["mrn"],
                            "member": PHI["member_id"],
                            "phone": PHI["phone"],
                        },
                    )
                ],
            ),
        ],
    )
    path.mkdir(parents=True, exist_ok=True)
    (path / "templates").mkdir(exist_ok=True)
    (path / "templates" / "a.png").write_bytes(b"\x89PNG-a" + PHI["mrn"].encode())
    (path / "templates" / "b.png").write_bytes(b"\x89PNG-b")
    workflow.save(path)
    return path


def _approved(tmp_path: Path, *, new_search: str):
    prior = _phi_bundle(
        tmp_path / "prior", search_selector="#open-chart", save_selector="#save"
    )
    proposed = _phi_bundle(
        tmp_path / "proposed",
        search_selector=new_search,
        save_selector="[data-testid=save-note]",
    )
    store = RepairStore(tmp_path / "store")
    candidate = store.add_candidate(build_candidate(prior, proposed, source="manual"))
    store.review(candidate.candidate_id, PERSON)
    for kind in ("replay", "fault"):
        store.record_campaign(
            candidate.candidate_id,
            CampaignResult(
                campaign=kind,
                passed=True,
                total=1,
                failed=0,
                cases=[
                    CampaignCaseResult(
                        label="case-x", kind=QUALIFICATION_CASE_KIND, passed=True
                    )
                ],
            ),
            AUTOMATION,
        )
    store.approve(candidate.candidate_id, PERSON)
    return store, store.load_candidate(candidate.candidate_id), prior, proposed


def _signer(families=(sim.APP_FAMILY,)):
    root = Ed25519PrivateKey.generate()
    key = Ed25519PrivateKey.generate()
    now = datetime.now(timezone.utc)
    cert = issue_certificate(
        root,
        tenant_pseudonym="tenant-src-01",
        signer_public_key=public_key_b64(key),
        app_families=families,
        not_before=now - timedelta(minutes=1),
        not_after=now + timedelta(days=1),
    )
    return root, key, cert


def _export(store, candidate_id, key, cert):
    return export_patch(
        store,
        candidate_id,
        workflow_family=sim.WORKFLOW_FAMILY,
        app_before=sim.app_fingerprint("7.1"),
        app_after=sim.app_fingerprint("7.2"),
        signer_key=key,
        certificate=cert,
    )


def test_export_redaction_carries_no_fixture_value_or_digest(tmp_path):
    store, candidate, prior, proposed = _approved(tmp_path, new_search="#chart-link")
    _, key, cert = _signer()
    patch = _export(store, candidate.candidate_id, key, cert).patch
    serialized = patch.to_json()
    text = serialized.lower()

    forbidden = list(PHI.values()) + [
        f"Open chart {PHI['name']}",
        f"Chart - {PHI['name']}",
        f"intake for {PHI['name']}",
        SALT,
    ]
    for value in forbidden:
        assert value.lower() not in text, value
        # Also as a word run in any spelling (jane-q-sample, jane_q_sample).
        assert not find_leaks(serialized, [value]), value
    for bundle in (prior, proposed):
        workflow = Workflow.load(bundle)
        template = workflow.steps[2].anchor.identity_template
        for digest in [template.salt, template.structured] + [
            t.c for t in template.tokens
        ]:
            assert digest.lower() not in text
    for value in PHI.values():
        assert value_digest(value) not in text
    for change in candidate.binding_changes:
        for digest in (change.old_sha256, change.new_sha256):
            assert digest is None or digest not in text
    # Nothing tenant-local: step ids, paths, template files.
    for local in ("step-1", str(tmp_path).lower(), "templates/", "png"):
        assert local not in text
    assert {m.old.selector for m in patch.payload.maps} == {"#open-chart", "#save"}
    assert all(not m.touches_identity for m in patch.payload.maps)


@pytest.mark.parametrize(
    "selector",
    [
        "#row-00012345",  # a record key
        'a:has-text("Jane Q Sample")',  # a text engine
        "text=Jane Q Sample",
        '[aria-label="Open chart Jane Q Sample"]',  # a text-bearing attribute
        "[data-testid=chart-3f9a2c7d1b]",  # a hex id
    ],
)
def test_export_refuses_a_selector_that_can_carry_data(tmp_path, selector):
    with pytest.raises(PatchRedactionError):
        sanitize_selector(selector)
    store, candidate, _, _ = _approved(tmp_path, new_search=selector)
    _, key, cert = _signer()
    with pytest.raises(PatchExportError):
        _export(store, candidate.candidate_id, key, cert)


def test_export_refuses_a_selector_that_spells_a_record_value(tmp_path):
    store, candidate, _, _ = _approved(tmp_path, new_search="#jane-q-sample")
    _, key, cert = _signer()
    with pytest.raises(PatchExportError, match="tenant data"):
        _export(store, candidate.candidate_id, key, cert)


def test_export_requires_a_person_approved_repair(tmp_path):
    _, _, prior, proposed = _approved(tmp_path, new_search="#chart-link")
    other = RepairStore(tmp_path / "unapproved")
    pending = other.add_candidate(build_candidate(prior, proposed, source="manual"))
    _, key, cert = _signer()
    with pytest.raises(PatchExportError, match="only an approved repair"):
        _export(other, pending.candidate_id, key, cert)


# --------------------------------------------------------------------------- #
# Signature chain
# --------------------------------------------------------------------------- #


def _assert_refused(tenant, patch, error=PatchSignatureError, **kwargs):
    out = tenant.root / "bundles" / "refused"
    with pytest.raises(error):
        import_patch(
            tenant.store,
            patch,
            trust=kwargs.pop("trust", tenant.trust),
            tenant_bundle=tenant.bundle,
            tenant_app=tenant.app(),
            out_dir=out,
            **kwargs,
        )
    assert tenant.store.list_candidates() == []
    assert not out.exists()


def test_bad_signature_is_refused(fleet_with_patch):
    fleet, patch = fleet_with_patch
    tenant = _receiver(fleet, "sig-check")

    # Payload changed after signing (a selector swapped in transit).
    data = patch.model_dump(mode="json")
    data["payload"]["maps"][0]["new"]["selector"] = "#evil"
    tampered = SignedStructuralPatch.model_validate(data)
    _assert_refused(tenant, tampered)

    # The attacker also recomputes the patch id: the signature still fails.
    data["patch_id"] = tampered.payload.patch_id()
    _assert_refused(tenant, SignedStructuralPatch.model_validate(data))

    # A well-formed patch from a root this tenant never pinned.
    _, rogue_key, rogue_cert = _signer()
    rogue = sign_patch(
        patch.payload.model_copy(update={"source_tenant_pseudonym": "tenant-src-01"}),
        rogue_key,
        rogue_cert,
    )
    _assert_refused(tenant, rogue)

    # North's real certificate, but another key's signature.
    _assert_refused(tenant, patch.model_copy(update={"signature": rogue.signature}))

    # Revoked signer.
    revoked = FleetTrust(
        roots=tenant.trust.roots,
        revoked_signer_key_ids=frozenset({patch.certificate.signer_key_id}),
    )
    _assert_refused(tenant, patch, trust=revoked)

    # Expired certificate.
    _assert_refused(tenant, patch, now=datetime.now(timezone.utc) + timedelta(days=365))


def test_patch_for_another_app_version_is_refused(fleet_with_patch):
    fleet, patch = fleet_with_patch
    tenant = _receiver(fleet, "old-version", portal_version="7.1")
    _assert_refused(tenant, patch, error=PatchImportError)


# --------------------------------------------------------------------------- #
# Import, canary, policy
# --------------------------------------------------------------------------- #


def test_auto_promotion_only_under_policy(fleet_with_patch):
    fleet, patch = fleet_with_patch

    east = _receiver(fleet, "east")
    decision = _receive(fleet, east, patch)
    assert decision.outcome == "auto_promoted", decision.reasons
    candidate = decision.candidate
    assert candidate.state == "canary"
    assert candidate.approval.pre_approved_by == "east-compliance-officer"
    assert candidate.approval.pre_approval_policy_sha256 == east.policy.policy_sha256()
    assert (
        candidate.approval.proposed_content_digest == candidate.proposed_content_digest
    )
    assert east.store.active_pointer().mode == "canary"
    # Only the structural locator changed; east's own identity band stayed.
    patched = Workflow.load(Path(candidate.proposed_bundle_path))
    assert patched.steps[1].anchor.context_text == east.member_band
    assert (
        patched.steps[1].anchor.structural.selector == "[data-testid=run-eligibility]"
    )
    assert all(not change.identity for change in candidate.binding_changes)

    off = _receiver(fleet, "policy-off", auto_promote=False)
    decision = _receive(fleet, off, patch)
    assert decision.outcome == "needs_person"
    assert decision.candidate.state == "candidate"
    assert decision.candidate.replay_campaign.passed  # the canary still ran
    assert off.store.active_pointer() is None

    other_family = _receiver(fleet, "other-family", app_families=("sim-ehr/chart",))
    decision = _receive(fleet, other_family, patch)
    assert decision.outcome == "needs_person"
    assert decision.candidate.state == "candidate"
    assert other_family.store.active_pointer() is None

    too_few = _receiver(fleet, "too-few-cases")
    too_few.policy = too_few.policy.model_copy(update={"min_healthy_cases": 5})
    decision = _receive(fleet, too_few, patch)
    assert decision.outcome == "needs_person"
    assert decision.candidate.approval is None
    assert too_few.store.active_pointer() is None
    # A person can still approve it through the ordinary path.
    approved = too_few.store.approve(decision.candidate.candidate_id, PERSON)
    assert approved.state == "approved"
    assert approved.approval.pre_approval_policy_sha256 is None


def test_policy_grant_never_reviews_a_non_fleet_repair(tmp_path):
    _, candidate, prior, proposed = _approved(tmp_path, new_search="#chart-link")
    other = RepairStore(tmp_path / "s2")
    pending = other.add_candidate(build_candidate(prior, proposed, source="heal"))
    policy = FleetPromotionPolicy(
        approved_by="cmo",
        approved_at="2026-10-17T00:00:00+00:00",
        auto_promote=True,
        app_families=(sim.APP_FAMILY,),
    )
    with pytest.raises(RepairLifecycleError):
        other.review_under_policy(pending.candidate_id, policy.grant())
    with pytest.raises(RepairLifecycleError):
        other.approve_under_policy(pending.candidate_id, policy.grant())


def test_identity_anchor_patch_always_needs_a_person(tmp_path):
    root = tmp_path.resolve()
    with sim._private_durable_authority(root):
        fleet = _new_fleet(root)
        exported, _, _ = _source_patch(fleet, change_identity=True)
        payload = exported.patch.payload
        assert payload.identity_evidence_changed
        assert exported.notes
        flagged = [m for m in payload.maps if m.touches_identity]
        assert flagged and flagged[0].identity_fields_changed == ("context_text",)
        assert "lovelace" not in exported.patch.to_json().lower()

        east = _receiver(fleet, "east", fault_kinds=("wrong_entity",))
        decision = sim.receive(fleet, east, exported.patch)
    assert decision.outcome == "needs_person"
    assert any("identity" in reason for reason in decision.reasons)
    candidate = decision.candidate
    assert candidate.fleet_patch.identity_evidence_changed
    assert candidate.state == "candidate"
    assert candidate.replay_campaign is not None and candidate.replay_campaign.passed
    grant = east.policy.grant()
    with pytest.raises(RepairLifecycleError, match="identity"):
        east.store.review_under_policy(candidate.candidate_id, grant)
    with pytest.raises(RepairLifecycleError):
        east.store.approve_under_policy(candidate.candidate_id, grant)
    assert east.store.active_pointer() is None


class _BrokenRunner:
    """East runs a different 7.2 build: the patched control is absent."""

    def __init__(self, tenant):
        self.tenant = tenant

    def run_case(self, bundle_dir, case, run_dir):
        fault = case.fault if case.kind == "fault" else "missing_control"
        return self.tenant.replay(bundle_dir, run_dir, fault=fault)


def test_canary_failure_blocks_the_patch(fleet_with_patch):
    fleet, patch = fleet_with_patch
    east = _receiver(fleet, "east-broken")
    with sim._private_durable_authority(east.root.resolve()):
        result = import_patch(
            east.store,
            patch,
            trust=east.trust,
            tenant_bundle=east.bundle,
            tenant_app=east.app(),
            out_dir=east.root / "bundles" / "patched",
        )
        decision = process_imported_patch(
            east.store,
            result.candidate.candidate_id,
            policy=east.policy,
            runner=_BrokenRunner(east),
            cases=east.cases(),
            work_dir=east.root / "canary",
        )
    assert decision.outcome == "blocked"
    candidate = decision.candidate
    assert candidate.replay_campaign is not None
    assert not candidate.replay_campaign.passed
    assert candidate.state == "reviewed"
    with pytest.raises(RepairLifecycleError):
        east.store.approve_under_policy(candidate.candidate_id, east.policy.grant())
    with pytest.raises(RepairLifecycleError):
        east.store.approve(candidate.candidate_id, PERSON)
    assert east.store.active_pointer() is None


# --------------------------------------------------------------------------- #
# Zero model calls
# --------------------------------------------------------------------------- #


def _report(**kwargs) -> RunReport:
    base = dict(workflow_name="w", started_at="2026-10-17T00:00:00+00:00")
    base.update(kwargs)
    return RunReport(**base)


def test_healthy_replay_makes_zero_model_calls(fleet_with_patch):
    # A VERIFIED run that needed a model or a heal is not a healthy replay.
    ok = dict(success=True, execution_outcome="VERIFIED")
    assert judge_healthy(_report(**ok))[0]
    assert not judge_healthy(_report(**ok, model_calls=1))[0]
    assert not judge_healthy(_report(**ok, heal_count=1))[0]
    assert not judge_healthy(
        _report(success=True, execution_outcome="COMPLETED_UNVERIFIED")
    )[0]
    assert not judge_fault(_report(execution_outcome="HALTED", model_calls=2))[0]

    # Real replays of the promoted bundle on a receiving tenant.
    fleet, patch = fleet_with_patch
    east = _receiver(fleet, "east-zero")
    decision = _receive(fleet, east, patch)
    assert decision.outcome == "auto_promoted"
    bundle = east.store.bundle_path(decision.candidate.proposed_content_digest)
    with sim._private_durable_authority(east.root.resolve()):
        report = east.replay(bundle, east.root / "runs" / "healthy")
    assert report.execution_outcome == "VERIFIED"
    assert report.model_calls == 0
    assert report.heal_count == 0
    assert east.model_calls == 0


# --------------------------------------------------------------------------- #
# Feed and labels
# --------------------------------------------------------------------------- #


def test_file_feed_round_trip_and_cloud_stub(tmp_path, fleet_with_patch):
    _, patch = fleet_with_patch
    feed = FilePatchFeed(tmp_path / "feed")
    assert feed.publish(patch) == patch.patch_id
    assert feed.publish(patch) == patch.patch_id
    (tmp_path / "feed" / "junk.json").write_text("{not json")
    found = feed.list_patches(app_family=sim.APP_FAMILY)
    assert [p.patch_id for p in found] == [patch.patch_id]
    assert feed.list_patches(app_family="sim-ehr/chart") == []
    old = sim.app_fingerprint("7.1").fingerprint_sha256()
    assert (
        feed.list_patches(app_family=sim.APP_FAMILY, app_fingerprint_sha256=old) == []
    )
    with pytest.raises(NotImplementedError):
        CloudPatchFeed("https://cloud.example").list_patches(app_family=sim.APP_FAMILY)


def test_qualification_case_ids_stay_local():
    case = QualificationCase(
        case_id="Jane Q Sample", kind="healthy", params={"mrn": PHI["mrn"]}
    )
    assert "jane" not in _label(case).lower()
