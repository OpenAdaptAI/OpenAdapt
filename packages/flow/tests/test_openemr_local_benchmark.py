"""Focused offline tests for the matched local OpenEMR scaffold."""

from __future__ import annotations

import base64
import inspect
import json
import stat
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from benchmark.openemr_local.fixture import (
    ACTOR_SCOPE,
    EXPECTED_TABLE_DELTAS,
    ORACLE_SCOPE,
    FixtureError,
    OpenEMRFixture,
    _jwt_scope_set,
    audit_table_deltas,
    unexpected_table_deltas,
)
from openadapt_flow.benchmark.openemr_local import (
    ARMS,
    CONDITIONS,
    OpenEMRPatientOracle,
    PrimaryOutcome,
    SyntheticPatientSpec,
    TrialRow,
    aggregate_rows,
    canonical_patient_records,
    classify_patient_trial,
    patient_api_binding,
    patient_effects,
    patient_records_sha256,
    publication_gate,
)
from openadapt_flow.ir import StructuralLocator
from openadapt_flow.runtime.effects import EffectKind
from scripts.openemr_local_demo import (
    AGENT_STANDARD_INPUT_LIMIT,
    AGENT_TOKEN_COUNT_MARGIN,
    AgentBudgetRefusal,
    CapturedEvidence,
    OpenEMRPlaywrightBackend,
    _AtomicUsageLedger,
    _BudgetedAgentClient,
    _capture_post_evidence,
    _confirm_create_locator,
    _duplicate_dialog_postcondition,
    _form_frame,
    _marked_save_step_id,
    _persist_evidence,
    _plan,
    _preauthenticate_browser,
    _reset_recording_state,
    _row,
    _stable_pre_trial_counts,
    _tree_manifest_sha256,
    _validate_benchmark_bundle,
    record,
    run_matrix,
)

ROOT = Path(__file__).resolve().parents[1] / "benchmark/openemr_local"


def test_issued_oauth_jwt_scope_claim_is_exact_and_well_formed() -> None:
    def segment(value: object) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    token = f"{segment({'alg': 'none'})}.{segment({'scopes': ORACLE_SCOPE.split()})}.x"
    assert _jwt_scope_set(token) == set(ORACLE_SCOPE.split())

    duplicate = (
        f"{segment({'alg': 'none'})}.{segment({'scopes': ['openid', 'openid']})}.x"
    )
    with pytest.raises(FixtureError, match="duplicate scopes"):
        _jwt_scope_set(duplicate)
    with pytest.raises(FixtureError, match="three-part JWT"):
        _jwt_scope_set("not-a-jwt")


def exact_record(**updates: object) -> dict[str, object]:
    spec = SyntheticPatientSpec()
    row: dict[str, object] = {
        "id": "11",
        "pid": "11",
        "uuid": "12345678-1234-4234-9234-123456789abc",
        **spec.fields,
    }
    row.update(updates)
    return row


def make_row(
    arm: str, condition: str, trial: int, *, snapshot: str = "a" * 64
) -> TrialRow:
    return TrialRow(
        arm=arm,
        condition=condition,
        trial=trial,
        primary_outcome=PrimaryOutcome.CORRECT.value,
        success=True,
        silent_incorrect_success=False,
        over_halt=False,
        wall_s=1.0,
        baseline_snapshot_sha256=snapshot,
    )


class FakeResponse:
    status_code = 200

    def __init__(self, body: object) -> None:
        self.body = body

    def json(self) -> object:
        return self.body


class FakeSession:
    def __init__(self, body: object) -> None:
        self.body = body
        self.urls: list[str] = []

    def get(self, url: str, *, timeout: float) -> FakeResponse:
        self.urls.append(url)
        return FakeResponse(self.body)


def test_lock_pins_exact_release_image_source_and_runtime_proofs() -> None:
    lock = json.loads((ROOT / "environment.lock.json").read_text())
    assert lock["upstreams"]["openemr"] == {
        "url": "https://github.com/openemr/openemr.git",
        "ref": "v8_0_0_3",
        "commit": "7c96c8eefe460d6fadbccbe93d0fa6bf819acd69",
    }
    assert lock["services"]["openemr"].endswith(
        "@sha256:0aa4d3d52b22fa69986c087e7c99e9854d8dfd70440634eb7c8af0e08f19f3ab"
    )
    assert lock["services"]["mariadb"].endswith(
        "@sha256:efb4959ef2c835cd735dbc388eb9ad6aab0c78dd64febcd51bc17481111890c4"
    )
    assert len(lock["source_proofs"]) == 13
    for path in (
        "interface/new/new_comprehensive.php",
        "interface/new/new_search_popup.php",
        "src/Services/PatientService.php",
        "src/Validators/PatientValidator.php",
        "interface/login/login.php",
        "src/Common/Command/RegisterApiTestClientCommand.php",
    ):
        assert "/var/www/localhost/htdocs/openemr/" + path in lock["source_proofs"]
    assert all(len(digest) == 64 for digest in lock["source_proofs"].values())
    OpenEMRFixture(Path("/tmp/unused-openemr-fixture"))._validate_lock()


