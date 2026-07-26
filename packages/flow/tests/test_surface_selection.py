"""Surface-selection neutrality + surface-bound workflows (roadmap Section 5).

Covers: the production-profile explicit-target refusal, the visible demo
default notice, the Demo-only last-used CLI convenience, the workflow surface
binding (compile stamp, serialization compatibility, cross-surface refusal,
and the report-recorded override), all with the heavy browser/vision stack
faked as in ``test_cli_new_commands``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openadapt_flow.__main__ import main
from openadapt_flow.compiler.compile import _surface_binding_from_meta
from openadapt_flow.ir import BackendHints, Workflow
from openadapt_flow.surface_selection import (
    demo_default_notice,
    execution_mode_for_surface,
    load_last_surface,
    store_last_surface,
)

# ---------------------------------------------------------------------------
# fakes (mirrors test_cli_new_commands)
# ---------------------------------------------------------------------------


class _FakePage:
    video = None

    def goto(self, url):
        self.url = url


class _FakeBrowser:
    def new_page(self, viewport=None):
        return _FakePage()

    def close(self):
        pass


class _FakeChromium:
    def launch(self, headless=True):
        return _FakeBrowser()


class _FakePlaywright:
    chromium = _FakeChromium()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeReport:
    success = True
    screenshots_may_leave_box = False


def _install_fake_browser(monkeypatch, captured: dict) -> None:
    class _FakeReplayer:
        def __init__(self, backend, **kwargs):
            captured["ctor"] = kwargs

        def run(self, workflow, **kwargs):
            captured["run"] = kwargs
            return _FakeReport()

    import playwright.sync_api as psa

    import openadapt_flow._browser_setup as bs
    import openadapt_flow.backends.playwright_backend as pwb
    import openadapt_flow.report as report_mod
    import openadapt_flow.runtime as runtime_mod
    import openadapt_flow.runtime.grounder as grounder_mod
    import openadapt_flow.runtime.remote_vlm as remote_mod

    monkeypatch.setattr(psa, "sync_playwright", lambda: _FakePlaywright())
    monkeypatch.setattr(pwb, "PlaywrightBackend", lambda page: "backend")
    monkeypatch.setattr(bs, "ensure_chromium_installed", lambda: None)
    monkeypatch.setattr(grounder_mod, "build_grounder", lambda fallback=None: None)
    monkeypatch.setattr(remote_mod, "appliance_from_env", lambda: None)
    monkeypatch.setattr(report_mod, "render_run_report", lambda run_dir: "REPORT.md")
    monkeypatch.setattr(runtime_mod, "Replayer", _FakeReplayer)


@pytest.fixture()
def cli_state(tmp_path: Path, monkeypatch) -> Path:
    state = tmp_path / "state" / "flow_cli.json"
    monkeypatch.setenv("OPENADAPT_FLOW_CLI_STATE", str(state))
    return state


# ---------------------------------------------------------------------------
# execution mode + last-used state file
# ---------------------------------------------------------------------------


def test_execution_mode_per_surface() -> None:
    assert execution_mode_for_surface("rdp") == "external"
    assert execution_mode_for_surface("citrix") == "external"
    for surface in ("web", "windows", "macos", "linux"):
        assert execution_mode_for_surface(surface) == "in_session"


def test_last_surface_roundtrip(cli_state: Path) -> None:
    assert load_last_surface() is None
    store_last_surface("windows")
    assert load_last_surface() == "windows"
    assert json.loads(cli_state.read_text())["last_backend"] == "windows"


def test_last_surface_invalid_values_ignored(cli_state: Path) -> None:
    cli_state.parent.mkdir(parents=True, exist_ok=True)
    cli_state.write_text('{"last_backend": "mainframe"}')
    assert load_last_surface() is None
    cli_state.write_text("not json")
    assert load_last_surface() is None


# ---------------------------------------------------------------------------
# record: explicit surface in production, visible default in demo
# ---------------------------------------------------------------------------


def test_record_production_profile_requires_explicit_backend(
    tmp_path: Path, capsys, cli_state: Path
) -> None:
    for profile in ("standard", "regulated"):
        rc = main(["record", "--out", str(tmp_path / "rec"), "--profile", profile])
        assert rc == 2
        out = capsys.readouterr().out
        assert f"record REFUSED: the {profile} profile requires an explicit" in out
        assert "web (browser), windows, macos, linux, rdp, citrix" in out
        assert "Nothing was recorded." in out


def test_record_demo_defaults_to_browser_with_notice(
    tmp_path: Path, capsys, cli_state: Path
) -> None:
    # No last-used target: the demo default is the browser, said out loud.
    # (The web recorder then fails fast on the missing --url; the notice must
    # already have been printed.)
    with pytest.raises(SystemExit):
        main(["record", "--out", str(tmp_path / "rec"), "--profile", "demo"])
    out = capsys.readouterr().out
    assert "NOTE: defaulting to browser (demo convenience)." in out


def test_record_demo_uses_last_used_target(
    tmp_path: Path, capsys, cli_state: Path, monkeypatch
) -> None:
    store_last_surface("macos")
    recorded: dict = {}

    def _fake_capture(out_dir, **kwargs):
        recorded["kwargs"] = kwargs
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "meta.json").write_text("{}")
        return out_dir

    import openadapt_flow.desktop_record as dr

    monkeypatch.setattr(dr, "record_desktop_capture", _fake_capture)
    rc = main(["record", "--out", str(tmp_path / "rec"), "--profile", "demo"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "defaulting to backend 'macos' (demo convenience" in out
    # The recording is stamped with the exact surface for compile-time binding.
    assert json.loads((tmp_path / "rec" / "meta.json").read_text())["surface"] == (
        "macos"
    )


def test_record_demo_persists_explicit_backend(
    tmp_path: Path, cli_state: Path, monkeypatch
) -> None:
    def _fake_capture(out_dir, **kwargs):
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "meta.json").write_text("{}")
        return out_dir

    import openadapt_flow.desktop_record as dr

    monkeypatch.setattr(dr, "record_desktop_capture", _fake_capture)
    rc = main(
        [
            "record",
            "--out",
            str(tmp_path / "rec"),
            "--profile",
            "demo",
            "--backend",
            "windows",
        ]
    )
    assert rc == 0
    assert load_last_surface() == "windows"


def test_record_without_profile_keeps_permissive_default_with_notice(
    tmp_path: Path, capsys, cli_state: Path
) -> None:
    store_last_surface("windows")  # must NOT apply outside --profile demo
    with pytest.raises(SystemExit, match="--url"):
        main(["record", "--out", str(tmp_path / "rec")])
    out = capsys.readouterr().out
    assert "NOTE: defaulting to browser (demo convenience)." in out


# ---------------------------------------------------------------------------
# compile: the surface stamp binds the bundle
# ---------------------------------------------------------------------------


def test_surface_binding_from_meta() -> None:
    assert _surface_binding_from_meta({}, None) == (None, None)
    assert _surface_binding_from_meta({"surface": "web"}, None) == (
        "web",
        "in_session",
    )
    hints = BackendHints(backend="citrix")
    # Legacy rdp/citrix recordings are bound through their backend_hints.
    assert _surface_binding_from_meta({}, hints) == ("citrix", "external")
    assert _surface_binding_from_meta({"surface": "citrix"}, hints) == (
        "citrix",
        "external",
    )
    with pytest.raises(ValueError, match="must be one of"):
        _surface_binding_from_meta({"surface": "mainframe"}, None)
    with pytest.raises(ValueError, match="contradicts"):
        _surface_binding_from_meta({"surface": "web"}, hints)


def test_workflow_surface_serialization_is_additive(tmp_path: Path) -> None:
    unbound = Workflow(name="legacy", steps=[])
    data = unbound.model_dump()
    assert "surface" not in data
    assert "execution_mode" not in data

    bound = Workflow(
        name="bound", steps=[], surface="windows", execution_mode="in_session"
    )
    bundle = tmp_path / "bundle"
    bound.save(bundle)
    loaded = Workflow.load(bundle)
    assert loaded.surface == "windows"
    assert loaded.execution_mode == "in_session"


# ---------------------------------------------------------------------------
# replay/run: surface binding is enforced; override records itself
# ---------------------------------------------------------------------------


def _bound_bundle(tmp_path: Path, surface: str) -> Path:
    wf = Workflow(
        name="bound",
        steps=[],
        surface=surface,  # type: ignore[arg-type]
        execution_mode=execution_mode_for_surface(surface),  # type: ignore[arg-type]
    )
    bundle = tmp_path / f"bundle-{surface}"
    wf.save(bundle)
    return bundle


def test_replay_refuses_cross_surface_run(
    tmp_path: Path, capsys, cli_state: Path, monkeypatch
) -> None:
    bundle = _bound_bundle(tmp_path, "windows")
    captured: dict = {}
    _install_fake_browser(monkeypatch, captured)
    rc = main(
        [
            "replay",
            str(bundle),
            "--backend",
            "web",
            "--url",
            "http://app.example",
            "--run-dir",
            str(tmp_path / "run"),
        ]
    )
    assert rc == 2
    out = capsys.readouterr().out
    assert "replay REFUSED: this workflow is bound to surface 'windows'" in out
    assert "execution mode 'in_session'" in out
    assert "--allow-surface-override" in out
    assert "run" not in captured  # nothing executed


def test_replay_override_is_recorded_in_report(
    tmp_path: Path, capsys, cli_state: Path, monkeypatch
) -> None:
    bundle = _bound_bundle(tmp_path, "windows")
    captured: dict = {}
    _install_fake_browser(monkeypatch, captured)
    rc = main(
        [
            "replay",
            str(bundle),
            "--backend",
            "web",
            "--url",
            "http://app.example",
            "--allow-surface-override",
            "--run-dir",
            str(tmp_path / "run"),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "NOTE: surface override in effect" in out
    assert captured["run"]["surface_override"] is True
    assert captured["run"]["execution_target_kind"] == "web"


def test_bound_surface_is_the_replay_default(
    tmp_path: Path, cli_state: Path, monkeypatch
) -> None:
    # A web-bound bundle replays on web with no --backend flag and NO demo
    # default notice (the binding is an explicit selection).
    bundle = _bound_bundle(tmp_path, "web")
    captured: dict = {}
    _install_fake_browser(monkeypatch, captured)
    rc = main(
        [
            "replay",
            str(bundle),
            "--url",
            "http://app.example",
            "--run-dir",
            str(tmp_path / "run"),
        ]
    )
    assert rc == 0
    assert captured["run"]["execution_target_kind"] == "web"
    assert captured["run"]["surface_override"] is False


def test_bound_desktop_surface_routes_off_browser(
    tmp_path: Path, cli_state: Path, monkeypatch
) -> None:
    # A windows-bound bundle must NOT silently fall back to the browser: with
    # no --backend it resolves to the bound surface and fails loudly on the
    # missing windows target config instead of driving the wrong substrate.
    bundle = _bound_bundle(tmp_path, "windows")
    captured: dict = {}
    _install_fake_browser(monkeypatch, captured)
    with pytest.raises((SystemExit, ValueError), match="agent_url"):
        main(
            [
                "replay",
                str(bundle),
                "--run-dir",
                str(tmp_path / "run"),
            ]
        )
    assert "run" not in captured


def test_replay_unbound_bundle_prints_demo_default_notice(
    tmp_path: Path, capsys, cli_state: Path, monkeypatch
) -> None:
    wf = Workflow(name="legacy", steps=[])
    bundle = tmp_path / "bundle"
    wf.save(bundle)
    captured: dict = {}
    _install_fake_browser(monkeypatch, captured)
    rc = main(
        [
            "replay",
            str(bundle),
            "--url",
            "http://app.example",
            "--run-dir",
            str(tmp_path / "run"),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "NOTE: defaulting to browser (demo convenience)." in out


def test_replay_explicit_backend_has_no_default_notice(
    tmp_path: Path, capsys, cli_state: Path, monkeypatch
) -> None:
    wf = Workflow(name="legacy", steps=[])
    bundle = tmp_path / "bundle"
    wf.save(bundle)
    captured: dict = {}
    _install_fake_browser(monkeypatch, captured)
    rc = main(
        [
            "replay",
            str(bundle),
            "--backend",
            "web",
            "--url",
            "http://app.example",
            "--run-dir",
            str(tmp_path / "run"),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "demo convenience" not in out


def test_run_production_profile_requires_explicit_surface(
    tmp_path: Path, capsys, cli_state: Path, monkeypatch
) -> None:
    key = "surface-gate-key"
    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", key)
    wf = Workflow(name="legacy", steps=[])
    bundle = tmp_path / "bundle"
    wf.save(bundle, encrypt=True, key=key)
    rc = main(
        [
            "run",
            str(bundle),
            "--profile",
            "standard",
            "--run-dir",
            str(tmp_path / "run"),
        ]
    )
    assert rc == 2
    out = capsys.readouterr().out
    assert "run REFUSED: the standard profile requires an explicit" in out
    assert "no implicit browser default in production" in out
    assert "Nothing was executed." in out


def test_notice_texts_are_stable() -> None:
    assert demo_default_notice("web", from_last_used=False).startswith(
        "NOTE: defaulting to browser (demo convenience)."
    )
    assert demo_default_notice("windows", from_last_used=True).startswith(
        "NOTE: defaulting to backend 'windows' (demo convenience"
    )
