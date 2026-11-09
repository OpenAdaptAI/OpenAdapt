"""Whole-set coverage needs distinct identities in one declared frozen view."""

from __future__ import annotations

from typing import Any, Literal

import pytest

from openadapt_flow.deployment import EffectsConfig, build_effect_verifier
from openadapt_flow.ensure_record import EnsureContract, FhirReader, Unreadable
from openadapt_flow.runtime.effects._common import page_snapshot_token
from openadapt_flow.runtime.effects.effect import Effect, Verdict
from openadapt_flow.runtime.effects.fhir import FhirEffectVerifier
from openadapt_flow.runtime.effects.rest import RestRecordVerifier
from tests.e2e.rest_page_cap_server import CappedRecordServer

ReaderKind = Literal["rest", "fhir"]
AnyReaderKind = Literal["rest", "fhir", "ensure"]


def _reader(
    server: CappedRecordServer, kind: ReaderKind, *, declared: bool = True
) -> Any:
    common: dict[str, Any] = {
        "kind": kind,
        "base_url": server.url if kind == "rest" else server.url + "/fhir",
        "timeout_s": 1,
    }
    if declared:
        common["records_snapshot_path"] = "page.snapshot"
    if kind == "rest":
        common.update(
            records_path="/records",
            records_key="records",
            records_next_path="page.next",
            records_total_path="page.total",
        )
    else:
        common.update(
            resource_type="Patient", field_paths={"id": "id", "value": "value"}
        )
    built = build_effect_verifier(EffectsConfig.model_validate(common))
    assert isinstance(built, (RestRecordVerifier, FhirEffectVerifier))
    return built


def _close(reader: Any) -> None:
    if isinstance(reader, FhirReader):
        if reader._client is not None:
            reader._client.close()
    else:
        reader._get_session().close()


def _readable(reader: Any) -> bool:
    if isinstance(reader, FhirReader):
        try:
            reader.resources("Patient")
        except Unreadable:
            return False
        return True
    return reader.capture_pre_state().reachable


@pytest.mark.parametrize("kind", ["rest", "fhir"])
def test_frozen_pages_survive_a_foreign_commit_without_changing_the_read_view(
    kind: ReaderKind,
) -> None:
    original = [{"id": "a", "value": "prior"}, {"id": "b", "value": "prior"}]
    changed = [{"id": "x", "value": "foreign"}, original[1]]
    with CappedRecordServer(page_size=1, rows=original, immutable_pages=True) as server:
        server.after_first_page = lambda store: store.replace(changed)
        reader = _reader(server, kind)
        try:
            captured = reader.capture_pre_state()
            assert captured.reachable
            assert list(captured.records) == original
            assert server.snapshot() == changed
            # The next complete chain is a new view of the actual current store.
            current = reader.capture_pre_state()
            assert current.reachable
            assert list(current.records) == changed
        finally:
            _close(reader)


@pytest.mark.parametrize("kind", ["rest", "fhir"])
def test_live_offset_insert_and_delete_cannot_hide_an_unintended_write(
    kind: ReaderKind,
) -> None:
    prior = {"id": "prior", "value": "keep"}
    target = {"id": "target", "value": "wanted"}
    unintended = {"id": "unintended", "value": "wrong"}
    with CappedRecordServer(page_size=1, rows=[prior]) as server:
        reader = _reader(server, kind, declared=False)
        try:
            before = reader.capture_pre_state()
            assert before.reachable and list(before.records) == [prior]
            server.replace([prior, target])
            # Live page zero is [prior], page one becomes [target]. There are
            # no duplicate IDs and total stays two, but the omitted row is real.
            server.after_first_page = lambda store: store.replace([unintended, target])
            effect = Effect.model_validate(
                {
                    "kind": "exact_new_set",
                    "match": {},
                    "new_records": [{"id": "target", "value": "wanted"}],
                    "expected_count": 1,
                    "identity_field": "id",
                    "timeout_s": 0,
                }
            )
            result = reader.verify(effect, before)
            assert server.snapshot() == [unintended, target]
            assert len({row["id"] for row in server.snapshot()}) == 2
            assert result.verdict is Verdict.INDETERMINATE
            assert "snapshot" in reader.read_problem
        finally:
            _close(reader)


