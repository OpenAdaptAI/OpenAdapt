"""openIMIS coverage / eligibility-check reference workflow — contract tests.

No docker, no network, no PostgreSQL: these tests pin the COMMITTED surface
of the eligibility demo (`benchmark/openimis_claims/deployment.eligibility.yaml`
+ `scripts/openimis_eligibility_demo.py::eligibility_effects` + the fixture's
oracle SQL) so the deployment YAML, the fixture's ground-truth oracle, and the
bundle's effect contracts can never drift apart. The live loop (record ->
compile -> replay -> SQL-verified verdicts) runs against the pinned stack via
the demo driver, exactly like the claims demo.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from openadapt_flow.backend import StructuralResolutionRefused
from openadapt_flow.deployment import build_effect_verifier, load_deployment
from openadapt_flow.ir import StructuralLocator
from openadapt_flow.runtime.effects import (
    EffectKind,
    EffectState,
    SqlRecordVerifier,
    Verdict,
)
from openadapt_flow.runtime.effects._common import judge_records
from openadapt_flow.runtime.effects.sql import assert_read_only_sql

REPO_ROOT = Path(__file__).resolve().parents[1]
for entry in (REPO_ROOT / "benchmark", REPO_ROOT / "scripts"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from openimis_claims import fixture as oi  # noqa: E402
from openimis_eligibility_demo import (  # noqa: E402
    OpenIMISEligibilityBackend,
    _report_contract_error,
    eligibility_effects,
)

DEPLOYMENT_YAML = (
    REPO_ROOT / "benchmark" / "openimis_claims" / "deployment.eligibility.yaml"
)
EXPECTED_CONTRACT_HASH = "sha256:" + "a" * 64


def test_browser_adapter_records_stable_structural_and_run_bound_identity() -> None:
    class Page:
        viewport_size = {"width": 1280, "height": 800}

        @staticmethod
        def evaluate(script: str, point: list[int]) -> object:
            assert point == [320, 240]
            if "target_kind" in script:
                return {
                    "context": {
                        "target_kind": "eligibility_service",
                        "target_id": "service_code",
                    },
                    "dialog_identifiers": [["999000003"]],
                }
            return {
                "selector": 'input[placeholder^="Search Service"]',
                "role": "textbox",
                "name": "Search Service",
            }

    backend = OpenIMISEligibilityBackend(Page())  # type: ignore[arg-type]
    locator = backend.structural_locator_at(320, 240)
    assert locator == StructuralLocator(
        selector='input[placeholder^="Search Service"]',
        role="textbox",
        name="Search Service",
    )
    identity = backend.structured_text_at(320, 240)
    assert identity is not None
    assert '"insurance_no":"999000003"' in identity


@pytest.mark.parametrize(
    "dialog_identifiers",
    [
        [["999000003"], ["999000003"]],
        [["999000003"], ["999000004"]],
        [["999000003", "999000004"]],
        [],
    ],
    ids=[
        "same-record-two-dialogs",
        "different-records",
        "two-ids-one-dialog",
        "missing",
    ],
)
def test_browser_adapter_refuses_ambiguous_record_dialogs(
    dialog_identifiers: list[list[str]],
) -> None:
    class Page:
        @staticmethod
        def evaluate(script: str, point: list[int]) -> object:
            assert point == [320, 240]
            return {
                "context": {
                    "target_kind": "eligibility_service",
                    "target_id": "service_code",
                },
                "dialog_identifiers": dialog_identifiers,
            }

    backend = OpenIMISEligibilityBackend(Page())  # type: ignore[arg-type]
    assert backend.structured_text_at(320, 240) is None


def test_browser_adapter_refuses_ambiguous_structural_candidates() -> None:
    class Candidates:
        @staticmethod
        def evaluate_all(script: str) -> list[int]:
            return [0, 1]

    class Page:
        @staticmethod
        def locator(selector: str) -> Candidates:
            assert selector == 'input[placeholder^="Search Service"]'
            return Candidates()

    backend = OpenIMISEligibilityBackend(Page())  # type: ignore[arg-type]
    with pytest.raises(StructuralResolutionRefused, match="ambiguous"):
        backend.locate_structural(
            StructuralLocator(selector='input[placeholder^="Search Service"]')
        )


def test_browser_adapter_ignores_hidden_structural_duplicate() -> None:
    class Candidate:
        @staticmethod
        def bounding_box() -> dict[str, float]:
            return {"x": 100, "y": 50, "width": 40, "height": 20}

        @staticmethod
        def evaluate(script: str, point: list[int]) -> bool:
            assert point == [120, 60]
            return True

    class Candidates:
        @staticmethod
        def evaluate_all(script: str) -> list[int]:
            # CSS matched a hidden React template at index 0 and the sole live
            # control at index 1; only the latter is returned by the JS filter.
            return [1]

        @staticmethod
        def nth(index: int) -> Candidate:
            assert index == 1
            return Candidate()

    class Page:
        viewport_size = {"width": 1280, "height": 800}

        @staticmethod
        def locator(selector: str) -> Candidates:
            assert selector == 'input[placeholder^="Search Service"]'
            return Candidates()

    backend = OpenIMISEligibilityBackend(Page())  # type: ignore[arg-type]
    handle = backend.locate_structural(
        StructuralLocator(selector='input[placeholder^="Search Service"]')
    )
    assert handle is not None
    assert handle.point == (120, 60)
    assert handle.candidate_count == 1


def _report(*, success: bool, verdict: str | None, earlier_ok: bool = True):
    prior = SimpleNamespace(
        ok=earlier_ok,
        effect_contract_hashes=[],
        effect_verified=None,
        effect_results=[],
    )
    final = SimpleNamespace(
        ok=success,
        effect_contract_hashes=[EXPECTED_CONTRACT_HASH],
        effect_verified=(None if verdict is None else verdict == "CONFIRMED"),
        effect_results=(
            [] if verdict is None else [f"[sql] field_equals: {verdict} — evidence"]
        ),
    )
    return SimpleNamespace(success=success, results=[prior, final])


def test_demo_accepts_only_exact_confirmed_sql_outcome() -> None:
    assert (
        _report_contract_error(
            _report(success=True, verdict="CONFIRMED"),
            expect_halt=False,
            expected_contract_hash=EXPECTED_CONTRACT_HASH,
        )
        is None
    )
    assert _report_contract_error(
        _report(success=True, verdict=None),
        expect_halt=False,
        expected_contract_hash=EXPECTED_CONTRACT_HASH,
    )


def test_demo_refuses_a_confirmed_but_different_effect_contract() -> None:
    report = _report(success=True, verdict="CONFIRMED")
    report.results[-1].effect_contract_hashes = ["sha256:" + "b" * 64]
    assert _report_contract_error(
        report,
        expect_halt=False,
        expected_contract_hash=EXPECTED_CONTRACT_HASH,
    )


def test_expected_halt_refuses_unrelated_or_early_failure() -> None:
    exact = _report(success=False, verdict="REFUTED")
    assert (
        _report_contract_error(
            exact,
            expect_halt=True,
            expected_contract_hash=EXPECTED_CONTRACT_HASH,
        )
        is None
    )
    assert _report_contract_error(
        _report(success=False, verdict=None),
        expect_halt=True,
        expected_contract_hash=EXPECTED_CONTRACT_HASH,
    )
    assert _report_contract_error(
        _report(success=False, verdict="REFUTED", earlier_ok=False),
        expect_halt=True,
        expected_contract_hash=EXPECTED_CONTRACT_HASH,
    )


# -- the committed deployment YAML -------------------------------------------


def test_deployment_yaml_wires_the_sql_kit() -> None:
    config = load_deployment(DEPLOYMENT_YAML)
    assert config.effects.kind == "sql"
    assert config.effects.sql_driver == "psycopg"
    assert config.effects.sql_password_env == oi.ORACLE_PASSWORD_ENV
    assert config.effects.sql_connect_args["user"] == oi.ORACLE_ROLE
    assert config.effects.sql_connect_args["port"] == oi.DB_PORT
    assert config.effects.sql_connect_args["host"] == "127.0.0.1"
    # Run-parameter binding is explicit: the verifier follows the full question.
    assert config.effects.sql_query_params["insurance_no"].param == "insurance_no"
    assert config.effects.sql_query_params["service_code"].param == "service_code"
    assert config.effects.sql_query_params["as_of_date"].param == "as_of_date"


def test_deployment_query_matches_the_fixture_oracle() -> None:
    """One statement, two readers: the YAML and the fixture cannot drift."""
    config = load_deployment(DEPLOYMENT_YAML)
    normalize = lambda sql: " ".join(sql.split())  # noqa: E731
    assert normalize(config.effects.sql_query) == normalize(oi.ELIGIBILITY_ORACLE_SQL)


def test_deployment_query_is_read_only() -> None:
    config = load_deployment(DEPLOYMENT_YAML)
    assert_read_only_sql(config.effects.sql_query)


def test_deployment_query_proves_the_declared_service_and_date_window() -> None:
    """The fixture asks a specific benefit question, not merely "row exists"."""
    query = load_deployment(DEPLOYMENT_YAML).effects.sql_query
    for required in (
        'JOIN "tblProductServices"',
        'JOIN "tblServices"',
        's."ServCode" = %(service_code)s',
        'p."PolicyStatus" = 2',
        'p."EffectiveDate" <= CAST(%(as_of_date)s AS date)',
        'p."ExpiryDate" >= CAST(%(as_of_date)s AS date)',
        'ip."EffectiveDate" <= CAST(%(as_of_date)s AS date)',
        'ip."ExpiryDate" >= CAST(%(as_of_date)s AS date)',
        'CAST(ps."ValidityFrom" AS date)',
        'CAST(s."ValidityFrom" AS date)',
    ):
        assert required in query
    assert "CURRENT_DATE" not in query, "reference result must not rot with host time"


def _stub_psycopg(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand a stub DB-API module in for psycopg (an optional deployment dep).

    ``build_effect_verifier`` imports the configured ``sql_driver`` before the
    secret/param checks run, so the fast suite (which does not install
    PostgreSQL drivers) stubs it: construction must not open a connection (the
    verifier connects per-read), so the stub also proves the driver is only
    imported, never dialed, at build time.
    """
    stub = types.ModuleType("psycopg")

    def _connect(**kwargs: object) -> None:  # pragma: no cover - never dialed
        raise AssertionError("construction must not open a connection")

    stub.connect = _connect  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "psycopg", stub)


