"""Fleet drift patches: keys, hub re-signing, sanitizer, opt-in, status, policy.

The end-to-end tests drive the real Replayer (Standard profile, independent
effect verification) against the simulated payer portal in
``openadapt_flow.fleet.simulator``. No network, no model, no browser. Every
simulated run claims its durable-run path in a private authority database
under the test's tmp directory, never in ``~/.openadapt``.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from openadapt_flow.fleet import (
    CloudPatchFeed,
    CustodyCeremonyError,
    FilePatchFeed,
    FleetOptIn,
    FleetTrust,
    HubRefused,
    OfflineRootCustody,
    PatchExportError,
    PatchImportError,
    PatchRedactionError,
    PatchSignatureError,
    PolicyVerificationError,
    QualificationCase,
    RoleRegistry,
    SignedStructuralPatch,
    SoftwareKeyCustody,
    apply_fleet_kill_switches,
    export_patch,
    import_patch,
    issue_signer_certificate,
    process_imported_patch,
    read_fleet_status,
    register_fingerprint,
    sign_policy,
    verify_policy,
)
from openadapt_flow.fleet import simulator as sim
from openadapt_flow.fleet.canary import _label, judge_fault, judge_healthy
from openadapt_flow.fleet.fingerprint import (
    AppVersionFingerprint,
    dom_skeleton_sha256,
    version_sha256,
    window_hierarchy_sha256,
)
from openadapt_flow.fleet.hub import FleetHub
from openadapt_flow.fleet.patch import (
    PATCH_DOMAIN,
    StructuralAnchorRef,
    find_leaks,
    is_generated_name,
    is_static_hook_value,
    looks_name_like,
    sanitize_selector,
    sign_patch,
)
from openadapt_flow.fleet.signing import sign_document
from openadapt_flow.fleet.status import (
    STATUS_STATE_FILE,
    FleetStatusStateError,
    KillSwitchPayload,
    RevocationListPayload,
    StatusHighWater,
    sign_kill_switch,
    sign_revocation_list,
)
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
ALLOW = FleetOptIn.off().allow("export", added_by="admin", app_family=sim.APP_FAMILY)
ALLOW = ALLOW.allow("import", added_by="admin", app_family=sim.APP_FAMILY)


# --------------------------------------------------------------------------- #
# Shared fleet: north repairs the 7.2 drift once and contributes a patch.
# --------------------------------------------------------------------------- #


def _bystanders(fleet: sim.Fleet, count: int = 2) -> None:
    """Other tenants on 7.1, so the base version has three registrations."""
    for index in range(count):
        fleet.add_tenant(
            f"bystander-{index}",
            member_band=f"Member Bystander {index}",
            member_key=f"by-{index}",
            auto_promote=False,
            healthy_cases=1,
            fault_kinds=("wrong_entity",),
        )


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
    exported, decision = sim.contribute(fleet, north, candidate_id)
    return exported, decision, north, candidate_id


@pytest.fixture(scope="module")
def fleet_with_patch(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("fleet").resolve()
    with sim._private_durable_authority(root):
        fleet = sim.build_fleet_core(root)
        _bystanders(fleet)
        _, decision, north, candidate_id = _source_patch(fleet)
        assert decision.outcome == "distributed", decision.reason
        assert sim.live_canary(north, candidate_id) == "active"
    return fleet, decision.patch


def _receiver(fleet: sim.Fleet, name: str, **kwargs) -> sim.SimulatedTenant:
    options = dict(
        member_band="Member Grace Hopper DOB 1906-12-09",
        member_key=f"{name}-m-7",
        auto_promote=True,
        portal_version="7.2",
        healthy_cases=1,
        fault_kinds=("wrong_entity", "duplicate_target"),
        register=False,
    )
    options.update(kwargs)
    # Every tenant recorded its bundle on 7.1; ``portal_version`` is what the
    # portal runs now.
    running = options.pop("portal_version")
    tenant = fleet.add_tenant(name, portal_version="7.1", **options)
    tenant.portal_version = running
    return tenant


def _receive(fleet, tenant, patch, *, status_source=None):
    with sim._private_durable_authority(tenant.root.resolve()):
        if status_source is None:
            return sim.receive(fleet, tenant, patch)
        result = import_patch(
            tenant.store,
            patch,
            trust=tenant.trust,
            opt_in=tenant.opt_in,
            tenant_bundle=tenant.bundle,
            tenant_app=tenant.app(),
            out_dir=tenant.root / "bundles" / f"patch-{patch.patch_id[:12]}",
        )
        return process_imported_patch(
            tenant.store,
            result.candidate.candidate_id,
            policy=tenant.policy,
            roles=tenant.roles,
            trust=tenant.trust,
            status_source=status_source,
            status_state=tenant.status_state,
            runner=tenant,
            cases=tenant.cases(),
            work_dir=tenant.root / "canary" / patch.patch_id[:12],
        )


def _status_feed(fleet: sim.Fleet, path: Path, *, sequence: int = 1) -> FilePatchFeed:
    """A separate status feed signed by the fleet's real status key."""
    feed = FilePatchFeed(path)
    signer, chain = fleet.hub._status
    now = datetime.now(timezone.utc)
    feed.publish_revocation_list(
        sign_revocation_list(
            RevocationListPayload(
                sequence=sequence,
                issued_at=now.isoformat(),
                next_update=(now + timedelta(hours=12)).isoformat(),
            ),
            signer,
            chain,
        )
    )
    return feed


def _authority(fleet, tenant, *, status_source=None):
    """Everything approve_under_policy verifies for itself."""
    return dict(
        policy=tenant.policy,
        roles=tenant.roles,
        trust=tenant.trust,
        status_source=status_source or fleet.feed,
        status_state=tenant.status_state,
    )


def _kill(fleet, feed, *, sequence=2, disable=False, rollback=(), reason="bad_patch"):
    signer, chain = fleet.hub._status
    doc = sign_kill_switch(
        KillSwitchPayload(
            sequence=sequence,
            issued_at=datetime.now(timezone.utc).isoformat(),
            disable_auto_promotion=disable,
            rollback_patch_ids=tuple(sorted(rollback)),
            reason=reason,
        ),
        signer,
        chain,
    )
    feed.publish_kill_switch(doc)
    return doc


# --------------------------------------------------------------------------- #
# Fingerprint
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


def _approved(tmp_path: Path, *, new_search: str, store_name: str = "store"):
    prior = _phi_bundle(
        tmp_path / f"{store_name}-prior",
        search_selector="#open-chart",
        save_selector="#save",
    )
    proposed = _phi_bundle(
        tmp_path / f"{store_name}-proposed",
        search_selector=new_search,
        save_selector="button[name=save-note]",
    )
    store = RepairStore(tmp_path / store_name)
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


