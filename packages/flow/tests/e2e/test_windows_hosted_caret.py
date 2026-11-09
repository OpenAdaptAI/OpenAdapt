"""Bounded owned-fixture caret observation, not a qualification campaign."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from tests.e2e.windows_caret_diagnostic import OwnedCaret, close_owned, collect, project


@pytest.mark.skipif(
    sys.platform != "win32" or os.environ.get("OAFLOW_WINDOWS_HOSTED_E2E") != "1",
    reason="opt-in owned Windows caret diagnostic",
)
@pytest.mark.timeout(180)
def test_owned_caret_frame_correlation(tmp_path: Path) -> None:
    from tests.e2e.test_windows_hosted_replay import _Host, _points

    root = tmp_path / "owned-caret"
    root.mkdir()
    host = _Host(root)
    backend = None
    app = agent = None
    summary = {
        "schema": "openadapt.test-owned-caret/v1",
        "status": "UNRUN",
        "exception_type": None,
        "database_unchanged": False,
        "capture_count": 0,
        "static_fixture_handle_checks": False,
        "process_creation_bound": False,
        "diagnostic": {"status": "UNKNOWN", "code": "not_measured", "captures": 0},
        "cleanup": {
            "app_started": False,
            "app_waited": False,
            "agent_started": False,
            "agent_waited": False,
            "http_closed": False,
        },
    }
    primary = None
    try:
        backend = host.start_agent()
        agent = host.agent
        summary["cleanup"]["agent_started"] = agent is not None
        host.launch_app()
        app = host.app
        summary["cleanup"]["app_started"] = app is not None
        _points(backend)  # Existing bounded readiness lookup, before calibration.
        before = host.rows()
        observer = OwnedCaret(app)
        samples = collect(backend.guarded_keyboard_frame, observer.observe)
        summary["capture_count"] = len(samples)
        summary["diagnostic"] = project(samples)
        handles_valid = all(
            s[k]["owner"] and s[k]["focus"]
            for s in samples
            for k in ("before", "after")
        )
        summary["static_fixture_handle_checks"] = handles_valid
        summary["process_creation_bound"] = observer.creation > 0
        unchanged = before == host.rows()
        summary["database_unchanged"] = unchanged
        assert len(samples) == 8
        assert handles_valid
        assert summary["process_creation_bound"]
        assert unchanged
        summary["status"] = "PASSED"
    except Exception as exc:  # noqa: BLE001 - fixed projection; no raw native error in artifact
        primary = exc
        summary["status"] = "FAILED"
        summary["exception_type"] = (
            type(exc).__name__
            if type(exc).__name__
            in {
                "AssertionError",
                "RuntimeError",
                "TimeoutError",
                "OSError",
                "ValueError",
            }
            else "OtherError"
        )
    finally:
        try:
            if backend is not None:
                backend._session.close()
                summary["cleanup"]["http_closed"] = True
        except Exception as exc:  # noqa: BLE001 - preserve first failure independently
            if primary is None:
                primary = exc
                summary["status"] = "FAILED"
                summary["exception_type"] = "CleanupError"
        # The host may have created a child before a setup call raised.
        # Capture those exact objects and attempt both, independent of failures.
        app = app if app is not None else host.app
        agent = agent if agent is not None else host.agent
        primary = close_owned({"app": app, "agent": agent}, summary["cleanup"], primary)
        if primary is not None and summary["status"] != "FAILED":
            summary["status"] = "FAILED"
            summary["exception_type"] = "CleanupError"
        if not all(summary["cleanup"].values()):
            summary["status"] = "FAILED"
            if primary is None:
                primary = RuntimeError("owned cleanup incomplete")
                summary["exception_type"] = "CleanupError"
        (root / "caret-evidence.json").write_text(
            json.dumps(summary, sort_keys=True) + "\n", encoding="utf-8"
        )
    if primary is not None:
        raise AssertionError(
            "owned caret measurement failed; see fixed safe summary"
        ) from None
    assert all(summary["cleanup"].values())