def test_compose_is_loopback_only_and_uses_only_pinned_variables() -> None:
    compose = yaml.safe_load((ROOT / "compose.yml").read_text())
    ports = compose["services"]["openemr"]["ports"]
    assert ports == [
        "127.0.0.1:${OPENEMR_HTTP_PORT:-9301}:80",
        "127.0.0.1:${OPENEMR_HTTPS_PORT:-9300}:443",
    ]
    assert compose["services"]["openemr"]["image"].startswith("${OPENEMR_IMAGE:")
    assert compose["services"]["db"]["image"].startswith("${MARIADB_IMAGE:")


def test_local_image_identity_refuses_missing_locked_repo_digest(
    tmp_path: Path,
) -> None:
    fixture = OpenEMRFixture(tmp_path)
    locked = fixture.lock["services"]["openemr"]
    digest = locked.rsplit("@", 1)[1]

    def runner(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        payload = [
            {
                "Id": "sha256:local",
                "RepoDigests": [f"openemr/openemr@{digest}"],
            }
        ]
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(payload).encode(), stderr=b""
        )

    fixture.runner = runner
    identity = fixture.image_identity()
    assert identity["repo_digest"].endswith(digest)
    assert identity["source_commit"] == ("7c96c8eefe460d6fadbccbe93d0fa6bf819acd69")

    def podman_runner(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        payload = [
            {
                "Id": "sha256:local",
                "RepoDigests": [f"docker.io/openemr/openemr@{digest}"],
            }
        ]
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(payload).encode(), stderr=b""
        )

    fixture.runner = podman_runner
    assert fixture.image_identity()["repo_digest"] == f"openemr/openemr@{digest}"

    def wrong_runner(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        payload = [{"Id": "sha256:local", "RepoDigests": []}]
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(payload).encode(), stderr=b""
        )

    fixture.runner = wrong_runner
    with pytest.raises(FixtureError, match="locked RepoDigest"):
        fixture.image_identity()


