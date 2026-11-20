"""Regression checks for qualification and commit evidence boundaries."""

import json
from pathlib import Path

import pytest

from openadapt_flow.tx.induce import (
    DefaultFrameAndIntentJudge,
    induce_lens_from_har,
    infer_field_codec,
)
from openadapt_flow.tx.judge import (
    FrameAndIntentJudge,
    SafeNormalizedEntailmentOracle,
    canonicalize_iso_date,
    iso_dates_match,
    strict_equal,
)
from openadapt_flow.tx.lens import TransactionalCommitEngine
from openadapt_flow.tx.receipt import task_key_for
from openadapt_flow.tx.types import CertifiedLensPack, FieldCodec, TxTask
from tests import test_tx_lens as _lens_cases
from tests.test_tx_lens import _make_replace_lens, _StubWireTap

_source_fixture = _lens_cases.live_app


@pytest.fixture
def live_app(_source_fixture):
    return _source_fixture


@pytest.mark.parametrize(
    "intended,stored",
    [("Alice", "Bob"), ("10 mg", "100 mg"), ("AB01", "ABO1"), ("Georgia", "GA")],
)
def test_an_observed_difference_does_not_certify_its_own_equivalence(intended, stored):
    assert infer_field_codec(intended, stored) is None


def test_har_induction_does_not_learn_a_corrupted_write_result():
    # The trace itself is the untrusted evidence: the server stored different
    # values than were sent. Induction must not turn that into an accepting rule.
    sent = {"full_name": "Alice", "dose": "10 mg", "code": "AB01"}
    stored = {"pid": "ent-42", "full_name": "Bob", "dose": "100 mg", "code": "ABO1"}
    har = {
        "log": {
            "version": "1.2",
            "entries": [
                {
                    "request": {
                        "method": "PUT",
                        "url": "http://127.0.0.1:9999/v2/profiles/ent-42",
                        "postData": {
                            "mimeType": "application/json",
                            "text": json.dumps(sent),
                        },
                    },
                    "response": {
                        "status": 200,
                        "content": {
                            "mimeType": "application/json",
                            "text": json.dumps({"data": stored}),
                        },
                    },
                }
            ],
        }
    }
    spec = induce_lens_from_har(har, intended_inputs=sent)
    for field in sent:
        codec = spec.field_codecs.get(field)
        assert codec is None or codec.kind == "exact", (field, codec)
    verdict = FrameAndIntentJudge().evaluate(
        {k: "old" for k in sent},
        {k: stored[k] for k in sent},
        sent,
        field_codecs=spec.field_codecs,
    )
    assert not verdict.passed


@pytest.mark.parametrize(
    "before,after,delta",
    [
        (None, {"x": 1}, {"x": True}),
        ({"x": True}, {"x": 1}, {}),
        (None, {"x": "ok", "privilege": "admin"}, {"x": "ok"}),
    ],
)
def test_qualification_and_execution_use_one_policy(before, after, delta):
    assert not FrameAndIntentJudge().evaluate(before, after, delta).passed
    assert not DefaultFrameAndIntentJudge().evaluate(before, after, delta).passed


def test_distinct_instants_and_scalar_types_remain_distinct():
    assert not iso_dates_match("2026-01-01T00:00:00+14:00", "2026-01-01T00:00:00-10:00")
    assert not iso_dates_match(
        "2026-01-01T00:00:00.0000001Z", "2026-01-01T00:00:00.0000009Z"
    )
    assert not strict_equal(1, 1.0)
    assert not SafeNormalizedEntailmentOracle().entails("account_id", "Georgia", "GA")[
        0
    ]


@pytest.mark.parametrize(
    "date_only,timestamp",
    [
        ("2026-01-01", "2026-01-01T00:00:00+14:00"),
        ("2026-01-01", "2026-01-01T00:00:00-10:00"),
        ("2026-01-01", "2026-01-01T00:00:00.000-05:00"),
    ],
)
def test_a_calendar_date_does_not_match_midnight_at_a_nonzero_offset(
    date_only, timestamp
):
    # Midnight +14:00 and midnight -10:00 are 24 hours apart. A bare date that
    # matched both would make two different instants equal through the date.
    assert not iso_dates_match(date_only, timestamp)
    assert not iso_dates_match(timestamp, date_only)
    assert canonicalize_iso_date(timestamp) != canonicalize_iso_date(date_only)
    codecs = {"at": FieldCodec(kind="iso_date")}
    judge = FrameAndIntentJudge()
    assert not judge.evaluate(
        {"at": "old"}, {"at": timestamp}, {"at": date_only}, field_codecs=codecs
    ).passed
    assert not judge.evaluate(
        {"at": "old"}, {"at": date_only}, {"at": timestamp}, field_codecs=codecs
    ).passed