@pytest.fixture
def core(tmp_path) -> sim.Fleet:
    """Root, intermediate, hub, and no tenants. No replays."""
    return sim.build_fleet_core(tmp_path.resolve() / "core")


def _contributor(fleet: sim.Fleet, subject: str, families=(sim.APP_FAMILY,)):
    key = SoftwareKeyCustody.generate()
    return key, fleet.issue("contributor", subject, key, app_families=families)


def _export(store, candidate_id, key, chain, opt_in=ALLOW, **kwargs):
    return export_patch(
        store,
        candidate_id,
        workflow_family=sim.WORKFLOW_FAMILY,
        app_before=sim.app_fingerprint("7.1"),
        app_after=sim.app_fingerprint("7.2"),
        signer=key,
        chain=chain,
        opt_in=opt_in,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# Export redaction and sanitizer
# --------------------------------------------------------------------------- #


def test_export_redaction_carries_no_fixture_value_or_digest(tmp_path, core):
    store, candidate, prior, proposed = _approved(tmp_path, new_search="#chart-link")
    key, chain = _contributor(core, "tenant-src")
    contribution = _export(store, candidate.candidate_id, key, chain).contribution
    serialized = contribution.to_json()
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
    for local in ("step-1", str(tmp_path).lower(), "templates/", "png"):
        assert local not in text
    # The bundle digests travel to the hub only, never inside the patch.
    patch_text = contribution.payload.patch.model_dump_json()
    assert candidate.prior_content_digest not in patch_text
    assert candidate.proposed_content_digest not in patch_text
    maps = contribution.payload.patch.maps
    assert {m.old.selector for m in maps} == {"#open-chart", "#save"}
    assert all(not m.touches_identity for m in maps)


@pytest.mark.parametrize(
    "selector",
    [
        "#row-00012345",  # a record key
        'a:has-text("Jane Q Sample")',  # a text engine
        "text=Jane Q Sample",
        '[aria-label="Open chart Jane Q Sample"]',  # a text-bearing attribute
        "[data-testid=chart-3f9a2c7d1b]",  # a hex id
        "[data-track=save-note]",  # a data-* value outside the test hooks
        "[data-testid=row-00012345]",  # a test hook holding a record key
        "[data-cy=a1b2c3d4]",  # a test hook holding a hex id
        "[data-qa=row-123e4567-e89b-12d3-a456-426614174000]",  # a UUID
        "[data-test-id=patient-Jane-Doe]",  # a test hook holding a name
        "[data-test=row_Smith]",
        "[data-patient=jane]",  # not an allowed attribute at all
        'iframe[src="/chart?mrn=1"]',  # an address with a query string
        "a.chart?mrn=1",
        ".Button_primary__3xK9a",  # CSS modules
        ".css-1q2w3e",  # emotion
        ".sc-bdVaJa",  # styled-components
        ".jss12",  # JSS
        "button.card-a1b2c",  # trailing build hash
    ],
)
def test_export_refuses_a_selector_that_can_carry_data(tmp_path, core, selector):
    with pytest.raises(PatchRedactionError):
        sanitize_selector(selector)
    store, candidate, _, _ = _approved(tmp_path, new_search=selector)
    key, chain = _contributor(core, "tenant-src")
    with pytest.raises(PatchExportError):
        _export(store, candidate.candidate_id, key, chain)


def test_sanitizer_keeps_developer_selectors_and_checks_frame_paths():
    for selector in (
        "#member-search",
        "button[name=run-eligibility]",
        "[data-testid]",
        ".btn-primary",
        ".col-md-6",
        "main > section:nth-of-type(2) button",
    ):
        assert sanitize_selector(selector) == selector
    assert not is_generated_name("heading2")
    assert is_generated_name("Card_title__x8Yz1")
    for frame in (
        "iframe#chart?mrn=12",
        'iframe[src="https://ehr.example/x"]',
        ".css-8f7g6h",
    ):
        with pytest.raises(ValueError):
            StructuralAnchorRef(selector="#save", frame_path=(frame,))
    ok = StructuralAnchorRef(selector="#save", frame_path=("iframe#chart",))
    assert ok.frame_path == ("iframe#chart",)


@pytest.mark.parametrize(
    "selector",
    [
        "[data-testid=save-note]",
        "button[data-test=submit]",
        "[data-test-id=member_search]",
        "[data-cy=runEligibility]",
        "[data-qa=SaveButton]",
        "#chart [data-testid='open-chart']",
    ],
)
def test_sanitizer_allows_static_test_hook_values(selector):
    assert sanitize_selector(selector) == selector


def test_test_hook_value_rules():
    for value in ("save-note", "step-2", "runEligibility", "SaveButton", "tab_3"):
        assert is_static_hook_value(value), value
    for value in (
        "row-1234",
        "deadbeef",
        "123e4567e89b12d3a456426614174000",
        "patient-Jane-Doe",
        "Smith_row",
    ):
        assert not is_static_hook_value(value), value
    assert looks_name_like("mary-Ann")
    assert not looks_name_like("Mary")  # one word: indistinguishable from a control
    # Only the fixed test hooks may carry a value.
    for attr in ("data-id", "data-row", "data-patient-id", "data-name"):
        with pytest.raises(PatchRedactionError):
            sanitize_selector(f"[{attr}=save-note]")


@pytest.mark.parametrize(
    "selector",
    ["#patient-Jane-Doe", ".row_Smith", "[name=member-Mary-Ann]", "[for=dob-Jane-Doe]"],
)
def test_sanitizer_refuses_name_like_ids_classes_and_values(selector):
    with pytest.raises(PatchRedactionError, match="name"):
        sanitize_selector(selector)


def test_leak_check_finds_names_written_together():
    for text in ("rowJaneQSample", "row-jane-q-sample", "rowjaneqsample"):
        assert find_leaks(text, ["Jane Q. Sample"]), text
    # Single words are still matched as whole words only.
    assert not find_leaks("saveNote", ["ave"])
    assert not find_leaks("savenote", ["note"])


def test_export_carries_a_static_test_hook_value(tmp_path, core):
    store, candidate, _, _ = _approved(
        tmp_path, new_search="[data-testid=member-search-v2]"
    )
    key, chain = _contributor(core, "tenant-src")
    exported = _export(store, candidate.candidate_id, key, chain)
    selectors = {m.new.selector for m in exported.contribution.payload.patch.maps}
    assert "[data-testid=member-search-v2]" in selectors


def test_export_refuses_a_selector_that_spells_a_record_value(tmp_path, core):
    store, candidate, _, _ = _approved(tmp_path, new_search="#jane-q-sample")
    key, chain = _contributor(core, "tenant-src")
    with pytest.raises(PatchExportError, match="tenant data"):
        _export(store, candidate.candidate_id, key, chain)


def test_export_requires_a_person_approved_repair(tmp_path, core):
    _, _, prior, proposed = _approved(tmp_path, new_search="#chart-link")
    other = RepairStore(tmp_path / "unapproved")
    pending = other.add_candidate(build_candidate(prior, proposed, source="manual"))
    key, chain = _contributor(core, "tenant-src")
    with pytest.raises(PatchExportError, match="only an approved repair"):
        _export(other, pending.candidate_id, key, chain)


def test_export_is_off_until_an_admin_allows_the_app(tmp_path, core):
    store, candidate, _, _ = _approved(tmp_path, new_search="#chart-link")
    key, chain = _contributor(core, "tenant-src")
    with pytest.raises(PatchExportError, match="fleet export is off"):
        _export(store, candidate.candidate_id, key, chain, opt_in=FleetOptIn.off())
    # Allowing import does not allow export.
    import_only = FleetOptIn.off().allow(
        "import", added_by="a", app_family=sim.APP_FAMILY
    )
    with pytest.raises(PatchExportError, match="fleet export is off"):
        _export(store, candidate.candidate_id, key, chain, opt_in=import_only)
    by_domain = FleetOptIn.off().allow(
        "export", added_by="a", domain="sim-payer.example"
    )
    with pytest.raises(PatchExportError, match="fleet export is off"):
        _export(store, candidate.candidate_id, key, chain, opt_in=by_domain)
    result = _export(
        store,
        candidate.candidate_id,
        key,
        chain,
        opt_in=by_domain,
        origin_host="portal.sim-payer.example",
    )
    assert "sim-payer.example" not in result.contribution.to_json()
    path = tmp_path / "opt-in.json"
    assert FleetOptIn.load(path) == FleetOptIn.off()
    by_domain.save(path)
    assert FleetOptIn.load(path).allows(
        "export", "other/app", "portal.sim-payer.example"
    )


# --------------------------------------------------------------------------- #
# Key custody
# --------------------------------------------------------------------------- #


def test_root_signs_only_with_a_custodian_quorum_and_certs_are_short_lived(core):
    now = datetime.now(timezone.utc)
    root = OfflineRootCustody(
        SoftwareKeyCustody.generate(), custodians=("ann", "bo", "cy"), threshold=2
    )
    with pytest.raises(ValueError):
        OfflineRootCustody(
            SoftwareKeyCustody.generate(), custodians=("ann",), threshold=1
        )
    inter = SoftwareKeyCustody.generate()
    args = dict(
        purposes=("distribution",),
        not_before=now,
        not_after=now + timedelta(days=30),
        ceremony_id="c-1",
    )
    with pytest.raises(CustodyCeremonyError):
        root.issue_intermediate(inter.public_key_b64(), approvals=("ann",), **args)
    with pytest.raises(CustodyCeremonyError):
        root.issue_intermediate(
            inter.public_key_b64(), approvals=("ann", "eve"), **args
        )
    cert = root.issue_intermediate(
        inter.public_key_b64(), approvals=("bo", "ann"), **args
    )
    assert cert.payload.approvals == ("ann", "bo")

    signer = SoftwareKeyCustody.generate()
    with pytest.raises(ValueError, match="7 days"):
        issue_signer_certificate(
            inter,
            cert,
            purpose="distribution",
            subject="fleet-hub",
            signer_public_key=signer.public_key_b64(),
            app_families=(sim.APP_FAMILY,),
            not_before=now,
            not_after=now + timedelta(days=8),
        )
    with pytest.raises(PatchSignatureError, match="may not issue"):
        issue_signer_certificate(
            inter,
            cert,
            purpose="contributor",
            subject="tenant-x",
            signer_public_key=signer.public_key_b64(),
            app_families=(sim.APP_FAMILY,),
            not_before=now,
            not_after=now + timedelta(days=1),
        )


def test_a_contributor_certificate_cannot_sign_for_distribution(fleet_with_patch):
    fleet, patch = fleet_with_patch
    key, chain = _contributor(fleet, "tenant-rogue")
    with pytest.raises(PatchSignatureError):
        sign_patch(patch.payload, key, chain)
    forged = SignedStructuralPatch(
        payload=patch.payload,
        patch_id=patch.patch_id,
        signature=sign_document(key, PATCH_DOMAIN, patch.payload),
        chain=chain,
    )
    tenant = _receiver(fleet, "forged-check")
    _assert_refused(tenant, forged)


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
            opt_in=kwargs.pop("opt_in", tenant.opt_in),
            tenant_bundle=tenant.bundle,
            tenant_app=tenant.app(),
            out_dir=out,
            **kwargs,
        )
    assert tenant.store.list_candidates() == []
    assert not out.exists()


