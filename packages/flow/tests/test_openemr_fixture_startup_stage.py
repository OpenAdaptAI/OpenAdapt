"""Offline startup-boundary controls; no Docker or app process is launched."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from benchmark.openemr_local.fixture import FixtureError, OpenEMRFixture

STAGES = (
    "runtime_preflight",
    "image_pull",
    "image_identity",
    "compose_start",
    "health_ready",
    "source_verify",
)


def intercept_startup(monkeypatch, fixture, *, fail_at=None, failure=None):
    seen = []

    def observe(stage):
        assert fixture.startup_stage == stage
        seen.append(stage)
        if stage == fail_at:
            raise failure

    for name, stage in (
        ("runtime_preflight", "runtime_preflight"),
        ("image_identity", "image_identity"),
        ("_wait_ready", "health_ready"),
        ("_verify_running_source", "source_verify"),
    ):
        monkeypatch.setattr(fixture, name, lambda stage=stage: observe(stage))

    def compose(*args, **kwargs):
        assert kwargs == {"timeout_s": 600.0}
        if args == ("pull",):
            observe("image_pull")
        else:
            assert args == ("up", "-d")
            observe("compose_start")

    monkeypatch.setattr(fixture, "_compose", compose)
    return seen


def test_stage_is_read_only_and_success_requires_all_original_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = Mock(side_effect=AssertionError("unexpected subprocess"))
    fixture = OpenEMRFixture(tmp_path / "fresh", runner=runner)
    assert fixture.startup_stage == "not_started"
    with pytest.raises(AttributeError):
        fixture.startup_stage = "ready"
    seen = intercept_startup(monkeypatch, fixture)
    assert fixture.up() is None
    assert seen == list(STAGES)
    assert fixture.startup_stage == "ready"
    runner.assert_not_called()
    assert not fixture.state_dir.exists()


@pytest.mark.parametrize("stage", STAGES)
def test_failure_retains_exact_stage_exception_and_original_call_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    runner = Mock(side_effect=AssertionError("unexpected subprocess"))
    fixture = OpenEMRFixture(tmp_path / "fresh", runner=runner)
    failure = FixtureError("synthetic error must not enter stage token")
    seen = intercept_startup(monkeypatch, fixture, fail_at=stage, failure=failure)
    with pytest.raises(FixtureError) as caught:
        fixture.up()
    assert caught.value is failure
    assert fixture.startup_stage == stage
    assert seen == list(STAGES[: STAGES.index(stage) + 1])
    runner.assert_not_called()
    assert not fixture.state_dir.exists()


def test_interruption_is_not_caught_or_promoted_to_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = OpenEMRFixture(tmp_path / "fresh")
    failure = KeyboardInterrupt()
    seen = intercept_startup(
        monkeypatch, fixture, fail_at="health_ready", failure=failure
    )
    with pytest.raises(KeyboardInterrupt) as caught:
        fixture.up()
    assert caught.value is failure
    assert fixture.startup_stage == "health_ready"
    assert seen == list(STAGES[:5])
