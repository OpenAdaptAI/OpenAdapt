"""The comparison runner bounds every item: a hung browser call becomes an error row."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BENCH = Path(__file__).resolve().parents[1] / "benchmark"
if str(BENCH) not in sys.path:
    sys.path.insert(0, str(BENCH))

compare = pytest.importorskip("lifecycle.arms.compare")
from lifecycle.arm import ItemResult  # noqa: E402


class _Session:
    closed = 0

    def close(self) -> None:
        _Session.closed += 1


class HangingArm:
    """Blocks on a child process named like a Playwright driver, as a hung browser call does."""

    name = "hanging"

    def __init__(self, options: dict) -> None:
        self.session = _Session()

    def describe(self) -> dict:
        return {}

    def setup(self, env, operator):  # noqa: ANN001
        return []

    def run_item(self, item, env, operator):  # noqa: ANN001
        proc = subprocess.Popen(
            ["bash", "-c", "exec -a playwright-driver-stand-in sleep 60"]
        )
        proc.wait()
        return ItemResult(
            item_id=item.item_id, claimed="done", product_status="SAVED", wall_s=0.0
        )

    def after_drift(self, event, env, operator):  # noqa: ANN001
        return []

    def close(self) -> None:
        pass


def factory(options: dict) -> HangingArm:
    return HangingArm(options)


def test_hung_item_is_an_error_and_the_browser_is_restarted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(compare, "ITEM_TIMEOUT_S", 1.0)
    monkeypatch.setattr(compare, "free_gib", lambda: 1000.0)
    arm = compare.Guarded({"_inner": f"{__name__}:factory"})
    item = SimpleNamespace(item_id="ref-00001")
    env = SimpleNamespace(workdir=tmp_path)
    result = arm.run_item(item, env, None)
    assert result.claimed == "error"
    assert "timed out" in result.product_status
    assert _Session.closed == 1
    assert "item timeout" in (tmp_path / "stacks.txt").read_text()