def test_bad_signature_is_refused(fleet_with_patch, tmp_path):
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

    # A well-formed patch from a fleet this tenant never pinned.
    rogue_fleet = sim.build_fleet_core(tmp_path.resolve() / "rogue")
    signer, chain = rogue_fleet.hub._dist
    rogue = sign_patch(patch.payload, signer, chain)
    _assert_refused(tenant, rogue)

    # The real hub chain, but another key's signature.
    _assert_refused(tenant, patch.model_copy(update={"signature": rogue.signature}))

    # Revoked locally, by signer key or by intermediate.
    for revoked in (
        FleetTrust(
            roots=tenant.trust.roots,
            revoked_key_ids=frozenset({patch.chain.signer_key_id}),
        ),
        FleetTrust(
            roots=tenant.trust.roots, revoked_serials=frozenset({patch.chain.serial})
        ),
        FleetTrust(
            roots=tenant.trust.roots,
            revoked_key_ids=frozenset({patch.chain.intermediate_key_id}),
        ),
    ):
        _assert_refused(tenant, patch, trust=revoked)

    # Revoked by the signed revocation list.
    feed = _status_feed(fleet, tmp_path / "status")
    signer, chain = fleet.hub._status
    now = datetime.now(timezone.utc)
    feed.publish_revocation_list(
        sign_revocation_list(
            RevocationListPayload(
                sequence=2,
                issued_at=now.isoformat(),
                next_update=(now + timedelta(hours=1)).isoformat(),
                revoked_serials=(patch.chain.serial,),
            ),
            signer,
            chain,
        )
    )
    status = read_fleet_status(feed, tenant.trust)
    assert status.fresh
    _assert_refused(tenant, patch, status=status)

    # A signer certificate is valid for at most seven days.
    _assert_refused(tenant, patch, now=datetime.now(timezone.utc) + timedelta(days=8))


