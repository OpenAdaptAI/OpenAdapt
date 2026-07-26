"""Verifier adapter platform: interface, classification, redaction, plugins.

Qualification fixtures for the platform surface itself (per-substrate
adversarial fixtures live in the per-adapter test modules): every built-in
adapter conforms to the ``VerifierAdapter`` protocol and lifecycle; the
refined result classes (UNAVAILABLE / STALE / CONFLICTING / INDETERMINATE)
map onto the transaction taxonomy with no path to a silent pass; evidence
redaction minimizes without softening; and the customer plugin seam works
through both registration paths.
"""

from __future__ import annotations

import importlib.metadata
from datetime import datetime, timedelta, timezone

import pytest

from openadapt_flow.deployment import EffectsConfig, build_effect_verifier
from openadapt_flow.runtime.effects import (
    AdapterResult,
    ConnectionProbe,
    DocumentArrivalVerifier,
    DocumentHashVerifier,
    Effect,
    EffectKind,
    EffectState,
    EffectVerdict,
    FhirEffectVerifier,
    FileArrivalVerifier,
    GraphQLRecordVerifier,
    MaildirDeliveryVerifier,
    RedactingVerifier,
    RedactionPolicy,
    RestRecordVerifier,
    SqlRecordVerifier,
    Verdict,
    VerifierAdapter,
    VerifierAdapterBase,
    apply_collateral_hooks,
    classify_adapter_result,
    confidence_label,
    enforce_freshness,
    poll_until_settled,
    reconciliation_required,
    redact_verdict,
    register_verifier_factory,
    transaction_outcome_for,
)
from openadapt_flow.runtime.effects import adapter as adapter_mod
from openadapt_flow.runtime.effects.adapter import parse_timestamp
from openadapt_flow.runtime.effects.onscreen import OnScreenReadbackVerifier
from openadapt_flow.transaction import TransactionOutcome
from openadapt_flow.verification import VerificationTier
from tests.example_verifier_plugin import CsvLedgerVerifier, build_csv_ledger_verifier


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FakeSession:
    """requests-style session serving a scripted sequence of responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def _next(self):
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]

    def get(self, url, timeout=None, headers=None):
        self.calls.append(("get", url, headers))
        return self._next()

    def post(self, url, json=None, timeout=None, headers=None):
        self.calls.append(("post", url, json, headers))
        return self._next()


def make_effect(**kwargs):
    kwargs.setdefault("kind", EffectKind.RECORD_WRITTEN)
    kwargs.setdefault("match", {"patient_id": "p1"})
    kwargs.setdefault("timeout_s", 0.0)
    return Effect(**kwargs)


def reachable_state(records=(), substrate="test"):
    return EffectState(substrate=substrate, reachable=True, records=list(records))


# -- interface conformance ----------------------------------------------------


class TestAdapterInterface:
    def test_builtin_adapters_conform_to_protocol(self, tmp_path):
        adapters = [
            RestRecordVerifier("http://sor"),
            GraphQLRecordVerifier("http://sor/graphql", query="query { r { id } }"),
            FhirEffectVerifier("http://sor/fhir"),
            SqlRecordVerifier(lambda: None, "SELECT id FROM t"),
            FileArrivalVerifier(str(tmp_path)),
            MaildirDeliveryVerifier(str(tmp_path)),
            DocumentArrivalVerifier(str(tmp_path)),
            DocumentHashVerifier(str(tmp_path)),
            OnScreenReadbackVerifier(backend=None),
            CsvLedgerVerifier(str(tmp_path / "ledger.csv")),
        ]
        for verifier in adapters:
            assert isinstance(verifier, VerifierAdapter), type(verifier).__name__
            assert isinstance(verifier, VerifierAdapterBase), type(verifier).__name__
            assert verifier.substrate
            assert isinstance(verifier.verification_tier, VerificationTier)

    def test_screen_readback_is_demoted_not_independent_proof(self):
        onscreen = OnScreenReadbackVerifier(backend=None)
        assert onscreen.independent_system_of_record is False
        assert onscreen.verification_tier is VerificationTier.IMMEDIATE_SCREEN
        assert confidence_label(onscreen.verification_tier) == "screen-consistency"
        # Every system-of-record adapter IS independent proof.
        assert RestRecordVerifier("http://sor").independent_system_of_record is True

    def test_confidence_labels_cover_every_tier(self):
        labels = {confidence_label(tier) for tier in VerificationTier}
        assert labels == {
            "independent-system",
            "independent-session",
            "reacquired-state",
            "screen-consistency",
        }

    def test_test_connection_ok(self):
        session = FakeSession([FakeResponse(200, {"records": []})])
        verifier = RestRecordVerifier("http://sor", session=session)
        probe = verifier.test_connection()
        assert isinstance(probe, ConnectionProbe)
        assert probe.ok is True
        assert probe.substrate == "rest"

    def test_test_connection_credential_failure_not_ok(self):
        session = FakeSession([FakeResponse(401, {"error": "bad token"})])
        probe = RestRecordVerifier("http://sor", session=session).test_connection()
        assert probe.ok is False

    def test_test_connection_never_raises(self):
        class Exploding(VerifierAdapterBase):
            substrate = "boom"

            def capture_pre_state(self, context=None):
                raise RuntimeError("kaboom")

        probe = Exploding().test_connection()
        assert probe.ok is False
        assert "kaboom" in probe.reason

    def test_capture_post_state_defaults_to_fresh_snapshot(self):
        session = FakeSession(
            [
                FakeResponse(200, {"records": []}),
                FakeResponse(200, {"records": [{"id": 1}]}),
            ]
        )
        verifier = RestRecordVerifier("http://sor", session=session)
        before = verifier.capture_pre_state()
        after = verifier.capture_post_state()
        assert before.records == []
        assert after.records == [{"id": 1}]


# -- result classification + transaction mapping ------------------------------


class TestResultClassification:
    def _verdict(self, verdict, **kwargs):
        kwargs.setdefault("kind", EffectKind.RECORD_WRITTEN)
        return EffectVerdict(verdict=verdict, **kwargs)

    def test_confirmed(self):
        v = self._verdict(Verdict.CONFIRMED)
        assert classify_adapter_result(v) is AdapterResult.CONFIRMED
        assert reconciliation_required(AdapterResult.CONFIRMED) is False
        assert transaction_outcome_for(AdapterResult.CONFIRMED) is None

    def test_refuted_absent_halts_before_effect(self):
        v = self._verdict(Verdict.REFUTED, observed_count=0, expected_count=1)
        assert classify_adapter_result(v) is AdapterResult.REFUTED
        assert (
            transaction_outcome_for(AdapterResult.REFUTED)
            is TransactionOutcome.HALTED_BEFORE_EFFECT
        )

    def test_conflicting_duplicate_requires_reconciliation(self):
        v = self._verdict(Verdict.REFUTED, observed_count=2, expected_count=1)
        assert classify_adapter_result(v) is AdapterResult.CONFLICTING
        assert reconciliation_required(AdapterResult.CONFLICTING) is True
        assert (
            transaction_outcome_for(AdapterResult.CONFLICTING)
            is TransactionOutcome.RECONCILIATION_REQUIRED
        )

    def test_unavailable_requires_reconciliation(self):
        v = self._verdict(Verdict.INDETERMINATE, unavailable=True)
        assert classify_adapter_result(v) is AdapterResult.UNAVAILABLE
        assert reconciliation_required(AdapterResult.UNAVAILABLE) is True
        assert (
            transaction_outcome_for(AdapterResult.UNAVAILABLE)
            is TransactionOutcome.RECONCILIATION_REQUIRED
        )

    def test_stale_requires_reconciliation(self):
        v = self._verdict(Verdict.INDETERMINATE, stale=True)
        assert classify_adapter_result(v) is AdapterResult.STALE
        assert (
            transaction_outcome_for(AdapterResult.STALE)
            is TransactionOutcome.RECONCILIATION_REQUIRED
        )

    def test_indeterminate_requires_reconciliation(self):
        v = self._verdict(Verdict.INDETERMINATE)
        assert classify_adapter_result(v) is AdapterResult.INDETERMINATE
        assert (
            transaction_outcome_for(AdapterResult.INDETERMINATE)
            is TransactionOutcome.RECONCILIATION_REQUIRED
        )

    def test_no_non_confirmed_result_maps_to_a_pass(self):
        for result in AdapterResult:
            if result is AdapterResult.CONFIRMED:
                continue
            outcome = transaction_outcome_for(result)
            assert outcome in (
                TransactionOutcome.HALTED_BEFORE_EFFECT,
                TransactionOutcome.RECONCILIATION_REQUIRED,
            ), result

    def test_unreachable_sor_classifies_unavailable(self):
        session = FakeSession([FakeResponse(500)])
        verifier = RestRecordVerifier("http://sor", session=session)
        before = verifier.capture_pre_state()
        verdict = verifier.verify(make_effect(), before)
        assert verdict.unavailable is True
        assert classify_adapter_result(verdict) is AdapterResult.UNAVAILABLE

    def test_sql_credential_failure_is_unavailable_not_pass(self):
        def connect():
            raise PermissionError("access denied for user 'oracle'")

        verifier = SqlRecordVerifier(connect, "SELECT id, patient_id FROM t")
        before = verifier.capture_pre_state()
        assert before.reachable is False
        verdict = verifier.verify(make_effect(), before)
        assert verdict.verdict is Verdict.INDETERMINATE
        assert verdict.should_halt is True
        assert classify_adapter_result(verdict) is AdapterResult.UNAVAILABLE


# -- settlement + freshness ---------------------------------------------------


class TestSettlementAndFreshness:
    def test_poll_until_settled_confirms_late_write(self):
        reads = [[], [], [{"patient_id": "p1"}]]
        clock = iter(range(100))
        verdict = poll_until_settled(
            lambda: reads.pop(0) if reads else [{"patient_id": "p1"}],
            make_effect(timeout_s=10.0),
            reachable_state(),
            substrate="test",
            clock=lambda: float(next(clock)),
            sleep=lambda s: None,
        )
        assert verdict.verdict is Verdict.CONFIRMED

    def test_settlement_timeout_is_failure_not_pass(self):
        clock = iter(range(100))
        verdict = poll_until_settled(
            lambda: [],  # the write never lands
            make_effect(timeout_s=3.0),
            reachable_state(),
            substrate="test",
            clock=lambda: float(next(clock)),
            sleep=lambda s: None,
        )
        assert verdict.verdict is Verdict.REFUTED
        assert verdict.should_halt is True

    def test_enforce_freshness_demotes_stale_evidence(self):
        confirmed = EffectVerdict(
            verdict=Verdict.CONFIRMED,
            kind=EffectKind.RECORD_WRITTEN,
            matched_records=[
                {"patient_id": "p1", "updated_at": "2020-01-01T00:00:00Z"}
            ],
        )
        verdict = enforce_freshness(
            confirmed, freshness_field="updated_at", window_s=60.0
        )
        assert verdict.verdict is Verdict.INDETERMINATE
        assert verdict.stale is True
        assert classify_adapter_result(verdict) is AdapterResult.STALE

    def test_enforce_freshness_keeps_fresh_evidence(self):
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        confirmed = EffectVerdict(
            verdict=Verdict.CONFIRMED,
            kind=EffectKind.RECORD_WRITTEN,
            matched_records=[{"updated_at": now.isoformat()}],
        )
        verdict = enforce_freshness(
            confirmed, freshness_field="updated_at", window_s=60.0, now=now
        )
        assert verdict.verdict is Verdict.CONFIRMED

    def test_enforce_freshness_missing_timestamp_is_stale(self):
        confirmed = EffectVerdict(
            verdict=Verdict.CONFIRMED,
            kind=EffectKind.RECORD_WRITTEN,
            matched_records=[{"patient_id": "p1"}],
        )
        verdict = enforce_freshness(confirmed, freshness_field="ts", window_s=60.0)
        assert verdict.stale is True

    def test_enforce_freshness_rejects_far_future_timestamp(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        confirmed = EffectVerdict(
            verdict=Verdict.CONFIRMED,
            kind=EffectKind.RECORD_WRITTEN,
            matched_records=[{"ts": future}],
        )
        verdict = enforce_freshness(confirmed, freshness_field="ts", window_s=60.0)
        assert verdict.stale is True

    def test_enforce_freshness_never_rescues_a_failure(self):
        refuted = EffectVerdict(verdict=Verdict.REFUTED, kind=EffectKind.RECORD_WRITTEN)
        assert enforce_freshness(refuted, freshness_field="ts", window_s=1.0) is refuted

    def test_parse_timestamp_forms(self):
        assert parse_timestamp(0).year == 1970
        assert parse_timestamp("2026-01-01T00:00:00Z") is not None
        assert parse_timestamp("2026-01-01T00:00:00") is not None  # naive -> UTC
        assert parse_timestamp("1600000000") is not None
        assert parse_timestamp("not a time") is None
        assert parse_timestamp(None) is None
        assert parse_timestamp(True) is None


# -- collateral hooks ---------------------------------------------------------


class TestCollateralHooks:
    def test_hook_violation_demotes_confirmed(self):
        confirmed = EffectVerdict(
            verdict=Verdict.CONFIRMED, kind=EffectKind.RECORD_WRITTEN
        )
        verdict = apply_collateral_hooks(
            confirmed,
            reachable_state(),
            reachable_state(),
            [lambda before, after: "a message left to an unexpected recipient"],
        )
        assert verdict.verdict is Verdict.REFUTED
        assert "unexpected recipient" in verdict.reason

    def test_clean_hooks_leave_verdict_alone(self):
        confirmed = EffectVerdict(
            verdict=Verdict.CONFIRMED, kind=EffectKind.RECORD_WRITTEN
        )
        verdict = apply_collateral_hooks(
            confirmed, reachable_state(), reachable_state(), [lambda b, a: None]
        )
        assert verdict.verdict is Verdict.CONFIRMED

    def test_hooks_never_upgrade_a_failure(self):
        refuted = EffectVerdict(verdict=Verdict.REFUTED, kind=EffectKind.RECORD_WRITTEN)
        verdict = apply_collateral_hooks(
            refuted, reachable_state(), reachable_state(), [lambda b, a: None]
        )
        assert verdict.verdict is Verdict.REFUTED


# -- evidence minimization ----------------------------------------------------


class TestRedaction:
    def _confirmed(self):
        return EffectVerdict(
            verdict=Verdict.CONFIRMED,
            kind=EffectKind.FIELD_EQUALS,
            observed_value="123-45-6789",
            expected_value="123-45-6789",
            matched_records=[{"id": 1, "ssn": "123-45-6789", "status": "final"}],
        )

    def test_redact_fields_hashes_values(self):
        policy = RedactionPolicy(redact_fields=["ssn"])
        verdict = redact_verdict(self._confirmed(), policy, field="ssn")
        record = verdict.matched_records[0]
        assert "123-45-6789" not in str(record["ssn"])
        assert record["ssn"] == "[redacted]"
        assert record["status"] == "final"  # untouched
        assert "123-45-6789" not in str(verdict.observed_value)
        assert "123-45-6789" not in str(verdict.expected_value)

    def test_keep_fields_allowlist(self):
        policy = RedactionPolicy(keep_fields=["id"])
        record = redact_verdict(self._confirmed(), policy).matched_records[0]
        assert record["id"] == 1
        assert "123-45-6789" not in str(record["ssn"])
        assert "final" not in str(record["status"])

    def test_redaction_never_alters_the_verdict(self):
        refuted = EffectVerdict(
            verdict=Verdict.REFUTED,
            kind=EffectKind.RECORD_WRITTEN,
            observed_count=2,
            expected_count=1,
            matched_records=[{"ssn": "x"}, {"ssn": "y"}],
        )
        verdict = redact_verdict(refuted, RedactionPolicy(redact_fields=["ssn"]))
        assert verdict.verdict is Verdict.REFUTED
        assert verdict.observed_count == 2

    def test_no_policy_is_identity(self):
        verdict = self._confirmed()
        assert redact_verdict(verdict, None) is verdict

    def test_config_builds_redacting_wrapper(self, tmp_path):
        (tmp_path / "r.json").write_text('{"claim": {"id": "c-9", "ssn": "s"}}')
        cfg = EffectsConfig(
            kind="document",
            root=str(tmp_path),
            document_field_paths={"claim_id": "claim.id", "ssn": "claim.ssn"},
            evidence_redact_fields=["ssn"],
        )
        verifier = build_effect_verifier(cfg)
        assert isinstance(verifier, RedactingVerifier)
        # Wrapper is protocol-transparent.
        assert verifier.substrate == "document"
        before = EffectState(substrate="document", reachable=True, records=[])
        effect = make_effect(match={"claim_id": "c-9", "parseable": "True"})
        verdict = verifier.verify(effect, before)
        assert verdict.verdict is Verdict.CONFIRMED
        assert str(verdict.matched_records[0]["ssn"]).startswith("[redacted")
        assert verdict.matched_records[0]["claim_id"] == "c-9"
        assert verifier.test_connection().ok is True


# -- plugin SDK seam ----------------------------------------------------------


@pytest.fixture()
def clean_registry(monkeypatch):
    monkeypatch.setattr(adapter_mod, "_REGISTRY", {})
    monkeypatch.setattr(adapter_mod, "_ENTRY_POINTS_LOADED", False)
    yield


class TestPluginSeam:
    def test_programmatic_registration_builds_from_config(
        self, clean_registry, tmp_path
    ):
        ledger = tmp_path / "ledger.csv"
        ledger.write_text("id,patient_id\n1,p1\n")
        register_verifier_factory("csv-ledger", build_csv_ledger_verifier)
        verifier = build_effect_verifier(
            EffectsConfig(kind="csv-ledger", root=str(ledger))
        )
        assert isinstance(verifier, CsvLedgerVerifier)
        assert verifier.test_connection().ok is True
        before = verifier.capture_pre_state()
        verdict = verifier.verify(make_effect(), before)
        assert verdict.verdict is Verdict.CONFIRMED

    def test_duplicate_registration_fails_loud(self, clean_registry):
        register_verifier_factory("csv-ledger", build_csv_ledger_verifier)
        with pytest.raises(ValueError, match="already registered"):
            register_verifier_factory("csv-ledger", build_csv_ledger_verifier)
        register_verifier_factory("csv-ledger", build_csv_ledger_verifier, replace=True)

    def test_entry_point_registration(self, clean_registry, monkeypatch, tmp_path):
        class FakeEntryPoint:
            name = "csv-ledger"
            value = "tests.example_verifier_plugin:build_csv_ledger_verifier"

            @staticmethod
            def load():
                return build_csv_ledger_verifier

        def fake_entry_points(group=None):
            assert group == adapter_mod.ENTRY_POINT_GROUP
            return [FakeEntryPoint()]

        monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points)
        ledger = tmp_path / "ledger.csv"
        ledger.write_text("id,patient_id\n1,p1\n")
        verifier = build_effect_verifier(
            EffectsConfig(kind="csv-ledger", root=str(ledger))
        )
        assert isinstance(verifier, CsvLedgerVerifier)

    def test_broken_entry_point_fails_loud(self, clean_registry, monkeypatch):
        class BrokenEntryPoint:
            name = "acme"
            value = "acme:missing"

            @staticmethod
            def load():
                raise ImportError("no module named acme")

        monkeypatch.setattr(
            importlib.metadata, "entry_points", lambda group=None: [BrokenEntryPoint()]
        )
        with pytest.raises(ValueError, match="failed to load"):
            build_effect_verifier(EffectsConfig(kind="acme"))

    def test_unknown_kind_still_fails_loud(self, clean_registry, monkeypatch):
        monkeypatch.setattr(importlib.metadata, "entry_points", lambda group=None: [])
        with pytest.raises(ValueError, match="unknown effects.kind"):
            build_effect_verifier(EffectsConfig(kind="definitely-not-registered"))

    def test_builtin_kinds_are_not_shadowed_by_plugins(self, clean_registry, tmp_path):
        # A plugin registering a built-in name never wins: built-ins resolve
        # first inside build_effect_verifier.
        register_verifier_factory("rest", build_csv_ledger_verifier)
        verifier = build_effect_verifier(
            EffectsConfig(kind="rest", base_url="http://sor")
        )
        assert isinstance(verifier, RestRecordVerifier)
