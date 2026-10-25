"""The golden-run harness records faithfully and hides only volatile values."""

from __future__ import annotations

from pathlib import Path

import pytest

from openadapt_flow.backend import (
    GuardedCoordinateActionBackend,
    RemoteActuationBackend,
    RichPointerActionBackend,
)
from tests.golden_runs.harness import (
    artifact_hashes,
    normalize,
    tracing_backend,
)


class _PointerOnly:
    viewport = (1280, 800)

    def screenshot(self) -> bytes:
        return b"png-bytes"

    def click(self, x: int, y: int) -> None:
        self.clicked = (x, y)

    def right_click(self, x: int, y: int) -> None: ...

    def drag(self, x0: int, y0: int, x1: int, y1: int) -> None: ...

    def hover(self, x: int, y: int) -> None: ...


def test_proxy_has_exactly_the_capabilities_of_the_backend() -> None:
    inner = _PointerOnly()
    proxy = tracing_backend(inner, [])
    for protocol in (
        RichPointerActionBackend,
        GuardedCoordinateActionBackend,
        RemoteActuationBackend,
    ):
        assert isinstance(proxy, protocol) == isinstance(inner, protocol), protocol


def test_proxy_records_calls_in_order_and_forwards_results() -> None:
    trace: list[dict] = []
    inner = _PointerOnly()
    proxy = tracing_backend(inner, trace)

    assert proxy.screenshot() == b"png-bytes"
    proxy.click(10, y=20)

    assert inner.clicked == (10, 20)  # state is shared with the original
    assert isinstance(proxy, _PointerOnly)
    assert proxy.viewport == (1280, 800)
    # Observations such as screenshots are not part of the action trace.
    assert trace == [{"call": "click", "args": [10], "kwargs": {"y": 20}}]


def test_proxy_records_a_raised_exception() -> None:
    class Failing:
        def click(self, x: int, y: int) -> None:
            raise TimeoutError("gone")

    trace: list[dict] = []
    with pytest.raises(TimeoutError):
        tracing_backend(Failing(), trace).click(1, 2)
    assert trace == [{"call": "click", "args": [1, 2], "raised": "TimeoutError"}]


def test_normalize_hides_times_ids_and_roots_but_keeps_outcomes() -> None:
    report = {
        "success": False,
        "started_at": "2026-10-24T10:00:00Z",
        "run_id": "abc",
        "steps": [
            {"elapsed_ms": 1234, "timeout_s": 5.0, "error": "halted in /tmp/x/run"}
        ],
        "confidence": 0.123456,
    }
    assert normalize(report, roots=["/tmp/x"]) == {
        "success": False,
        "started_at": "<started_at>",
        "run_id": "<run_id>",
        "steps": [
            {
                "elapsed_ms": "<elapsed_ms>",
                "timeout_s": 5.0,
                "error": "halted in <root0>/run",
            }
        ],
        "confidence": 0.123,
    }


def test_artifact_hashes_skip_named_files(tmp_path: Path) -> None:
    (tmp_path / "steps").mkdir()
    (tmp_path / "steps" / "a.png").write_bytes(b"a")
    (tmp_path / "report.json").write_text("{}")
    hashes = artifact_hashes(tmp_path, skip={"report.json"})
    assert list(hashes) == ["steps/a.png"]