def test_patch_for_another_app_version_is_refused(fleet_with_patch):
    fleet, patch = fleet_with_patch
    tenant = _receiver(fleet, "old-version", portal_version="7.1")
    _assert_refused(tenant, patch, error=PatchImportError)


def test_a_failed_registration_leaves_no_bundle_copy(fleet_with_patch, monkeypatch):
    fleet, patch = fleet_with_patch
    tenant = _receiver(fleet, "register-fails")
    out_dir = tenant.root / "bundles" / "patched"

    def refuse(self, candidate):
        raise RepairLifecycleError("store refused")

    monkeypatch.setattr(RepairStore, "add_candidate", refuse)
    with pytest.raises(RepairLifecycleError):
        import_patch(
            tenant.store,
            patch,
            trust=tenant.trust,
            opt_in=tenant.opt_in,
            tenant_bundle=tenant.bundle,
            tenant_app=tenant.app(),
            out_dir=out_dir,
        )
    assert not out_dir.exists()


def test_import_is_off_until_an_admin_allows_the_app(fleet_with_patch):
    fleet, patch = fleet_with_patch
    tenant = _receiver(fleet, "not-opted-in", opt_in=False)
    _assert_refused(tenant, patch, error=PatchImportError)
    export_only = FleetOptIn.off().allow(
        "export", added_by="a", app_family=sim.APP_FAMILY
    )
    _assert_refused(tenant, patch, error=PatchImportError, opt_in=export_only)


# --------------------------------------------------------------------------- #
# Hub: re-signing, unlinkability, three-tenant rule, revocation
# --------------------------------------------------------------------------- #


def test_hub_resigns_so_receivers_cannot_link_patches_to_a_source(tmp_path, core):
    a_key, a_chain = _contributor(core, "tenant-alpha")
    b_key, b_chain = _contributor(core, "tenant-beta")
    c_key, c_chain = _contributor(core, "tenant-gamma")
    base = sim.app_fingerprint("7.1")
    for key, chain in ((a_key, a_chain), (b_key, b_chain)):
        core.hub.register(register_fingerprint(base, key, chain))

    def contribute(key, chain, search, name):
        store, candidate, _, _ = _approved(tmp_path, new_search=search, store_name=name)
        return _export(store, candidate.candidate_id, key, chain).contribution

    first = contribute(a_key, a_chain, "#chart-link", "a1")
    # Two tenants registered the base version: the patch is held.
    held = core.hub.accept(first)
    assert held.outcome == "held" and held.patch is None
    assert core.feed.list_patches(app_family=sim.APP_FAMILY) == []
    # A contributor that never registered the base version is refused.
    with pytest.raises(HubRefused):
        core.hub.accept(contribute(c_key, c_chain, "#chart-open", "c1"))

    core.hub.register(register_fingerprint(base, c_key, c_chain))
    assert core.hub.tenants_on(base.fingerprint_sha256()) == 3
    released = core.hub.release_held()
    assert [d.outcome for d in released] == ["distributed"]
    contributions = [
        contribute(a_key, a_chain, "#chart-go", "a2"),
        contribute(b_key, b_chain, "#chart-view", "b1"),
    ]
    second, third = (core.hub.accept(c) for c in contributions)
    patches = [released[0].patch, second.patch, third.patch]
    assert all(p is not None for p in patches)

    # Every distributed patch carries the same hub chain and nothing else
    # that identifies a source: A's two patches look exactly like A and B's.
    assert len({p.chain.model_dump_json() for p in patches}) == 1
    assert {p.chain.signer.payload.subject for p in patches} == {sim.HUB_ID}
    source_values = set()
    for chain in (a_chain, b_chain):
        source_values |= {
            chain.signer.payload.subject,
            chain.signer_key_id,
            chain.serial,
            chain.signer.payload.signer_public_key,
        }
    for contribution in (first, *contributions):
        source_values |= {
            contribution.payload.source_bundle_sha256,
            contribution.payload.source_prior_bundle_sha256,
            contribution.signature,
        }
    for patch in patches:
        text = patch.to_json()
        for value in source_values:
            assert value not in text
        assert set(patch.payload.model_dump()) == {
            "schema_version",
            "workflow_family",
            "app_before",
            "app_after",
            "maps",
            "created_at",
        }
        created = datetime.fromisoformat(patch.payload.created_at)
        assert created.minute == created.second == created.microsecond == 0
    # The hub keeps who contributed what, internally, for revocation.
    assert core.hub.contributor_of(patches[1].patch_id) == "tenant-alpha"
    assert core.hub.contributor_of(patches[2].patch_id) == "tenant-beta"

    revocations, kill = core.hub.revoke_contributor("tenant-alpha")
    assert a_chain.serial in revocations.payload.revoked_serials
    assert b_chain.serial not in revocations.payload.revoked_serials
    assert kill is not None
    assert set(kill.payload.rollback_patch_ids) == {
        patches[0].patch_id,
        patches[1].patch_id,
    }
    with pytest.raises(HubRefused):
        core.hub.accept(contribute(a_key, a_chain, "#chart-again", "a3"))


def test_a_revoked_contributor_does_not_count_toward_three_tenants(tmp_path, core):
    base = sim.app_fingerprint("7.1")
    keys = {name: _contributor(core, name) for name in ("t-one", "t-two", "t-three")}
    for key, chain in keys.values():
        core.hub.register(register_fingerprint(base, key, chain))
    assert core.hub.tenants_on(base.fingerprint_sha256()) == 3
    core.hub.revoke_contributor("t-three")
    assert core.hub.tenants_on(base.fingerprint_sha256()) == 2

    key, chain = keys["t-one"]
    store, candidate, _, _ = _approved(tmp_path, new_search="#chart-link")
    contribution = _export(store, candidate.candidate_id, key, chain).contribution
    decision = core.hub.accept(contribution)
    assert decision.outcome == "held"
    assert core.hub.release_held() == []
    assert core.feed.list_patches(app_family=sim.APP_FAMILY) == []


def test_hub_refuses_a_distribution_rule_below_three_tenants(core):
    signer, chain = core.hub._dist
    status_signer, status_chain = core.hub._status
    with pytest.raises(ValueError, match="at least 3"):
        FleetHub(
            core.root / "hub2",
            trust=core.trust,
            feed=core.feed,
            distribution_signer=signer,
            distribution_chain=chain,
            status_signer=status_signer,
            status_chain=status_chain,
            min_tenants=2,
        )


