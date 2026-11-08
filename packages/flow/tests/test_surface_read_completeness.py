"""Routed baselines preserve actual REST read completeness without widening scope.

These are read-scope controls, not causal write or application qualification.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest

from openadapt_flow.runtime.effects.effect import Effect, EffectState, Verdict
from openadapt_flow.runtime.effects.rest import RestRecordVerifier
from openadapt_flow.runtime.effects.surfaces import SurfaceRoutedVerifier, surface_probe
from tests.e2e.rest_page_cap_server import CappedRecordServer


def _isolate_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    absent = tmp_path / "absent-netrc"
    assert not absent.exists()
    monkeypatch.setenv("NETRC", str(absent))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost,::1")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost,::1")


def _reader(server: CappedRecordServer, complete: bool | None) -> RestRecordVerifier:
    # This owned server has only the prior and target rows below, both within
    # its two-row response; unknown/false remain deliberate refusal controls.
    return RestRecordVerifier(
        server.url,
        records_path="/records",
        records_key="records",
        records_complete=complete,
        timeout_s=1,
        poll_interval_s=0,
    )


def _effect(*, routed: bool) -> Effect:
    return Effect.model_validate(
        {
            "kind": "exact_new_set",
            "match": {"id": "target"},
            "new_records": [{"id": "target", "value": "landed"}],
            "expected_count": 1,
            "identity_field": "id",
            "timeout_s": 0,
            "probe": surface_probe("records") if routed else None,
        }
    )


@pytest.mark.parametrize("complete", [None, False, True])
@pytest.mark.parametrize("path", ["direct", "routed", "serialized"])
def test_actual_rest_completeness_survives_the_baseline_path(
    complete: bool | None,
    path: Literal["direct", "routed", "serialized"],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _isolate_client(monkeypatch, tmp_path)
    with CappedRecordServer(rows=[{"id": "prior", "value": "keep"}]) as server:
        child = _reader(server, complete)
        reader = (
            child if path == "direct" else SurfaceRoutedVerifier({"records": child})
        )
        before = reader.capture_pre_state()
        if path == "direct":
            assert before.readback_complete is complete
        else:
            sub = before.detail["records"]
            assert sub.get("readback_complete") is complete
            assert set(sub) <= {
                "substrate",
                "reachable",
                "records",
                "readback_complete",
            }
            if complete is None:
                assert "readback_complete" not in sub
        if path == "serialized":
            before = EffectState.model_validate_json(before.model_dump_json())
            assert before.detail["records"].get("readback_complete") is complete
        server.append({"id": "target", "value": "landed"})
        verdict = reader.verify(_effect(routed=path != "direct"), before)
        assert verdict.readback_complete is complete
        assert verdict.verdict is (
            Verdict.CONFIRMED if complete is True else Verdict.INDETERMINATE
        )
        assert server.snapshot() == [
            {"id": "prior", "value": "keep"},
            {"id": "target", "value": "landed"},
        ]


@pytest.mark.parametrize("malformed", [0, 1, "false", "true", [], {}])
def test_routed_baseline_does_not_coerce_malformed_completeness(
    malformed: object, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_client(monkeypatch, tmp_path)
    with CappedRecordServer(rows=[{"id": "prior", "value": "keep"}]) as server:
        child = _reader(server, True)
        reader = SurfaceRoutedVerifier({"records": child})
        before = reader.capture_pre_state()
        assert before.detail["records"]["readback_complete"] is True
        before.detail["records"]["readback_complete"] = malformed
        restored = EffectState.model_validate_json(before.model_dump_json())
        server.append({"id": "target", "value": "landed"})
        verdict = reader.verify(_effect(routed=True), restored)
        assert verdict.verdict is Verdict.INDETERMINATE
        assert "baseline read completeness was not established" in verdict.reason


def test_legacy_routed_baseline_without_completeness_remains_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_client(monkeypatch, tmp_path)
    with CappedRecordServer(rows=[{"id": "prior", "value": "keep"}]) as server:
        child = _reader(server, True)
        reader = SurfaceRoutedVerifier({"records": child})
        before = reader.capture_pre_state()
        assert before.detail["records"].pop("readback_complete") is True
        restored = EffectState.model_validate_json(before.model_dump_json())
        server.append({"id": "target", "value": "landed"})
        verdict = reader.verify(_effect(routed=True), restored)
        assert verdict.verdict is Verdict.INDETERMINATE
        assert "baseline read completeness was not established" in verdict.reason