def test_database_secret_is_not_exposed_in_host_process_argv(tmp_path: Path) -> None:
    fixture = OpenEMRFixture(tmp_path)
    secret = "S" * 32
    fixture.runtime_env.write_text(
        "\n".join(
            (
                f"OPENEMR_IMAGE={fixture.lock['services']['openemr']}",
                f"MARIADB_IMAGE={fixture.lock['services']['mariadb']}",
                f"MARIADB_ROOT_PASSWORD={secret}",
                f"OPENEMR_DB_PASSWORD={'D' * 32}",
                "OPENEMR_ACTOR_USER=openadapt_actor",
                f"OPENEMR_ACTOR_PASSWORD={'A' * 32}",
                "OPENEMR_HTTP_PORT=9301",
                "OPENEMR_HTTPS_PORT=9300",
                "",
            )
        )
    )
    observed: list[str] = []

    def runner(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        observed.extend(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=b"1\n", stderr=b"")

    fixture.runner = runner
    assert fixture._db_lines("SELECT 1") == ["1"]
    assert secret not in observed
    assert any('MYSQL_PWD="$MARIADB_ROOT_PASSWORD"' in arg for arg in observed)


def test_protected_file_creation_is_exclusive_and_mode_0600(tmp_path: Path) -> None:
    fixture = OpenEMRFixture(tmp_path)
    fixture._protect_state_dir()
    path = tmp_path / "secret"
    fixture._write_private_exclusive(path, b"synthetic-secret")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    with pytest.raises(FixtureError, match="refusing overwrite"):
        fixture._write_private_exclusive(path, b"replacement")


def test_docker_capacity_probe_writes_and_removes_daemon_layer(tmp_path: Path) -> None:
    fixture = OpenEMRFixture(tmp_path)
    imported = False
    observed: list[tuple[list[str], int]] = []

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        nonlocal imported
        payload = kwargs.get("input")
        observed.append((argv, len(payload) if isinstance(payload, bytes) else 0))
        if argv[1:4] == ["image", "ls", "--quiet"]:
            stdout = b"sha256:probe\n" if imported else b""
        elif argv[1:3] == ["image", "import"]:
            imported = True
            stdout = b"sha256:probe\n"
        elif argv[1:3] == ["image", "rm"]:
            imported = False
            stdout = b"deleted\n"
        else:  # pragma: no cover - guards command scope
            raise AssertionError(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr=b"")

    fixture.runner = runner
    fixture._docker_storage_write_probe()
    assert not imported
    assert any(
        argv[1:3] == ["image", "import"] and size > 1_000_000 for argv, size in observed
    )
    assert any(argv[1:3] == ["image", "rm"] for argv, _ in observed)


@pytest.mark.parametrize("existing", ("baseline.sql", "baseline.sql.sha256"))
def test_snapshot_refuses_to_overwrite_existing_evidence(
    tmp_path: Path, existing: str
) -> None:
    fixture = OpenEMRFixture(tmp_path)
    (tmp_path / existing).write_text("existing")
    with pytest.raises(FixtureError, match="refusing overwrite"):
        fixture.snapshot()


def test_distinct_oauth_clients_are_narrowed_to_writer_and_read_only_scopes() -> None:
    assert ACTOR_SCOPE == "openid api:oemr user/patient.crus"
    assert ORACLE_SCOPE == "openid api:oemr user/patient.rs"
    assert ACTOR_SCOPE != ORACLE_SCOPE
    assert not any(flag in ORACLE_SCOPE.rsplit(".", 1)[-1] for flag in "cud")


def test_effects_and_api_binding_cover_same_synthetic_fields() -> None:
    spec = SyntheticPatientSpec()
    effects = patient_effects(spec)
    assert effects[0].kind is EffectKind.RECORD_WRITTEN
    assert effects[0].expected_count == 1
    assert effects[0].match["email"].param == "email"
    assert effects[0].match["lname"].param == "lname"
    assert {effect.field for effect in effects[1:]} == set(spec.fields) - {
        "email",
        "lname",
    }
    resolved = [effect.resolve(spec.params()) for effect in effects]
    assert str(resolved[0].match["email"]) == spec.email
    state_effect = next(effect for effect in resolved if effect.field == "state")
    assert str(state_effect.value) == "MA"

    binding = patient_api_binding()
    assert binding.method == "POST"
    assert binding.url_template == "/apis/default/api/patient"
    assert binding.expected_status == [201]
    assert set(binding.body_template) == set(spec.fields)
    assert binding.body_template["state"] == "MA"
    assert binding.body_template["city"] == "{city}"
    assert spec.params()["state_label"] == "Massachusetts"
    assert "state" not in spec.params()


def test_oracle_filters_reserved_identity_through_read_only_record_shape() -> None:
    session = FakeSession({"data": [exact_record()]})
    oracle = OpenEMRPatientOracle("https://fixture", session, timeout_s=0.1)
    state = oracle.capture()
    assert state.reachable
    assert state.records == [exact_record()]
    assert "LoanParity" in session.urls[0]
    assert "openadapt.loan-parity%40example.invalid" in session.urls[0]


def test_rest_uuid_and_sql_hex_uuid_canonicalize_identically() -> None:
    rest = exact_record(uuid="12345678-1234-4234-9234-123456789ABC")
    sql = exact_record(uuid="12345678123442349234123456789abc", id=11, pid=11)
    assert canonical_patient_records([rest]) == canonical_patient_records([sql])
    assert patient_records_sha256([rest]) == patient_records_sha256([sql])


def test_malformed_oracle_identifier_persists_as_indeterminate_row() -> None:
    malformed = exact_record(id="not-an-integer")
    row = _row(
        arm="agent",
        condition="baseline",
        trial=1,
        baseline_hash="a" * 64,
        actor_reported_success=True,
        halted=False,
        wall_s=1.0,
        rest_records=[malformed],
        db_records=[malformed],
        unexpected_deltas=[],
        model_calls=1,
        cost_usd=0.01,
    )
    assert row.primary_outcome == PrimaryOutcome.ORACLE_INDETERMINATE.value
    assert row.silent_incorrect_success
    assert row.cost_usd == 0.01
    assert row.rest_records_sha256 == ""


def test_correct_result_ignores_actor_self_report_as_oracle() -> None:
    verdict = classify_patient_trial(
        actor_reported_success=False,
        halted=False,
        rest_records=[exact_record()],
        db_records=[exact_record()],
    )
    assert verdict.primary_outcome is PrimaryOutcome.CORRECT
    assert verdict.success
    assert not verdict.silent_incorrect_success


@pytest.mark.parametrize(
    ("records", "outcome"),
    [
        ([], PrimaryOutcome.MISSING_WRITE),
        ([exact_record(city="Wrongtown")], PrimaryOutcome.PARTIAL_WRITE),
        (
            [exact_record(), exact_record(id=12, pid=12, uuid="2" * 32)],
            PrimaryOutcome.DUPLICATE_WRITE,
        ),
    ],
)
def test_failure_taxonomy_and_silent_incorrect_success(
    records: list[dict[str, object]], outcome: PrimaryOutcome
) -> None:
    verdict = classify_patient_trial(
        actor_reported_success=True,
        halted=False,
        rest_records=records,
        db_records=records,
    )
    assert verdict.primary_outcome is outcome
    assert not verdict.success
    assert verdict.silent_incorrect_success


def test_disagreement_collateral_indeterminate_and_over_halt_are_distinct() -> None:
    disagreement = classify_patient_trial(
        actor_reported_success=True,
        halted=False,
        rest_records=[exact_record()],
        db_records=[],
    )
    assert disagreement.primary_outcome is PrimaryOutcome.REST_DB_DISAGREEMENT
    assert disagreement.silent_incorrect_success

    collateral = classify_patient_trial(
        actor_reported_success=True,
        halted=False,
        rest_records=[exact_record()],
        db_records=[exact_record()],
        unexpected_db_deltas=["payments:+1"],
    )
    assert collateral.primary_outcome is PrimaryOutcome.COLLATERAL_WRITE

    indeterminate = classify_patient_trial(
        actor_reported_success=False,
        halted=True,
        rest_records=None,
        db_records=[],
    )
    assert indeterminate.primary_outcome is PrimaryOutcome.ORACLE_INDETERMINATE

    over_halt = classify_patient_trial(
        actor_reported_success=False,
        halted=True,
        rest_records=[],
        db_records=[],
        environment_healthy=True,
        task_feasible=True,
    )
    assert over_halt.primary_outcome is PrimaryOutcome.MISSING_WRITE
    assert over_halt.over_halt

    deficient = classify_patient_trial(
        actor_reported_success=False,
        halted=True,
        rest_records=[],
        db_records=[],
        unexpected_db_deltas=["patient_data:+0 (expected +1)"],
        environment_healthy=True,
        task_feasible=True,
    )
    assert deficient.primary_outcome is PrimaryOutcome.MISSING_WRITE
    assert deficient.over_halt
    assert "database delta contract" in deficient.detail

    readable_error = classify_patient_trial(
        actor_reported_success=False,
        halted=True,
        rest_records=[exact_record()],
        db_records=[exact_record()],
        task_feasible=True,
        execution_error="browser teardown failed",
    )
    assert readable_error.primary_outcome is PrimaryOutcome.CORRECT
    assert not readable_error.success
    assert readable_error.over_halt
    assert "execution error after oracle capture" in readable_error.detail


def test_exact_arm_delta_contract_catches_duplicates_and_collateral() -> None:
    compiled = EXPECTED_TABLE_DELTAS["compiled"]
    assert compiled["patient_data"] == 1
    assert compiled["history_data"] == 1
    assert compiled["api_log"] == 13
    assert compiled["log"] == 311
    assert compiled["uuid_registry"] == 14
    assert EXPECTED_TABLE_DELTAS["agent"]["api_log"] == 1
    assert EXPECTED_TABLE_DELTAS["agent"]["log"] == 229
    assert EXPECTED_TABLE_DELTAS["api"] == {
        "api_log": 2,
        "log": 16,
        "log_comment_encrypt": 16,
        "patient_data": 1,
        "uuid_mapping": 1,
        "uuid_registry": 2,
    }
    before = {table: 2 for table in compiled}
    before["payments"] = 0
    correct_after = {
        table: before.get(table, 0) + delta for table, delta in compiled.items()
    }
    correct_after["payments"] = 0
    violations, all_deltas = audit_table_deltas(before, correct_after, arm="compiled")
    assert violations == []
    assert all_deltas["history_data"] == 1
    assert all_deltas["patient_data"] == 1
    assert all_deltas["payments"] == 0

    duplicate_after = dict(
        correct_after, patient_data=correct_after["patient_data"] + 1
    )
    assert "patient_data:+2 (expected +1)" in unexpected_table_deltas(
        before, duplicate_after, arm="compiled"
    )

    collateral_after = dict(correct_after, payments=1)
    assert "payments:+1 (expected +0)" in unexpected_table_deltas(
        before, collateral_after, arm="compiled"
    )


def test_pre_trial_counts_require_three_identical_complete_inventories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventories = iter(
        [
            {"log": 1},
            {"log": 2},
            {"log": 2},
            {"log": 2},
        ]
    )

    class Fixture:
        @staticmethod
        def table_counts() -> dict[str, int]:
            return next(inventories)

    monkeypatch.setattr("scripts.openemr_local_demo.time.sleep", lambda _s: None)
    assert _stable_pre_trial_counts(Fixture()) == {  # type: ignore[arg-type]
        "log": 2
    }


def test_iframe_confirm_locator_and_explicit_save_event_marker(tmp_path: Path) -> None:
    class Locator:
        def __init__(self, visible: bool) -> None:
            self.visible = visible

        def is_visible(self) -> bool:
            return self.visible

    class Frame:
        def __init__(self, visible: bool) -> None:
            self.visible = visible

        def locator(self, selector: str) -> Locator:
            assert selector == "#confirmCreate"
            return Locator(self.visible)

    class Page:
        frames = [Frame(False), Frame(True)]

        @staticmethod
        def wait_for_timeout(_milliseconds: int) -> None:
            raise AssertionError("visible locator should be found immediately")

    locator = _confirm_create_locator(Page(), timeout_s=0.1)
    assert locator.is_visible()

    recording = tmp_path / "recording"
    recording.mkdir()
    (recording / "events.jsonl").write_text(
        json.dumps({"i": 7, "kind": "click", "x": 1, "y": 2}) + "\n"
    )
    (recording / "openemr-save-event.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "event_index": 7,
                "dom_target": "#confirmCreate inside duplicate-check iframe",
            }
        )
    )
    assert _marked_save_step_id(recording) == "step_007"
    (recording / "events.jsonl").write_text(
        json.dumps({"i": 7, "kind": "key", "key": "Enter"}) + "\n"
    )
    with pytest.raises(FixtureError, match="confirmation click"):
        _marked_save_step_id(recording)

    postcondition = _duplicate_dialog_postcondition()
    assert postcondition.kind.value == "text_present"
    assert postcondition.text == "Confirm Create New Patient"