# --------------------------------------------------------------------------- #
# Fleet status: revocation list freshness and kill switches
# --------------------------------------------------------------------------- #


def test_fleet_status_fails_closed(tmp_path, core):
    trust = core.trust
    feed = FilePatchFeed(tmp_path / "empty")
    assert any("unreachable" in p for p in read_fleet_status(feed, trust).problems)
    assert not read_fleet_status(CloudPatchFeed("https://cloud.example"), trust).fresh

    good = read_fleet_status(core.feed, trust)
    assert good.fresh and good.revocation_sequence == 1

    # Stale: issued 25 hours ago.
    signer, chain = core.hub._status
    old = datetime.now(timezone.utc) - timedelta(hours=25)
    stale_feed = FilePatchFeed(tmp_path / "stale")
    stale_feed.publish_revocation_list(
        sign_revocation_list(
            RevocationListPayload(
                sequence=1,
                issued_at=old.isoformat(),
                next_update=(old + timedelta(hours=12)).isoformat(),
            ),
            signer,
            chain,
        )
    )
    assert any("stale" in p for p in read_fleet_status(stale_feed, trust).problems)
    # A shorter tenant limit refuses a list that is otherwise in date.
    assert not read_fleet_status(core.feed, trust, max_age=timedelta(seconds=0)).fresh

    # Signed by a key from another fleet.
    rogue = sim.build_fleet_core(tmp_path.resolve() / "rogue")
    assert any(
        "does not verify" in p for p in read_fleet_status(rogue.feed, trust).problems
    )
    # A distribution certificate cannot sign status.
    dist_signer, dist_chain = core.hub._dist
    with pytest.raises(PatchSignatureError):
        sign_revocation_list(
            RevocationListPayload(
                sequence=9,
                issued_at=datetime.now(timezone.utc).isoformat(),
                next_update=(
                    datetime.now(timezone.utc) + timedelta(hours=1)
                ).isoformat(),
            ),
            dist_signer,
            dist_chain,
        )

    # Kill switches: latest sequence decides; an unverifiable entry fails closed.
    core.hub.issue_kill_switch(disable_auto_promotion=True, reason="incident")
    assert read_fleet_status(core.feed, trust).auto_promotion_disabled
    core.hub.issue_kill_switch(disable_auto_promotion=False, reason="resume")
    assert not read_fleet_status(core.feed, trust).auto_promotion_disabled
    rogue.hub.issue_kill_switch(disable_auto_promotion=False, reason="resume")
    for path in (rogue.feed.status_dir).glob("kill-switch-*.json"):
        (core.feed.status_dir / ("kill-switch-99999999.json")).write_text(
            path.read_text()
        )
    status = read_fleet_status(core.feed, trust)
    assert any("kill-switch entry does not verify" in p for p in status.problems)


def test_a_malformed_kill_switch_entry_does_not_hide_a_rollback(core):
    bad = "a" * 64
    core.hub.issue_kill_switch(
        disable_auto_promotion=False, rollback_patch_ids=[bad], reason="bad_patch"
    )
    # A junk entry sorts after the real one, as a newer entry would.
    (core.feed.status_dir / "kill-switch-99999999.json").write_text("{not json")
    status = read_fleet_status(core.feed, core.trust)
    assert bad in status.rollback_patch_ids
    assert not status.fresh
    assert "a kill-switch entry is malformed" in status.problems


def test_status_high_water_refuses_a_replayed_revocation_list(tmp_path, core):
    trust = core.trust
    state = StatusHighWater(tmp_path / "tenant" / STATUS_STATE_FILE)
    assert state.load().revocation_sequence == 0  # nothing accepted yet

    new = _status_feed(core, tmp_path / "new", sequence=5)
    old = _status_feed(core, tmp_path / "old", sequence=2)
    assert read_fleet_status(new, trust, high_water=state).fresh
    assert state.load().revocation_sequence == 5

    replayed = read_fleet_status(old, trust, high_water=state)
    assert not replayed.fresh
    assert any("refusing a replayed list" in p for p in replayed.problems)
    assert state.load().revocation_sequence == 5  # never lowered
    # The same sequence again is fine; without state there is no check.
    assert read_fleet_status(new, trust, high_water=state).fresh
    assert read_fleet_status(old, trust).fresh


def test_status_high_water_refuses_older_kill_switch_history(tmp_path, core):
    trust = core.trust
    state = StatusHighWater.in_dir(tmp_path / "tenant")
    feed = _status_feed(core, tmp_path / "feed", sequence=1)
    _kill(core, feed, sequence=3, disable=True, reason="incident")
    status = read_fleet_status(feed, trust, high_water=state)
    assert status.fresh and status.auto_promotion_disabled
    assert state.load().kill_switch_sequence == 3

    # A feed that dropped the newest entry would turn auto-promotion back on.
    rolled_back = _status_feed(core, tmp_path / "rolled-back", sequence=1)
    _kill(core, rolled_back, sequence=2, disable=False, reason="resume")
    status = read_fleet_status(rolled_back, trust, high_water=state)
    assert not status.fresh
    assert any("kill-switch history" in p for p in status.problems)
    no_entries = _status_feed(core, tmp_path / "none", sequence=1)
    assert not read_fleet_status(no_entries, trust, high_water=state).fresh
    assert state.load().kill_switch_sequence == 3


def _blocked_while_locked(lock_path: Path, action) -> None:
    """Run ``action`` in a thread while this test holds ``lock_path``.

    Fails unless ``action`` waits for the lock, then finishes once it is
    released. Uses fcntl directly so it does not depend on the code's helper.
    """
    fcntl = pytest.importorskip("fcntl")
    import threading

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    errors: list[BaseException] = []

    def run():
        try:
            action()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    worker = threading.Thread(target=run)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        worker.start()
        worker.join(0.5)
        assert worker.is_alive(), "the update ran without waiting for the lock"
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
    worker.join(10)
    assert not worker.is_alive() and not errors, errors


def test_status_state_update_waits_for_the_lock(tmp_path):
    state = StatusHighWater(tmp_path / "tenant" / STATUS_STATE_FILE)
    lock = state.path.with_name(state.path.name + ".lock")
    _blocked_while_locked(
        lock,
        lambda: state.advance(
            revocation_sequence=4,
            kill_switch_sequence=2,
            now=datetime.now(timezone.utc),
        ),
    )
    assert state.load().revocation_sequence == 4