@pytest.mark.parametrize("kind", ["rest", "fhir"])
def test_an_observed_snapshot_value_does_not_replace_a_declared_contract(
    kind: ReaderKind,
) -> None:
    with CappedRecordServer(
        page_size=1,
        rows=[{"id": "a"}, {"id": "b"}],
        immutable_pages=True,
    ) as server:
        reader = _reader(server, kind, declared=False)
        try:
            state = reader.capture_pre_state()
            assert not state.reachable
            assert "snapshot" in reader.read_problem
        finally:
            _close(reader)


@pytest.mark.parametrize("kind", ["rest", "fhir"])
def test_frozen_snapshot_exposes_an_extra_row_and_refutes_the_exact_set(
    kind: ReaderKind,
) -> None:
    with CappedRecordServer(page_size=1, immutable_pages=True) as server:
        reader = _reader(server, kind)
        try:
            before = reader.capture_pre_state()
            assert before.reachable
            server.replace(
                [{"id": "target", "value": "wanted"}, {"id": "other", "value": "wrong"}]
            )
            result = reader.verify(
                Effect.model_validate(
                    {
                        "kind": "exact_new_set",
                        "match": {},
                        "new_records": [{"id": "target", "value": "wanted"}],
                        "expected_count": 1,
                        "identity_field": "id",
                        "timeout_s": 0,
                    }
                ),
                before,
            )
            assert result.verdict is Verdict.REFUTED
            assert len(server.snapshot()) == 2
        finally:
            _close(reader)


@pytest.mark.parametrize("kind", ["rest", "fhir"])
def test_distinct_frozen_rows_with_equal_fields_remain_distinct(
    kind: ReaderKind,
) -> None:
    with CappedRecordServer(page_size=1, immutable_pages=True) as server:
        reader = _reader(server, kind)
        try:
            before = reader.capture_pre_state()
            assert before.reachable
            server.replace(
                [{"id": str(index), "value": "wanted"} for index in range(3)]
            )
            result = reader.verify(
                Effect.model_validate(
                    {
                        "kind": "exact_new_set",
                        "match": {},
                        "new_records": [{"value": "wanted"}] * 3,
                        "expected_count": 3,
                        "identity_field": "id",
                        "timeout_s": 0,
                    }
                ),
                before,
            )
            assert result.verdict is Verdict.CONFIRMED
            assert result.observed_count == 3
        finally:
            _close(reader)


@pytest.mark.parametrize("value", [None, True, False, 1.0, "", " ", [], {}, -1])
def test_snapshot_token_requires_an_exact_supported_nonempty_value(value: Any) -> None:
    with pytest.raises(ValueError, match="unsupported"):
        page_snapshot_token({"page": {"snapshot": value}}, "page.snapshot")


@pytest.mark.parametrize("value", [0, 1, "0", "view-v1"])
def test_snapshot_token_preserves_its_exact_type_and_value(value: Any) -> None:
    observed = page_snapshot_token({"page": {"snapshot": value}}, "page.snapshot")
    assert type(observed) is type(value)
    assert observed == value


@pytest.mark.parametrize("kind", ["rest", "fhir"])
@pytest.mark.parametrize("path", ["", ".x", "x.", "x..y", "x y", 1, False, [], {}])
def test_snapshot_declaration_rejects_malformed_paths(kind: str, path: Any) -> None:
    with pytest.raises(ValueError):
        EffectsConfig.model_validate({"kind": kind, "records_snapshot_path": path})


def test_snapshot_declaration_preserves_legacy_serialization_and_binds_identity() -> (
    None
):
    from openadapt_flow.deployment import _effect_verifier_config_identity

    legacy = EffectsConfig.model_validate(
        {"kind": "rest", "base_url": "http://sor", "records_path": "/records"}
    )
    declared = EffectsConfig.model_validate(
        {**legacy.model_dump(mode="json"), "records_snapshot_path": "page.snapshot"}
    )
    assert "records_snapshot_path" not in legacy.model_dump(mode="json")
    assert declared.model_dump(mode="json")["records_snapshot_path"] == "page.snapshot"
    assert _effect_verifier_config_identity(
        legacy, object()
    ) != _effect_verifier_config_identity(declared, object())