def test_recording_opens_real_contact_accordion_before_address_fields() -> None:
    source = inspect.getsource(record)
    contact = source.index('form.get_by_text("Contact", exact=True)')
    scroll = source.index("recorder.scroll(0, 600)")
    street = source.index('text_field("street"')
    assert contact < scroll < street
    country = source.index('select_by_label("country_code"')
    save_scroll = source.index("recorder.scroll(0, 600)", scroll + 1)
    save = source.index('form.locator("#create")')
    assert country < save_scroll < save


def test_form_frame_requires_authentic_session_shell() -> None:
    class Frame:
        url = "http://openemr/interface/new/new_comprehensive.php"

        @staticmethod
        def evaluate(script: str) -> bool:
            assert "top.restoreSession" in script
            return True

    class Page:
        @staticmethod
        def frame(*, name: str) -> Frame:
            assert name == "openadapt-new-patient"
            return Frame()

    assert isinstance(_form_frame(Page()), Frame)

    class DetachedFrame(Frame):
        @staticmethod
        def evaluate(_script: str) -> bool:
            return False

    class DetachedPage(Page):
        @staticmethod
        def frame(*, name: str) -> DetachedFrame:
            assert name == "openadapt-new-patient"
            return DetachedFrame()

    with pytest.raises(FixtureError, match="detached from its session shell"):
        _form_frame(DetachedPage())