def test_hub_state_updates_wait_for_the_lock(core):
    key, chain = _contributor(core, "t-locked")
    registration = register_fingerprint(sim.app_fingerprint("7.1"), key, chain)
    lock = core.hub.root / "state.lock"
    _blocked_while_locked(lock, lambda: core.hub.register(registration))
    assert core.hub.tenants_on(sim.app_fingerprint("7.1").fingerprint_sha256()) == 1
    before = core.hub._state()["status_sequence"]
    _blocked_while_locked(lock, core.hub.issue_revocation_list)
    assert core.hub._state()["status_sequence"] == before + 1
    _blocked_while_locked(lock, lambda: core.hub.revoke_contributor("t-locked"))
    assert "t-locked" in core.hub._state()["revoked_subjects"]


@pytest.mark.parametrize(
    "damage",
    [
        "not json",
        '{"schema_version": "openadapt.fleet.status-state/v1", '
        '"revocation_sequence": -1}',
        '{"revocation_sequence": 1, "unexpected": true}',
        "directory",
    ],
)
def test_unreadable_status_state_fails_closed(tmp_path, core, damage):
    path = tmp_path / "tenant" / STATUS_STATE_FILE
    path.parent.mkdir()
    if damage == "directory":
        path.mkdir()
    else:
        path.write_text(damage)
    state = StatusHighWater(path)
    with pytest.raises(FleetStatusStateError):
        state.load()
    status = read_fleet_status(core.feed, core.trust, high_water=state)
    assert not status.fresh
    assert any("unreadable" in p for p in status.problems)
    # The damaged file is left for an admin; it is not overwritten.
    if damage != "directory":
        assert path.read_text() == damage


def test_auto_promotion_needs_persisted_status_state(fleet_with_patch, tmp_path):
    fleet, patch = fleet_with_patch
    # The tenant already accepted revocation list 7; the feed now serves 1.
    tenant = _receiver(fleet, "status-replayed")
    tenant.status_state.advance(
        revocation_sequence=7, kill_switch_sequence=0, now=datetime.now(timezone.utc)
    )
    feed = _status_feed(fleet, tmp_path / "replayed", sequence=1)
    decision = _receive(fleet, tenant, patch, status_source=feed)
    assert decision.outcome == "needs_person"
    assert any("replayed" in r for r in decision.reasons)
    assert tenant.store.active_pointer() is None

    # Damaged state: auto-promotion stops even with a fresh, newer list.
    damaged = _receiver(fleet, "status-damaged")
    damaged.status_state.path.parent.mkdir(parents=True, exist_ok=True)
    damaged.status_state.path.write_text("{")
    feed = _status_feed(fleet, tmp_path / "fresh", sequence=9)
    decision = _receive(fleet, damaged, patch, status_source=feed)
    assert decision.outcome == "needs_person"
    assert any("unreadable" in r for r in decision.reasons)
    assert damaged.store.active_pointer() is None


def test_auto_promotion_aborts_without_fresh_status_or_under_a_kill_switch(
    fleet_with_patch, tmp_path
):
    fleet, patch = fleet_with_patch
    # No revocation list reachable: the canary runs, a person decides.
    unreachable = _receiver(fleet, "status-missing")
    decision = _receive(
        fleet, unreachable, patch, status_source=FilePatchFeed(tmp_path / "none")
    )
    assert decision.outcome == "needs_person"
    assert any("unreachable" in r for r in decision.reasons)
    assert decision.candidate.replay_campaign is not None
    assert decision.candidate.approval is None
    assert unreachable.store.active_pointer() is None

    # A verified kill switch turned auto-promotion off fleet-wide.
    feed = _status_feed(fleet, tmp_path / "killed")
    _kill(fleet, feed, disable=True, reason="incident")
    paused = _receiver(fleet, "status-killed")
    decision = _receive(fleet, paused, patch, status_source=feed)
    assert decision.outcome == "needs_person"
    assert any("kill switch" in r for r in decision.reasons)
    # A person can still approve through the ordinary path.
    store = paused.store
    store.review(decision.candidate.candidate_id, PERSON)
    assert store.load_candidate(decision.candidate.candidate_id).state == "reviewed"


@pytest.mark.parametrize("level", ["root", "intermediate", "serial"])
def test_a_revoked_key_anywhere_in_the_chain_stops_auto_promotion(
    fleet_with_patch, tmp_path, level
):
    fleet, patch = fleet_with_patch
    tenant = _receiver(fleet, f"revoked-{level}")
    signer, chain = fleet.hub._status
    now = datetime.now(timezone.utc)
    keys = {
        "root": patch.chain.root_key_id,
        "intermediate": patch.chain.intermediate_key_id,
    }
    feed = FilePatchFeed(tmp_path / level)
    feed.publish_revocation_list(
        sign_revocation_list(
            RevocationListPayload(
                sequence=1,
                issued_at=now.isoformat(),
                next_update=(now + timedelta(hours=1)).isoformat(),
                revoked_key_ids=(keys[level],) if level in keys else (),
                revoked_serials=(patch.chain.serial,) if level == "serial" else (),
            ),
            signer,
            chain,
        )
    )
    decision = _receive(fleet, tenant, patch, status_source=feed)
    assert decision.outcome == "needs_person"
    assert any("the patch's chain" in r for r in decision.reasons), decision.reasons
    assert tenant.store.active_pointer() is None


def test_kill_switch_rolls_back_a_named_patch(fleet_with_patch, tmp_path):
    fleet, patch = fleet_with_patch
    tenant = _receiver(fleet, "east-kill")
    feed = _status_feed(fleet, tmp_path / "status")
    decision = _receive(fleet, tenant, patch, status_source=feed)
    assert decision.outcome == "auto_promoted", decision.reasons
    pointer = tenant.store.active_pointer()
    assert pointer is not None and pointer.mode == "canary"

    _kill(fleet, feed, rollback=[patch.patch_id])
    status = read_fleet_status(feed, tenant.trust)
    changed = apply_fleet_kill_switches(tenant.store, status)
    assert changed == [f"{decision.candidate.candidate_id}: rolled back"]
    candidate = tenant.store.load_candidate(decision.candidate.candidate_id)
    assert candidate.state == "rolled_back"
    assert tenant.store.active_pointer().active_digest == candidate.prior_content_digest
    # The same patch is refused on import from now on.
    with pytest.raises(PatchImportError, match="kill switch"):
        import_patch(
            tenant.store,
            patch,
            trust=tenant.trust,
            opt_in=tenant.opt_in,
            status=status,
            tenant_bundle=tenant.bundle,
            tenant_app=tenant.app(),
            out_dir=tenant.root / "bundles" / "again",
        )