def test_verifier_requires_the_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kit convention: a missing secret env var fails LOUD at construction."""
    _stub_psycopg(monkeypatch)
    monkeypatch.delenv(oi.ORACLE_PASSWORD_ENV, raising=False)
    config = load_deployment(DEPLOYMENT_YAML)
    with pytest.raises(ValueError, match=oi.ORACLE_PASSWORD_ENV):
        build_effect_verifier(config.effects, {"insurance_no": oi.POLICYHOLDER_CHF})


def test_verifier_builds_with_secret_and_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The YAML constructs a SqlRecordVerifier bound to the run's insuree."""
    _stub_psycopg(monkeypatch)
    monkeypatch.setenv(oi.ORACLE_PASSWORD_ENV, "synthetic-test-secret")
    config = load_deployment(DEPLOYMENT_YAML)
    verifier = build_effect_verifier(
        config.effects,
        {
            "insurance_no": oi.LAPSED_CHF,
            "service_code": oi.ELIGIBILITY_SERVICE_CODE,
            "as_of_date": oi.ELIGIBILITY_AS_OF_DATE,
        },
    )
    assert isinstance(verifier, SqlRecordVerifier)
    assert verifier.query_params == {
        "insurance_no": oi.LAPSED_CHF,
        "service_code": oi.ELIGIBILITY_SERVICE_CODE,
        "as_of_date": oi.ELIGIBILITY_AS_OF_DATE,
    }