def test_openemr_adapter_binds_unique_control_to_exact_form_context() -> None:
    class Control:
        @staticmethod
        def count() -> int:
            return 1

        @staticmethod
        def bounding_box() -> dict[str, float]:
            # Playwright reports frame-locator boxes in main-frame coordinates.
            return {"x": 120, "y": 70, "width": 20, "height": 20}

    class Frame:
        url = "http://openemr/interface/new/new_comprehensive.php"

        @staticmethod
        def evaluate(script: str, _arg: object = None) -> object:
            if "top.restoreSession" in script:
                return True
            assert "document.elementFromPoint" in script
            return {
                "selector": "#form_state",
                "role": "combobox",
                "fieldname": "state",
                "target_kind": "field",
                "target_id": "state",
            }

        @staticmethod
        def locator(selector: str) -> Control:
            assert selector == "#form_state"
            return Control()

    class Iframe:
        @staticmethod
        def count() -> int:
            return 1

        @staticmethod
        def bounding_box() -> dict[str, float]:
            return {"x": 100, "y": 50, "width": 500, "height": 700}

        @staticmethod
        def evaluate(script: str, point: list[int]) -> bool:
            assert "document.elementFromPoint" in script
            assert point == [130, 80]
            return True

    class Page:
        viewport_size = {"width": 1280, "height": 800}

        @staticmethod
        def locator(selector: str) -> Iframe:
            assert selector == 'iframe[name="openadapt-new-patient"]'
            return Iframe()

        @staticmethod
        def frame(*, name: str) -> Frame:
            assert name == "openadapt-new-patient"
            return Frame()

    backend = OpenEMRPlaywrightBackend(Page())  # type: ignore[arg-type]
    locator = backend.structural_locator_at(130, 80)
    assert locator == StructuralLocator(selector="#form_state", role="combobox")
    handle = backend.locate_structural(locator)
    assert handle is not None and handle.point == (130, 80)
    assert json.loads(backend.structured_text_at(130, 80) or "") == {
        "form_path": "/interface/new/new_comprehensive.php",
        "target_kind": "field",
        "target_id": "state",
    }


def test_openemr_adapter_resolves_confirmation_inside_unique_modal_frame() -> None:
    class Confirm:
        @staticmethod
        def count() -> int:
            return 1

        @staticmethod
        def is_visible() -> bool:
            return True

        @staticmethod
        def bounding_box() -> dict[str, float]:
            return {"x": 200, "y": 180, "width": 40, "height": 20}

        @staticmethod
        def evaluate(_script: str) -> bool:
            return True

    class Frame:
        @staticmethod
        def locator(selector: str) -> Confirm:
            assert selector == "#confirmCreate"
            return Confirm()

    class Modal:
        @staticmethod
        def count() -> int:
            return 1

        @staticmethod
        def evaluate(_script: str, point: list[int]) -> bool:
            assert point == [220, 190]
            return True

    class Page:
        frames = [Frame()]
        viewport_size = {"width": 1280, "height": 800}

        @staticmethod
        def locator(selector: str) -> Modal:
            assert selector == "#modalframe"
            return Modal()

    backend = OpenEMRPlaywrightBackend(Page())  # type: ignore[arg-type]
    locator = backend.structural_locator_at(210, 185)
    assert locator == StructuralLocator(
        selector="openemr://confirm-create",
        role="button",
        name="Create New Patient",
    )
    handle = backend.locate_structural(locator)
    assert handle is not None and handle.point == (220, 190)
    assert json.loads(backend.structured_text_at(210, 185) or "")["target_id"] == (
        "confirm_create"
    )


def test_browser_setup_uses_authentic_shell_and_refuses_telemetry() -> None:
    source = inspect.getsource(_preauthenticate_browser)
    assert "window.navigateTab(path, name)" in source
    assert "window.activateTabByName(name, true)" in source
    assert "#allowTelemetry" in source
    assert "is_checked()" in source
    assert "telemetry.uncheck()" in source
    assert 'name="Ask again later"' in source


def test_recording_state_resets_before_issuing_oracle_token() -> None:
    calls: list[str] = []

    class Fixture:
        api_base_url = "https://fixture"

        def reset(self) -> str:
            calls.append("reset")
            return "a" * 64

        def token_session(self, role: str) -> FakeSession:
            calls.append("token:" + role)
            return FakeSession({"data": []})

    oracle = _reset_recording_state(Fixture())  # type: ignore[arg-type]
    assert isinstance(oracle, OpenEMRPatientOracle)
    assert calls == ["reset", "token:oracle"]