@pytest.mark.parametrize(
    "timestamp", ["2026-01-01T00:00:00Z", "2026-01-01T00:00:00.000+00:00"]
)
def test_a_calendar_date_still_matches_utc_midnight(timestamp):
    assert iso_dates_match("2026-01-01", timestamp)
    assert canonicalize_iso_date(timestamp) == "2026-01-01"


def test_date_match_agrees_with_date_canonicalization():
    # The same UTC-midnight instant written at another offset names the date.
    assert iso_dates_match("2026-01-01", "2026-01-01T05:00:00+05:00")
    assert canonicalize_iso_date("2026-01-01T05:00:00+05:00") == "2026-01-01"


def test_certified_boolean_without_the_complete_probe_record_is_refused(
    live_app, tmp_path: Path
):
    base_url, _ = live_app
    pack = CertifiedLensPack(
        lens=_make_replace_lens(base_url),
        certified=False,
        probes_passed=True,
        sensitivity=1,
        specificity=1,
    ).with_computed_digest()
    with pytest.raises(ValueError, match="incomplete_probe_set"):
        TransactionalCommitEngine(
            pack,
            workdir=tmp_path / "invalid",
            allow_unqualified_prototype=True,
        )


def test_precommit_read_cannot_return_a_different_entity(
    live_app, tmp_path: Path, monkeypatch
):
    base_url, store = live_app
    original = store.get_one
    monkeypatch.setattr(
        store,
        "get_one",
        lambda entity_id: (
            original("rec-0002") if entity_id == "rec-0001" else original(entity_id)
        ),
    )
    before = store.sql_writes_executed
    engine = TransactionalCommitEngine(
        _make_replace_lens(base_url),
        workdir=tmp_path / "wrong-read",
        allow_unqualified_prototype=True,
    )
    result = engine.execute(
        TxTask(
            task_id="wrong-read",
            operation="update",
            entity_id="rec-0001",
            intended_delta={"phone": "555-2222"},
        )
    )
    assert result.outcome == "BLOCKED_PRECOMMIT"
    assert store.sql_writes_executed == before


def test_undo_refuses_to_overwrite_an_intervening_human_edit(live_app, tmp_path: Path):
    base_url, store = live_app
    engine = TransactionalCommitEngine(
        _make_replace_lens(base_url),
        workdir=tmp_path / "undo",
        allow_unqualified_prototype=True,
    )
    result = engine.execute(
        TxTask(
            task_id="undo-target",
            operation="update",
            entity_id="rec-0001",
            intended_delta={"phone": "555-2222"},
        )
    )
    assert result.outcome == "UNCERTAIN" and not result.can_compensate
    store.direct_concurrent_touch(
        "rec-0001", notes_override="human edit after operation"
    )
    writes_before = store.sql_writes_executed
    assert engine.compensate("undo-target") is False
    assert store.direct_inspect("rec-0001")["notes"] == "human edit after operation"
    assert store.sql_writes_executed == writes_before


def test_same_task_id_cannot_authorize_a_second_target(live_app, tmp_path: Path):
    base_url, store = live_app
    engine = TransactionalCommitEngine(
        _make_replace_lens(base_url),
        workdir=tmp_path / "request-id",
        allow_unqualified_prototype=True,
    )
    first = TxTask(
        task_id="one-request",
        operation="update",
        entity_id="rec-0001",
        intended_delta={"phone": "555-2222"},
    )
    assert engine.execute(first).outcome == "UNCERTAIN"
    writes_before = store.sql_writes_executed
    assert (
        engine.execute(first.model_copy(update={"entity_id": "rec-0002"})).outcome
        == "BLOCKED_PRECOMMIT"
    )
    assert store.sql_writes_executed == writes_before


def test_unobserved_callable_cannot_claim_a_confirmed_transaction(
    live_app, tmp_path: Path
):
    base_url, store = live_app
    tap = _StubWireTap(base_url)
    engine = TransactionalCommitEngine(
        _make_replace_lens(base_url),
        workdir=tmp_path / "unobserved",
        wire_tap=tap,
        allow_unqualified_prototype=True,
    )
    existing = store.direct_inspect("rec-0001")["phone"]
    result = engine.wrap_action(
        TxTask(
            task_id="unobserved",
            operation="update",
            entity_id="rec-0001",
            intended_delta={"phone": existing},
        ),
        lambda: store.direct_concurrent_touch("rec-0002"),
    )
    assert result.outcome == "UNCERTAIN"
    assert "wire_evidence_incomplete" in result.reasons
    assert result.verification_scope == "target_record"
    assert result.collateral_effects_verified is False


