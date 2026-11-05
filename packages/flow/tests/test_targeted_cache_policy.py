"""Source-only registered controls; no native-app/cache qualification claim.

Reuse the existing actual loopback server. Declared zero is explicit fixture
configuration, not inferred from a response, helper, clock or default.
"""

from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from openadapt_flow import verifier as api
from openadapt_flow.deployment import (
    EffectsConfig,
    _identity_config_dump,
    build_effect_verifier,
)
from openadapt_flow.ensure_record import EnsureContract, FhirReader
from openadapt_flow.runtime.effects import targeted
from openadapt_flow.runtime.effects.fhir import FhirEffectVerifier
from tests.test_targeted_recheck import ACTOR, PARAMS, contract, server


def configured(base: str, bound: Any = 0.0, **target_options: Any) -> api.Contract:
    doc = contract(base, **target_options).config.model_dump(mode="json", by_alias=True)
    doc["verifier"]["fhir_cache_bound_s"] = bound
    return api.Contract.from_dict(doc)


def issue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server: Any,
    *,
    bound: Any = 0.0,
    name: str | None = None,
    identity: str = "private-record-id",
) -> tuple[Any, Any, Any, Any]:
    monkeypatch.setenv("OPENADAPT_VERIFIER_STATE_DIR", str(tmp_path / "authority"))
    store, base = server
    cfg = configured(base, bound)
    params = dict(PARAMS) if name is None else {**PARAMS, "name": name}
    ledger = api.Ledger(api.MemoryLedger())
    pending = api.begin_write(
        cfg, params, actor=ACTOR, ledger=ledger, workdir=tmp_path / identity
    )
    store.save(identity, name=params["name"])
    write = api.verify_write(pending, write_attempted=True)
    assert write.outcome is api.Outcome.CONFIRMED and write.recheck is not None
    return cfg, params, write, ledger


@pytest.mark.parametrize("bound", [None, 0, 0.125, 60])
def test_operator_bound_routes_through_config_factory_and_ensure(
    server: Any, bound: Any
) -> None:
    _, base = server
    cfg = configured(base, bound)
    adapter = build_effect_verifier(cfg.config.verifier, PARAMS)
    assert adapter.cache_bound_s == bound
    search = cfg.config.frame.targeted.search.model_dump(mode="json", by_alias=True)
    search["cache_bound_s"] = bound
    ensure = EnsureContract.model_validate(search)
    reader = FhirReader(ensure, get=lambda url, query: (503, {}, {}))
    assert reader.cache_bound_s == bound
    bridge_params = {"name": PARAMS["name"]}
    bridge = api.Contract.from_ensure(ensure, bridge_params)
    assert bridge.config.verifier.fhir_cache_bound_s == bound
    from openadapt_flow.verify_run import _build_verifier

    bridged_adapter = _build_verifier(bridge.config, bridge_params)
    assert bridged_adapter.cache_bound_s == bound
    assert bridged_adapter.reader.cache_bound_s == bound
    dumped = cfg.config.verifier.model_dump(mode="json")
    if bound is None:
        assert "fhir_cache_bound_s" not in dumped
        assert "cache_bound_s" not in ensure.model_dump(mode="json")
        absent = copy.deepcopy(dumped)
        absent.pop("fhir_cache_bound_s", None)
        assert _identity_config_dump(
            EffectsConfig.model_validate(absent)
        ) == _identity_config_dump(cfg.config.verifier)
    else:
        assert dumped["fhir_cache_bound_s"] == bound


@pytest.mark.parametrize(
    "bound", [True, False, -1, "0", float("nan"), float("inf"), float("-inf")]
)
def test_invalid_bound_refuses_config_and_direct_constructor(
    server: Any, bound: Any
) -> None:
    _, base = server
    with pytest.raises(ValueError):
        configured(base, bound)
    with pytest.raises(ValueError):
        FhirEffectVerifier(
            base, resource_type="Basic", field_paths={"id": "id"}, cache_bound_s=bound
        )
    data = contract(base).config.frame.targeted.search.model_dump(
        mode="json", by_alias=True
    )
    data["cache_bound_s"] = bound
    with pytest.raises(ValueError):
        EnsureContract.model_validate(data)


def test_non_fhir_bound_refuses() -> None:
    with pytest.raises(ValueError):
        EffectsConfig.model_validate({"kind": "none", "fhir_cache_bound_s": 0})


