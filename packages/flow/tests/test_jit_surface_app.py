"""openadapt_flow.jit on Windows, macOS, Linux, RDP and Citrix apps.

:class:`~openadapt_flow.jit.SurfaceApp` names the app the way a deployment
file's ``backend`` section does. These tests check that it is validated with
named refusals, that recording, canaries and runs open the backend the section
names, and that an admitted program runs only on the surface its canary ran
on. No browser and no model: runs are stubbed as in test_jit_lifecycle.py.
tests/test_jit_surface_pixels.py records through the real RDP and Citrix
backends.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from openadapt_flow.deployment import BackendConfig
from openadapt_flow.jit import (
    SURFACE_KINDS,
    AdmissionError,
    SurfaceApp,
    TaskSignature,
    WebApp,
    compile_agent,
)
from tests.test_jit_lifecycle import (  # noqa: F401 - _policy_passes is autouse
    REVIEWER,
    IndependentVerifier,
    _ci,
    _jit,
    _policy_passes,
    _report,
    _rerun,
    _review,
    _stub_runs,
)

SECTIONS: dict[str, dict[str, Any]] = {
    "windows": {"kind": "windows", "agent_url": "http://127.0.0.1:5001"},
    "macos": {
        "kind": "macos",
        "macos_app": "TextEdit",
        "macos_window_title": "Visit note for Jane Sample",
    },
    "linux": {
        "kind": "linux",
        "linux_app": "patient-notes",
        "linux_window_title": "Visit note for Jane Sample",
    },
    "rdp": {"kind": "rdp", "rdp_host": "10.0.0.5"},
    "citrix": {
        "kind": "citrix",
        "rdp_window": "Citrix Viewer",
        "rdp_window_title": "Visit note for Jane Sample",
        "rdp_readiness_text": "Patient Notes",
    },
}
ORIGINS = {
    "windows": "windows://127.0.0.1:5001",
    "macos": "macos://TextEdit",
    "linux": "linux://patient-notes",
    "rdp": "rdp://10.0.0.5",
    "citrix": "citrix://Citrix Viewer",
}


def test_every_non_web_backend_kind_is_a_surface_app() -> None:
    assert set(SURFACE_KINDS) == set(SECTIONS)


@pytest.mark.parametrize("kind", sorted(SECTIONS))
def test_a_section_names_its_surface_without_window_titles(kind: str) -> None:
    app = SurfaceApp(SECTIONS[kind])
    assert app.kind == kind
    assert app.url is None and app.entry_url is None
    assert app.origin == ORIGINS[kind]
    # A window title can carry a patient or document name; it is never part
    # of the origin a canary, an admission policy or a report records.
    assert "Jane" not in app.origin
    assert app.run_refusal() is None


def test_a_deployment_file_names_the_app(tmp_path: Path) -> None:
    deploy = tmp_path / "deploy.yaml"
    deploy.write_text("name: notes\nbackend:\n  kind: macos\n  macos_app: TextEdit\n")
    app = SurfaceApp.from_deployment(deploy)
    assert app.kind == "macos" and app.origin == "macos://TextEdit"
    assert isinstance(app.backend, BackendConfig)


@pytest.mark.parametrize(
    ("section", "reason"),
    [
        ({"kind": "web", "url": "https://emr.example/"}, "use WebApp"),
        ({"kind": "windows"}, "backend.agent_url"),
        ({"kind": "macos"}, "backend.macos_app"),
        ({"kind": "linux", "linux_app": "gedit"}, "linux_window_title"),
        ({"kind": "rdp"}, "exactly one of backend.rdp_host or backend.rdp_window"),
        (
            {"kind": "rdp", "rdp_host": "h", "rdp_window": "mstsc"},
            "exactly one of",
        ),
        ({"kind": "citrix", "rdp_host": "h"}, "local Workspace window"),
        ({"kind": "vnc"}, "unknown backend.kind"),
    ],
)
def test_a_section_without_its_target_is_refused(section: dict, reason: str) -> None:
    with pytest.raises(ValueError, match=reason):
        SurfaceApp(section)


def test_the_playwright_mcp_dialect_is_refused_off_the_browser(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="CSS selectors in a browser page"):
        compile_agent(
            task=TaskSignature("t", "x", {}),
            app=SurfaceApp(SECTIONS["citrix"]),
            verifier=IndependentVerifier(),
            policy="clinical-write",
            cache_dir=tmp_path,
            dialect="playwright_mcp",
        )


def test_open_builds_the_backend_the_section_names(monkeypatch) -> None:
    import openadapt_flow.backends.factory as factory

    built: list[BackendConfig] = []

    class Backend:
        closed = False

        def close(self) -> None:
            Backend.closed = True

    def build_backend(cfg: BackendConfig, **_: Any) -> Backend:
        built.append(cfg)
        return Backend()

    monkeypatch.setattr(factory, "build_backend", build_backend)
    app = SurfaceApp(SECTIONS["linux"])
    backend, close = app.open()
    assert isinstance(backend, Backend) and built == [app.backend]
    close()
    assert Backend.closed


def test_citrix_runs_need_a_readiness_marker_before_any_backend_opens(
    tmp_path: Path,
) -> None:
    opened: list = []
    section = dict(SECTIONS["citrix"], rdp_readiness_text=None)
    app = SurfaceApp(section, open_backend=opened.append)
    assert "rdp_readiness_text" in (app.run_refusal() or "")
    jit = _rerun(_jit(tmp_path), app=app)
    detail, report = jit._run_governed(
        None, "digest", {}, tmp_path / "run", attended_by=REVIEWER
    )
    assert report is None and "refused before any action" in (detail or "")
    assert opened == []


@pytest.mark.parametrize("kind", sorted(SECTIONS))
def test_an_admitted_surface_program_runs_only_on_that_surface(
    tmp_path: Path, monkeypatch, kind: str
) -> None:
    app = SurfaceApp(SECTIONS[kind])
    jit = _rerun(_jit(tmp_path), app=app)
    _review(jit)
    _stub_runs(jit, monkeypatch, _report())
    canary = jit.canary(principal_id=REVIEWER)
    assert canary.status == "VERIFIED"
    manifest = jit.admit_with_key(_ci(jit, monkeypatch))["manifest"]
    assert manifest["canary"]["target_origin"] == ORIGINS[kind]

    hit = jit.run()
    assert hit.status == "VERIFIED" and hit.cache == "hit" and not hit.agent_called

    for other in (
        WebApp(url="https://emr.example/"),
        SurfaceApp(SECTIONS["macos" if kind != "macos" else "linux"]),
    ):
        moved = _rerun(jit, app=other)
        calls = _stub_runs(moved, monkeypatch, _report())
        refused = moved.run()
        assert refused.status == "refused"
        assert f"the canary ran on {ORIGINS[kind]}" in (refused.detail or "")
        assert calls == []


def test_a_draft_still_needs_a_person_on_a_surface(tmp_path: Path) -> None:
    jit = _rerun(_jit(tmp_path), app=SurfaceApp(SECTIONS["windows"]))
    with pytest.raises(AdmissionError, match="review the draft"):
        jit.canary(principal_id="op:someone-else")
