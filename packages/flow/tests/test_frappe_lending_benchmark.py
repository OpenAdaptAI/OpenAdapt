"""Focused, offline tests for the Frappe Lending benchmark scaffold."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from benchmark.frappe_lending.fixture import (
    EXPECTED_TABLE_DELTAS,
    SOURCE_LABELS,
    FixtureError,
    FrappeFixture,
    audit_table_deltas,
)
from openadapt_flow.benchmark.frappe_lending import (
    ARMS,
    CONDITIONS,
    FrappeLoanApplicationOracle,
    LoanApplicationSpec,
    PrimaryOutcome,
    TrialRow,
    aggregate_rows,
    canonical_records,
    classify_trial,
    loan_application_api_binding,
    loan_application_effects,
    publication_gate,
    records_sha256,
)
from openadapt_flow.runtime.effects import EffectKind
from scripts.frappe_lending_demo import (
    AGENT_MAX_SINGLE_CALL_RESERVE_USD,
    MAX_SCREENSHOT_BYTES,
    RECORDING_FAILED_MARKER,
    RECORDING_READY_MARKER,
    CapturedEvidence,
    FrappePlaywrightBackend,
    _agent_prompt,
    _persist_evidence,
    _plan,
    _preauthenticate_browser,
    _refuse_indeterminate_paid_continuation,
    _row,
    _tree_manifest_sha256,
    _validate_bundle_contract,
    _validate_recording_ready,
    record,
    run_matrix,
)


def exact_record(**updates: object) -> dict[str, object]:
    spec = LoanApplicationSpec()
    row: dict[str, object] = {
        "name": "ACC-LOAP-2026-00001",
        **spec.fields,
        "docstatus": 0,
    }
    row.update(updates)
    return row


def make_row(
    arm: str,
    condition: str,
    trial: int,
    *,
    snapshot: str = "a" * 64,
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


def test_lock_contains_exact_source_and_oci_pins() -> None:
    lock_path = (
        Path(__file__).resolve().parents[1]
        / "benchmark/frappe_lending/environment.lock.json"
    )
    lock = json.loads(lock_path.read_text())
    assert lock["upstreams"]["lending"]["commit"] == (
        "caed066b6636075634418f4f0382798b60c0e188"
    )
    assert lock["upstreams"]["frappe"]["commit"] == (
        "73decbb00106a12c4e854c98dce8c0e3f42f514e"
    )
    assert lock["upstreams"]["erpnext"]["commit"] == (
        "9d5c7605b8eae7fb5aaf9efd00a778adae2daeb1"
    )
    assert lock["upstreams"]["frappe_docker"]["commit"] == (
        "c004361e790125ed13aaa933d11f7838711a8960"
    )
    assert all("@sha256:" in image for image in lock["services"].values())


def test_fixture_binds_loopback_and_rechecks_app_heads_inside_build() -> None:
    root = Path(__file__).resolve().parents[1] / "benchmark/frappe_lending"
    compose = yaml.safe_load((root / "compose.yml").read_text())
    assert compose["services"]["frontend"]["ports"] == [
        "127.0.0.1:${FRAPPE_PORT:-8080}:8080"
    ]
    create_site = compose["services"]["create-site"]["command"][0]
    assert "bench new-site \\\n" in create_site
    assert '--set-default \\\n  "${SITE_NAME}"' in create_site

    containerfile = (root / "Containerfile.pinned").read_text()
    for app, arg in (
        ("frappe", "FRAPPE_COMMIT"),
        ("erpnext", "ERPNEXT_COMMIT"),
        ("lending", "LENDING_COMMIT"),
    ):
        assert f"ARG {arg}" in containerfile
        assert f'git -C apps/{app} rev-parse HEAD)" = "${{{arg}}}"' in containerfile

    fixture_source = (root / "fixture.py").read_text()
    for arg in (
        "FRAPPE_COMMIT",
        "ERPNEXT_COMMIT",
        "LENDING_COMMIT",
        "FRAPPE_DOCKER_COMMIT",
    ):
        assert f'"{arg}=' in fixture_source

    for upstream in ("frappe", "erpnext", "lending", "frappe_docker"):
        assert f"ai.openadapt.benchmark.{upstream}.commit" in containerfile
        assert f'"{upstream}": "ai.openadapt.benchmark.{upstream}.commit"' in (
            fixture_source
        )


def test_bootstrap_contract_completes_setup_and_verifies_role_boundary() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "benchmark/frappe_lending/bootstrap_fixture.py"
    ).read_text()
    assert "enable_setup_wizard_complete(app_name)" in source
    assert "if not frappe.is_setup_complete()" in source
    assert source.index("frappe.is_setup_complete()") < source.index(
        'import_module("lending.tests.test_utils")'
    )
    assert "frappe.in_test = True" in source
    assert "frappe.in_test = previous_in_test" in source
    assert "test_employee import make_employee" not in source
    assert 'global_defaults = frappe.get_single("Global Defaults")' in source
    assert 'global_defaults.country = "United States"' in source
    assert 'frappe.db.get_default("country") != "United States"' in source
    assert '"Loan Manager"' in source
    assert 'reset_perms("Loan Application")' in source
    assert 'add_permission(\n    "Loan Application",' in source
    assert source.index('reset_perms("Loan Application")') < source.index(
        'add_permission(\n    "Loan Application",'
    )
    assert 'reset_perms("Company")' in source
    assert (
        'add_permission("Company", "Loan Manager", permlevel=0, ptype="read")' in source
    )
    assert 'get_valid_perms("Company", user=ACTOR)' in source
    assert 'frappe.has_permission("Company", forbidden, user=ACTOR)' in source
    assert 'reset_perms("Loan Origination Settings")' in source
    assert (
        '"Loan Origination Settings", "Loan Manager", permlevel=0, ptype="read"'
        in source
    )
    assert 'get_valid_perms(\n    "Loan Origination Settings", user=ACTOR' in source
    assert (
        'frappe.has_permission("Loan Origination Settings", forbidden, user=ACTOR)'
        in source
    )
    assert 'reset_perms("Loan Purpose")' in source
    assert (
        'add_permission("Loan Purpose", "Loan Manager", permlevel=0, ptype="read")'
        in source
    )
    assert 'get_valid_perms("Loan Purpose", user=ACTOR)' in source
    assert 'frappe.has_permission("Loan Purpose", forbidden, user=ACTOR)' in source
    assert "No Customer access" in source
    assert '"doctype": "Custom DocPerm"' not in source
    assert 'get_valid_perms("Loan Application", user=ACTOR)' in source
    assert 'get_valid_perms("Loan Application", user=ORACLE)' in source
    assert "check_password(ACTOR, ACTOR_PASSWORD" in source
    assert "check_password(ORACLE, ORACLE_PASSWORD" in source
    assert 'frappe.has_permission("Loan Application", "create", user=ACTOR)' in source
    assert 'frappe.has_permission("Loan Application", forbidden, user=ORACLE)' in source
    assert 'series_key = f"ACC-LOAP-{now_datetime().year}-"' in source


def test_bootstrap_runs_as_one_fail_fast_console_cell(tmp_path: Path) -> None:
    fixture = FrappeFixture(tmp_path)
    fixture.runtime_env.write_text("fixture=test\n")
    captured: dict[str, bytes | None] = {}

    def compose(*_args: str, input_bytes: bytes | None = None) -> bytes:
        captured["input"] = input_bytes
        return b"OPENADAPT_FRAPPE_FIXTURE_READY\n"

    fixture._compose = compose  # type: ignore[method-assign]
    fixture.bootstrap()
    payload = (captured["input"] or b"").decode()
    assert payload.startswith(
        "namespace = {'__name__': '__openadapt_frappe_fixture__'}; exec(compile("
    )
    assert payload.count("exec(compile(") == 1
    assert "namespace, namespace)" in payload
    assert "before_tests()" in payload


def test_bootstrap_readiness_marker_cannot_be_forged_by_echoed_source() -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "benchmark/frappe_lending/bootstrap_fixture.py"
    ).read_text()
    assert "OPENADAPT_FRAPPE_FIXTURE_READY" not in source
    assert 'print("OPENADAPT_FRAPPE_" + "FIXTURE_READY")' in source

    fixture = FrappeFixture(Path("/tmp/unused-frappe-bootstrap-echo"))
    fixture._compose = lambda *_args, **_kwargs: source.encode()  # type: ignore[method-assign]
    with pytest.raises(FixtureError, match="readiness sentinel"):
        fixture.bootstrap()


def test_login_and_supported_customer_route_are_exact_shared_setup() -> None:
    calls: list[tuple[object, ...]] = []
    spec = LoanApplicationSpec()

    class Locator:
        def fill(self, value: str) -> None:
            calls.append(("fill", value))

        def click(self) -> None:
            calls.append(("click",))

        def wait_for(self, **kwargs: object) -> None:
            calls.append(("wait_for", kwargs))

    class Page:
        def goto(self, url: str) -> None:
            calls.append(("goto", url))

        def locator(self, selector: str) -> Locator:
            calls.append(("locator", selector))
            return Locator()

        def get_by_role(self, role: str, *, name: str, exact: bool) -> Locator:
            calls.append(("get_by_role", role, name, exact))
            return Locator()

        def wait_for_url(self, pattern: str, *, timeout: int) -> None:
            calls.append(("wait_for_url", pattern, timeout))

        def evaluate(self, expression: str, arg: object | None = None) -> object | None:
            calls.append(("evaluate", expression, arg))
            if arg is not None:
                return None
            return {
                "doctype": "Loan Application",
                "applicant_type": spec.applicant_type,
                "applicant": spec.applicant,
                "company": spec.company,
                "repayment_method": spec.repayment_method,
                "applicant_control_status": "Read",
                "applicant_disabled": True,
                "orphan_modal_backdrops": 0,
                "body_modal_open": False,
            }

    backend = type("Backend", (), {"page": Page()})()
    fixture = type("Fixture", (), {"base_url": "http://fixture"})()
    _preauthenticate_browser(backend, fixture)  # type: ignore[arg-type]
    assert ("get_by_role", "button", "Continue", True) in calls
    assert ("wait_for_url", "**/desk**", 60_000) in calls
    assert (
        "wait_for_url",
        "**/loan-application/new-loan-application-*",
        60_000,
    ) in calls
    route_calls = [call for call in calls if call[0] == "evaluate" and call[2]]
    assert len(route_calls) == 1
    assert "frappe.route_options" in str(route_calls[0][1])
    assert "frappe.model.get_new_doc" in str(route_calls[0][1])
    assert "frappe.new_doc(" not in str(route_calls[0][1])
    assert route_calls[0][2] == {
        "applicantType": spec.applicant_type,
        "applicant": spec.applicant,
    }
    assert not any(
        call[:2]
        == ("goto", "http://fixture/desk/loan-application/new-loan-application-1")
        for call in calls
    )
    assert ("locator", 'input[data-fieldname="company"]') in calls
    assert not any(
        call[:2] == ("locator", '[data-fieldname="applicant"]') for call in calls
    )
    assert not any(call[:2] == ("locator", ".btn-login") for call in calls)


def test_record_resets_and_enters_loan_product_once() -> None:
    source = __import__("inspect").getsource(record)
    assert source.index("fixture.reset()") < source.index(
        "FrappePlaywrightBackend.launch"
    )
    assert "fixture.up()" not in source
    assert source.count('link_field("loan_product"') == 1
    assert 'link_field("company"' not in source
    assert 'link_field("applicant"' not in source
    phone = source.index('"applicant_phone_number"')
    scroll = source.index("recorder.scroll(0, 600)")
    product = source.index('link_field("loan_product"')
    assert phone < scroll < product
    amount = source.index('type_field("loan_amount"')
    repayment_scroll = source.index("recorder.scroll(0, 500)")
    repayment = source.index('"repayment_periods"', repayment_scroll)
    assert amount < repayment_scroll < repayment
    assert "exact visible" in source
    assert 'ul[role="listbox"]:not([hidden]) > [role="option"]' in source
    assert 'li[role="option"]' not in source
    assert source.index("suggestion.wait_for") < source.index('recorder.press("Enter")')
    assert "_capture_post_evidence(" in source
    assert "RECORDING_READY_MARKER" in source


def test_frappe_backend_binds_field_selector_to_fixed_form_context() -> None:
    spec = LoanApplicationSpec()

    class Page:
        def evaluate(self, expression: str, arg: object) -> object:
            if "document.elementFromPoint" in expression:
                return {
                    "selector": 'input[data-fieldname="loan_amount"]',
                    "role": "textbox",
                    "fieldname": "loan_amount",
                    "target_kind": "field",
                    "target_id": "loan_amount",
                }
            assert arg == {"target_kind": "field", "target_id": "loan_amount"}
            return json.dumps(
                {
                    "doctype": "Loan Application",
                    "applicant_type": spec.applicant_type,
                    "applicant": spec.applicant,
                    "company": spec.company,
                    "repayment_method": spec.repayment_method,
                    "target_kind": "field",
                    "target_id": "loan_amount",
                },
                separators=(",", ":"),
            )

    backend = FrappePlaywrightBackend(Page())  # type: ignore[arg-type]
    locator = backend.structural_locator_at(10, 20)
    assert locator is not None
    assert locator.selector == 'input[data-fieldname="loan_amount"]'
    identity = backend.structured_text_at(10, 20)
    assert identity is not None
    context = json.loads(identity)
    assert context == {
        "doctype": "Loan Application",
        "applicant_type": spec.applicant_type,
        "applicant": spec.applicant,
        "company": spec.company,
        "repayment_method": spec.repayment_method,
        "target_kind": "field",
        "target_id": "loan_amount",
    }


def test_frappe_backend_binds_save_action_to_fixed_form_context() -> None:
    spec = LoanApplicationSpec()

    class Page:
        def evaluate(self, expression: str, arg: object) -> object:
            if "document.elementFromPoint" in expression:
                return {
                    "selector": 'button.primary-action[data-label="Save"]',
                    "role": "button",
                    "name": "Save",
                    "target_kind": "action",
                    "target_id": "save",
                }
            assert arg == {"target_kind": "action", "target_id": "save"}
            return json.dumps(
                {
                    "doctype": "Loan Application",
                    "applicant_type": spec.applicant_type,
                    "applicant": spec.applicant,
                    "company": spec.company,
                    "repayment_method": spec.repayment_method,
                    "target_kind": "action",
                    "target_id": "save",
                },
                separators=(",", ":"),
            )

    backend = FrappePlaywrightBackend(Page())  # type: ignore[arg-type]
    locator = backend.structural_locator_at(1240, 24)
    assert locator is not None
    assert locator.selector == 'button.primary-action[data-label="Save"]'
    assert locator.role == "button" and locator.name == "Save"
    identity = backend.structured_text_at(1240, 24)
    assert identity is not None
    assert json.loads(identity)["target_kind"] == "action"
    assert json.loads(identity)["target_id"] == "save"


def test_agent_prompt_treats_applicant_and_company_as_fixed_shared_context() -> None:
    spec = LoanApplicationSpec()
    prompt = _agent_prompt(spec)
    assert spec.applicant in prompt
    assert spec.company in prompt
    assert spec.repayment_method in prompt
    assert "already selected fixed shared setup" in prompt
    assert "do not try to change them" in prompt
    assert "- Applicant Type:" not in prompt
    assert "- Applicant:" not in prompt
    assert "- Company:" not in prompt
    assert "- Repayment Method:" not in prompt


def test_recording_marker_binds_verified_contents(tmp_path: Path) -> None:
    recording = tmp_path / "recording"
    recording.mkdir()
    event = recording / "events.jsonl"
    event.write_text('{"kind":"click"}\n')
    marker = {
        "schema_version": 1,
        "baseline_snapshot_sha256": "a" * 64,
        "recording_manifest_sha256": _tree_manifest_sha256(recording),
        "primary_outcome": "correct",
        "rest_records_sha256": "b" * 64,
        "db_records_sha256": "b" * 64,
        "before_non_target_loan_sha256": "c" * 64,
        "after_non_target_loan_sha256": "c" * 64,
        "all_table_deltas": {"tabLoan Application": 1, "tabSeries": 0},
        "delta_violations": [],
        "oracle_errors": [],
    }
    ready = recording / RECORDING_READY_MARKER
    ready.write_text(json.dumps(marker))
    ready.chmod(0o600)
    assert _validate_recording_ready(recording)["primary_outcome"] == "correct"

    event.write_text('{"kind":"tampered"}\n')
    with pytest.raises(FixtureError, match="changed after"):
        _validate_recording_ready(recording)
    (recording / RECORDING_FAILED_MARKER).write_text("failed")
    with pytest.raises(FixtureError, match="unambiguous"):
        _validate_recording_ready(recording)


def test_bundle_preflight_requires_exact_final_effect_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openadapt_flow.ir import ActionKind, Workflow

    final = type(
        "Final",
        (),
        {
            "effects": loan_application_effects(),
            "action": ActionKind.CLICK,
            "risk": "reversible",
            "api_binding": None,
        },
    )()
    workflow = type(
        "FakeWorkflow",
        (),
        {
            "name": "frappe-lending-create-loan-application",
            "params": LoanApplicationSpec().params(),
            "steps": [final],
        },
    )()
    monkeypatch.setattr(Workflow, "load", lambda _path: workflow)
    assert _validate_bundle_contract(tmp_path) is workflow

    workflow.params = {**workflow.params, "applicant": LoanApplicationSpec().applicant}
    with pytest.raises(FixtureError, match="fixed Customer and prepopulated Company"):
        _validate_bundle_contract(tmp_path)
    workflow.params = LoanApplicationSpec().params()

    final.effects = final.effects[:-1]
    with pytest.raises(FixtureError, match="effect contract"):
        _validate_bundle_contract(tmp_path)


def test_image_identity_refuses_missing_or_wrong_source_labels(tmp_path: Path) -> None:
    fixture = FrappeFixture(tmp_path)
    labels = {
        label: fixture.lock["upstreams"][upstream]["commit"]
        for upstream, label in SOURCE_LABELS.items()
    }

    def runner(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        payload = [
            {"Id": "sha256:image", "RepoDigests": [], "Config": {"Labels": labels}}
        ]
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(payload).encode(), stderr=b""
        )

    fixture.runner = runner
    identity = fixture.image_identity()
    assert identity["id"] == "sha256:image"
    assert identity["source_labels"] == labels

    labels[SOURCE_LABELS["lending"]] = "0" * 40
    with pytest.raises(FixtureError, match="rebuild the pinned image"):
        fixture.image_identity()


def test_native_podman_build_keeps_pins_secret_and_explicit_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = FrappeFixture(
        tmp_path,
        build_engine="podman",
        podman_connection="openadapt-benchmark",
    )
    observed: list[str] = []

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        observed[:] = argv
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    fixture.runner = run
    monkeypatch.setattr(fixture, "runtime_preflight", lambda **_kwargs: None)
    monkeypatch.setattr(fixture, "prepare", lambda: None)
    exact_copy = tmp_path / "podman-build-secret-exact"
    unrelated = tmp_path / "podman-build-secret-unrelated"
    apps_path = (
        Path(__file__).resolve().parents[1] / "benchmark/frappe_lending/apps.json"
    )
    exact_copy.write_bytes(apps_path.read_bytes())
    unrelated.write_text("do not remove")
    fixture.build()

    assert observed[:4] == [
        "podman",
        "--connection",
        "openadapt-benchmark",
        "build",
    ]
    assert "--secret" in observed
    assert f"id=apps_json,src={apps_path}" in observed
    assert not exact_copy.exists()
    assert unrelated.read_text() == "do not remove"
    for upstream in ("frappe", "erpnext", "lending", "frappe_docker"):
        assert fixture.lock["upstreams"][upstream]["commit"] in " ".join(observed)
    assert all(
        observed[index + 1] != "--build-arg"
        for index, value in enumerate(observed[:-1])
        if value == "--build-arg"
    )


def test_podman_internal_store_probe_imports_checks_and_removes_layer(
    tmp_path: Path,
) -> None:
    fixture = FrappeFixture(
        tmp_path,
        build_engine="podman",
        podman_connection="benchmark",
    )
    calls: list[tuple[list[str], bytes | None]] = []
    image_ls_calls = 0

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        nonlocal image_ls_calls
        payload = kwargs.get("input")
        calls.append((argv, payload if isinstance(payload, bytes) else None))
        stdout = b""
        if argv[-4:-1] == ["image", "ls", "--quiet"]:
            image_ls_calls += 1
            stdout = b"sha256:probe\n" if image_ls_calls == 2 else b""
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr=b"")

    fixture.runner = runner
    fixture._container_store_write_probe("podman")
    argvs = [argv for argv, _payload in calls]
    assert any(
        argv[:4] == ["podman", "--connection", "benchmark", "import"] for argv in argvs
    )
    assert any(argv[-3:-1] == ["image", "rm"] for argv in argvs)
    imported = next(payload for argv, payload in calls if "import" in argv)
    assert imported is not None and len(imported) >= 1024 * 1024


def test_build_engine_is_allow_listed(tmp_path: Path) -> None:
    with pytest.raises(FixtureError, match="exactly 'docker' or 'podman'"):
        FrappeFixture(tmp_path, build_engine="shell-string")


@pytest.mark.parametrize("existing", ("baseline.sql", "baseline.sql.sha256"))
def test_snapshot_refuses_to_overwrite_any_existing_baseline_state(
    tmp_path: Path, existing: str
) -> None:
    fixture = FrappeFixture(tmp_path)
    (tmp_path / existing).write_text("existing evidence")
    with pytest.raises(FixtureError, match="refusing overwrite"):
        fixture.snapshot()


def test_atomic_private_write_never_deletes_racing_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "baseline.sql"
    real_link = os.link

    def racing_link(source: Path, target: Path) -> None:
        Path(target).write_bytes(b"racer-owned")
        real_link(source, target)

    monkeypatch.setattr(os, "link", racing_link)
    with pytest.raises(FixtureError, match="refusing overwrite"):
        FrappeFixture._write_private_atomic_new(destination, b"ours")
    assert destination.read_bytes() == b"racer-owned"
    assert not list(tmp_path.glob(".baseline.sql.tmp-*"))


def test_snapshot_and_hash_are_published_mode_0600(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = FrappeFixture(tmp_path)
    sql = b"-- pinned synthetic baseline\n" * 100
    monkeypatch.setattr(fixture, "_site_db_name", lambda: "fixture_db")

    def compose(*args: str, input_bytes: bytes | None = None) -> bytes:
        del input_bytes
        return sql if any("mariadb-dump" in arg for arg in args) else b""

    monkeypatch.setattr(fixture, "_compose", compose)
    ready: list[bool] = []
    monkeypatch.setattr(fixture, "_wait_http_ready", lambda: ready.append(True))
    digest = fixture.snapshot()
    assert ready == [True]
    assert digest == __import__("hashlib").sha256(sql).hexdigest()
    assert stat.S_IMODE(fixture.snapshot_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(fixture.snapshot_hash_path.stat().st_mode) == 0o600
    assert fixture.baseline_sha256() == digest


def test_artifact_manifest_refuses_symlink_escape(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("must not be hashed through link")
    (tree / "escape").symlink_to(outside)
    with pytest.raises(FixtureError, match="non-regular"):
        _tree_manifest_sha256(tree)


def test_typed_effects_and_api_binding_share_all_task_fields() -> None:
    spec = LoanApplicationSpec()
    assert set(spec.fixed_fields) == {
        "applicant",
        "applicant_type",
        "company",
        "repayment_method",
        "is_term_loan",
        "rate_of_interest",
    }
    assert set(spec.params()) == {
        "applicant_email_address",
        "applicant_phone_number",
        "loan_product",
        "loan_amount",
        "repayment_periods",
    }
    assert set(spec.fixed_fields).isdisjoint(spec.params())
    effects = loan_application_effects(spec)
    assert effects[0].kind is EffectKind.RECORD_WRITTEN
    assert effects[0].expected_count == 1
    assert effects[0].match["applicant"].literal == spec.applicant
    assert effects[0].match["applicant"].param is None
    resolved = [effect.resolve(spec.params()) for effect in effects]
    assert str(resolved[0].match["applicant"]) == spec.applicant
    fields = {effect.field for effect in effects[1:]}
    assert fields == set(spec.fields) - {"applicant", "loan_product"}
    fixed_effects = {effect.field: effect for effect in effects[1:]}
    assert fixed_effects["applicant_type"].value.literal == spec.applicant_type
    assert fixed_effects["applicant_type"].value.param is None
    assert fixed_effects["company"].value.literal == spec.company
    assert fixed_effects["company"].value.param is None
    assert fixed_effects["repayment_method"].value.literal == spec.repayment_method
    assert fixed_effects["repayment_method"].value.param is None
    assert (
        fixed_effects["applicant_phone_number"].value.literal
        == spec.applicant_phone_number
    )
    assert fixed_effects["applicant_phone_number"].value.param is None
    assert spec.params()["applicant_phone_number"] == spec.applicant_phone_input
    assert spec.fields["applicant_phone_number"] == spec.applicant_phone_number

    binding = loan_application_api_binding(spec)
    assert binding.method == "POST"
    assert binding.url_template == "/api/resource/Loan Application"
    assert set(binding.body_template) == set(spec.fields)
    assert binding.body_template["applicant"] == spec.applicant
    assert binding.body_template["applicant_type"] == spec.applicant_type
    assert binding.body_template["company"] == spec.company
    assert binding.body_template["repayment_method"] == spec.repayment_method
    assert (
        binding.body_template["applicant_phone_number"] == spec.applicant_phone_number
    )
    assert binding.body_template["loan_amount"] == 125000.0
    assert binding.body_template["repayment_periods"] == 18
    assert binding.body_template["is_term_loan"] == 1
    assert binding.body_template["rate_of_interest"] == 9.2
    assert {
        key for key, value in binding.body_template.items() if value == "{" + key + "}"
    } == {"applicant_email_address", "loan_product"}


def test_oracle_uses_filtered_read_only_rest_record_shape() -> None:
    session = FakeSession({"data": [exact_record()]})
    oracle = FrappeLoanApplicationOracle("http://fixture", session, timeout_s=0.1)
    state = oracle.capture()
    assert state.reachable
    assert state.records == [exact_record()]
    assert "Loan%20Application" in session.urls[0]
    assert "OpenAdapt+Synthetic+Applicant" in session.urls[0]


def test_numeric_rest_and_sql_formats_canonicalize_identically() -> None:
    rest = exact_record(
        loan_amount=125000.0,
        repayment_periods=18,
        is_term_loan=1,
        rate_of_interest=9.2,
        docstatus=0,
    )
    sql = exact_record(
        loan_amount="125000.000000",
        repayment_periods="18",
        is_term_loan="1",
        rate_of_interest="9.200000000",
        docstatus="0",
    )
    assert canonical_records([rest]) == canonical_records([sql])
    assert records_sha256([rest]) == records_sha256([sql])


def test_correct_result_ignores_actor_self_report_as_oracle() -> None:
    verdict = classify_trial(
        actor_reported_success=False,
        halted=False,
        rest_records=[exact_record()],
        db_records=[exact_record()],
    )
    assert verdict.primary_outcome is PrimaryOutcome.CORRECT
    assert verdict.success
    assert not verdict.silent_incorrect_success


@pytest.mark.parametrize(
    ("rest", "outcome"),
    [
        ([], PrimaryOutcome.MISSING_WRITE),
        (
            [exact_record(loan_amount="1")],
            PrimaryOutcome.PARTIAL_WRITE,
        ),
        (
            [exact_record(), exact_record(name="ACC-LOAP-2026-00002")],
            PrimaryOutcome.DUPLICATE_WRITE,
        ),
    ],
)
def test_failure_taxonomy_and_silent_incorrect_success(
    rest: list[dict[str, object]], outcome: PrimaryOutcome
) -> None:
    verdict = classify_trial(
        actor_reported_success=True,
        halted=False,
        rest_records=rest,
        db_records=rest,
    )
    assert verdict.primary_outcome is outcome
    assert not verdict.success
    assert verdict.silent_incorrect_success


def test_over_halt_is_counted_when_safe_task_was_feasible() -> None:
    verdict = classify_trial(
        actor_reported_success=False,
        halted=True,
        rest_records=[],
        db_records=[],
        environment_healthy=True,
        task_feasible=True,
    )
    assert verdict.primary_outcome is PrimaryOutcome.MISSING_WRITE
    assert verdict.over_halt


def test_missing_write_stays_primary_when_delta_contract_is_deficient() -> None:
    verdict = classify_trial(
        actor_reported_success=False,
        halted=True,
        rest_records=[],
        db_records=[],
        unexpected_db_deltas=["tabLoan Application:+0 (expected +1)"],
        environment_healthy=True,
        task_feasible=True,
    )
    assert verdict.primary_outcome is PrimaryOutcome.MISSING_WRITE
    assert verdict.over_halt
    assert "database delta contract" in verdict.detail


def test_readable_effect_outcome_outranks_execution_exception() -> None:
    verdict = classify_trial(
        actor_reported_success=False,
        halted=True,
        rest_records=[exact_record()],
        db_records=[exact_record()],
        task_feasible=True,
        execution_error="browser teardown failed",
    )
    assert verdict.primary_outcome is PrimaryOutcome.CORRECT
    assert not verdict.success
    assert verdict.over_halt
    assert "execution error after oracle capture" in verdict.detail


def test_oracle_disagreement_collateral_and_indeterminate_are_distinct() -> None:
    disagreement = classify_trial(
        actor_reported_success=True,
        halted=False,
        rest_records=[exact_record()],
        db_records=[],
    )
    assert disagreement.primary_outcome is PrimaryOutcome.REST_DB_DISAGREEMENT
    assert disagreement.silent_incorrect_success

    collateral = classify_trial(
        actor_reported_success=True,
        halted=False,
        rest_records=[exact_record()],
        db_records=[exact_record()],
        unexpected_db_deltas=["tabCustomer"],
    )
    assert collateral.primary_outcome is PrimaryOutcome.COLLATERAL_WRITE

    indeterminate = classify_trial(
        actor_reported_success=False,
        halted=True,
        rest_records=None,
        db_records=[],
    )
    assert indeterminate.primary_outcome is PrimaryOutcome.ORACLE_INDETERMINATE
    assert not indeterminate.success


def test_malformed_oracle_becomes_serializable_indeterminate_row() -> None:
    malformed: object = object()
    verdict = classify_trial(
        actor_reported_success=True,
        halted=False,
        rest_records=[malformed],  # type: ignore[list-item]
        db_records=[exact_record()],
    )
    assert verdict.primary_outcome is PrimaryOutcome.ORACLE_INDETERMINATE
    row = _row(
        arm="agent",
        condition="baseline",
        trial=1,
        baseline_hash="a" * 64,
        actor_reported_success=True,
        halted=False,
        wall_s=1.0,
        rest_records=[malformed],
        db_records=[exact_record()],
        unexpected_deltas=[],
    )
    assert row.primary_outcome == PrimaryOutcome.ORACLE_INDETERMINATE.value
    assert row.rest_records_sha256 == ""
    json.dumps(row.__dict__, sort_keys=True)


def test_evidence_is_private_durable_and_bounded(tmp_path: Path) -> None:
    evidence = CapturedEvidence(
        rest_records=[{"malformed": "x" * (2 * 1024 * 1024)}],
        db_records=[],
        before_table_counts={"tabLoan Application": 0},
        after_table_counts={"tabLoan Application": 1},
        all_table_deltas={"tabLoan Application": 1},
        before_non_target_loan_sha256="a" * 64,
        after_non_target_loan_sha256="a" * 64,
        delta_violations=[],
        errors=[],
    )
    metadata = _persist_evidence(
        tmp_path / "evidence",
        output_root=tmp_path,
        evidence=evidence,
        environment_identity={"baseline": "a" * 64},
        final_screenshot=b"x" * (MAX_SCREENSHOT_BYTES + 1),
        action_log=["y" * (512 * 1024)],
    )
    evidence_path = tmp_path / "evidence/oracle-evidence.json"
    assert evidence_path.stat().st_size <= 1024 * 1024
    assert stat.S_IMODE(evidence_path.stat().st_mode) == 0o600
    assert (tmp_path / "evidence/final.json").exists()
    assert metadata["final_screenshot_bounded_omission"] is True
    assert metadata["evidence_relative_path"] == "evidence/oracle-evidence.json"
    assert stat.S_IMODE((tmp_path / "evidence/actions.json").stat().st_mode) == 0o600


def test_model_free_arms_reject_cost_or_model_usage() -> None:
    for arm in ("compiled", "api"):
        for field in (
            "model_calls",
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "cost_usd",
        ):
            with pytest.raises(ValueError, match="model-free"):
                replace(make_row(arm, "baseline", 1), **{field: 1})


def test_aggregate_reports_silent_errors_over_halts_and_cost() -> None:
    good = make_row("compiled", "baseline", 1)
    bad = TrialRow(
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
    )
    over_halt = TrialRow(
        arm="api",
        condition="ui_cosmetic_v1",
        trial=1,
        primary_outcome=PrimaryOutcome.MISSING_WRITE.value,
        success=False,
        silent_incorrect_success=False,
        over_halt=True,
        wall_s=0.2,
        baseline_snapshot_sha256="a" * 64,
    )
    aggregate = aggregate_rows([good, bad, over_halt])
    assert aggregate["agent"]["baseline"]["silent_incorrect_success_count"] == 1
    assert aggregate["agent"]["baseline"]["model_calls_total"] == 2
    assert aggregate["agent"]["baseline"]["cost_usd_total"] == 0.1
    assert aggregate["api"]["ui_cosmetic_v1"]["over_halt_count"] == 1


def test_publication_gate_requires_equal_n_and_one_snapshot() -> None:
    rows = [
        make_row(arm, condition, trial)
        for arm in ARMS
        for condition in CONDITIONS
        for trial in range(1, 4)
    ]
    assert publication_gate(rows, required_per_cell=3) == (True, [])
    complete, reasons = publication_gate(rows, required_per_cell=10)
    assert not complete
    assert len(reasons) == len(ARMS) * len(CONDITIONS)

    rows[-1].baseline_snapshot_sha256 = "b" * 64
    complete, reasons = publication_gate(rows, required_per_cell=3)
    assert not complete
    assert any("identical baseline" in reason for reason in reasons)


@pytest.mark.parametrize("arm", ("compiled", "agent", "api"))
def test_db_delta_contract_is_exact_and_does_not_mask_allowed_tables(
    arm: str,
) -> None:
    assert EXPECTED_TABLE_DELTAS[arm] == {"tabLoan Application": 1}
    before = {
        "tabLoan Application": 0,
        "tabSeries": 1,
        "tabVersion": 2,
        "tabCustomer": 1,
    }
    clean = {**before, "tabLoan Application": 1}
    assert audit_table_deltas(before, clean, arm=arm)[0] == []

    after = {
        **clean,
        "tabSeries": 2,
        "tabVersion": 3,
        "tabCustomer": 2,
    }
    violations, all_deltas = audit_table_deltas(before, after, arm=arm)
    assert violations == [
        "tabCustomer:+1 (expected +0)",
        "tabSeries:+1 (expected +0)",
        "tabVersion:+1 (expected +0)",
    ]
    assert all_deltas["tabLoan Application"] == 1

    missing_target, _ = audit_table_deltas(before, before, arm=arm)
    assert missing_target == ["tabLoan Application:+0 (expected +1)"]


def test_plan_is_read_only_and_matrix_refuses_paid_arm_without_opt_in(
    tmp_path: Path,
) -> None:
    plan = _plan(3)
    assert plan["total_trials"] == 18
    assert plan["trials_per_cell"] == 3

    with pytest.raises(FixtureError, match="paid agent arm"):
        run_matrix(
            object(),  # type: ignore[arg-type]
            tmp_path / "bundle",
            tmp_path / "out",
            n=3,
            allow_paid_agent=False,
            max_cost_per_run_usd=1.5,
            max_total_agent_cost_usd=None,
            headed=False,
        )


def test_model_free_plan_is_equal_scoped_and_never_publication_eligible() -> None:
    plan = _plan(10, model_free=True)
    spec = LoanApplicationSpec()
    assert plan["run_mode"] == "model_free"
    assert plan["arms"] == ["compiled", "api"]
    assert plan["omitted_arms"] == ["agent"]
    assert plan["trials_per_cell"] == 10
    assert plan["total_trials"] == 40
    assert plan["publication_eligible"] is False
    assert "never a complete three-arm" in plan["scope"]
    assert plan["task_contract"] == {
        "fixed_fields": spec.fixed_fields,
        "recorded_params": spec.params(),
        "persisted_fields": spec.fields,
        "browser_setup": (
            "supported Customer -> new Loan Application route with exact "
            "prepopulated Company"
        ),
    }


def test_model_free_run_skips_agent_credentials_and_is_not_publishable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.frappe_lending_demo as driver

    class FakeFixture:
        def up(self) -> None:
            return None

        def baseline_sha256(self) -> str:
            return "a" * 64

        def image_identity(self) -> str:
            return "sha256:" + "b" * 64

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("model-free mode must not touch agent credentials/model")

    monkeypatch.setattr(driver, "_validate_bundle_contract", lambda path: None)
    monkeypatch.setattr(driver, "_tree_manifest_sha256", lambda path: "c" * 64)
    monkeypatch.setattr(driver.agent_baseline, "load_api_key", forbidden)
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
        max_cost_per_run_usd=2.0,
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
            max_cost_per_run_usd=2.0,
            max_total_agent_cost_usd=None,
            headed=False,
            model_free=True,
        )


def test_matrix_refuses_soft_cost_cap_before_fixture_or_model_use(
    tmp_path: Path,
) -> None:
    class NeverFixture:
        def up(self) -> None:
            raise AssertionError("must refuse before touching fixture")

    with pytest.raises(FixtureError, match="one-call reserve"):
        run_matrix(
            NeverFixture(),  # type: ignore[arg-type]
            tmp_path / "bundle",
            tmp_path / "out",
            n=3,
            allow_paid_agent=True,
            max_cost_per_run_usd=AGENT_MAX_SINGLE_CALL_RESERVE_USD,
            max_total_agent_cost_usd=100.0,
            headed=False,
        )


def test_driver_has_no_unaccounted_paid_preflight_and_shared_timing_boundary() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "scripts/frappe_lending_demo.py"
    ).read_text()
    assert "preflight_check(" not in source
    assert (
        source.count(
            '"timing_boundary": "actuation start through REST/SQL/delta verification"'
        )
        == 3
    )
    compiled = source[
        source.index("def run_compiled_trial") : source.index("def run_agent_trial")
    ]
    agent = source[
        source.index("def run_agent_trial") : source.index("def _refuse_indeterminate")
    ]
    assert compiled.index("_capture_post_evidence(") < compiled.index("close()")
    assert agent.index("_capture_post_evidence(") < agent.index("backend.screenshot()")
    assert agent.index("_capture_post_evidence(") < agent.index("close()")


def test_indeterminate_provider_spend_refuses_later_paid_calls() -> None:
    row = make_row("agent", "baseline", 1)
    row.metadata["spend_indeterminate"] = True
    with pytest.raises(FixtureError, match="all later paid calls are refused"):
        _refuse_indeterminate_paid_continuation(row)
