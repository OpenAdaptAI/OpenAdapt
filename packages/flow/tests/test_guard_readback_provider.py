"""The commit guard's read-back seam: :mod:`openadapt_flow.agent_guard.readback`.

The default provider is today's ``Guard.confirm`` (verify-run). Whatever a
provider says, only an independent read-back is VERIFIED, and a field the
read shows with another value is refuted. Synthetic MockMed, localhost.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from benchmark.harness.guard_demo import NOTE, FakeAgent, verify_yaml
from openadapt_flow.agent_guard.commit_guard import CommitGuard
from openadapt_flow.agent_guard.config import ConfigError, parse_config
from openadapt_flow.agent_guard.readback import (
    KIND_PREFIX,
    ReadbackResult,
    VerifyRunReadback,
    load_provider,
    rule,
)
from openadapt_flow.guard import GuardedSave, GuardTask, GuardVerdict
from openadapt_flow.mockmed.fault_server import serve as fault_serve
from openadapt_flow.verify_run import parse_verify_config
from tests.test_agent_guard import FakePage

OTHER = "Referral to cardiology for chest pain"


@pytest.fixture
def sor():
    url, db, stop = fault_serve()
    try:
        yield url.rstrip("/"), db
    finally:
        stop()


def _task() -> GuardTask:
    return GuardTask(
        task_id="task-readback-0001",
        expected_record={"patient_id": "p1"},
        intended_fields={"note": NOTE},
    )


class ScreenOnly:
    """A provider that read the saved record off a second screen session."""

    def __init__(self, note: str = NOTE) -> None:
        self.note = note

    def read(self, task: Any, guard: Any) -> ReadbackResult:
        return ReadbackResult(
            tier="screen_readback",
            fields={"note": self.note},
            verdict=GuardVerdict.VERIFIED,
        )


def _guard(sor, tmp_path: Path, page: FakePage, **kw: Any) -> CommitGuard:
    guarded = GuardedSave(
        parse_verify_config(verify_yaml(sor[0])),
        tmp_path / "state",
        approval="not_gated",
    )
    guard = CommitGuard(
        guarded,
        _task(),
        page,
        app_origin=sor[0],
        events_path=tmp_path / "events.jsonl",
        **kw,
    )
    page.guard = guard
    return guard


def test_the_default_provider_is_verify_run_unchanged(sor, tmp_path: Path) -> None:
    page = FakePage(FakeAgent(sor[0]), "p1")
    guard = _guard(sor, tmp_path, page)
    assert isinstance(guard.readback, VerifyRunReadback)
    page.save()
    guard.poll()
    assert guard.summary.verdict == "VERIFIED"
    assert guard.result is not None and guard.result.receipt_path
    ledger = json.loads(
        (tmp_path / "state" / "tasks" / "task-readback-0001.json").read_text()
    )
    assert ledger["verdict"] == "VERIFIED"


def test_a_screen_only_provider_never_yields_verified(sor, tmp_path: Path) -> None:
    page = FakePage(FakeAgent(sor[0]), "p1")
    guard = _guard(sor, tmp_path, page, readback=ScreenOnly())
    page.save()
    guard.poll()
    assert guard.summary.verdict == "RECONCILIATION_REQUIRED"
    # The task's own ledger says so too: the next attempt goes to a person.
    ledger = json.loads(
        (tmp_path / "state" / "tasks" / "task-readback-0001.json").read_text()
    )
    assert ledger["verdict"] == "RECONCILIATION_REQUIRED"
    row = json.loads((tmp_path / "guard_ledger_row.json").read_text())
    assert row["readback_tier"] == "screen_readback"
    assert row["verdict"] == "ESCALATED"


@pytest.mark.parametrize("tier", ["screen_readback", "unchecked", "anything"])
def test_the_tier_rule(tier: str) -> None:
    result = ReadbackResult(tier=tier, fields={}, verdict=GuardVerdict.VERIFIED)
    assert rule(_task(), result) is GuardVerdict.RECONCILIATION_REQUIRED
    independent = ReadbackResult(
        tier="independent_readback",
        fields={"note": NOTE},
        verdict=GuardVerdict.VERIFIED,
    )
    assert rule(_task(), independent) is GuardVerdict.VERIFIED


def test_a_differing_field_is_refuted_whatever_the_provider_says() -> None:
    result = ReadbackResult(
        tier="independent_readback",
        fields={"note": OTHER},
        verdict=GuardVerdict.VERIFIED,
    )
    assert rule(_task(), result) is GuardVerdict.REFUTED
    # A field the read didn't reach is not a refutation (and not a match).
    unread = ReadbackResult(
        tier="independent_readback",
        fields={"note": None},
        verdict=GuardVerdict.VERIFIED,
    )
    assert rule(_task(), unread) is GuardVerdict.VERIFIED


def test_a_provider_that_fails_is_reconciliation_required(sor, tmp_path: Path) -> None:
    class Broken:
        def read(self, task: Any, guard: Any) -> ReadbackResult:
            raise TimeoutError("api down")

    page = FakePage(FakeAgent(sor[0]), "p1")
    guard = _guard(sor, tmp_path, page, readback=Broken())
    page.save()
    guard.poll()
    assert guard.summary.verdict == "RECONCILIATION_REQUIRED"
    assert guard.result is not None
    assert guard.result.reason == "readback_provider_failed:TimeoutError"


def test_a_provider_that_fails_after_verify_run_keeps_the_tier_rule(
    sor, tmp_path: Path
) -> None:
    class ConfirmsThenFails:
        def read(self, task: Any, guard: Any) -> ReadbackResult:
            guard.confirm()
            raise RuntimeError("the extra read broke")

    page = FakePage(FakeAgent(sor[0]), "p1")
    guard = _guard(sor, tmp_path, page, readback=ConfirmsThenFails())
    page.save()
    guard.poll()
    # verify-run's own independent read stands; nothing upgrades it.
    assert guard.summary.verdict == "VERIFIED"
    row = json.loads((tmp_path / "guard_ledger_row.json").read_text())
    assert row["readback_tier"] == "independent_readback"
    assert row["fields_checked"] == []


def test_a_planted_wrong_value_is_caught_by_the_default_read(
    sor, tmp_path: Path
) -> None:
    class WrongNote(FakePage):
        def continue_request(self, request_id: str) -> None:
            self.sent.append(request_id)
            held = self.pending.pop(request_id, None)
            if held is not None and held.method == "POST":
                self.agent.click_save(self.patient, OTHER)
                assert self.guard is not None
                self.guard.on_request_done(held.network_id)

    page = WrongNote(FakeAgent(sor[0]), "p1")
    guard = _guard(sor, tmp_path, page)
    page.save()
    guard.poll()
    assert guard.summary.verdict == "REFUTED"
    assert guard.result is not None and guard.result.reason == "readback:fields_differ"


def test_a_provider_is_named_through_the_verifier_factory_registry(
    tmp_path: Path,
) -> None:
    from openadapt_flow.runtime.effects.adapter import (
        _REGISTRY,
        register_verifier_factory,
    )

    built: list[Any] = []

    def factory(config: Any, params: Any) -> ScreenOnly:
        built.append((dict(config), dict(params)))
        return ScreenOnly()

    register_verifier_factory(KIND_PREFIX + "test_second_session", factory)
    try:
        provider = load_provider(
            {"kind": "test_second_session", "base_url": "http://127.0.0.1:1"},
            {"patient_id": "p1"},
        )
        assert isinstance(provider, ScreenOnly)
        assert built[0][0]["base_url"] == "http://127.0.0.1:1"
        assert isinstance(load_provider(None, {}), VerifyRunReadback)
        assert isinstance(load_provider("verify_run", {}), VerifyRunReadback)
        with pytest.raises(ValueError, match="no readback provider"):
            load_provider("not_registered_anywhere", {})
    finally:
        _REGISTRY.pop(KIND_PREFIX + "test_second_session", None)

    verify = tmp_path / "verify.yaml"
    verify.write_text("x: 1\n")
    base = {
        "schema_version": "openadapt.agent-guard/v1",
        "app_url": "http://127.0.0.1:8765/",
        "verify": str(verify),
    }
    cfg = parse_config({**base, "readback_provider": "api"}, base=tmp_path)
    assert cfg.readback_provider == "api" and cfg.approval_fields is True
    off = parse_config({**base, "approval_fields": False}, base=tmp_path)
    assert off.approval_fields is False and off.readback_provider is None
    with pytest.raises(ConfigError, match="readback_provider"):
        parse_config({**base, "readback_provider": {"base_url": "x"}}, base=tmp_path)