# --------------------------------------------------------------------------- #
# Signed policy and role separation
# --------------------------------------------------------------------------- #


def test_policy_must_be_signed_by_an_approver_who_is_not_an_author(fleet_with_patch):
    fleet, _ = fleet_with_patch
    tenant = _receiver(fleet, "roles-check")
    policy = verify_policy(tenant.policy, tenant.roles)
    assert policy.approver.name and policy.approver.title
    assert policy.security_official.statement.startswith(
        "Our organisation's security official"
    )

    # Changed after signing.
    flipped = tenant.policy.model_copy(
        update={"policy": policy.model_copy(update={"min_healthy_cases": 1000})}
    )
    with pytest.raises(PolicyVerificationError):
        verify_policy(flipped, tenant.roles)
    # Signed by someone not registered as a policy approver.
    stranger = SoftwareKeyCustody.generate()
    with pytest.raises(PolicyVerificationError):
        verify_policy(sign_policy(policy, stranger), tenant.roles)
    # The approver cannot also author workflows, in the registry ...
    person = policy.approver.person_id
    with pytest.raises(ValueError, match="workflow author"):
        RoleRegistry(
            workflow_authors=(person,),
            policy_approvers=dict(tenant.roles.policy_approvers),
        )
    # ... or by naming an author's id in the policy.
    author = tenant.roles.workflow_authors[0]
    as_author = policy.model_copy(
        update={"approver": policy.approver.model_copy(update={"person_id": author})}
    )
    with pytest.raises(PolicyVerificationError, match="workflow author"):
        verify_policy(sign_policy(as_author, tenant.policy_key), tenant.roles)
    # The security official is a different person from the approver.
    with pytest.raises(ValueError, match="security official"):
        type(policy).model_validate(
            {
                **policy.model_dump(),
                "security_official": {
                    **policy.security_official.model_dump(),
                    "official_name": policy.approver.name,
                },
            }
        )


def test_role_registry_and_policy_refuse_malformed_entries():
    good = SoftwareKeyCustody.generate().public_key_b64()
    for bad in ("not-base64!", "AAAA"):
        with pytest.raises(ValueError, match="public key"):
            RoleRegistry(policy_approvers={"pat": bad})
    with pytest.raises(ValueError, match="person id"):
        RoleRegistry(policy_approvers={"Pat Lee": good})
    assert RoleRegistry(policy_approvers={"pat": good}).policy_approvers
    policy = sim._policy_for(
        "t",
        auto_promote=True,
        app_families=(sim.APP_FAMILY,),
        healthy_cases=1,
        fault_cases=1,
        now=datetime.now(timezone.utc),
    )
    with pytest.raises(ValueError, match="app family"):
        type(policy).model_validate(
            {**policy.model_dump(), "app_families": ["Not A Family"]}
        )


def test_auto_promotion_only_under_policy(fleet_with_patch):
    fleet, patch = fleet_with_patch

    east = _receiver(fleet, "east")
    decision = _receive(fleet, east, patch)
    assert decision.outcome == "auto_promoted", decision.reasons
    candidate = decision.candidate
    policy = east.policy.policy
    assert candidate.state == "canary"
    assert candidate.approval.pre_approved_by == policy.approved_by
    assert "Chief Medical Information Officer" in candidate.approval.pre_approved_by
    assert candidate.approval.pre_approval_policy_sha256 == policy.policy_sha256()
    assert (
        candidate.approval.proposed_content_digest == candidate.proposed_content_digest
    )
    assert east.store.active_pointer().mode == "canary"
    # Only the structural locator changed; east's own identity band stayed.
    patched = Workflow.load(Path(candidate.proposed_bundle_path))
    assert patched.steps[1].anchor.context_text == east.member_band
    assert patched.steps[1].anchor.structural.selector == "button[name=run-eligibility]"
    assert all(not change.identity for change in candidate.binding_changes)
    # Provenance names the hub's chain, not the contributing tenant.
    assert candidate.fleet_patch.signer_key_id == patch.chain.signer_key_id
    assert "north" not in candidate.model_dump_json()

    off = _receiver(fleet, "policy-off", auto_promote=False)
    decision = _receive(fleet, off, patch)
    assert decision.outcome == "needs_person"
    assert decision.candidate.state == "candidate"
    assert decision.candidate.replay_campaign.passed  # the canary still ran
    assert off.store.active_pointer() is None

    other_family = _receiver(fleet, "other-family", app_families=("sim-ehr/chart",))
    decision = _receive(fleet, other_family, patch)
    assert decision.outcome == "needs_person"
    assert other_family.store.active_pointer() is None

    unsigned = _receiver(fleet, "policy-tampered")
    unsigned.policy = unsigned.policy.model_copy(
        update={
            "policy": unsigned.policy.policy.model_copy(update={"canary_max_runs": 1})
        }
    )
    decision = _receive(fleet, unsigned, patch)
    assert decision.outcome == "needs_person"
    assert any("does not verify" in r for r in decision.reasons)

    too_few = _receiver(fleet, "too-few-cases")
    too_few.policy = sign_policy(
        too_few.policy.policy.model_copy(update={"min_healthy_cases": 5}),
        too_few.policy_key,
    )
    decision = _receive(fleet, too_few, patch)
    assert decision.outcome == "needs_person"
    assert decision.candidate.approval is None
    assert too_few.store.active_pointer() is None
    # A person can still approve it through the ordinary path.
    approved = too_few.store.approve(decision.candidate.candidate_id, PERSON)
    assert approved.state == "approved"
    assert approved.approval.pre_approval_policy_sha256 is None


class _FlipAfterFirstRead:
    """Fleet status that changes after the first revocation-list read."""

    def __init__(self, first, then):
        self.sources, self.reads = (first, then), 0

    def _current(self):
        return self.sources[min(self.reads, 1)]

    def revocation_list(self):
        current = self._current()
        self.reads += 1
        return current.revocation_list()

    def kill_switch_documents(self):
        return self.sources[min(self.reads - 1, 1)].kill_switch_documents()


