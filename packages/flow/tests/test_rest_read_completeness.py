"""Actual page caps cannot establish absence beyond the observed page."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Literal

import pytest

from openadapt_flow.deployment import EffectsConfig, build_effect_verifier
from openadapt_flow.qualify.config import VerifyConfig as QualificationConfig
from openadapt_flow.qualify.session import verifier_factory
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectKind,
    EffectVerdict,
    Verdict,
)
from openadapt_flow.runtime.effects.rest import RestRecordVerifier
from tests.e2e.rest_page_cap_server import CappedRecordServer
from tests.e2e.temporal_observation_protocol import (
    TemporalScenario,
    temporal_violations,
)
from tests.e2e.temporal_rest_completeness import (
    ActualReadFact,
    capture_rest_reads,
    observe_rest_verdict,
)

ReadMode = Literal["unconfigured", "total", "pages", "identity"]
Entrypoint = Literal["direct", "builder", "session_factory"]

# A complete raw delta is not admitted causal new-write evidence. These
# failures must remain visible rather than being ignored by positive reads.
UNQUALIFIED_NEW_WRITE_VIOLATIONS = frozenset(
    {
        "confirmation_without_observed_dispatch_effect",
        "confirmation_without_verifier_attribution",
        "confirmation_incomplete_claim_ref",
        "confirmation_without_authenticated_principal",
        "confirmation_profile_binding_mismatch",
        "confirmation_without_admitted_profile",
        "confirmation_readback_effect_mismatch",
        "confirmation_without_new_commit_disposition",
        "confirmation_response_target_mismatch",
        "confirmation_commit_version_mismatch",
        "confirmation_with_unsupported_evidence_channel",
    }
)


def _verifier(
    server: CappedRecordServer,
    mode: ReadMode,
    target: str,
    entrypoint: Entrypoint,
) -> RestRecordVerifier:
    records_path = f"/records/{target}" if mode == "identity" else "/records?limit=1000"
    total_path = "page.total" if mode in ("total", "pages", "identity") else None
    next_path = "page.next" if mode == "pages" else None
    if entrypoint == "direct":
        return RestRecordVerifier(
            server.url,
            records_path=records_path,
            records_key="records",
            timeout_s=2,
            poll_interval_s=0,
            total_path=total_path,
            next_path=next_path,
            snapshot_path="page.snapshot" if mode == "pages" else None,
        )
    section = {
        "kind": "rest",
        "base_url": server.url,
        "records_path": records_path,
        "records_key": "records",
        "timeout_s": 2,
        "poll_interval_s": 0,
        "records_total_path": total_path,
        "records_next_path": next_path,
        "records_snapshot_path": "page.snapshot" if mode == "pages" else None,
    }
    if entrypoint == "builder":
        built = build_effect_verifier(EffectsConfig.model_validate(section))
    else:
        # This is a real closed configuration and real importable client hook,
        # not a qualification result, mock loader or invented profile admission.
        config = QualificationConfig.model_validate(
            {
                "schema_version": "openadapt.verify/v1",
                "app": {
                    "name": "synthetic collection",
                    "url": server.url,
                    "version": "1",
                },
                "verifier": {
                    **section,
                    "session": "tests.e2e.rest_page_cap_server:open_read_session",
                },
                "effects": [_effect(target).model_dump(mode="json")],
            }
        )
        built = verifier_factory(config)({})
    assert isinstance(built, RestRecordVerifier)
    return built


def _effect(target: str) -> Effect:
    return Effect(
        kind=EffectKind.EXACT_NEW_SET,
        match={"id": target},
        new_records=[{"id": target, "value": "landed"}],
        expected_count=1,
        identity_field="id",
        timeout_s=0,
    )


def _read_case(
    cap: int,
    mode: ReadMode,
    tmp_path: Path,
    *,
    metadata: bool = True,
    write_target: bool = True,
    http_status: int = 200,
    entrypoint: Entrypoint = "direct",
) -> tuple[EffectVerdict, ActualReadFact, tuple[str, ...]]:
    target = str(cap + 1)
    rows = [{"id": str(i), "value": "prior"} for i in range(1, cap + 1)]
    with (
        pytest.MonkeyPatch.context() as environment,
        CappedRecordServer(
            page_size=cap,
            metadata=metadata,
            http_status=http_status,
            rows=rows,
            immutable_pages=mode == "pages",
        ) as server,
    ):
        absent_netrc = tmp_path / "absent-netrc"
        assert not absent_netrc.exists()
        environment.setenv("NETRC", str(absent_netrc))
        environment.setenv("NO_PROXY", "127.0.0.1,localhost,::1")
        environment.setenv("no_proxy", "127.0.0.1,localhost,::1")
        verifier, effect = _verifier(server, mode, target, entrypoint), _effect(target)
        before = verifier.capture_pre_state()
        if write_target:
            server.append({"id": target, "value": "landed"})
        native = server.snapshot()
        with capture_rest_reads(verifier) as reads:
            verdict = verifier.verify(effect, before)
        assert len(reads) == 1, "timeout_s=0 requires exactly one actual read chain"
        observation = observe_rest_verdict(verifier, effect, before, verdict, reads[0])
        scenario = TemporalScenario(initial_target="absent", intent="create")
        violations = temporal_violations(scenario, observation)
        (tmp_path / "actual-read-completeness.json").write_text(
            json.dumps(
                {
                    "cap": cap,
                    "mode": mode,
                    "entrypoint": entrypoint,
                    "actual_reader_config": {
                        "records_path": verifier.records_path,
                        "total_path": verifier.total_path,
                        "next_path": verifier.next_path,
                        "records_complete": getattr(verifier, "records_complete", None),
                    },
                    "native_count": len(native),
                    "native_target_present": any(row["id"] == target for row in native),
                    "actual_read": asdict(reads[0]),
                    "actual_verdict": verdict.model_dump(mode="json"),
                    "observation": asdict(observation),
                    "violations": list(violations),
                },
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        verifier._get_session().close()
        return verdict, reads[0], violations


@pytest.mark.parametrize("cap", [2, 200])
def test_target_after_declared_server_cap_is_unread_without_paging_contract(
    cap: int,
    tmp_path: Path,
) -> None:
    verdict, read, violations = _read_case(cap, "unconfigured", tmp_path)
    if read.pages:
        # The genuine parent reads and falsely refutes from this capped page.
        assert read.pages[0].server_total == cap + 1
        assert read.pages[0].server_cap == cap
        assert read.pages[0].server_has_next is True
        assert read.complete is False
    else:
        # Refusal before I/O establishes no read completeness or page facts.
        assert read.returned_count is None and read.complete is None
    assert not violations
    assert verdict.verdict is Verdict.INDETERMINATE
    assert verdict.observed_effect == "unknown"


@pytest.mark.parametrize("cap", [2, 200])
def test_unknown_completeness_of_limited_read_cannot_refute(
    cap: int,
    tmp_path: Path,
) -> None:
    verdict, read, violations = _read_case(
        cap, "unconfigured", tmp_path, metadata=False
    )
    if read.pages:
        assert read.pages[0].server_total is None
        assert read.pages[0].server_cap is None
    else:
        assert read.returned_count is None
    assert read.complete is None
    assert not violations
    assert verdict.verdict is Verdict.INDETERMINATE
    assert verdict.observed_effect == "unknown"


@pytest.mark.parametrize("cap", [2, 200])
def test_declared_total_refuses_a_truncated_read(cap: int, tmp_path: Path) -> None:
    verdict, read, violations = _read_case(cap, "total", tmp_path)
    assert read.complete is False
    assert verdict.verdict is Verdict.INDETERMINATE
    assert not violations


@pytest.mark.parametrize("cap", [2, 200])
@pytest.mark.parametrize("mode", ["pages", "identity"])
def test_declared_complete_read_sees_record_beyond_collection_cap(
    cap: int,
    mode: ReadMode,
    tmp_path: Path,
) -> None:
    verdict, read, violations = _read_case(cap, mode, tmp_path)
    assert read.complete is True
    assert verdict.verdict is Verdict.CONFIRMED
    assert verdict.observed_count == 1
    # A correct read is not, by itself, causal new-write attribution. The
    # observer leaves owned dispatch/admission facts unavailable. The model
    # reports all known missing attribution facts, including one repeated
    # principal diagnostic; compare the exact set of diagnostic kinds.
    assert set(violations) == UNQUALIFIED_NEW_WRITE_VIOLATIONS


@pytest.mark.parametrize("cap", [2, 200])
def test_complete_absence_refutes_raw_delta_but_not_historical_claim(
    cap: int, tmp_path: Path
) -> None:
    verdict, read, violations = _read_case(cap, "total", tmp_path, write_target=False)
    assert read.complete is True
    assert verdict.verdict is Verdict.REFUTED
    assert verdict.observed_count == 0
    # The raw snapshot-delta predicate is false. It does not establish that
    # no historical commit occurred: a commit followed by removal gives
    # the same endpoint observations. Retain that unsupported conclusion.
    assert violations == ("refutation_temporal_scope_mismatch",)


def test_partial_content_cannot_refute_even_when_record_total_matches(
    tmp_path: Path,
) -> None:
    verdict, read, violations = _read_case(
        2, "total", tmp_path, write_target=False, http_status=206
    )
    assert len(read.pages) == 1
    assert read.pages[0].http_status == 206
    assert read.pages[0].server_total == read.pages[0].returned_count == 2
    assert read.complete is False
    assert not violations
    assert verdict.verdict is Verdict.INDETERMINATE
    assert verdict.observed_effect == "unknown"


def test_normal_builder_preserves_unknown_read_refusal(tmp_path: Path) -> None:
    verdict, read, violations = _read_case(
        2, "unconfigured", tmp_path, entrypoint="builder"
    )
    assert read.complete is not True
    assert not violations
    assert verdict.verdict is Verdict.INDETERMINATE


def test_custom_session_factory_preserves_declared_pagination(tmp_path: Path) -> None:
    verdict, read, violations = _read_case(
        2, "pages", tmp_path, entrypoint="session_factory"
    )
    assert read.complete is True
    assert verdict.verdict is Verdict.CONFIRMED
    assert verdict.observed_count == 1
    assert set(violations) == UNQUALIFIED_NEW_WRITE_VIOLATIONS


def test_custom_session_factory_refuses_partial_content(tmp_path: Path) -> None:
    verdict, read, violations = _read_case(
        2,
        "total",
        tmp_path,
        entrypoint="session_factory",
        write_target=False,
        http_status=206,
    )
    assert len(read.pages) == 1 and read.pages[0].http_status == 206
    assert read.pages[0].server_total == read.pages[0].returned_count == 2
    assert read.complete is False
    assert not violations
    assert verdict.verdict is Verdict.INDETERMINATE
