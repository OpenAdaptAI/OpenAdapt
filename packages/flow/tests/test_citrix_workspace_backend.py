"""Unit contract for the Citrix Workspace-window pixel backend.

These run without Docker / a browser / pyobjc: they assert the preset owner
resolution, that the backend conforms to the base ``Backend`` protocol while
deliberately NOT implementing the structural / identity capabilities (the ICA
pixel floor), and that the factory builds it for ``backend.kind: citrix``.

The END-TO-END proof that the backend actually records->compiles->replays and
safe-halts over a real no-DOM surface is the fixture qualification in
``benchmark/citrix_workspace`` (driven through the ``WindowClient`` seam over the
Part-1 canvas). See that directory's README.
"""

from __future__ import annotations

import pytest

from openadapt_flow.backend import (
    Backend,
    IdentityBackend,
    StructuralActionBackend,
    StructuralBackend,
    SystemOfRecordBackend,
)
from openadapt_flow.backends.citrix_workspace import (
    CITRIX_WINDOW_OWNERS,
    CitrixWorkspaceBackend,
    default_citrix_owner,
)
from openadapt_flow.backends.factory import build_backend
from openadapt_flow.backends.remote_display import RemoteDisplayError
from openadapt_flow.deployment import BackendConfig


class _NoopWindowClient:
    """A do-nothing ``WindowClient`` sufficient to CONSTRUCT the backend (no
    capture/inject is exercised here)."""

    def input_trusted(self) -> bool:  # pragma: no cover - not called
        return True

    def find_windows(self, owner, title):  # pragma: no cover - not called
        return []


def test_default_owner_per_platform():
    assert default_citrix_owner("darwin") == "Citrix Viewer"
    assert default_citrix_owner("win32") == "wfica32"
    assert default_citrix_owner("linux") == "Citrix Viewer"
    # Every platform table is non-empty and its default is the first entry.
    for plat, owners in CITRIX_WINDOW_OWNERS.items():
        assert owners, plat


def test_backend_defaults_to_citrix_owner():
    be = CitrixWorkspaceBackend(_NoopWindowClient())
    assert be._owner_substr == default_citrix_owner()
    assert be._citrix_owner == default_citrix_owner()


def test_owner_and_title_overrides():
    be = CitrixWorkspaceBackend(
        _NoopWindowClient(),
        owner_substr="Citrix Viewer (2)",
        window_title="Claims App - ICA",
    )
    assert be._owner_substr == "Citrix Viewer (2)"
    assert be._title_substr == "Claims App - ICA"


def test_pixel_only_protocol_surface():
    """Base Backend yes; structural/identity/system-of-record NO (ICA floor)."""
    be = CitrixWorkspaceBackend(_NoopWindowClient())
    assert isinstance(be, Backend)
    assert not isinstance(be, StructuralBackend)
    assert not isinstance(be, IdentityBackend)
    assert not isinstance(be, StructuralActionBackend)
    assert not isinstance(be, SystemOfRecordBackend)


def test_readiness_text_builds_probe():
    be = CitrixWorkspaceBackend(_NoopWindowClient(), readiness_text="Patient")
    assert be._readiness_probe is not None


def test_factory_builds_citrix_backend_with_default_owner():
    be = build_backend(BackendConfig(kind="citrix"), window_client=_NoopWindowClient())
    assert isinstance(be, CitrixWorkspaceBackend)
    assert be._owner_substr == default_citrix_owner()


def test_factory_builds_citrix_backend_on_linux_with_injected_client(monkeypatch):
    monkeypatch.setattr(
        "openadapt_flow.backends.citrix_workspace.sys.platform", "linux"
    )
    be = build_backend(BackendConfig(kind="citrix"), window_client=_NoopWindowClient())
    assert isinstance(be, CitrixWorkspaceBackend)
    assert be._owner_substr == CITRIX_WINDOW_OWNERS["linux"][0]


def test_factory_refuses_default_citrix_client_on_linux(monkeypatch):
    monkeypatch.setattr(
        "openadapt_flow.backends.citrix_workspace.sys.platform", "linux"
    )
    with pytest.raises(
        RemoteDisplayError,
        match=r"Linux requires an injected WindowClient.*macOS.*Windows",
    ):
        build_backend(BackendConfig(kind="citrix"))


def test_factory_citrix_owner_and_title_override():
    be = build_backend(
        BackendConfig(
            kind="citrix",
            rdp_window="wfica32",
            rdp_window_title="Claims App - ICA",
        ),
        window_client=_NoopWindowClient(),
    )
    assert isinstance(be, CitrixWorkspaceBackend)
    assert be._owner_substr == "wfica32"
    assert be._title_substr == "Claims App - ICA"


def test_factory_refuses_network_rdp_host_for_citrix():
    with pytest.raises(ValueError, match="local Citrix Workspace window"):
        build_backend(
            BackendConfig(kind="citrix", rdp_host="192.0.2.10"),
            window_client=_NoopWindowClient(),
        )


def test_cli_exposes_citrix_record_replay_and_report_run():
    from openadapt_flow.__main__ import _resolve_backend_config, build_parser
    from openadapt_flow.deployment import DeploymentConfig

    parser = build_parser()
    record_args = parser.parse_args(
        ["record", "--backend", "citrix", "--out", "recording"]
    )
    replay_args = parser.parse_args(
        [
            "replay",
            "bundle",
            "--backend",
            "citrix",
            "--rdp-window",
            "wfica32",
            "--rdp-window-title",
            "Claims App - ICA",
            "--rdp-readiness-text",
            "Claims queue",
        ]
    )
    report_args = parser.parse_args(["report-run", "run", "--backend", "citrix"])

    assert record_args.backend == "citrix"
    resolved = _resolve_backend_config(replay_args, DeploymentConfig())
    assert resolved.kind == "citrix"
    assert resolved.rdp_window == "wfica32"
    assert resolved.rdp_window_title == "Claims App - ICA"
    assert resolved.rdp_readiness_text == "Claims queue"
    assert report_args.backend == "citrix"