def _ensure_contract(server: CappedRecordServer, *, declared: bool) -> EnsureContract:
    data: dict[str, Any] = {
        "resource_type": "Patient",
        "base_url": server.url + "/fhir",
        "self": ["Practitioner/synthetic"],
        "fields": {"value": {"path": "value"}},
        "identity": [["value"]],
        "search": {"value": "value"},
    }
    if declared:
        data["records_snapshot_path"] = "page.snapshot"
    return EnsureContract.model_validate(data)


def test_ensure_reader_rejects_the_same_live_offset_omission() -> None:
    original = [{"id": "a", "value": "prior"}, {"id": "b", "value": "target"}]
    changed = [{"id": "x", "value": "unintended"}, original[1]]
    with CappedRecordServer(page_size=1, rows=original) as server:
        server.after_first_page = lambda store: store.replace(changed)
        reader = FhirReader(_ensure_contract(server, declared=False))
        try:
            with pytest.raises(Unreadable, match="snapshot"):
                reader.resources("Patient")
            assert server.snapshot() == changed
        finally:
            if reader._client is not None:
                reader._client.close()


def test_ensure_reader_uses_one_real_frozen_snapshot() -> None:
    original = [{"id": "a", "value": "prior"}, {"id": "b", "value": "target"}]
    changed = [{"id": "x", "value": "unintended"}, original[1]]
    with CappedRecordServer(page_size=1, rows=original, immutable_pages=True) as server:
        server.after_first_page = lambda store: store.replace(changed)
        reader = FhirReader(_ensure_contract(server, declared=True))
        try:
            expected = [{"resourceType": "Patient", **row} for row in original]
            assert reader.resources("Patient") == expected
            assert server.snapshot() == changed
        finally:
            if reader._client is not None:
                reader._client.close()


@pytest.mark.parametrize("declared", [False, True])
def test_ensure_history_keeps_distinct_versions_of_the_same_resource(
    declared: bool,
) -> None:
    versions = [
        {"id": "a", "value": "prior", "meta": {"versionId": "1"}},
        {"id": "a", "value": "changed", "meta": {"versionId": "2"}},
    ]
    with CappedRecordServer(page_size=1, rows=versions, immutable_pages=True) as server:
        reader = FhirReader(_ensure_contract(server, declared=declared))
        try:
            if declared:
                entries = reader.bundle("Patient/_history")
                assert [
                    entry["resource"]["meta"]["versionId"] for entry in entries
                ] == ["1", "2"]
            else:
                with pytest.raises(Unreadable, match="snapshot"):
                    reader.bundle("Patient/_history")
        finally:
            if reader._client is not None:
                reader._client.close()


def test_ensure_snapshot_field_none_keeps_the_original_contract_bytes() -> None:
    with CappedRecordServer() as server:
        legacy = _ensure_contract(server, declared=False)
        dumped = legacy.model_dump(mode="json", by_alias=True)
        assert "records_snapshot_path" not in dumped
        assert (
            EnsureContract.model_validate(dumped).model_dump(mode="json", by_alias=True)
            == dumped
        )
        declared = _ensure_contract(server, declared=True)
        assert (
            declared.model_dump(mode="json")["records_snapshot_path"] == "page.snapshot"
        )


@pytest.mark.parametrize("kind", ["rest", "fhir", "ensure"])
@pytest.mark.parametrize(
    "fault",
    [
        "missing_first",
        "missing_later",
        "changed",
        "type_changed",
        "blank",
        "boolean",
        "float",
        "container",
    ],
)
def test_snapshot_metadata_faults_make_the_actual_chain_unreadable(
    kind: AnyReaderKind, fault: str
) -> None:
    def mutate(body: dict[str, Any], page: int) -> None:
        if fault == "missing_first" and page == 0:
            del body["page"]["snapshot"]
        if page == 1:
            if fault == "missing_later":
                del body["page"]["snapshot"]
            elif fault == "changed":
                body["page"]["snapshot"] = "another-view"
            elif fault == "type_changed":
                body["page"]["snapshot"] = int(body["page"]["snapshot"])
            elif fault in {"blank", "boolean", "float", "container"}:
                body["page"]["snapshot"] = {
                    "blank": " ",
                    "boolean": True,
                    "float": 1.0,
                    "container": {},
                }[fault]

    with CappedRecordServer(
        page_size=1,
        rows=[{"id": "a"}, {"id": "b"}],
        immutable_pages=True,
        transform_response=mutate,
    ) as server:
        reader = (
            FhirReader(_ensure_contract(server, declared=True))
            if kind == "ensure"
            else _reader(server, kind)
        )
        try:
            assert not _readable(reader)
            assert len(server.snapshot()) == 2
        finally:
            _close(reader)