def test_verifier_refuses_an_unbound_run_param(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_psycopg(monkeypatch)
    monkeypatch.setenv(oi.ORACLE_PASSWORD_ENV, "synthetic-test-secret")
    config = load_deployment(DEPLOYMENT_YAML)
    with pytest.raises(ValueError, match="insurance_no"):
        build_effect_verifier(config.effects, {})


# -- the bundle's effect contracts -------------------------------------------


def test_eligibility_effects_bind_the_run_param() -> None:
    effects = eligibility_effects()
    assert [e.kind for e in effects] == [EffectKind.FIELD_EQUALS]
    outcome = effects[0]
    assert outcome.match["chf_id"].param == "insurance_no"
    assert outcome.match["service_code"].param == "service_code"
    assert outcome.match["as_of_date"].param == "as_of_date"
    assert outcome.field == "eligibility"
    assert str(outcome.value) == "Eligible"


def test_eligibility_effects_resolve_to_the_complete_question() -> None:
    params = {
        "insurance_no": oi.SECOND_ACTIVE_CHF,
        "service_code": oi.ELIGIBILITY_SERVICE_CODE,
        "as_of_date": oi.ELIGIBILITY_AS_OF_DATE,
    }
    for effect in eligibility_effects():
        resolved = effect.resolve(params)
        assert str(resolved.match["chf_id"]) == oi.SECOND_ACTIVE_CHF
        assert str(resolved.match["service_code"]) == oi.ELIGIBILITY_SERVICE_CODE
        assert str(resolved.match["as_of_date"]) == oi.ELIGIBILITY_AS_OF_DATE


def _outcome_row(chf: str, eligibility: str = "Eligible") -> dict[str, str]:
    return {
        "chf_id": chf,
        "service_code": oi.ELIGIBILITY_SERVICE_CODE,
        "as_of_date": oi.ELIGIBILITY_AS_OF_DATE,
        "eligibility": eligibility,
    }


def _judge(chf: str, current: list[dict[str, str]], *, before=None):
    effect = eligibility_effects()[0].resolve(
        {
            "insurance_no": chf,
            "service_code": oi.ELIGIBILITY_SERVICE_CODE,
            "as_of_date": oi.ELIGIBILITY_AS_OF_DATE,
        }
    )
    baseline = current if before is None else before
    return judge_records(
        effect,
        EffectState(substrate="sql", reachable=True, records=baseline),
        current,
        substrate="sql",
    )


def test_preexisting_eligible_row_is_a_confirmed_read_outcome_not_a_mutation() -> None:
    """Eligibility is expected to pre-exist; no fabricated write is required."""
    existing = [_outcome_row(oi.SECOND_ACTIVE_CHF)]
    verdict = _judge(oi.SECOND_ACTIVE_CHF, existing, before=existing)
    assert verdict.verdict is Verdict.CONFIRMED
    assert verdict.kind is EffectKind.FIELD_EQUALS


@pytest.mark.parametrize(
    ("chf", "records"),
    [
        (oi.LAPSED_CHF, [_outcome_row(oi.LAPSED_CHF, "Ineligible")]),
        (oi.FUTURE_CHF, [_outcome_row(oi.FUTURE_CHF, "Ineligible")]),
        (
            oi.SECOND_ACTIVE_CHF,
            [
                _outcome_row(oi.SECOND_ACTIVE_CHF),
                _outcome_row(oi.SECOND_ACTIVE_CHF),
            ],
        ),
        (oi.SECOND_ACTIVE_CHF, []),
    ],
    ids=["expired", "not-yet-effective", "duplicate", "no-row"],
)
def test_eligibility_outcome_refuses_every_negative_case(
    chf: str, records: list[dict[str, str]]
) -> None:
    assert _judge(chf, records).verdict is Verdict.REFUTED


# -- the fixture's eligibility scenario --------------------------------------


def test_scenario_constants_are_synthetic_and_distinct() -> None:
    chfs = {oi.POLICYHOLDER_CHF, oi.LAPSED_CHF, oi.SECOND_ACTIVE_CHF, oi.FUTURE_CHF}
    assert len(chfs) == 4
    for chf in chfs:
        assert chf.startswith("999"), "synthetic 999* insuree-number range"
    assert oi.POLICY_STATUS_ACTIVE == 2
    assert oi.POLICY_STATUS_EXPIRED == 8


def test_oracle_role_bootstrap_revokes_accumulated_privileges_before_granting() -> None:
    """Repeated fixture upgrades cannot silently accumulate table access."""
    fixture = oi.OpenIMISFixture.__new__(oi.OpenIMISFixture)
    statements: list[str] = []
    fixture.oracle_password = lambda: "synthetic-oracle-password"  # type: ignore[method-assign]

    def fake_psql(sql: str) -> str:
        statements.append(sql)
        if "has_database_privilege" in sql:
            return "read-only-exact"
        return "1" if "FROM pg_roles" in sql else ""

    fixture._psql = fake_psql  # type: ignore[method-assign]
    fixture._bootstrap_oracle_role()

    grant_sql = statements[-2]
    privilege_audit_sql = statements[-1]
    assert "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT" in grant_sql
    assert "NOREPLICATION NOBYPASSRLS" in grant_sql
    assert "REVOKE CREATE ON SCHEMA public FROM PUBLIC" in grant_sql
    assert 'REVOKE CREATE ON DATABASE "IMIS" FROM PUBLIC' in grant_sql
    assert 'REVOKE TEMPORARY ON DATABASE "IMIS" FROM PUBLIC' in grant_sql
    assert (
        "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM PUBLIC" in grant_sql
    )
    assert "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public" in grant_sql
    assert "REVOKE CREATE ON SCHEMA public" in grant_sql
    assert 'REVOKE CREATE ON DATABASE "IMIS" FROM "oa_eligibility_oracle"' in grant_sql
    assert 'REVOKE TEMPORARY ON DATABASE "IMIS"' in grant_sql
    assert (
        'GRANT SELECT ON "tblInsuree", "tblPolicy", "tblInsureePolicy", '
        '"tblProductServices", "tblServices"'
    ) in grant_sql
    assert "has_database_privilege" in privilege_audit_sql
    assert "has_schema_privilege" in privilege_audit_sql
    assert "information_schema.table_privileges" in privilege_audit_sql
    assert "grantee IN ('oa_eligibility_oracle', 'PUBLIC')" in privilege_audit_sql
    assert "AND 5 = (" in privilege_audit_sql
    assert "default_transaction_read_only=on" in privilege_audit_sql
    assert "pg_auth_members" in privilege_audit_sql
    assert "pg_class c" in privilege_audit_sql
    assert "pg_namespace" in privilege_audit_sql
    assert "pg_database" in privilege_audit_sql

    # PUBLIC revokes do not strand the reference app: the isolated database is
    # created under IMISuser, which retains owner privileges independently of
    # grants made to PUBLIC.
    compose = (REPO_ROOT / "benchmark/openimis_claims/compose.yml").read_text()
    assert "POSTGRES_DB=IMIS" in compose
    assert "POSTGRES_USER=IMISuser" in compose


def test_oracle_role_bootstrap_fails_closed_on_unexpected_effective_privilege() -> None:
    fixture = oi.OpenIMISFixture.__new__(oi.OpenIMISFixture)
    fixture.oracle_password = lambda: "synthetic-oracle-password"  # type: ignore[method-assign]

    def fake_psql(sql: str) -> str:
        if "has_database_privilege" in sql:
            return "unexpected-privileges"
        return "1" if "FROM pg_roles" in sql else ""

    fixture._psql = fake_psql  # type: ignore[method-assign]
    with pytest.raises(oi.FixtureError, match="privilege audit"):
        fixture._bootstrap_oracle_role()


def test_fixture_supports_isolated_project_ports_and_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENIMIS_COMPOSE_PROJECT", "openadapt-pr145-isolated")
    monkeypatch.setenv("OPENIMIS_DB_PORT", "9442")
    monkeypatch.setenv("OPENIMIS_STATE_DIR", str(tmp_path / "state"))
    fixture = oi.OpenIMISFixture(http_port=9441)
    assert fixture.project_name == "openadapt-pr145-isolated"
    assert fixture.http_port == 9441
    assert fixture.db_port == 9442
    assert fixture.state_dir == tmp_path / "state"
    assert fixture._compose_env()["OPENIMIS_DB_PORT"] == "9442"


def test_policyholder_sql_template_refuses_suspicious_values() -> None:
    fixture = oi.OpenIMISFixture.__new__(oi.OpenIMISFixture)
    with pytest.raises(oi.FixtureError, match="suspicious"):
        fixture._bootstrap_policyholder(
            chf="999000009",
            last="O'Hara",  # embedded quote must refuse, not inject
            other="Test",
            dob="1990-01-01",
            gender="F",
            address="1 Synthetic Lane",
            enroll="2026-01-01",
            expiry="2027-01-01",
            status=oi.POLICY_STATUS_ACTIVE,
        )


def test_coverage_oracle_refuses_suspicious_insuree_numbers() -> None:
    fixture = oi.OpenIMISFixture.__new__(oi.OpenIMISFixture)
    with pytest.raises(oi.FixtureError, match="suspicious"):
        fixture.coverage("1; DROP TABLE x")


@pytest.mark.parametrize(
    ("service", "as_of"),
    [("A1; DROP TABLE x", oi.ELIGIBILITY_AS_OF_DATE), ("A1", "07/18/2026")],
)
def test_coverage_oracle_refuses_suspicious_service_or_date(
    service: str, as_of: str
) -> None:
    fixture = oi.OpenIMISFixture.__new__(oi.OpenIMISFixture)
    with pytest.raises(oi.FixtureError):
        fixture.coverage(
            oi.POLICYHOLDER_CHF,
            service_code=service,
            as_of_date=as_of,
        )