def test_full_evidence_is_private_hash_bound_and_non_json_safe(tmp_path: Path) -> None:
    evidence = CapturedEvidence(
        rest_records=[exact_record()],
        db_records=[exact_record()],
        before_table_counts={"patient_data": 1},
        after_table_counts={"patient_data": 2},
        all_table_deltas={"patient_data": 1},
        before_non_target_patient_sha256="a" * 64,
        after_non_target_patient_sha256="a" * 64,
        before_history_data_sha256="b" * 64,
        after_history_data_sha256="c" * 64,
        after_non_target_history_sha256="b" * 64,
        target_history_count=1,
        history_binding_readable=True,
        delta_violations=[],
        errors=[],
    )
    metadata = _persist_evidence(
        tmp_path / "trial",
        evidence=evidence,
        environment_identity={"source_proofs": {"x": b"non-json"}},
        relative_root=tmp_path,
        final_screenshot=b"PNG",
        action_log=["1: synthetic action"],
    )
    for name in ("oracle-evidence.json", "final.png", "actions.json"):
        assert stat.S_IMODE((tmp_path / "trial" / name).stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "trial").stat().st_mode) == 0o700
    payload = json.loads((tmp_path / "trial/oracle-evidence.json").read_text())
    assert payload["before_table_counts"] == {"patient_data": 1}
    assert payload["all_table_deltas"] == {"patient_data": 1}
    assert payload["rest_records"] == [exact_record()]
    assert metadata["evidence_relative_path"] == "trial/oracle-evidence.json"
    assert len(metadata["evidence_sha256"]) == 64
    assert len(metadata["final_screenshot_sha256"]) == 64
    with pytest.raises(FixtureError, match="refusing overwrite"):
        _persist_evidence(
            tmp_path / "trial",
            evidence=evidence,
            environment_identity={},
            relative_root=tmp_path,
        )


def test_free_token_count_guard_refuses_before_paid_cap_overshoot() -> None:
    class Count:
        def __init__(self, input_tokens: int) -> None:
            self.input_tokens = input_tokens

    class Messages:
        def __init__(self, count: int) -> None:
            self.count = count
            self.count_calls = 0
            self.paid_calls = 0

        def count_tokens(self, **_kwargs: object) -> Count:
            self.count_calls += 1
            return Count(self.count)

        def create(self, **_kwargs: object) -> str:
            self.paid_calls += 1
            return "paid-response"

    class Beta:
        def __init__(self, messages: Messages) -> None:
            self.messages = messages

    class Client:
        def __init__(self, messages: Messages) -> None:
            self.beta = Beta(messages)

    kwargs = {
        "model": "claude-sonnet-5",
        "messages": [{"role": "user", "content": "synthetic"}],
        "tools": [],
        "betas": [],
        "max_tokens": 4096,
    }
    ledger = __import__(
        "openadapt_flow.benchmark.agent_baseline", fromlist=["UsageLedger"]
    ).UsageLedger()

    allowed_messages = Messages(10_000)
    allowed = _BudgetedAgentClient(Client(allowed_messages), ledger, 0.20)
    assert allowed.beta.messages.create(**kwargs) == "paid-response"
    assert allowed_messages.count_calls == 1
    assert allowed_messages.paid_calls == 1

    capped_messages = Messages(10_000)
    capped = _BudgetedAgentClient(Client(capped_messages), ledger, 0.17)
    with pytest.raises(AgentBudgetRefusal, match="per-run cap"):
        capped.beta.messages.create(**kwargs)
    assert capped_messages.count_calls == 1
    assert capped_messages.paid_calls == 0

    long_messages = Messages(AGENT_STANDARD_INPUT_LIMIT - AGENT_TOKEN_COUNT_MARGIN + 1)
    long_context = _BudgetedAgentClient(Client(long_messages), ledger, 10.0)
    with pytest.raises(AgentBudgetRefusal, match="long-context"):
        long_context.beta.messages.create(**kwargs)
    assert long_messages.paid_calls == 0

    ambiguous_messages = Messages(10_000)

    def ambiguous_create(**_kwargs: object) -> str:
        ambiguous_messages.paid_calls += 1
        raise TimeoutError("response lost after possible provider acceptance")

    ambiguous_messages.create = ambiguous_create  # type: ignore[method-assign]
    ambiguous = _BudgetedAgentClient(Client(ambiguous_messages), ledger, 1.5)
    with pytest.raises(TimeoutError):
        ambiguous.beta.messages.create(**kwargs)
    assert ambiguous.messages.spend_indeterminate
    assert ambiguous.messages.paid_attempts == 1

    class MalformedResponse:
        usage = object()

    unaccounted_messages = Messages(10_000)

    def unaccounted_create(**_kwargs: object) -> MalformedResponse:
        unaccounted_messages.paid_calls += 1
        return MalformedResponse()

    unaccounted_messages.create = unaccounted_create  # type: ignore[method-assign]
    atomic_ledger = _AtomicUsageLedger()
    unaccounted = _BudgetedAgentClient(Client(unaccounted_messages), atomic_ledger, 1.5)
    response = unaccounted.beta.messages.create(**kwargs)
    with pytest.raises(AttributeError):
        atomic_ledger.record(response.usage)
    assert atomic_ledger.api_calls == 0
    assert unaccounted.messages.spend_is_indeterminate(atomic_ledger.api_calls)