@pytest.mark.parametrize(
    "bound,elapsed_delta,problem",
    [
        (0.01, 0, "late_search_window_does_not_exceed_declared_bound"),
        (0.02, 0, "late_search_window_does_not_exceed_declared_bound"),
        (0, -1, "late_search_window_not_elapsed"),
        (0, 0, None),
    ],
)
def test_strict_bound_and_actual_elapsed_are_separate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server: Any,
    bound: float,
    elapsed_delta: int,
    problem: str | None,
) -> None:
    cfg, params, write, _ = issue(tmp_path, monkeypatch, server, bound=bound)
    state = write.recheck
    post = state.payload["post_state"]["targeted"]
    reader = build_effect_verifier(cfg.config.verifier, params)
    stamp = state._targeted_read_clock.completed_ns
    assert (
        targeted.late_search_problem(
            cfg.config.frame.targeted,
            reader,
            post,
            bytes.fromhex(state.payload["salt"]),
            state._targeted_read_clock,
            stamp + 10_000_000 + elapsed_delta,
        )
        == problem
    )


def test_absent_bound_is_unknown_and_skips_late_identifying_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: Any
) -> None:
    cfg, params, write, ledger = issue(tmp_path, monkeypatch, server, bound=None)
    monkeypatch.setattr(api, "seconds_until", lambda due: 0)
    store, _ = server
    before = len(store.calls)
    result = api.recheck(cfg, write, params=params, actor=ACTOR, ledger=ledger)
    assert result.outcome == "UNCERTAIN" and not result.verified and not result.final
    detail = result.evidence["targeted"]
    assert detail["cache_policy"] == {"bound_s": None, "source": "unknown", "W_s": 0.01}
    assert "late_search_cache_bound_unknown" in detail["problems"]
    assert not any(
        urlsplit(p).path == "/Basic" and "name" in parse_qs(urlsplit(p).query)
        for p in store.calls[before:]
    )


@pytest.mark.parametrize("ledger_mode", ["original", "none", "alternate"])
@pytest.mark.parametrize(
    "forgery",
    ["missing", "stamp", "domain", "foreign_live", "mac", "copied_foreign_receipt"],
)
def test_unsealed_or_foreign_clock_cannot_grant_late_search(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    server: Any,
    ledger_mode: str,
    forgery: str,
) -> None:
    cfg, params, write, ledger = issue(tmp_path, monkeypatch, server)
    state = write.recheck
    clock = state._targeted_read_clock
    assert clock is not None
    if forgery == "missing":
        state._targeted_read_clock = None
    elif forgery == "stamp":
        state._targeted_read_clock = replace(clock, completed_ns=clock.completed_ns - 1)
    elif forgery == "domain":
        state._targeted_read_clock = replace(clock, domain="1:1")
    elif forgery == "foreign_live":
        state._targeted_read_clock = replace(clock, live_domain=object())
    elif forgery == "mac":
        state._targeted_read_clock = replace(clock, post_mac="hmac-sha256:" + "0" * 64)
    else:
        _, _, other, _ = issue(
            tmp_path,
            monkeypatch,
            server,
            name="another-private-name",
            identity="another-private-id",
        )
        state._targeted_read_clock = other.recheck._targeted_read_clock
    monkeypatch.setattr(api, "seconds_until", lambda due: 0)
    store, _ = server
    before = len(store.calls)
    selected = (
        ledger
        if ledger_mode == "original"
        else api.Ledger(api.MemoryLedger())
        if ledger_mode == "alternate"
        else None
    )
    result = api.recheck(cfg, write, params=params, actor=ACTOR, ledger=selected)
    assert result.outcome == "UNCERTAIN" and not result.verified and not result.final
    assert (
        "late_search_live_read_clock_binding_unknown"
        in result.evidence["targeted"]["problems"]
    )
    assert not any(
        urlsplit(p).path == "/Basic" and "name" in parse_qs(urlsplit(p).query)
        for p in store.calls[before:]
    )


@pytest.mark.parametrize("ledger_mode", ["original", "none", "alternate"])
def test_resealed_caller_clock_data_cannot_replace_issued_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: Any, ledger_mode: str
) -> None:
    cfg, params, write, ledger = issue(tmp_path, monkeypatch, server)
    state = write.recheck
    post = state.payload["post_state"]["targeted"]
    clock = state._targeted_read_clock
    post["read_clock"]["completed_ns"] -= 1
    # The caller can hash its own modified copy using the visible run salt;
    # it still cannot replace the original canonical issued-state commitment.
    state._targeted_read_clock = replace(
        clock,
        completed_ns=clock.completed_ns - 1,
        post_mac=targeted._digest(
            "read_clock", post, bytes.fromhex(state.payload["salt"])
        ),
    )
    store, _ = server
    before = list(store.calls)
    selected = (
        ledger
        if ledger_mode == "original"
        else api.Ledger(api.MemoryLedger())
        if ledger_mode == "alternate"
        else None
    )
    result = api.recheck(cfg, write, params=params, actor=ACTOR, ledger=selected)
    assert result.outcome == "UNCERTAIN" and not result.verified and not result.final
    assert store.calls == before
    assert not list((tmp_path / "authority").rglob("*.inflight"))