def test_approve_under_policy_verifies_policy_and_status_itself(
    fleet_with_patch, tmp_path
):
    fleet, patch = fleet_with_patch
    tenant = _receiver(fleet, "direct-approve")
    fresh = _status_feed(fleet, tmp_path / "fresh")
    killed = _status_feed(fleet, tmp_path / "killed")
    _kill(fleet, killed, disable=True, reason="incident")
    # Fresh for the first check, killed by the time the store approves.
    decision = _receive(
        fleet, tenant, patch, status_source=_FlipAfterFirstRead(fresh, killed)
    )
    assert decision.outcome == "needs_person"
    assert any("kill switch" in r for r in decision.reasons), decision.reasons
    cid = decision.candidate.candidate_id
    store = tenant.store
    assert store.load_candidate(cid).state == "fault_passed"

    # A grant on its own (the old API) approves nothing.
    with pytest.raises((TypeError, RepairLifecycleError)):
        store.approve_under_policy(cid, tenant.policy.policy.grant())
    refusals = {
        "kill switch": _authority(fleet, tenant, status_source=killed),
        "unreachable": _authority(
            fleet, tenant, status_source=FilePatchFeed(tmp_path / "none")
        ),
        "persisted": {**_authority(fleet, tenant), "status_state": None},
        "does not verify": {
            **_authority(fleet, tenant),
            "policy": tenant.policy.model_copy(
                update={
                    "policy": tenant.policy.policy.model_copy(
                        update={"canary_max_runs": 1}
                    )
                }
            ),
        },
        "healthy case": {
            **_authority(fleet, tenant),
            "policy": sign_policy(
                tenant.policy.policy.model_copy(update={"min_healthy_cases": 50}),
                tenant.policy_key,
            ),
        },
    }
    for match, authority in refusals.items():
        with pytest.raises(RepairLifecycleError, match=match):
            store.approve_under_policy(cid, **authority)
        assert store.load_candidate(cid).state == "fault_passed"
        assert store.load_candidate(cid).approval is None

    # Once a later entry turns auto-promotion back on, the same call approves.
    _kill(fleet, killed, sequence=3, disable=False, reason="resume")
    approved = store.approve_under_policy(
        cid, **_authority(fleet, tenant, status_source=killed)
    )
    assert approved.state == "approved"
    assert approved.approval.pre_approval_policy_sha256 is not None


def test_policy_grant_never_reviews_a_non_fleet_repair(tmp_path, fleet_with_patch):
    fleet, _ = fleet_with_patch
    _, candidate, prior, proposed = _approved(tmp_path, new_search="#chart-link")
    other = RepairStore(tmp_path / "s2")
    pending = other.add_candidate(build_candidate(prior, proposed, source="heal"))
    tenant = _receiver(fleet, "grant-check")
    with pytest.raises(RepairLifecycleError):
        other.review_under_policy(
            pending.candidate_id, policy=tenant.policy, roles=tenant.roles
        )
    with pytest.raises(RepairLifecycleError):
        other.approve_under_policy(pending.candidate_id, **_authority(fleet, tenant))


def test_identity_anchor_patch_always_needs_a_person(tmp_path):
    root = tmp_path.resolve()
    with sim._private_durable_authority(root):
        fleet = sim.build_fleet_core(root)
        _bystanders(fleet)
        exported, distributed, _, _ = _source_patch(fleet, change_identity=True)
        payload = distributed.patch.payload
        assert payload.identity_evidence_changed
        assert exported.notes
        flagged = [m for m in payload.maps if m.touches_identity]
        assert flagged and flagged[0].identity_fields_changed == ("context_text",)
        assert "lovelace" not in distributed.patch.to_json().lower()

        east = _receiver(fleet, "east", fault_kinds=("wrong_entity",))
        decision = sim.receive(fleet, east, distributed.patch)
    assert decision.outcome == "needs_person"
    assert any("identity" in reason for reason in decision.reasons)
    candidate = decision.candidate
    assert candidate.fleet_patch.identity_evidence_changed
    assert candidate.state == "candidate"
    assert candidate.replay_campaign is not None and candidate.replay_campaign.passed
    with pytest.raises(RepairLifecycleError, match="identity"):
        east.store.review_under_policy(
            candidate.candidate_id, policy=east.policy, roles=east.roles
        )
    with pytest.raises(RepairLifecycleError):
        east.store.approve_under_policy(
            candidate.candidate_id, **_authority(fleet, east)
        )
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
            opt_in=east.opt_in,
            tenant_bundle=east.bundle,
            tenant_app=east.app(),
            out_dir=east.root / "bundles" / "patched",
        )
        decision = process_imported_patch(
            east.store,
            result.candidate.candidate_id,
            policy=east.policy,
            roles=east.roles,
            trust=east.trust,
            status_source=fleet.feed,
            status_state=east.status_state,
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
        east.store.approve_under_policy(
            candidate.candidate_id, **_authority(fleet, east)
        )
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
# Feed, labels, CLI
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


def test_fleet_cli_opt_in_roles_and_policy(tmp_path, capsys):
    from openadapt_flow.__main__ import main

    fleet_dir = str(tmp_path / "fleet")
    run = lambda *argv: main(["fleet", "--fleet-dir", fleet_dir, *argv])  # noqa: E731

    assert run("status", "--json") == 0
    status = json.loads(capsys.readouterr().out)
    assert status["export"] == status["import"] == "off for every app"

    assert (
        run("allow", "--export", "--app-family", sim.APP_FAMILY, "--admin", "Ops") == 0
    )
    opt_in = FleetOptIn.load(tmp_path / "fleet" / "opt-in.json")
    assert opt_in.allows("export", sim.APP_FAMILY)
    assert not opt_in.allows("import", sim.APP_FAMILY)

    assert run("policy", "keygen", "--out", str(tmp_path / "k.pem")) == 0
    public = capsys.readouterr().out.strip().splitlines()[-1]
    assert run("roles", "--author", "dana") == 0
    with pytest.raises(SystemExit):
        run("roles", "--approver", "dana", "--public-key", public)
    assert run("roles", "--approver", "pat", "--public-key", public) == 0
    draft = tmp_path / "draft.json"
    draft.write_text(
        json.dumps({"auto_promote": True, "app_families": [sim.APP_FAMILY]})
    )
    sign = [
        "policy",
        "sign",
        "--draft",
        str(draft),
        "--key",
        str(tmp_path / "k.pem"),
        "--approver-name",
        "Pat Lee",
        "--approver-title",
        "CMIO",
        "--security-official-name",
        "Sam Roe",
        "--security-official-title",
        "HIPAA Security Official",
    ]
    with pytest.raises(SystemExit, match="attest"):
        run(*sign, "--approver-id", "pat")
    with pytest.raises(SystemExit, match="workflow author"):
        run(*sign, "--approver-id", "dana", "--attest-security-official")
    assert run(*sign, "--approver-id", "pat", "--attest-security-official") == 0
    capsys.readouterr()
    assert run("policy", "verify") == 0
    assert "approved by Pat Lee (CMIO)" in capsys.readouterr().out