def test_target_history_auxiliary_is_bound_to_created_patient_pid() -> None:
    before_counts = {table: 2 for table in EXPECTED_TABLE_DELTAS["compiled"]}
    after_counts = {
        table: before_counts[table] + delta
        for table, delta in EXPECTED_TABLE_DELTAS["compiled"].items()
    }

    class Fixture:
        def db_records(self) -> list[dict[str, object]]:
            return [exact_record()]

        def table_counts(self) -> dict[str, int]:
            return after_counts

        def non_target_patient_data_sha256(self) -> str:
            return "p" * 64

        def history_data_sha256(self, *, exclude_pid: int | None = None) -> str:
            assert exclude_pid in (None, 11)
            return "h" * 64 if exclude_pid == 11 else "a" * 64

        def history_count_for_pid(self, pid: int) -> int:
            assert pid == 11
            return 1

    oracle = OpenEMRPatientOracle(
        "https://fixture", FakeSession({"data": [exact_record()]}), timeout_s=0.1
    )
    evidence = _capture_post_evidence(
        oracle,
        Fixture(),  # type: ignore[arg-type]
        before_counts=before_counts,
        before_non_target_patient_sha256="p" * 64,
        before_history_data_sha256="h" * 64,
        arm="compiled",
    )
    assert evidence.readable
    assert evidence.target_history_count == 1
    assert evidence.delta_violations == []

    class WrongHistoryFixture(Fixture):
        def history_count_for_pid(self, pid: int) -> int:
            assert pid == 11
            return 0

    wrong = _capture_post_evidence(
        oracle,
        WrongHistoryFixture(),  # type: ignore[arg-type]
        before_counts=before_counts,
        before_non_target_patient_sha256="p" * 64,
        before_history_data_sha256="h" * 64,
        arm="compiled",
    )
    assert wrong.delta_violations == ["history_data:target-pid-count=0 (expected 1)"]


def test_no_target_history_is_readable_for_a_halted_no_write() -> None:
    before_counts = {table: 2 for table in EXPECTED_TABLE_DELTAS["compiled"]}

    class Fixture:
        def db_records(self) -> list[dict[str, object]]:
            return []

        def table_counts(self) -> dict[str, int]:
            return before_counts

        def non_target_patient_data_sha256(self) -> str:
            return "p" * 64

        def history_data_sha256(self, *, exclude_pid: int | None = None) -> str:
            assert exclude_pid is None
            return "h" * 64

    oracle = OpenEMRPatientOracle(
        "https://fixture", FakeSession({"data": []}), timeout_s=0.1
    )
    evidence = _capture_post_evidence(
        oracle,
        Fixture(),  # type: ignore[arg-type]
        before_counts=before_counts,
        before_non_target_patient_sha256="p" * 64,
        before_history_data_sha256="h" * 64,
        arm="compiled",
    )
    assert evidence.readable
    assert evidence.db_records == []
    assert evidence.target_history_count == 0
    assert evidence.after_non_target_history_sha256 == "h" * 64


def test_bundle_preflight_rejects_unrelated_effectless_bundle(tmp_path: Path) -> None:
    from openadapt_flow.ir import Workflow

    Workflow(name="unrelated", params={}).save(tmp_path)
    with pytest.raises(FixtureError, match="benchmark contract"):
        _validate_benchmark_bundle(tmp_path)