def test_one_observed_write_to_a_different_record_is_refuted(live_app, tmp_path):
    from openadapt_flow.tx.wire_tap import SessionWireTap
    from tests.test_tx_lens import _http_json

    base_url, store = live_app
    with SessionWireTap(base_url) as tap:
        engine = TransactionalCommitEngine(
            _make_replace_lens(base_url),
            workdir=tmp_path / "wrong-wire",
            wire_tap=tap,
            allow_unqualified_prototype=True,
        )
        intended = store.direct_inspect("rec-0001")["phone"]

        def action():
            other = store.direct_inspect("rec-0002")
            status, _ = _http_json(
                "PUT",
                tap.base_url + "/records/rec-0002",
                {**other, "notes": "wrong target mutation"},
            )
            assert status == 200

        result = engine.wrap_action(
            TxTask(
                task_id="wrong-wire",
                operation="update",
                entity_id="rec-0001",
                intended_delta={"phone": intended},
            ),
            action,
        )
    assert result.outcome == "REFUTED"
    assert "wire_target_mismatch" in result.reasons
    assert result.wire_summary.mutations_allowed == 1


def test_saved_har_and_undo_preimage_are_encrypted(live_app, tmp_path):
    from openadapt_flow import crypto
    from openadapt_flow.tx.wire_tap import dump_har, load_har

    base_url, store = live_app
    engine = TransactionalCommitEngine(
        _make_replace_lens(base_url),
        workdir=tmp_path / "encrypted-state",
        allow_unqualified_prototype=True,
    )
    result = engine.execute(
        TxTask(
            task_id="encrypted-state",
            operation="update",
            entity_id="rec-0001",
            intended_delta={"phone": "555-2222"},
        )
    )
    assert result.outcome == "UNCERTAIN"
    preimage = (
        tmp_path
        / "encrypted-state"
        / task_key_for("encrypted-state", workdir=tmp_path / "encrypted-state")
        / "undo_r0.json"
    )
    assert crypto.is_encrypted(preimage.read_bytes())
    assert b"First1" not in preimage.read_bytes()
    har = {
        "log": {"version": "1.2", "entries": [], "comment": "private synthetic trace"}
    }
    path = tmp_path / "saved.har"
    dump_har(har, path)
    assert crypto.is_encrypted(path.read_bytes())
    assert b"private synthetic trace" not in path.read_bytes()
    assert load_har(path)["log"]["version"] == "1.2"


def test_default_state_keeps_idempotency_across_engine_instances(live_app):
    base_url, store = live_app
    lens = _make_replace_lens(base_url)
    first = TransactionalCommitEngine(
        lens,
        allow_unqualified_prototype=True,
    )
    task = TxTask(
        task_id="durable-default",
        operation="update",
        entity_id="rec-0001",
        intended_delta={"phone": "555-2222"},
    )
    assert first.execute(task).outcome == "UNCERTAIN"
    writes_before = store.sql_writes_executed
    second = TransactionalCommitEngine(
        lens,
        allow_unqualified_prototype=True,
    )
    assert second.workdir == first.workdir
    assert second.execute(task).outcome == "BLOCKED_PRECOMMIT"
    assert store.sql_writes_executed == writes_before


def test_failed_benchmark_qualification_never_constructs_an_admitted_engine():
    import importlib

    from openadapt_flow.tx.types import EndpointTemplate, EntityLensSpec
    from tests import test_tx_acid_servers as _benchmark_support

    runner = importlib.import_module("benchmark.tx_acid.runner")
    assert (
        Path(runner.__file__).resolve()
        == Path(_benchmark_support._FLOW_BM_DIR) / "tx_acid" / "runner.py"
    )
    _AutoImportedOpenAdaptTxArm = runner._AutoImportedOpenAdaptTxArm
    lens = EntityLensSpec(
        entity_name="record",
        base_url="http://127.0.0.1:1",
        get_one=EndpointTemplate(method="GET", path_template="/records/{id}"),
        write_endpoint=EndpointTemplate(method="PATCH", path_template="/records/{id}"),
    )

    class Server:
        base_url = lens.base_url

        def lens_spec(self):
            return lens

        def make_background_create_payload(self, *args):
            return {"name": "Example"}

        def make_background_update_payload(self, *args):
            return "1", {"name": "Updated"}

    def refused_qualifier(*args, **kwargs):
        return CertifiedLensPack(
            lens=lens, certified=False, sensitivity=0, specificity=0
        )

    def must_not_construct(**kwargs):
        pytest.fail("a failed qualifier reached the admitted engine constructor")

    arm = _AutoImportedOpenAdaptTxArm(
        {
            "SessionWireTap": object,
            "FrameAndIntentJudge": FrameAndIntentJudge,
            "TransactionalCommitEngine": must_not_construct,
            "certify_lens_on_sandbox": refused_qualifier,
        }
    )
    try:
        with pytest.raises(RuntimeError, match="qualification failed"):
            arm._get_or_create_ctx(Server())
    finally:
        arm.close()
