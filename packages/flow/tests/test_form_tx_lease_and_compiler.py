"""Comprehensive tests for form_tx session lease, queue runner, pack compiler, and CLI.

Covers WarmSessionLease auth wall detection, source-suppressed browser auth
handoffs, fail-closed landmark verification, zero-mutation heartbeats,
FormTxQueueRunner idempotency and conflict detection, deterministic cross-site
FormPack induction with PII/volatile-ID scrubbing, and CLI subcommands.
Zero application, vendor, or customer names.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional, Sequence

import pytest
from pydantic import ValidationError

from openadapt_flow.form_tx.cli import main as cli_main
from openadapt_flow.form_tx.pack_compiler import (
    CanonicalDomainSchema,
    CanonicalFieldSpec,
    build_patient_registration_domain_schema,
    build_property_quote_domain_schema,
    induce_form_pack,
)
from openadapt_flow.form_tx.session_lease import (
    AuthHandoffError,
    BrowserAuthHandoffMarker,
    FormTxQueueRunner,
    HeartbeatMutationError,
    LeaseState,
    QueueJobSpec,
    RequestIdConflictError,
    SessionLeaseStateError,
    WarmSessionLease,
)
from openadapt_flow.form_tx.surface import (
    FormSurfaceBackend,
    match_anchor_in_snapshot,
)
from openadapt_flow.form_tx.types import (
    ControlKind,
    DossierFact,
    FieldAnchorSpec,
    FieldCodecSpec,
    FormCodecKind,
    FormPack,
    FormSnapshot,
    FormTxOutcome,
    FormTxResult,
    InputDossier,
    ObservedControl,
    ObservedEntityRow,
    ObservedOption,
    PopupSubroutineSpec,
    ProvenanceKind,
    UIProfileSpec,
    canonical_json_bytes,
)


class DeterministicFakeSurface:
    """In-memory deterministic implementation of ``FormSurfaceBackend`` for unit tests."""

    def __init__(
        self,
        *,
        ui_profile: Optional[UIProfileSpec] = None,
        url: str = "about:blank",
        auth_wall_present: bool = False,
        authenticated_landmark_present: bool = False,
        controls: Sequence[ObservedControl] = (),
        keepalive_status: int = 200,
        keepalive_mutates: bool = False,
        snapshot_mutates: bool = False,
    ) -> None:
        self._ui_profile = ui_profile or UIProfileSpec(
            profile_id="test_portal_profile",
            authenticated_landmark_selectors=("[data-workspace-ready='true']",),
            keepalive_path="/api/keepalive",
        )
        self.current_url = url
        self.auth_wall_present = auth_wall_present
        self.authenticated_landmark_present = authenticated_landmark_present
        self.controls: tuple[ObservedControl, ...] = tuple(controls)
        self.keepalive_status = keepalive_status
        self.keepalive_mutates = keepalive_mutates
        self.snapshot_mutates = snapshot_mutates
        self._mutation_count = 0
        self.goto_history: list[str] = []
        self.filled_fields: list[tuple[str, str]] = []
        self.keepalive_pings: list[str] = []

    @property
    def ui_profile(self) -> UIProfileSpec:
        return self._ui_profile

    @property
    def mutation_count(self) -> int:
        return self._mutation_count

    @property
    def confirmed_mutation_count(self) -> int:
        return self._mutation_count

    def has_confirmed_mutation_since(self, since_mutation_count: int) -> bool:
        return self._mutation_count > since_mutation_count

    def reset_mutation_counter(self) -> None:
        self._mutation_count = 0

    def snapshot(
        self,
        *,
        entity_tables: Sequence[object] = (),
        modal_rules: Sequence[object] = (),
        screens: Sequence[object] = (),
    ) -> FormSnapshot:
        if self.snapshot_mutates:
            self._mutation_count += 1
        return FormSnapshot(
            url_origin_and_path=self.current_url,
            title="Test Surface",
            auth_wall_present=self.auth_wall_present,
            authenticated_landmark_present=self.authenticated_landmark_present,
            controls=self.controls,
        )

    def goto(self, url_or_path: str) -> None:
        self.goto_history.append(url_or_path)
        if self.auth_wall_present:
            self.current_url = (
                "https://portal.example/auth/login?next=/intake&nonce=secret123"
            )
        else:
            self.current_url = url_or_path

    def fill_text(self, dom_selector: str, value: str) -> None:
        self.filled_fields.append((dom_selector, value))

    def select_native_option(self, dom_selector: str, option_value: str) -> None:
        self.filled_fields.append((dom_selector, option_value))

    def set_checkbox_or_radio(self, dom_selector: str, checked: bool = True) -> None:
        self.filled_fields.append((dom_selector, str(checked)))

    def open_portal_combobox_options(
        self, dom_selector: str
    ) -> tuple[ObservedOption, ...]:
        return ()

    def choose_portal_combobox_option(
        self, dom_selector: str, option: ObservedOption
    ) -> None:
        self.filled_fields.append((dom_selector, option.value))

    def click(self, dom_selector: str) -> None:
        self._mutation_count += 1

    @contextmanager
    def open_popup_surface(
        self,
        trigger_selector: str,
        *,
        url_pattern: Optional[str] = None,
        title_pattern: Optional[str] = None,
    ) -> Iterator[FormSurfaceBackend]:
        yield self

    def ping_keepalive(self, path: str) -> int:
        self.keepalive_pings.append(path)
        if self.keepalive_mutates:
            self._mutation_count += 1
        return self.keepalive_status


def _make_clock() -> tuple[list[datetime], object]:
    cursor = [datetime(2026, 11, 19, 18, 0, 0, tzinfo=timezone.utc)]

    def _tick() -> datetime:
        now = cursor[0]
        cursor[0] = now + timedelta(seconds=5)
        return now

    return cursor, _tick


def _make_sample_dossier(
    request_id: str = "req-1001",
    first_name: str = "Clara",
    last_name: str = "Oswald",
) -> InputDossier:
    return InputDossier(
        dossier_id=f"dos-{request_id}",
        request_id=request_id,
        facts={
            "first_name": DossierFact(
                key="first_name",
                value=first_name,
                provenance=ProvenanceKind.INTAKE_DOCUMENT,
                source_id="intake-packet-1",
            ),
            "last_name": DossierFact(
                key="last_name",
                value=last_name,
                provenance=ProvenanceKind.INTAKE_DOCUMENT,
                source_id="intake-packet-1",
            ),
        },
    )


def _make_sample_pack(pack_id: str = "pack-intake-v1") -> FormPack:
    return FormPack(
        pack_id=pack_id,
        domain_schema="patient_registration.v1",
        ui_profile_id="test_portal_profile",
    )


# ---------------------------------------------------------------------------
# 1. WarmSessionLease & BrowserAuthHandoffMarker Tests
# ---------------------------------------------------------------------------


def test_browser_auth_handoff_marker_privacy_invariant() -> None:
    assert isinstance(DeterministicFakeSurface(), FormSurfaceBackend)

    forbidden = {
        "username",
        "password",
        "otp",
        "vault_item",
        "account_name",
        "account_id",
        "provider_name",
        "secret",
        "token",
    }
    assert not (forbidden & set(BrowserAuthHandoffMarker.model_fields))

    # Passing any extra credential field is rejected fail-closed by Pydantic
    with pytest.raises(ValidationError):
        BrowserAuthHandoffMarker(
            interval_id="550e8400-e29b-41d4-a716-446655440000",
            methods=("password_manager",),
            requires_user_presence=True,
            saved_account_selected=True,
            started_at_utc="2026-11-19T18:00:00Z",
            ended_at_utc="2026-11-19T18:00:10Z",
            outcome="completed",
            pre_handoff_url_path="https://portal.example/auth/login",
            username="operator@example.org",  # type: ignore[call-arg]
        )

    # Query tokens or fragments in URL paths are rejected
    with pytest.raises(ValidationError):
        BrowserAuthHandoffMarker(
            interval_id="550e8400-e29b-41d4-a716-446655440000",
            methods=("sso",),
            requires_user_presence=True,
            saved_account_selected=False,
            started_at_utc="2026-11-19T18:00:00Z",
            ended_at_utc="2026-11-19T18:00:10Z",
            outcome="completed",
            pre_handoff_url_path="https://portal.example/sso?code=secret_oauth_code",
        )


def test_warm_session_lease_auth_wall_fail_closed_and_verified_handoff(
    tmp_path: Path,
) -> None:
    _, clock_fn = _make_clock()
    surface = DeterministicFakeSurface(
        auth_wall_present=True,
        authenticated_landmark_present=False,
    )
    ledger_path = tmp_path / "handoffs" / "browser_handoffs.json"
    lease = WarmSessionLease(
        surface,
        entry_url="https://portal.example/intake/new",
        clock_fn=clock_fn,  # type: ignore[arg-type]
        handoff_ledger_path=ledger_path,
    )
    assert lease.state == LeaseState.UNINITIALIZED

    # Preflight detects auth wall and transitions to AUTH_REQUIRED
    assert lease.preflight_auth() == LeaseState.AUTH_REQUIRED
    assert lease.state == LeaseState.AUTH_REQUIRED
    assert len(lease.persisted_snapshots) == 0

    # Open protected auth handoff
    interval_1 = lease.begin_auth_handoff(
        methods=("password_manager", "mfa"),
        requires_user_presence=True,
        saved_account_selected=True,
    )
    assert lease.state == LeaseState.AUTH_HANDOFF_OPEN
    assert lease.snapshot_persistence_suppressed is True

    # Nested handoffs are refused
    with pytest.raises(AuthHandoffError, match="Nested"):
        lease.begin_auth_handoff()

    # Snapshot persistence is suppressed while handoff is open
    login_snap = FormSnapshot(
        url_origin_and_path="https://portal.example/auth/mfa",
        auth_wall_present=False,
        authenticated_landmark_present=False,
        controls=(
            ObservedControl(
                control_id="otp",
                dom_selector="#otp",
                label_text="One-Time Code",
                current_value="492817",
            ),
        ),
    )
    assert lease.persist_snapshot(login_snap) is False
    assert len(lease.persisted_snapshots) == 0

    # Wrong interval_id is refused
    with pytest.raises(AuthHandoffError, match="Mismatched"):
        lease.end_auth_handoff("00000000-0000-0000-0000-000000000000")

    # Attempt 1: Operator closes handoff as "completed" while STILL on login/2FA screen!
    # Must fail-closed: refuse READY, stay AUTH_REQUIRED, mark verified=False.
    failed_marker = lease.end_auth_handoff(interval_1, outcome="completed")
    assert lease.state == LeaseState.AUTH_REQUIRED
    assert failed_marker.authenticated_landmark_verified is False
    assert failed_marker.outcome == "completed"
    # Query parameters were stripped from pre_handoff_url_path
    assert "?" not in failed_marker.pre_handoff_url_path
    assert "secret123" not in failed_marker.pre_handoff_url_path
    assert failed_marker.pre_handoff_url_path == "https://portal.example/auth/login"

    # Attempt 2: Auth wall clears, but authenticated landmark is STILL absent -> still AUTH_REQUIRED
    interval_2 = lease.begin_auth_handoff(methods=("mfa",))
    surface.auth_wall_present = False
    surface.authenticated_landmark_present = False
    surface.current_url = "https://portal.example/auth/pending?token=xyz"
    no_landmark_marker = lease.end_auth_handoff(interval_2, outcome="completed")
    assert lease.state == LeaseState.AUTH_REQUIRED
    assert no_landmark_marker.authenticated_landmark_verified is False
    assert (
        no_landmark_marker.post_handoff_url_path
        == "https://portal.example/auth/pending"
    )

    # Attempt 3: Operator completes login + landmark appears -> transitions to READY!
    interval_3 = lease.begin_auth_handoff(
        methods=("password_manager", "mfa"),
        requires_user_presence=True,
        saved_account_selected=True,
    )
    surface.auth_wall_present = False
    surface.authenticated_landmark_present = True
    surface.current_url = "https://portal.example/intake/new?session=hidden"

    ok_marker = lease.end_auth_handoff(interval_3, outcome="completed")
    assert lease.state == LeaseState.READY
    assert ok_marker.authenticated_landmark_verified is True
    assert ok_marker.post_handoff_url_path == "https://portal.example/intake/new"
    assert ok_marker.suppressed_sources == (
        "dom_values",
        "screenshots",
        "network_bodies",
        "input_events",
    )
    assert len(lease.handoff_ledger) == 3
    assert len(lease.persisted_snapshots) == 1
    assert ledger_path.is_file()
    persisted_raw = ledger_path.read_text(encoding="utf-8")
    assert "492817" not in persisted_raw
    assert "secret123" not in persisted_raw
    assert "hidden" not in persisted_raw


def test_warm_session_lease_zero_mutation_heartbeat_and_fault_detection() -> None:
    surface = DeterministicFakeSurface(
        url="https://portal.example/intake/new",
        auth_wall_present=False,
        authenticated_landmark_present=True,
    )
    lease = WarmSessionLease(surface, entry_url="https://portal.example/intake/new")

    # Heartbeat not allowed while UNINITIALIZED
    with pytest.raises(SessionLeaseStateError):
        lease.heartbeat()

    assert lease.preflight_auth() == LeaseState.READY

    # Clean zero-mutation keepalive returns True and stays READY
    assert lease.heartbeat() is True
    assert lease.state == LeaseState.READY
    assert surface.keepalive_pings == ["/api/keepalive"]
    assert surface.mutation_count == 0

    # Heartbeat prohibited during BUSY_RUNNING_JOB
    with lease.acquire_job_execution():
        assert lease.state == LeaseState.BUSY_RUNNING_JOB
        with pytest.raises(SessionLeaseStateError, match="READY"):
            lease.heartbeat()
    assert lease.state == LeaseState.READY

    # HTTP 401 on keepalive transitions lease to AUTH_REQUIRED and returns False
    surface.keepalive_status = 401
    assert lease.heartbeat() is False
    assert lease.state == LeaseState.AUTH_REQUIRED

    # Restore READY, then test fail-closed HeartbeatMutationError when keepalive mutates state
    surface.keepalive_status = 200
    assert lease.preflight_auth() == LeaseState.READY
    surface.keepalive_mutates = True
    with pytest.raises(HeartbeatMutationError, match="mutated surface state"):
        lease.heartbeat()
    assert lease.state == LeaseState.FAULTED

    # A faulted lease stays FAULTED on preflight (fail closed)
    assert lease.preflight_auth() == LeaseState.FAULTED


def test_heartbeat_landmark_fallback_and_unexpected_http_status() -> None:
    # Profile without keepalive_path uses landmark snapshot heartbeat
    profile_no_keepalive = UIProfileSpec(
        profile_id="landmark_only_profile",
        authenticated_landmark_selectors=("[data-workspace-ready='true']",),
        keepalive_path=None,
    )
    surface = DeterministicFakeSurface(
        ui_profile=profile_no_keepalive,
        url="https://portal.example/workspace",
        auth_wall_present=False,
        authenticated_landmark_present=True,
    )
    lease = WarmSessionLease(surface, entry_url="https://portal.example/workspace")
    assert lease.preflight_auth() == LeaseState.READY
    assert lease.heartbeat() is True

    # Auth wall appearing mid-shift flips heartbeat to False + AUTH_REQUIRED
    surface.auth_wall_present = True
    assert lease.heartbeat() is False
    assert lease.state == LeaseState.AUTH_REQUIRED

    # Unexpected HTTP 500 on keepalive raises HeartbeatMutationError -> FAULTED
    surface_500 = DeterministicFakeSurface(
        url="https://portal.example/workspace",
        authenticated_landmark_present=True,
        keepalive_status=500,
    )
    lease_500 = WarmSessionLease(
        surface_500, entry_url="https://portal.example/workspace"
    )
    assert lease_500.preflight_auth() == LeaseState.READY
    with pytest.raises(HeartbeatMutationError, match="500"):
        lease_500.heartbeat()
    assert lease_500.state == LeaseState.FAULTED


# ---------------------------------------------------------------------------
# 2. FormTxQueueRunner Idempotency, Mid-Shift Expiry & Conflict Tests
# ---------------------------------------------------------------------------


def test_queue_runner_idempotency_mid_shift_expiry_and_conflict(tmp_path: Path) -> None:
    surface = DeterministicFakeSurface(
        url="https://portal.example/intake/new",
        auth_wall_present=False,
        authenticated_landmark_present=True,
    )
    lease = WarmSessionLease(surface, entry_url="https://portal.example/intake/new")
    pack = _make_sample_pack("pack-reg-1")
    ledger_dir = tmp_path / "queue_ledger"

    executions: list[str] = []

    def executor_fn(
        backend: FormSurfaceBackend, active_pack: FormPack, dossier: InputDossier
    ) -> FormTxResult:
        # While executing, lease must be in BUSY_RUNNING_JOB
        assert lease.state == LeaseState.BUSY_RUNNING_JOB
        executions.append(dossier.request_id)
        backend.fill_text("#first_name", str(dossier.facts["first_name"].value))
        return FormTxResult(
            run_id=f"run-{len(executions)}",
            request_id=dossier.request_id,
            outcome=FormTxOutcome.VERIFIED_DONE,
            safe_to_retry=False,
            fields_filled_auto=2,
            outputs={"registration_id": "REG-9001"},
            what_happened="Verified registration on summary screen.",
        )

    runner = FormTxQueueRunner(
        lease,
        executor_fn,
        packs={pack.pack_id: pack},
        ledger_dir=ledger_dir,
    )

    dossier_1 = _make_sample_dossier(
        request_id="req-alpha", first_name="Ada", last_name="Lovelace"
    )
    job_1 = QueueJobSpec(
        job_id="job-001",
        request_id="req-alpha",
        pack_id=pack.pack_id,
        dossier=dossier_1,
    )

    # First run executes once and returns VERIFIED_DONE
    res_1 = runner.run_job(job_1)
    assert res_1.outcome == FormTxOutcome.VERIFIED_DONE
    assert res_1.outputs["registration_id"] == "REG-9001"
    assert executions == ["req-alpha"]

    # Second run with identical request_id + digests returns cached VERIFIED_DONE without re-executing
    res_1_dup = runner.run_job(job_1, retry_safe_failures=True)
    assert res_1_dup.run_id == res_1.run_id
    assert res_1_dup.outcome == FormTxOutcome.VERIFIED_DONE
    assert executions == ["req-alpha"]

    # Persisted ledger is value-scrubbed (contains no PII from dossier_1)
    ledger_files = list(ledger_dir.glob("*.json"))
    assert len(ledger_files) == 1
    raw_ledger = ledger_files[0].read_text(encoding="utf-8")
    assert "Lovelace" not in raw_ledger
    assert "Ada" not in raw_ledger

    # Fresh runner over the same ledger_dir still deduplicates req-alpha
    restarted_runner = FormTxQueueRunner(
        lease,
        executor_fn,
        packs={pack.pack_id: pack},
        ledger_dir=ledger_dir,
    )
    res_1_restarted = restarted_runner.run_job(job_1)
    assert res_1_restarted.run_id == res_1.run_id
    assert executions == ["req-alpha"]

    # Submitting the same request_id with modified dossier raises RequestIdConflictError
    conflicting_dossier = _make_sample_dossier(
        request_id="req-alpha", first_name="Grace", last_name="Hopper"
    )
    conflicting_job = QueueJobSpec(
        job_id="job-001-mutated",
        request_id="req-alpha",
        pack_id=pack.pack_id,
        dossier=conflicting_dossier,
    )
    with pytest.raises(RequestIdConflictError, match="req-alpha"):
        runner.run_job(conflicting_job)
    assert executions == ["req-alpha"]

    # Submitting the same request_id with a different FormPack digest also raises RequestIdConflictError
    modified_pack = pack.model_copy(update={"max_commit_mutations": 2})
    with pytest.raises(RequestIdConflictError, match="req-alpha"):
        runner.run_job(job_1, pack=modified_pack)

    # Mid-shift session expiry: auth wall appears BEFORE job_2 runs
    surface.auth_wall_present = True
    surface.authenticated_landmark_present = False
    filled_before = len(surface.filled_fields)

    dossier_2 = _make_sample_dossier(
        request_id="req-beta", first_name="Alan", last_name="Turing"
    )
    job_2 = QueueJobSpec(
        job_id="job-002",
        request_id="req-beta",
        pack_id=pack.pack_id,
        dossier=dossier_2,
    )
    held_res = runner.run_job(job_2)
    assert held_res.outcome == FormTxOutcome.AUTH_REQUIRED
    assert held_res.safe_to_retry is True
    assert len(surface.filled_fields) == filled_before
    assert executions == ["req-alpha"]

    # Without retry_safe_failures=True, cached AUTH_REQUIRED is returned without touching executor
    held_cached = runner.run_job(job_2, retry_safe_failures=False)
    assert held_cached.run_id == held_res.run_id
    assert executions == ["req-alpha"]

    # Operator completes protected re-authentication handoff
    handoff_id = lease.begin_auth_handoff(methods=("sso", "mfa"))
    surface.auth_wall_present = False
    surface.authenticated_landmark_present = True
    surface.current_url = "https://portal.example/intake/new"
    marker = lease.end_auth_handoff(handoff_id, outcome="completed")
    assert marker.authenticated_landmark_verified is True
    assert lease.state == LeaseState.READY

    # Now retrying req-beta with retry_safe_failures=True succeeds cleanly!
    res_2 = runner.run_job(job_2, retry_safe_failures=True)
    assert res_2.outcome == FormTxOutcome.VERIFIED_DONE
    assert res_2.safe_to_retry is False
    assert executions == ["req-alpha", "req-beta"]


# ---------------------------------------------------------------------------
# 3. Deterministic Cross-Site Pack Compiler (induce_form_pack) Tests
# ---------------------------------------------------------------------------


def test_induce_pack_across_two_distinct_patient_registration_portals_with_pii_scrubbing() -> (
    None
):
    domain = build_patient_registration_domain_schema()

    # Clinic Portal A: native HTML controls, dynamic numeric IDs (`dyn_42_fname`),
    # AND live patient PII values populated on screen that MUST be scrubbed.
    portal_a_snapshot = FormSnapshot(
        url_origin_and_path="https://clinic-alpha.example/registration",
        title="New Patient Intake",
        screen_id="demographics_step",
        authenticated_landmark_present=True,
        controls=(
            ObservedControl(
                control_id="ctrl_1",
                dom_selector="#dyn_42_fname",
                tag="input",
                input_type="text",
                id_attr="dyn_42_fname",
                label_text="Given Name *",
                section_text="Patient Demographics",
                current_value="Evelyn",
                required=True,
            ),
            ObservedControl(
                control_id="ctrl_2",
                dom_selector="#dyn_42_lname",
                tag="input",
                input_type="text",
                id_attr="dyn_42_lname",
                label_text="Surname *",
                section_text="Patient Demographics",
                current_value="Vance-Secret-PII",
                required=True,
            ),
            ObservedControl(
                control_id="ctrl_3",
                dom_selector="#txt_birth_date_8f3a91c2",
                tag="input",
                input_type="date",
                id_attr="txt_birth_date_8f3a91c2",
                label_text="Birth Date *",
                section_text="Patient Demographics",
                current_value="1979-11-03",
                required=True,
            ),
            ObservedControl(
                control_id="ctrl_4",
                dom_selector="#dyn_42_sex",
                tag="select",
                id_attr="dyn_42_sex",
                label_text="Administrative Sex",
                section_text="Patient Demographics",
                current_value="Female",
                options=(
                    ObservedOption(value="F", label="Female"),
                    ObservedOption(value="M", label="Male"),
                ),
            ),
            ObservedControl(
                control_id="ctrl_5",
                dom_selector="#dyn_42_mobile",
                tag="input",
                input_type="tel",
                id_attr="dyn_42_mobile",
                label_text="Mobile Phone *",
                section_text="Contact Information",
                current_value="555-867-5309",
                required=True,
            ),
            ObservedControl(
                control_id="ctrl_6",
                dom_selector="#dyn_42_member_id",
                tag="input",
                input_type="text",
                id_attr="dyn_42_member_id",
                label_text="Member ID",
                section_text="Insurance Coverage",
                current_value="MBR-99887766",
            ),
            ObservedControl(
                control_id="btn_submit",
                dom_selector="#dyn_42_register_btn",
                tag="button",
                role="button",
                id_attr="dyn_42_register_btn",
                label_text="Register Patient",
            ),
        ),
        entity_rows=(
            ObservedEntityRow(
                table_id="matches",
                entity_kind="patient",
                row_index=0,
                row_selector="tr.row-0",
                values={"full_name": "Evelyn Vance-Secret-PII"},
            ),
        ),
        banner_errors=("Duplicate check warning for Evelyn Vance-Secret-PII",),
    )

    # Clinic Portal B: completely different layout, ARIA portal comboboxes,
    # checkbox consent, colon-namespaced server IDs (`j_idt109:patientFirstName`).
    portal_b_snapshot = FormSnapshot(
        url_origin_and_path="https://clinic-beta.example/check-in",
        title="Patient Registration",
        screen_id="checkin_form",
        authenticated_landmark_present=True,
        controls=(
            ObservedControl(
                control_id="b_1",
                dom_selector="[id='j_idt109:patientFirstName']",
                tag="input",
                input_type="text",
                id_attr="j_idt109:patientFirstName",
                label_text="Patient First Name *",
                section_text="Personal Identity",
                required=True,
            ),
            ObservedControl(
                control_id="b_2",
                dom_selector="[id='j_idt109:patientLastName']",
                tag="input",
                input_type="text",
                id_attr="j_idt109:patientLastName",
                label_text="Patient Last Name *",
                section_text="Personal Identity",
                required=True,
            ),
            ObservedControl(
                control_id="b_3",
                dom_selector="[id='j_idt109:dob']",
                tag="input",
                input_type="text",
                id_attr="j_idt109:dob",
                label_text="DOB (MM/DD/YYYY) *",
                section_text="Personal Identity",
                required=True,
            ),
            ObservedControl(
                control_id="b_4",
                dom_selector="[id='j_idt109:legalSexCombo']",
                tag="div",
                role="combobox",
                id_attr="j_idt109:legalSexCombo",
                label_text="Legal Sex",
                section_text="Personal Identity",
            ),
            ObservedControl(
                control_id="b_5",
                dom_selector="[id='j_idt109:primaryPhone']",
                tag="input",
                input_type="text",
                id_attr="j_idt109:primaryPhone",
                label_text="Primary Phone *",
                section_text="Patient Contact",
                required=True,
            ),
            ObservedControl(
                control_id="b_6",
                dom_selector="[id='j_idt109:subscriberId']",
                tag="input",
                input_type="text",
                id_attr="j_idt109:subscriberId",
                label_text="Subscriber ID",
                section_text="Billing & Payer",
            ),
            ObservedControl(
                control_id="b_7",
                dom_selector="[id='j_idt109:hipaaConsent']",
                tag="input",
                input_type="checkbox",
                id_attr="j_idt109:hipaaConsent",
                label_text="Consent to Treat",
                section_text="Authorization",
            ),
            ObservedControl(
                control_id="b_submit",
                dom_selector="[id='j_idt109:submitBtn']",
                tag="button",
                role="button",
                id_attr="j_idt109:submitBtn",
                label_text="Submit Registration",
            ),
        ),
    )

    report_a = induce_form_pack(
        pack_id="clinic_portal_a_v1",
        ui_profile_id="standard_web_form",
        domain=domain,
        form_snapshots=(portal_a_snapshot,),
    )
    report_b = induce_form_pack(
        pack_id="clinic_portal_b_v1",
        ui_profile_id="async_spa_portal",
        domain=domain,
        form_snapshots=(portal_b_snapshot,),
    )

    assert report_a.certified_ready is False
    assert report_b.certified_ready is False
    assert report_a.unmatched_required_keys == ()
    assert report_b.unmatched_required_keys == ()
    assert report_a.ambiguous_controls == ()
    assert report_b.ambiguous_controls == ()
    assert report_a.unmapped_portal_required_labels == ()
    assert report_b.unmapped_portal_required_labels == ()

    # Both portals map onto the exact same canonical core keys
    shared_expected = {
        "first_name",
        "last_name",
        "date_of_birth",
        "administrative_sex",
        "phone_number",
        "insurance_member_id",
    }
    assert set(report_a.matched_keys) == shared_expected
    assert set(report_b.matched_keys) == shared_expected | {"consent_to_treat"}

    # Control kinds adapt per portal automatically
    kinds_a = {b.dossier_key: b.control_kind for b in report_a.pack.bindings}
    kinds_b = {b.dossier_key: b.control_kind for b in report_b.pack.bindings}
    assert kinds_a["date_of_birth"] == ControlKind.DATE
    assert kinds_b["date_of_birth"] == ControlKind.TEXT
    assert kinds_a["administrative_sex"] == ControlKind.SELECT
    assert kinds_b["administrative_sex"] == ControlKind.PORTAL_COMBOBOX
    assert kinds_b["consent_to_treat"] == ControlKind.CHECKBOX

    # PII & Volatile-ID Scrubbing Gate verification on Portal A pack JSON
    pack_a_json = canonical_json_bytes(report_a.pack).decode("utf-8")
    for forbidden_token in (
        "Evelyn",
        "Vance-Secret-PII",
        "1979-11-03",
        "555-867-5309",
        "MBR-99887766",
        "dyn_42",
        "dyn",
        "42",
        "8f3a91c2",
    ):
        assert forbidden_token not in pack_a_json

    pack_b_json = canonical_json_bytes(report_b.pack).decode("utf-8")
    assert "idt109" not in pack_b_json

    # Induced anchors still resolve uniquely when server re-renders new dynamic IDs!
    rerendered_portal_a = FormSnapshot(
        url_origin_and_path="https://clinic-alpha.example/registration",
        controls=tuple(
            c.model_copy(
                update={
                    "id_attr": c.id_attr.replace("42", "987").replace(
                        "8f3a91c2", "deadbeef"
                    ),
                    "dom_selector": c.dom_selector.replace("42", "987").replace(
                        "8f3a91c2", "deadbeef"
                    ),
                    "current_value": "",
                }
            )
            for c in portal_a_snapshot.controls
        ),
    )
    for binding in report_a.pack.bindings:
        matched_ctrl = match_anchor_in_snapshot(binding.anchor, rerendered_portal_a)
        assert matched_ctrl is not None, f"Failed to re-match {binding.dossier_key}"


def test_induce_multi_screen_property_quote_wizard_and_summary_page() -> None:
    domain = build_property_quote_domain_schema()

    screen_1 = FormSnapshot(
        url_origin_and_path="https://quoting.example/wizard/step-1",
        title="Step 1: Applicant & Property Location",
        screen_id="step_1_location",
        controls=(
            ObservedControl(
                control_id="s1_fn",
                dom_selector="#dyn_10_insured_fname",
                tag="input",
                input_type="text",
                id_attr="dyn_10_insured_fname",
                label_text="Applicant First Name *",
                section_text="Applicant",
                required=True,
            ),
            ObservedControl(
                control_id="s1_ln",
                dom_selector="#dyn_10_insured_lname",
                tag="input",
                input_type="text",
                id_attr="dyn_10_insured_lname",
                label_text="Applicant Last Name *",
                section_text="Applicant",
                required=True,
            ),
            ObservedControl(
                control_id="s1_addr",
                dom_selector="#dyn_10_risk_street",
                tag="input",
                input_type="text",
                id_attr="dyn_10_risk_street",
                label_text="Property Street Address *",
                section_text="Risk Location",
                required=True,
            ),
            ObservedControl(
                control_id="s1_zip",
                dom_selector="#dyn_10_risk_zip",
                tag="input",
                input_type="text",
                id_attr="dyn_10_risk_zip",
                label_text="Postal Code *",
                section_text="Risk Location",
                required=True,
            ),
            ObservedControl(
                control_id="s1_next",
                dom_selector="#btn_next",
                tag="button",
                role="button",
                id_attr="btn_next",
                label_text="Continue",
            ),
        ),
    )

    screen_2 = FormSnapshot(
        url_origin_and_path="https://quoting.example/wizard/step-2",
        title="Step 2: Dwelling & Coverage",
        screen_id="step_2_coverage",
        controls=(
            ObservedControl(
                control_id="s2_yr",
                dom_selector="#dyn_20_year_built",
                tag="input",
                input_type="text",
                id_attr="dyn_20_year_built",
                label_text="Year Built *",
                section_text="Building Construction",
                required=True,
            ),
            ObservedControl(
                control_id="s2_const",
                dom_selector="#dyn_20_construction_type",
                tag="div",
                role="combobox",
                id_attr="dyn_20_construction_type",
                label_text="Construction Type",
                section_text="Building Construction",
            ),
            ObservedControl(
                control_id="s2_cov",
                dom_selector="#dyn_20_coverage_a",
                tag="input",
                input_type="text",
                id_attr="dyn_20_coverage_a",
                label_text="Dwelling Coverage *",
                section_text="Coverage Limits",
                required=True,
            ),
            ObservedControl(
                control_id="s2_alarm",
                dom_selector="#dyn_20_alarm",
                tag="input",
                input_type="radio",
                id_attr="dyn_20_alarm",
                label_text="Monitored Alarm",
                section_text="Protective Discounts",
            ),
            ObservedControl(
                control_id="s2_commit",
                dom_selector="#btn_generate_quote",
                tag="button",
                role="button",
                id_attr="btn_generate_quote",
                label_text="Generate Quote",
            ),
        ),
    )

    summary_screen = FormSnapshot(
        url_origin_and_path="https://quoting.example/wizard/summary?token=ephemeral99",
        title="Quote Summary",
        screen_id="quote_summary",
        controls=(
            ObservedControl(
                control_id="sum_qnum",
                dom_selector="#out_quote_number",
                tag="span",
                id_attr="out_quote_number",
                label_text="Quote Number",
                current_value="Q-2026-00491",
                readonly=True,
            ),
            ObservedControl(
                control_id="sum_prem",
                dom_selector="#out_annual_premium",
                tag="span",
                id_attr="out_annual_premium",
                label_text="Annual Premium",
                current_value="$1,420.50",
                readonly=True,
            ),
            ObservedControl(
                control_id="sum_addr",
                dom_selector="#ro_risk_address",
                tag="dd",
                id_attr="ro_risk_address",
                label_text="Property Street Address",
                current_value="742 Evergreen Terrace",
                readonly=True,
            ),
            ObservedControl(
                control_id="sum_cov",
                dom_selector="#ro_dwelling_coverage",
                tag="dd",
                id_attr="ro_dwelling_coverage",
                label_text="Dwelling Coverage",
                current_value="$485,000",
                readonly=True,
            ),
        ),
    )

    report = induce_form_pack(
        pack_id="property_quote_portal_v1",
        ui_profile_id="server_postback_wizard",
        domain=domain,
        form_snapshots=(screen_1, screen_2),
        summary_snapshot=summary_screen,
    )

    assert report.certified_ready is False
    assert report.unmatched_required_keys == ()
    assert report.ambiguous_controls == ()
    assert report.unmapped_portal_required_labels == ()

    pack = report.pack
    assert len(pack.screens) == 2
    assert pack.screens[0].screen_id == "step_1_location"
    assert pack.screens[0].next_button_anchor is not None
    assert pack.screens[0].is_commit_screen is False
    assert pack.screens[1].screen_id == "step_2_coverage"
    assert pack.screens[1].is_commit_screen is False
    assert pack.screens[1].commit_button_anchor is None

    by_key = {b.dossier_key: b for b in pack.bindings}
    assert by_key["year_built"].codec.kind == FormCodecKind.INTEGER
    assert by_key["dwelling_coverage_amount"].codec.kind == FormCodecKind.CURRENCY_CENTS
    assert by_key["dwelling_coverage_amount"].triggers_re_render is True
    assert by_key["construction_type"].control_kind == ControlKind.PORTAL_COMBOBOX
    assert by_key["monitored_alarm"].control_kind == ControlKind.RADIO_GROUP

    assert pack.summary_check is not None
    assert (
        pack.summary_check.summary_url_path == "https://quoting.example/wizard/summary"
    )
    output_keys = {o.output_key: o for o in pack.summary_check.outputs}
    assert set(output_keys) == {"quote_number", "annual_premium"}
    assert output_keys["annual_premium"].codec.kind == FormCodecKind.CURRENCY_CENTS

    readback_keys = {r.dossier_key: r for r in pack.summary_check.field_readbacks}
    assert set(readback_keys) == {"property_street_address", "dwelling_coverage_amount"}
    assert (
        readback_keys["dwelling_coverage_amount"].control_kind
        == ControlKind.READONLY_OUTPUT
    )

    # Ensure summary live values ("Q-2026-00491", "$1,420.50", "742 Evergreen Terrace") were scrubbed
    serialized = canonical_json_bytes(pack).decode("utf-8")
    assert "Q-2026-00491" not in serialized
    assert "1,420.50" not in serialized
    assert "Evergreen" not in serialized
    assert "ephemeral99" not in serialized


def test_induce_pack_flags_ambiguous_controls_and_unmapped_required_questions() -> None:
    domain = build_patient_registration_domain_schema()

    problematic_snapshot = FormSnapshot(
        url_origin_and_path="https://clinic-gamma.example/intake",
        title="Intake",
        controls=(
            ObservedControl(
                control_id="fn_ok",
                dom_selector="#dyn_1_fname",
                tag="input",
                input_type="text",
                id_attr="dyn_1_fname",
                label_text="First Name *",
                required=True,
            ),
            ObservedControl(
                control_id="ln_ok",
                dom_selector="#dyn_2_lname",
                tag="input",
                input_type="text",
                id_attr="dyn_2_lname",
                label_text="Last Name *",
                required=True,
            ),
            ObservedControl(
                control_id="dob_ok",
                dom_selector="#dyn_3_dob",
                tag="input",
                input_type="date",
                id_attr="dyn_3_dob",
                label_text="Date of Birth *",
                required=True,
            ),
            # TWO indistinguishable "Phone Number" controls in the same section -> ambiguous!
            ObservedControl(
                control_id="phone_dup_1",
                dom_selector="#dyn_4",
                tag="input",
                input_type="tel",
                id_attr="dyn_4",
                label_text="Phone Number *",
                section_text="Contact",
                required=True,
            ),
            ObservedControl(
                control_id="phone_dup_2",
                dom_selector="#dyn_5",
                tag="input",
                input_type="tel",
                id_attr="dyn_5",
                label_text="Phone Number *",
                section_text="Contact",
                required=True,
            ),
            # Unmapped portal-specific required question not in intake packet!
            ObservedControl(
                control_id="custom_req_1",
                dom_selector="#dyn_6_referring_npi",
                tag="input",
                input_type="text",
                id_attr="dyn_6_referring_npi",
                label_text="Referring Physician NPI *",
                required=True,
            ),
            ObservedControl(
                control_id="custom_req_2",
                dom_selector="#dyn_7_parking_voucher",
                tag="input",
                input_type="text",
                id_attr="dyn_7_parking_voucher",
                label_text="Parking Validation Code *",
                required=True,
            ),
        ),
    )

    report = induce_form_pack(
        pack_id="clinic_gamma_uncertified",
        ui_profile_id="standard_web_form",
        domain=domain,
        form_snapshots=(problematic_snapshot,),
    )

    assert report.certified_ready is False
    assert "phone_number" not in report.matched_keys
    assert "phone_number" in report.unmatched_required_keys
    assert len(report.ambiguous_controls) == 2
    assert any("phone_dup_1" in item for item in report.ambiguous_controls)
    assert any("phone_dup_2" in item for item in report.ambiguous_controls)
    assert report.unmapped_portal_required_labels == (
        "Referring Physician NPI",
        "Parking Validation Code",
    )


# ---------------------------------------------------------------------------
# 4. CLI End-to-End Tests (`openadapt_flow.form_tx.cli.main`)
# ---------------------------------------------------------------------------


def test_cli_list_profiles_induce_pack_and_inspect_pack_e2e(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # 1. list-profiles (text & --json & custom --profiles-dir)
    custom_profiles_dir = tmp_path / "profiles"
    custom_profiles_dir.mkdir()
    custom_profile = UIProfileSpec(
        profile_id="custom_kiosk_portal",
        description="Touch kiosk intake surface.",
        keepalive_path="/kiosk/ping",
    )
    (custom_profiles_dir / "kiosk.json").write_text(
        json.dumps(custom_profile.model_dump(mode="json")),
        encoding="utf-8",
    )

    rc = cli_main(["list-profiles", "--profiles-dir", str(custom_profiles_dir)])
    assert rc == 0
    out_text = capsys.readouterr().out
    assert "standard_web_form" in out_text
    assert "server_postback_wizard" in out_text
    assert "custom_kiosk_portal" in out_text

    rc_json = cli_main(
        ["list-profiles", "--profiles-dir", str(custom_profiles_dir), "--json"]
    )
    assert rc_json == 0
    parsed_profiles = json.loads(capsys.readouterr().out)
    profile_ids = {entry["profile_id"] for entry in parsed_profiles}
    assert {
        "standard_web_form",
        "async_spa_portal",
        "custom_kiosk_portal",
    } <= profile_ids

    # 2. induce-pack with custom JSON domain schema + snapshots + summary
    custom_domain = CanonicalDomainSchema(
        schema_id="equipment_checkout.v1",
        fields=(
            CanonicalFieldSpec(
                dossier_key="requester_name",
                synonyms=("requester name", "employee name"),
                required=True,
            ),
            CanonicalFieldSpec(
                dossier_key="asset_tag",
                synonyms=("asset tag", "equipment tag"),
                expected_codec=FieldCodecSpec(kind=FormCodecKind.UPPER),
                required=True,
            ),
        ),
        expected_outputs=(
            CanonicalFieldSpec(
                dossier_key="ticket_id",
                synonyms=("ticket id", "confirmation id"),
                required=True,
            ),
        ),
    )
    domain_file = tmp_path / "domain.json"
    domain_file.write_text(
        json.dumps(custom_domain.model_dump(mode="json")),
        encoding="utf-8",
    )

    snap_file = tmp_path / "snap1.json"
    snap_obj = FormSnapshot(
        url_origin_and_path="https://internal.example/checkout",
        title="Equipment Checkout",
        screen_id="checkout_main",
        controls=(
            ObservedControl(
                control_id="c1",
                dom_selector="#dyn_11_requester",
                tag="input",
                input_type="text",
                id_attr="dyn_11_requester",
                label_text="Requester Name *",
                current_value="Scrubbed User",
                required=True,
            ),
            ObservedControl(
                control_id="c2",
                dom_selector="#dyn_11_asset_tag",
                tag="input",
                input_type="text",
                id_attr="dyn_11_asset_tag",
                label_text="Asset Tag *",
                current_value="AST-0091",
                required=True,
            ),
            ObservedControl(
                control_id="c_btn",
                dom_selector="#btn_save",
                tag="button",
                role="button",
                id_attr="btn_save",
                label_text="Submit Request",
            ),
        ),
    )
    snap_file.write_text(
        json.dumps(snap_obj.model_dump(mode="json")),
        encoding="utf-8",
    )

    sum_file = tmp_path / "summary.json"
    sum_obj = FormSnapshot(
        url_origin_and_path="https://internal.example/checkout/done",
        title="Checkout Complete",
        controls=(
            ObservedControl(
                control_id="s_ticket",
                dom_selector="#ticket_id",
                tag="span",
                id_attr="ticket_id",
                label_text="Ticket ID",
                current_value="TKT-5512",
                readonly=True,
            ),
        ),
    )
    sum_file.write_text(
        json.dumps(sum_obj.model_dump(mode="json")),
        encoding="utf-8",
    )

    out_pack_path = tmp_path / "out" / "checkout_pack.json"
    out_report_path = tmp_path / "out" / "checkout_report.json"

    rc_induce = cli_main(
        [
            "induce-pack",
            "--domain",
            str(domain_file),
            "--snapshots",
            str(snap_file),
            "--summary-snapshot",
            str(sum_file),
            "--ui-profile",
            "standard_web_form",
            "--pack-id",
            "checkout_pack_v1",
            "--out",
            str(out_pack_path),
            "--report-out",
            str(out_report_path),
        ]
    )
    assert rc_induce == 0
    induce_stdout = capsys.readouterr().out
    assert "certified_ready: False" in induce_stdout
    assert out_pack_path.is_file()
    assert out_report_path.is_file()

    # Enrich the pack with a popup subroutine to verify inspect-pack summarizes subroutines too
    loaded_pack = FormPack.model_validate_json(
        out_pack_path.read_text(encoding="utf-8")
    )
    enriched_pack = loaded_pack.model_copy(
        update={
            "popup_subroutines": (
                PopupSubroutineSpec(
                    subroutine_id="calc_replacement_cost",
                    trigger_anchor=FieldAnchorSpec(label_patterns=("^Calculate$",)),
                    commit_anchor=FieldAnchorSpec(label_patterns=("^Apply$",)),
                    parent_return_anchor=FieldAnchorSpec(
                        label_patterns=("^Asset Value$",)
                    ),
                    return_output_key="estimated_value_cents",
                ),
            )
        }
    )
    enriched_path = tmp_path / "out" / "enriched_pack.json"
    enriched_path.write_text(
        json.dumps(enriched_pack.model_dump(mode="json"), indent=2, sort_keys=True),
        encoding="utf-8",
    )

    # 3. inspect-pack (human-readable & --json)
    rc_inspect = cli_main(["inspect-pack", str(enriched_path)])
    assert rc_inspect == 0
    inspect_stdout = capsys.readouterr().out
    assert f"digest: {enriched_pack.digest()}" in inspect_stdout
    assert "screens (1):" in inspect_stdout
    assert "checkout_main [commit]" not in inspect_stdout
    assert "bindings (2):" in inspect_stdout
    assert "requester_name" in inspect_stdout
    assert "asset_tag" in inspect_stdout
    assert "subroutines (1):" in inspect_stdout
    assert "calc_replacement_cost -> estimated_value_cents" in inspect_stdout
    assert "outputs (1):" in inspect_stdout
    assert "ticket_id" in inspect_stdout

    rc_inspect_json = cli_main(["inspect-pack", str(enriched_path), "--json"])
    assert rc_inspect_json == 0
    inspect_json = json.loads(capsys.readouterr().out)
    assert inspect_json["digest"] == enriched_pack.digest()
    assert inspect_json["subroutines"] == ["calc_replacement_cost"]
    assert inspect_json["outputs"] == ["ticket_id"]

    # 4. --strict returns exit code 1 when uncertified
    incomplete_snap = FormSnapshot(
        url_origin_and_path="https://internal.example/incomplete",
        controls=(
            ObservedControl(
                control_id="only_one",
                dom_selector="#requester",
                tag="input",
                label_text="Requester Name *",
                required=True,
            ),
        ),
    )
    incomplete_file = tmp_path / "incomplete.json"
    incomplete_file.write_text(
        json.dumps(incomplete_snap.model_dump(mode="json")),
        encoding="utf-8",
    )
    uncert_out = tmp_path / "out" / "uncertified.json"
    rc_strict_fail = cli_main(
        [
            "induce-pack",
            "--domain",
            "patient_registration",
            "--snapshots",
            str(incomplete_file),
            "--ui-profile",
            "standard_web_form",
            "--pack-id",
            "incomplete_pack",
            "--out",
            str(uncert_out),
            "--strict",
        ]
    )
    assert rc_strict_fail == 1
    capsys.readouterr()


def test_handoff_abort_cancel_and_queue_hold_during_open_handoff() -> None:
    surface = DeterministicFakeSurface(
        url="https://portal.example/auth/login",
        auth_wall_present=True,
        authenticated_landmark_present=False,
    )
    lease = WarmSessionLease(surface, entry_url="https://portal.example/intake/new")
    assert lease.preflight_auth() == LeaseState.AUTH_REQUIRED

    # Single string method accepted; aborted handoff never snapshots credential UI
    handoff_id = lease.begin_auth_handoff(methods="passkey")
    assert lease.active_handoff_id == handoff_id

    # Queue runner holds job safely while AUTH_HANDOFF_OPEN without navigating or snapshotting
    pack = _make_sample_pack("pack-open-handoff")
    goto_before = len(surface.goto_history)
    runner = FormTxQueueRunner(
        lease,
        lambda *_: pytest.fail("executor_fn must not run while handoff is open"),
        packs={pack.pack_id: pack},
    )
    job = QueueJobSpec(
        job_id="job-open-1",
        request_id="req-open-1",
        pack_id=pack.pack_id,
        dossier=_make_sample_dossier("req-open-1"),
    )
    held = runner.run_job(job)
    assert held.outcome == FormTxOutcome.AUTH_REQUIRED
    assert held.safe_to_retry is True
    assert len(surface.goto_history) == goto_before
    assert lease.state == LeaseState.AUTH_HANDOFF_OPEN

    aborted = lease.end_auth_handoff(handoff_id, outcome="aborted")
    assert aborted.outcome == "aborted"
    assert aborted.post_handoff_url_path is None
    assert aborted.authenticated_landmark_verified is False
    assert lease.state == LeaseState.AUTH_REQUIRED
    assert lease.active_handoff_id is None


def test_run_batch_partial_completion_and_idempotent_resume() -> None:
    surface = DeterministicFakeSurface(
        url="https://portal.example/intake/new",
        auth_wall_present=False,
        authenticated_landmark_present=True,
    )
    lease = WarmSessionLease(surface, entry_url="https://portal.example/intake/new")
    pack = _make_sample_pack("pack-batch")
    ran_requests: list[str] = []

    def executor_fn(
        _backend: FormSurfaceBackend, _pack: FormPack, dossier: InputDossier
    ) -> FormTxResult:
        ran_requests.append(dossier.request_id)
        # Simulate session expiring immediately after first job commits
        if len(ran_requests) == 1:
            surface.auth_wall_present = True
            surface.authenticated_landmark_present = False
        return FormTxResult(
            run_id=f"run-{dossier.request_id}",
            request_id=dossier.request_id,
            outcome=FormTxOutcome.VERIFIED_DONE,
            safe_to_retry=False,
            fields_filled_auto=2,
        )

    runner = FormTxQueueRunner(lease, executor_fn, packs={pack.pack_id: pack})
    jobs = [
        QueueJobSpec(
            job_id=f"j-{idx}",
            request_id=f"req-{idx}",
            pack_id=pack.pack_id,
            dossier=_make_sample_dossier(f"req-{idx}"),
        )
        for idx in (1, 2, 3)
    ]

    first_pass = runner.run_batch(jobs)
    assert [r.outcome for r in first_pass] == [
        FormTxOutcome.VERIFIED_DONE,
        FormTxOutcome.AUTH_REQUIRED,
        FormTxOutcome.AUTH_REQUIRED,
    ]
    assert ran_requests == ["req-1"]

    # Re-authenticate and resume the entire batch: req-1 stays cached, req-2 & req-3 execute
    hid = lease.begin_auth_handoff(methods=("password_manager",))
    surface.auth_wall_present = False
    surface.authenticated_landmark_present = True
    assert lease.end_auth_handoff(
        hid, outcome="completed"
    ).authenticated_landmark_verified

    second_pass = runner.run_batch(jobs, retry_safe_failures=True)
    assert [r.outcome for r in second_pass] == [
        FormTxOutcome.VERIFIED_DONE,
        FormTxOutcome.VERIFIED_DONE,
        FormTxOutcome.VERIFIED_DONE,
    ]
    assert ran_requests == ["req-1", "req-2", "req-3"]


def test_section_disambiguation_and_hidden_disabled_control_filtering() -> None:
    domain = build_patient_registration_domain_schema()
    snapshot = FormSnapshot(
        url_origin_and_path="https://clinic-delta.example/intake",
        controls=(
            ObservedControl(
                control_id="pat_fn",
                dom_selector="#dyn_1_fname",
                tag="input",
                input_type="text",
                id_attr="dyn_1_fname",
                label_text="First Name *",
                section_text="Patient Information",
                required=True,
            ),
            ObservedControl(
                control_id="guar_fn",
                dom_selector="#dyn_2_fname",
                tag="input",
                input_type="text",
                id_attr="dyn_2_fname",
                label_text="First Name",
                section_text="Guarantor Information",
                required=False,
            ),
            ObservedControl(
                control_id="pat_ln",
                dom_selector="#dyn_3_lname",
                tag="input",
                input_type="text",
                id_attr="dyn_3_lname",
                label_text="Last Name *",
                section_text="Patient Information",
                required=True,
            ),
            ObservedControl(
                control_id="pat_dob",
                dom_selector="#dyn_4_patient_dob",
                tag="input",
                input_type="date",
                name_attr="dyn_4_patient_dob",
                label_text="",  # Attribute-only control without visible label
                section_text="Patient Information",
                required=True,
            ),
            ObservedControl(
                control_id="pat_phone",
                dom_selector="#dyn_5_phone",
                tag="input",
                input_type="tel",
                id_attr="dyn_5_phone",
                label_text="Phone *",
                section_text="Patient Information",
                required=True,
            ),
            # Hidden or disabled required controls must be ignored
            ObservedControl(
                control_id="hidden_req",
                dom_selector="#hidden_internal",
                tag="input",
                label_text="Hidden Internal Code *",
                required=True,
                visible=False,
            ),
            ObservedControl(
                control_id="disabled_req",
                dom_selector="#disabled_internal",
                tag="input",
                label_text="Disabled Legacy Code *",
                required=True,
                disabled=True,
            ),
        ),
    )

    report = induce_form_pack(
        pack_id="clinic_delta_v1",
        ui_profile_id="standard_web_form",
        domain=domain,
        form_snapshots=(snapshot,),
    )
    assert report.certified_ready is False
    assert report.unmapped_portal_required_labels == ()
    assert report.ambiguous_controls == ()

    by_key = {b.dossier_key: b for b in report.pack.bindings}
    matched_fn = match_anchor_in_snapshot(by_key["first_name"].anchor, snapshot)
    assert matched_fn is not None and matched_fn.control_id == "pat_fn"
    matched_dob = match_anchor_in_snapshot(by_key["date_of_birth"].anchor, snapshot)
    assert matched_dob is not None and matched_dob.control_id == "pat_dob"


def test_cli_missing_file_returns_clean_exit_code_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli_main(["inspect-pack", str(tmp_path / "does_not_exist.json")])
    assert rc == 2
    err = capsys.readouterr().err
    assert "error:" in err


def test_queue_crash_after_write_is_held_across_restart(tmp_path: Path) -> None:
    surface = DeterministicFakeSurface(
        url="https://portal.example/intake/new",
        auth_wall_present=False,
        authenticated_landmark_present=True,
    )
    lease = WarmSessionLease(surface, entry_url="https://portal.example/intake/new")
    pack = _make_sample_pack("pack-crash")
    executions: list[str] = []

    def crashing_executor(
        backend: FormSurfaceBackend, active_pack: FormPack, dossier: InputDossier
    ) -> FormTxResult:
        executions.append(dossier.request_id)
        backend.fill_text("#first_name", str(dossier.facts["first_name"].value))
        raise RuntimeError("process stopped after a possibly durable write")

    job = QueueJobSpec(
        job_id="job-crash",
        request_id="req-crash",
        pack_id=pack.pack_id,
        dossier=_make_sample_dossier(request_id="req-crash"),
    )
    ledger_dir = tmp_path / "ledger"
    runner = FormTxQueueRunner(
        lease, crashing_executor, packs={pack.pack_id: pack}, ledger_dir=ledger_dir
    )
    with pytest.raises(RuntimeError, match="possibly durable"):
        runner.run_job(job)
    restarted = FormTxQueueRunner(
        lease, crashing_executor, packs={pack.pack_id: pack}, ledger_dir=ledger_dir
    )
    for owner in (runner, restarted):
        result = owner.run_job(job, retry_safe_failures=True)
        assert result.outcome == FormTxOutcome.DELIVERY_UNCERTAIN
        assert result.safe_to_retry is False
    assert executions == ["req-crash"]


def test_queue_competing_workers_cannot_execute_same_request(tmp_path: Path) -> None:
    surface = DeterministicFakeSurface(
        url="https://portal.example/intake/new",
        auth_wall_present=False,
        authenticated_landmark_present=True,
    )
    pack = _make_sample_pack("pack-overlap")
    dossier = _make_sample_dossier(request_id="req-overlap")
    job = QueueJobSpec(
        job_id="job-overlap",
        request_id=dossier.request_id,
        pack_id=pack.pack_id,
        dossier=dossier,
    )
    ledger_dir = tmp_path / "ledger"
    other = FormTxQueueRunner(
        WarmSessionLease(surface, entry_url="https://portal.example/intake/new"),
        lambda *_: pytest.fail("second worker must not execute an active request"),
        packs={pack.pack_id: pack},
        ledger_dir=ledger_dir,
    )

    def executor(*_: object) -> FormTxResult:
        result = other.run_job(job, retry_safe_failures=True)
        assert result.outcome == FormTxOutcome.DELIVERY_UNCERTAIN
        return FormTxResult(
            run_id="run-owner",
            request_id=job.request_id,
            outcome=FormTxOutcome.VERIFIED_DONE,
            safe_to_retry=False,
        )

    owner = FormTxQueueRunner(
        WarmSessionLease(surface, entry_url="https://portal.example/intake/new"),
        executor,
        packs={pack.pack_id: pack},
        ledger_dir=ledger_dir,
    )
    assert owner.run_job(job).run_id == "run-owner"
    assert other.run_job(job).run_id == "run-owner"


def test_queue_requires_consistent_request_identity() -> None:
    with pytest.raises(ValueError, match="request_id must match"):
        QueueJobSpec(
            job_id="job",
            request_id="other",
            pack_id="pack",
            dossier=_make_sample_dossier(request_id="request"),
        )