def test_artifact_tree_manifest_refuses_symlink_escape(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("do not traverse")
    (tree / "escape").symlink_to(outside)
    with pytest.raises(FixtureError, match="symlink"):
        _tree_manifest_sha256(tree)

    broken_root = tmp_path / "broken-root"
    broken_root.symlink_to(tmp_path / "absent-directory")
    with pytest.raises(FixtureError, match="real directory"):
        _tree_manifest_sha256(broken_root)


def test_shared_result_schema_enforces_model_free_arms_and_full_accounting() -> None:
    for arm in ("compiled", "api"):
        with pytest.raises(ValueError, match="model-free"):
            replace(make_row(arm, "baseline", 1), model_calls=1)

    rows = [
        make_row("compiled", "baseline", 1),
        TrialRow(
            arm="agent",
            condition="baseline",
            trial=1,
            primary_outcome=PrimaryOutcome.DUPLICATE_WRITE.value,
            success=False,
            silent_incorrect_success=True,
            over_halt=False,
            wall_s=2.0,
            model_calls=2,
            input_tokens=100,
            output_tokens=20,
            cost_usd=0.1,
            baseline_snapshot_sha256="a" * 64,
        ),
    ]
    aggregate = aggregate_rows(rows)
    assert aggregate["compiled"]["baseline"]["model_calls_total"] == 0
    assert aggregate["agent"]["baseline"]["silent_incorrect_success_count"] == 1
    assert aggregate["agent"]["baseline"]["cost_usd_total"] == 0.1


def test_matrix_and_publication_gates_are_exactly_3_and_10_per_cell() -> None:
    assert ARMS == ("compiled", "agent", "api")
    assert CONDITIONS == ("baseline", "ui_cosmetic_v1")
    initial = _plan(3)
    publication = _plan(10)
    assert initial["total_trials"] == 18
    assert publication["total_trials"] == 60

    rows = [
        make_row(arm, condition, trial)
        for arm in ARMS
        for condition in CONDITIONS
        for trial in range(1, 11)
    ]
    ready, reasons = publication_gate(rows)
    assert ready and reasons == []
    not_ready, reasons = publication_gate(rows[:-1])
    assert not not_ready
    assert "exactly 10 required" in reasons[0]


def test_paid_matrix_is_refused_before_fixture_or_model_use(tmp_path: Path) -> None:
    class NeverFixture:
        def up(self) -> None:
            raise AssertionError("must refuse before touching fixture")

    with pytest.raises(FixtureError, match="paid agent arm"):
        run_matrix(
            NeverFixture(),  # type: ignore[arg-type]
            tmp_path / "bundle",
            tmp_path / "out",
            n=3,
            allow_paid_agent=False,
            max_cost_per_run_usd=1.5,
            max_total_agent_cost_usd=12.0,
            headed=False,
        )


@pytest.mark.parametrize("bad_cap", [float("nan"), float("inf"), float("-inf")])
def test_paid_matrix_refuses_non_finite_caps_before_fixture_or_model_use(
    tmp_path: Path, bad_cap: float
) -> None:
    class NeverFixture:
        def up(self) -> None:
            raise AssertionError("must refuse before touching fixture")

    with pytest.raises(FixtureError, match="max-cost-per-run-usd"):
        run_matrix(
            NeverFixture(),  # type: ignore[arg-type]
            tmp_path / "bundle",
            tmp_path / "per-run-out",
            n=3,
            allow_paid_agent=True,
            max_cost_per_run_usd=bad_cap,
            max_total_agent_cost_usd=9.0,
            headed=False,
        )

    with pytest.raises(FixtureError, match="max-total-agent-cost-usd"):
        run_matrix(
            NeverFixture(),  # type: ignore[arg-type]
            tmp_path / "bundle",
            tmp_path / "total-out",
            n=3,
            allow_paid_agent=True,
            max_cost_per_run_usd=1.5,
            max_total_agent_cost_usd=bad_cap,
            headed=False,
        )


def test_paid_matrix_requires_total_cap_for_complete_equal_matrix(
    tmp_path: Path,
) -> None:
    class NeverFixture:
        def up(self) -> None:
            raise AssertionError("must refuse before touching fixture")

    with pytest.raises(FixtureError, match="complete matrix's hard ceiling"):
        run_matrix(
            NeverFixture(),  # type: ignore[arg-type]
            tmp_path / "bundle",
            tmp_path / "out",
            n=3,
            allow_paid_agent=True,
            max_cost_per_run_usd=1.5,
            max_total_agent_cost_usd=8.99,
            headed=False,
        )


def test_model_free_plan_is_equal_scoped_and_never_publication_eligible() -> None:
    plan = _plan(10, model_free=True)
    assert plan["run_mode"] == "model_free"
    assert plan["arms"] == ["compiled", "api"]
    assert plan["omitted_arms"] == ["agent"]
    assert plan["trials_per_cell"] == 10
    assert plan["total_trials"] == 40
    assert plan["publication_eligible"] is False
    assert "never a complete three-arm" in plan["scope"]


def test_model_free_run_skips_agent_calls_and_is_not_publishable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.openemr_local_demo as driver

    class FakeFixture:
        def up(self) -> None:
            return None

        def baseline_hash(self) -> str:
            return "a" * 64

        def image_identity(self) -> str:
            return "sha256:" + "b" * 64

        def source_identity(self) -> dict[str, str]:
            return {"source": "pinned"}

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("model-free mode must not touch the agent/model")

    monkeypatch.setattr(driver, "_validate_benchmark_bundle", lambda path: None)
    monkeypatch.setattr(driver, "_tree_manifest_sha256", lambda path: "c" * 64)
    monkeypatch.setattr(driver, "run_agent_trial", forbidden)
    monkeypatch.setattr(
        driver,
        "run_compiled_trial",
        lambda *args, **kwargs: make_row(
            "compiled", kwargs["condition"], kwargs["trial"]
        ),
    )
    monkeypatch.setattr(
        driver,
        "run_api_trial",
        lambda *args, **kwargs: make_row("api", kwargs["condition"], kwargs["trial"]),
    )

    out = tmp_path / "model-free"
    rows = run_matrix(
        FakeFixture(),  # type: ignore[arg-type]
        tmp_path / "bundle",
        out,
        n=10,
        allow_paid_agent=False,
        max_cost_per_run_usd=1.5,
        max_total_agent_cost_usd=None,
        headed=False,
        model_free=True,
    )

    assert len(rows) == 40
    assert {row.arm for row in rows} == {"compiled", "api"}
    result = json.loads((out / "results.json").read_text())
    assert result["status"] == "model_free_subset_complete"
    assert result["selected_subset_complete"] is True
    assert result["full_matrix_complete"] is False
    assert result["publication_ready"] is False
    assert result["omitted_arms"] == ["agent"]
    assert result["environment"]["paid_agent_authorized"] is False
    assert result["environment"]["agent"]["model_calls_permitted"] is False
    assert any("intentionally omitted" in item for item in result["incomplete_reasons"])


def test_model_free_run_rejects_paid_flags_before_fixture_use(tmp_path: Path) -> None:
    class NeverFixture:
        def up(self) -> None:
            raise AssertionError("must refuse before touching fixture")

    with pytest.raises(FixtureError, match="cannot be combined"):
        run_matrix(
            NeverFixture(),  # type: ignore[arg-type]
            tmp_path / "bundle",
            tmp_path / "out",
            n=3,
            allow_paid_agent=True,
            max_cost_per_run_usd=1.5,
            max_total_agent_cost_usd=None,
            headed=False,
            model_free=True,
        )