def test_saved_state_does_not_restore_clock_even_in_live_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: Any
) -> None:
    cfg, params, write, ledger = issue(tmp_path, monkeypatch, server)
    state = write.recheck
    loaded = api.RecheckState.load(
        state.save(tmp_path / "saved-state.json"),
        params=params,
        target_ids=state.target_ids,
    )
    assert loaded._targeted_read_clock is None
    monkeypatch.setattr(api, "seconds_until", lambda due: 0)
    result = api.recheck(cfg, loaded, params=params, actor=ACTOR, ledger=ledger)
    assert result.outcome == "UNCERTAIN" and not result.verified and not result.final


@pytest.mark.parametrize("ledger_mode", ["original", "none", "alternate"])
def test_changed_declared_config_is_not_adopted_from_current_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: Any, ledger_mode: str
) -> None:
    cfg, params, write, ledger = issue(tmp_path, monkeypatch, server)
    store, base = server
    changed = configured(base, 0.005)
    before = list(store.calls)
    selected = (
        ledger
        if ledger_mode == "original"
        else api.Ledger(api.MemoryLedger())
        if ledger_mode == "alternate"
        else None
    )
    result = api.recheck(changed, write, params=params, actor=ACTOR, ledger=selected)
    assert result.outcome == "UNCERTAIN" and not result.verified
    assert store.calls == before
    assert not list((tmp_path / "authority").rglob("*.inflight"))


def test_explicit_bound_and_real_due_wait_allow_fresh_clean_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: Any
) -> None:
    cfg, params, write, ledger = issue(tmp_path, monkeypatch, server)
    assert write.evidence["frame"]["targeted"]["cache_policy"] == {
        "bound_s": 0.0,
        "source": "operator_declared_adapter_config",
        "W_s": 0.01,
    }
    result = api.recheck(
        cfg, write, params=params, actor=ACTOR, ledger=ledger, wait=True
    )
    assert result.outcome == "CONFIRMED" and result.verified and result.final
    assert result.evidence["targeted"]["phase"] == "late"


@pytest.mark.parametrize("ledger_mode", ["original", "none", "alternate"])
def test_initial_unknown_stays_overall_unknown_after_fresh_scoped_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: Any, ledger_mode: str
) -> None:
    monkeypatch.setenv("OPENADAPT_VERIFIER_STATE_DIR", str(tmp_path / "authority"))
    store, base = server
    cfg = configured(base)
    ledger = api.Ledger(api.MemoryLedger())
    pending = api.begin_write(
        cfg, PARAMS, actor=ACTOR, ledger=ledger, workdir=tmp_path / "runs"
    )
    store.save()
    store.history_mode = "empty"
    write = api.verify_write(pending, write_attempted=True)
    assert write.outcome is api.Outcome.UNCERTAIN and not write.final
    store.history_mode = "normal"
    selected = (
        ledger
        if ledger_mode == "original"
        else api.Ledger(api.MemoryLedger())
        if ledger_mode == "alternate"
        else None
    )
    result = api.recheck(
        cfg, write, params=PARAMS, actor=ACTOR, ledger=selected, wait=True
    )
    assert result.evidence["targeted"]["status"] == "held"
    assert result.outcome == "UNCERTAIN" and not result.verified and not result.final
    assert ledger.latest(write.write_key).resolved["outcome"] == "UNCERTAIN"
    assert ledger.latest(write.write_key).recheck_owed == write.recheck_due_at


@pytest.mark.parametrize("ledger_mode", ["original", "none", "alternate"])
def test_terminal_refutation_cannot_be_released_by_fresh_clean_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: Any, ledger_mode: str
) -> None:
    cfg, params, write, ledger = issue(tmp_path, monkeypatch, server)
    store, _ = server
    store.save(name="wrong-private-value")
    selected = (
        ledger
        if ledger_mode == "original"
        else api.Ledger(api.MemoryLedger())
        if ledger_mode == "alternate"
        else None
    )
    first = api.recheck(
        cfg, write, params=params, actor=ACTOR, ledger=selected, wait=True
    )
    assert first.outcome == "REFUTED"
    store.save()
    second = api.recheck(
        cfg, write, params=params, actor=ACTOR, ledger=selected, wait=True
    )
    assert second.outcome == "REFUTED" and not second.verified


def test_trusted_later_user_remains_a_separate_write_after_fresh_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: Any
) -> None:
    cfg, params, write, ledger = issue(tmp_path, monkeypatch, server)
    store, _ = server
    store.save(name="legitimate-later-private-value", author="actor/colleague")
    result = api.recheck(
        cfg, write, params=params, actor=ACTOR, ledger=ledger, wait=True
    )
    assert result.outcome == "CONFIRMED" and result.final
    assert result.evidence["targeted"]["legitimate_later_user_edits"] == 1
