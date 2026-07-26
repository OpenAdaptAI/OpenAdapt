"""Tests for capability-aware surface detection, its report, and enforcement.

The autouse ``all_surfaces_capable`` fixture (conftest) stubs
``capabilities.detect_capability`` so other engine tests stay hermetic; this
module binds the REAL implementation at import time (before fixtures run) and
exercises the probes with every dependency monkeypatched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from engine import capabilities
from engine.capabilities import (
    CAPABILITY_SCHEMA,
    SURFACES,
    CapabilityError,
    SurfaceCapability,
    ensure_backend_capability,
    refusal_message,
)
from engine.config import EngineConfig
from engine.dispatch import EngineDispatcher, EngineServices

# Bound before any fixture patches the module attribute.
_REAL_DETECT = capabilities.detect_capability


def _specs(monkeypatch, present: set[str]) -> None:
    monkeypatch.setattr(capabilities, "_find_spec", lambda name: name in present)


def _versions(monkeypatch, versions: dict[str, str]) -> None:
    monkeypatch.setattr(capabilities, "_dist_version", lambda name: versions.get(name))


def _platform(monkeypatch, value: str) -> None:
    monkeypatch.setattr("engine.capabilities.sys.platform", value)


class TestWebSurface:
    def test_missing_playwright_is_driver_required(self, monkeypatch) -> None:
        _specs(monkeypatch, set())
        cap = capabilities._check_web()
        assert cap.state == "driver_required"
        assert "playwright install chromium" in (cap.remediation or "")
        assert cap.blocking is True

    def test_missing_chromium_is_driver_required_but_non_blocking(self, monkeypatch) -> None:
        _specs(monkeypatch, {"playwright"})
        _versions(monkeypatch, {"playwright": "1.50.0"})
        monkeypatch.setattr(capabilities, "_chromium_installed", lambda: False)
        cap = capabilities._check_web()
        assert cap.state == "driver_required"
        assert cap.blocking is False
        assert "python -m playwright install chromium" in (cap.remediation or "")

    def test_playwright_and_chromium_available(self, monkeypatch) -> None:
        _specs(monkeypatch, {"playwright"})
        _versions(monkeypatch, {"playwright": "1.50.0"})
        monkeypatch.setattr(capabilities, "_chromium_installed", lambda: True)
        cap = capabilities._check_web()
        assert cap.state == "available"
        assert cap.remediation is None
        assert cap.to_dict()["driver"] == {"name": "playwright", "version": "1.50.0"}

    def test_chromium_probe_respects_browsers_path_env(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
        assert capabilities._chromium_installed() is False
        (tmp_path / "chromium-1234").mkdir()
        assert capabilities._chromium_installed() is True


class TestWindowsSurface:
    def test_non_windows_host_uses_in_guest_agent(self, monkeypatch) -> None:
        _platform(monkeypatch, "darwin")
        _specs(monkeypatch, {"requests"})
        _versions(monkeypatch, {"requests": "2.32.0"})
        cap = capabilities._check_windows()
        assert cap.state == "available"
        assert "in-guest WAA agent" in cap.detail
        assert "agent" in cap.detail and "URL" in cap.detail

    def test_non_windows_host_without_requests(self, monkeypatch) -> None:
        _platform(monkeypatch, "linux")
        _specs(monkeypatch, set())
        cap = capabilities._check_windows()
        assert cap.state == "driver_required"
        assert "pip install 'openadapt-flow[windows]'" in (cap.remediation or "")

    def test_windows_host_checks_uiautomation(self, monkeypatch) -> None:
        _platform(monkeypatch, "win32")
        _specs(monkeypatch, set())
        cap = capabilities._check_windows()
        assert cap.state == "driver_required"
        assert "pip install uiautomation" in (cap.remediation or "")

    def test_windows_host_with_uiautomation_available(self, monkeypatch) -> None:
        _platform(monkeypatch, "win32")
        _specs(monkeypatch, {"uiautomation"})
        _versions(monkeypatch, {"uiautomation": "2.0.20"})
        cap = capabilities._check_windows()
        assert cap.state == "available"


class TestMacosSurface:
    def test_non_darwin_host_is_unsupported_with_supported_path(self, monkeypatch) -> None:
        _platform(monkeypatch, "linux")
        cap = capabilities._check_macos()
        assert cap.state == "unsupported_host"
        assert "cannot exist" in cap.detail
        assert "RDP surface" in (cap.remediation or "")

    def test_missing_pyobjc_is_driver_required(self, monkeypatch) -> None:
        _platform(monkeypatch, "darwin")
        _specs(monkeypatch, set())
        cap = capabilities._check_macos()
        assert cap.state == "driver_required"
        assert "pip install 'openadapt-flow[macos]'" in (cap.remediation or "")

    def test_denied_accessibility_is_permission_required(self, monkeypatch) -> None:
        _platform(monkeypatch, "darwin")
        _specs(monkeypatch, {"ApplicationServices"})
        _versions(monkeypatch, {"pyobjc-framework-applicationservices": "10.3"})
        monkeypatch.setattr(capabilities, "_mac_accessibility_trusted", lambda: False)
        cap = capabilities._check_macos()
        assert cap.state == "permission_required"
        assert "Privacy & Security > Accessibility" in (cap.remediation or "")

    def test_denied_screen_recording_is_permission_required(self, monkeypatch) -> None:
        _platform(monkeypatch, "darwin")
        _specs(monkeypatch, {"ApplicationServices"})
        _versions(monkeypatch, {})
        monkeypatch.setattr(capabilities, "_mac_accessibility_trusted", lambda: True)
        monkeypatch.setattr(capabilities, "_mac_screen_recording_granted", lambda: False)
        cap = capabilities._check_macos()
        assert cap.state == "permission_required"
        assert "Screen & System Audio" in (cap.remediation or "")

    def test_all_grants_present_is_available(self, monkeypatch) -> None:
        _platform(monkeypatch, "darwin")
        _specs(monkeypatch, {"ApplicationServices"})
        _versions(monkeypatch, {"pyobjc-framework-applicationservices": "10.3"})
        monkeypatch.setattr(capabilities, "_mac_accessibility_trusted", lambda: True)
        monkeypatch.setattr(capabilities, "_mac_screen_recording_granted", lambda: True)
        cap = capabilities._check_macos()
        assert cap.state == "available"


class TestLinuxSurface:
    def test_non_linux_host_is_unsupported(self, monkeypatch) -> None:
        _platform(monkeypatch, "darwin")
        cap = capabilities._check_linux()
        assert cap.state == "unsupported_host"
        assert "RDP surface" in (cap.remediation or "")

    def test_missing_pygobject_is_driver_required(self, monkeypatch) -> None:
        _platform(monkeypatch, "linux")
        _specs(monkeypatch, set())
        cap = capabilities._check_linux()
        assert cap.state == "driver_required"
        assert "pip install 'openadapt-flow[linux]'" in (cap.remediation or "")
        assert "gir1.2-atspi-2.0" in (cap.remediation or "")

    def test_missing_atspi_typelib_names_system_packages(self, monkeypatch) -> None:
        _platform(monkeypatch, "linux")
        _specs(monkeypatch, {"gi"})
        _versions(monkeypatch, {"PyGObject": "3.48"})
        monkeypatch.setattr(capabilities, "_atspi_typelib_available", lambda: False)
        cap = capabilities._check_linux()
        assert cap.state == "driver_required"
        assert "at-spi2-core" in (cap.remediation or "")

    def test_atspi_present_is_available(self, monkeypatch) -> None:
        _platform(monkeypatch, "linux")
        _specs(monkeypatch, {"gi"})
        _versions(monkeypatch, {"PyGObject": "3.48"})
        monkeypatch.setattr(capabilities, "_atspi_typelib_available", lambda: True)
        cap = capabilities._check_linux()
        assert cap.state == "available"


class TestRdpSurface:
    def test_darwin_without_quartz_is_driver_required(self, monkeypatch) -> None:
        _platform(monkeypatch, "darwin")
        _specs(monkeypatch, set())
        _versions(monkeypatch, {})
        cap = capabilities._check_rdp()
        assert cap.state == "driver_required"
        assert "pip install 'openadapt-flow[macos]'" in (cap.remediation or "")

    def test_darwin_without_screen_recording_is_permission_required(self, monkeypatch) -> None:
        _platform(monkeypatch, "darwin")
        _specs(monkeypatch, {"Quartz"})
        _versions(monkeypatch, {})
        monkeypatch.setattr(capabilities, "_mac_screen_recording_granted", lambda: False)
        cap = capabilities._check_rdp()
        assert cap.state == "permission_required"
        assert "Screen & System Audio" in (cap.remediation or "")

    def test_darwin_ready_notes_aardwolf_for_network_path(self, monkeypatch) -> None:
        _platform(monkeypatch, "darwin")
        _specs(monkeypatch, {"Quartz"})
        _versions(monkeypatch, {})
        monkeypatch.setattr(capabilities, "_mac_screen_recording_granted", lambda: True)
        cap = capabilities._check_rdp()
        assert cap.state == "available"
        assert "pip install 'openadapt-flow[rdp]'" in cap.detail

    def test_linux_needs_aardwolf_for_network_rdp(self, monkeypatch) -> None:
        _platform(monkeypatch, "linux")
        _specs(monkeypatch, set())
        _versions(monkeypatch, {})
        cap = capabilities._check_rdp()
        assert cap.state == "driver_required"
        assert cap.to_dict()["driver"] == {"name": "aardwolf", "version": None}
        assert "pip install 'openadapt-flow[rdp]'" in (cap.remediation or "")

    def test_linux_with_aardwolf_is_available(self, monkeypatch) -> None:
        _platform(monkeypatch, "linux")
        _specs(monkeypatch, {"aardwolf"})
        _versions(monkeypatch, {"aardwolf": "0.2.14"})
        cap = capabilities._check_rdp()
        assert cap.state == "available"


class TestCitrixSurface:
    def test_linux_host_is_unsupported(self, monkeypatch) -> None:
        _platform(monkeypatch, "linux")
        cap = capabilities._check_citrix()
        assert cap.state == "unsupported_host"
        assert "macOS or Windows host" in (cap.remediation or "")

    def test_darwin_without_workspace_app_is_driver_required(self, monkeypatch) -> None:
        _platform(monkeypatch, "darwin")
        _specs(monkeypatch, {"Quartz"})
        monkeypatch.setattr(capabilities, "_citrix_workspace_installed", lambda: (False, None))
        cap = capabilities._check_citrix()
        assert cap.state == "driver_required"
        assert "Citrix Workspace app" in (cap.remediation or "")
        assert "Citrix Viewer" in (cap.remediation or "")

    def test_darwin_with_workspace_app_is_available(self, monkeypatch) -> None:
        _platform(monkeypatch, "darwin")
        _specs(monkeypatch, {"Quartz"})
        monkeypatch.setattr(
            capabilities, "_citrix_workspace_installed", lambda: (True, "24.5.0")
        )
        monkeypatch.setattr(capabilities, "_mac_screen_recording_granted", lambda: True)
        cap = capabilities._check_citrix()
        assert cap.state == "available"
        assert cap.to_dict()["driver"] == {"name": "Citrix Workspace", "version": "24.5.0"}


class TestDetectionNeverRaises:
    def test_probe_crash_degrades_to_explained_state(self, monkeypatch) -> None:
        def boom() -> SurfaceCapability:
            raise RuntimeError("probe exploded")

        monkeypatch.setitem(capabilities._CHECKS, "web", boom)
        cap = _REAL_DETECT("web")
        assert cap.state == "driver_required"
        assert "Capability detection failed" in cap.detail

    def test_unknown_surface_is_reported_not_raised(self) -> None:
        cap = _REAL_DETECT("solaris")
        assert cap.state == "unsupported_host"


class TestCapabilityReport:
    def test_schema_shape(self) -> None:
        report = capabilities.capability_report()
        assert report["schema"] == CAPABILITY_SCHEMA == "openadapt-desktop.capability-report/v1"
        assert report["generated_at"]
        assert set(report["host"]) == {"os", "os_version", "arch", "app_version"}
        assert set(report["surfaces"]) == set(SURFACES)
        for entry in report["surfaces"].values():
            assert set(entry) == {"state", "detail", "remediation", "driver"}
            assert entry["state"] in (
                "available",
                "driver_required",
                "permission_required",
                "unsupported_host",
            )

    def test_report_is_json_serializable(self) -> None:
        json.dumps(capabilities.capability_report())


class TestEnforcement:
    def test_refusal_message_wording(self) -> None:
        cap = SurfaceCapability(
            surface="citrix",
            state="driver_required",
            detail="No Citrix Workspace client app was found on this host.",
            remediation="Install the Citrix Workspace app from citrix.com.",
            requirement="the Citrix Workspace app",
        )
        assert refusal_message("record", cap) == (
            "record refused: Citrix needs the Citrix Workspace app. "
            "Install the Citrix Workspace app from citrix.com."
        )

    def test_blocking_gap_refuses(self, monkeypatch) -> None:
        cap = SurfaceCapability(
            surface="macos",
            state="permission_required",
            detail="denied",
            remediation="Open System Settings > Privacy & Security > Accessibility.",
            requirement="the macOS Accessibility permission",
        )
        monkeypatch.setattr(capabilities, "detect_capability", lambda surface: cap)
        with pytest.raises(CapabilityError, match="record refused: macOS"):
            ensure_backend_capability("macos", action="record")

    def test_non_blocking_gap_passes_through(self, monkeypatch) -> None:
        cap = SurfaceCapability(
            surface="web",
            state="driver_required",
            detail="no chromium",
            remediation="python -m playwright install chromium",
            requirement="the Playwright Chromium browser build",
            blocking=False,
        )
        monkeypatch.setattr(capabilities, "detect_capability", lambda surface: cap)
        assert ensure_backend_capability("web", action="record") is cap

    def test_available_passes(self, monkeypatch) -> None:
        cap = SurfaceCapability(surface="web", state="available", detail="ready")
        monkeypatch.setattr(capabilities, "detect_capability", lambda surface: cap)
        assert ensure_backend_capability("web").state == "available"


class _NullAudit:
    def log(self, *a, **k):
        pass


class _IdleController:
    """Minimal controller stand-in; capability refusals happen before use."""

    state = None
    is_recording = False


@pytest.fixture
def dispatcher(tmp_path: Path):
    config = EngineConfig(data_dir=tmp_path / ".openadapt", log_level="WARNING")
    config.data_dir.mkdir(parents=True, exist_ok=True)
    from engine.db import IndexDB

    db = IndexDB(tmp_path / "index.db")
    db.initialize()
    events: list[tuple[str, dict]] = []
    services = EngineServices(
        config,
        db=db,
        audit=_NullAudit(),
        storage=object(),
        controller=_IdleController(),
    )
    disp = EngineDispatcher(config, services=services, emit=lambda e, d: events.append((e, d)))
    yield disp, db, events
    db.close()


def _refused(surface: str) -> SurfaceCapability:
    return SurfaceCapability(
        surface=surface,
        state="permission_required",
        detail="denied for test",
        remediation="Open System Settings > Privacy & Security > Screen Recording.",
        requirement="the macOS Screen Recording permission",
    )


class TestDispatchIntegration:
    def test_get_capabilities_command_returns_report(self, dispatcher) -> None:
        disp, _db, _events = dispatcher
        assert "get_capabilities" in disp.commands
        report = disp.dispatch("get_capabilities", {})
        assert report["schema"] == CAPABILITY_SCHEMA
        assert set(report["surfaces"]) == set(SURFACES)

    def test_replay_refuses_fast_with_remediation(self, dispatcher, tmp_path, monkeypatch) -> None:
        disp, db, _events = dispatcher
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        db.insert_bundle("bnd1", str(bundle), capture_id="cap1")
        monkeypatch.setattr(capabilities, "detect_capability", _refused)

        result = disp.dispatch(
            "replay_workflow",
            {"workflow_id": "bnd1", "target": {"backend": "citrix"}},
        )
        assert result["pre_action_refusal"] is True
        assert result["outcome"] == "refused"
        assert result["error"] == (
            "replay refused: Citrix needs the macOS Screen Recording permission. "
            "Open System Settings > Privacy & Security > Screen Recording."
        )

    def test_governed_run_refusal_uses_run_verb(self, dispatcher, tmp_path, monkeypatch) -> None:
        disp, db, _events = dispatcher
        bundle = tmp_path / "bundle"
        bundle.mkdir()
        db.insert_bundle("bnd1", str(bundle), capture_id="cap1")
        monkeypatch.setattr(capabilities, "detect_capability", _refused)

        result = disp.dispatch(
            "run_workflow",
            {"workflow_id": "bnd1", "target": {"backend": "web"}},
        )
        assert result["pre_action_refusal"] is True
        assert result["error"].startswith("run refused: web (browser) needs ")

    def test_record_refuses_fast_and_emits_error(self, dispatcher, monkeypatch) -> None:
        disp, _db, events = dispatcher
        monkeypatch.setattr("engine.dispatch.sys.platform", "linux")
        monkeypatch.setattr(capabilities, "detect_capability", _refused)

        with pytest.raises(ValueError, match="record refused: linux \\(AT-SPI\\) needs"):
            disp.dispatch(
                "start_recording",
                {
                    "target": {
                        "backend": "linux",
                        "linux_app": "gedit",
                        "linux_window_title": "Untitled",
                    }
                },
            )
        assert any(
            e == "recording_error" and d["error"].startswith("record refused: ")
            for e, d in events
        )


class TestRecordHelperRefusal:
    def test_private_record_helper_refuses_before_flow(
        self, tmp_path, monkeypatch, capsys
    ) -> None:
        from engine import flow_record_entry

        monkeypatch.setattr(capabilities, "detect_capability", _refused)
        request = tmp_path / "request.yaml"
        stop_path = tmp_path / "stop"
        ready_path = tmp_path / "ready"
        request.write_text(
            yaml.safe_dump(
                {
                    "target": {"backend": "citrix"},
                    "out_dir": str(tmp_path / "out"),
                    "task": "",
                    "stop_path": str(stop_path),
                    "ready_path": str(ready_path),
                }
            ),
            encoding="utf-8",
        )

        code = flow_record_entry.main([str(request)])

        assert code == 2
        err = capsys.readouterr().err
        assert "record refused: Citrix needs the macOS Screen Recording permission." in err
        assert not ready_path.exists()
        assert not request.exists()


class TestCliCapabilities:
    def test_json_output_matches_schema(self, tmp_path, monkeypatch, capsys) -> None:
        from unittest.mock import patch as mock_patch

        from engine.cli import main as cli_main

        config = EngineConfig(data_dir=tmp_path / ".openadapt", log_level="WARNING")
        with mock_patch("engine.cli.EngineConfig", return_value=config):
            cli_main(["capabilities", "--json"])
        report = json.loads(capsys.readouterr().out)
        assert report["schema"] == CAPABILITY_SCHEMA
        assert set(report["host"]) == {"os", "os_version", "arch", "app_version"}
        assert set(report["surfaces"]) == set(SURFACES)
        for entry in report["surfaces"].values():
            assert set(entry) == {"state", "detail", "remediation", "driver"}

    def test_human_output_lists_every_surface(self, tmp_path, monkeypatch, capsys) -> None:
        from unittest.mock import patch as mock_patch

        from engine.cli import main as cli_main

        config = EngineConfig(data_dir=tmp_path / ".openadapt", log_level="WARNING")
        with mock_patch("engine.cli.EngineConfig", return_value=config):
            cli_main(["capabilities"])
        out = capsys.readouterr().out
        for surface in SURFACES:
            assert surface in out