@pytest.mark.parametrize("kind", ["rest", "fhir", "ensure"])
def test_duplicate_padding_refuses_even_when_the_snapshot_token_matches(
    kind: AnyReaderKind,
) -> None:
    rows = [
        {"id": "a", "value": "wanted"},
        {"id": "b", "value": "wanted"},
        {"id": "hidden", "value": "wrong"},
    ]
    with CappedRecordServer(
        page_size=1, rows=rows, immutable_pages=True, repeat_boundary=True
    ) as server:
        reader = (
            FhirReader(_ensure_contract(server, declared=True))
            if kind == "ensure"
            else _reader(server, kind)
        )
        try:
            assert not _readable(reader)
            assert server.snapshot() == rows
        finally:
            _close(reader)


@pytest.mark.parametrize("kind", ["rest", "fhir", "ensure"])
def test_partial_http_response_cannot_become_complete_from_snapshot_metadata(
    kind: AnyReaderKind,
) -> None:
    with CappedRecordServer(
        rows=[{"id": "a"}], immutable_pages=True, http_status=206
    ) as server:
        reader = (
            FhirReader(_ensure_contract(server, declared=True))
            if kind == "ensure"
            else _reader(server, kind)
        )
        try:
            assert not _readable(reader)
        finally:
            _close(reader)


@pytest.mark.parametrize("declared", [False, True])
def test_synthesis_paged_scope_stays_draft_without_the_snapshot_declaration(
    declared: bool,
) -> None:
    from openadapt_flow.record_check_synthesis import Surface, check_reads

    with CappedRecordServer(
        page_size=1, rows=[{"id": "a"}, {"id": "b"}], immutable_pages=True
    ) as server:
        config: dict[str, Any] = {
            "kind": "rest",
            "base_url": server.url,
            "records_path": "/records",
            "records_key": "records",
            "records_next_path": "page.next",
            "records_total_path": "page.total",
        }
        if declared:
            config["records_snapshot_path"] = "page.snapshot"
        surface = Surface(
            "rows", config, [], [], True, "explicit synthetic fixture contract"
        )
        problems = check_reads(
            [surface], {"kind": "surfaces", "surfaces": {"rows": config}}, {}
        )
        if declared:
            assert problems == []
        else:
            assert any("snapshot" in problem for problem in problems)
        reader = build_effect_verifier(EffectsConfig.model_validate(config))
        assert isinstance(reader, RestRecordVerifier)
        try:
            assert reader.capture_pre_state().reachable is declared
        finally:
            _close(reader)


@pytest.mark.parametrize("kind", ["rest", "fhir", "ensure"])
def test_consistent_unique_pages_still_respect_the_hard_page_bound(
    kind: AnyReaderKind, monkeypatch: pytest.MonkeyPatch
) -> None:
    import openadapt_flow.ensure_record as ensure
    from openadapt_flow.runtime.effects import fhir, rest

    monkeypatch.setattr(
        {"rest": rest, "fhir": fhir, "ensure": ensure}[kind], "MAX_PAGES", 2
    )
    with CappedRecordServer(
        page_size=1,
        rows=[{"id": str(index)} for index in range(3)],
        immutable_pages=True,
    ) as server:
        reader = (
            FhirReader(_ensure_contract(server, declared=True))
            if kind == "ensure"
            else _reader(server, kind)
        )
        try:
            assert not _readable(reader)
            assert len(server.snapshot()) == 3
        finally:
            _close(reader)
