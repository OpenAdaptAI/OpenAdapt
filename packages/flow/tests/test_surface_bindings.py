"""Binding backends to backend surfaces at replay time (docs/MULTI_SURFACE.md).

Unit tests for ``--surface NAME=KIND[:TARGET]`` and the config ``surfaces:``
map, plus one CLI end to end: a bundle that switches from the app's tab to a
second web app replays through ``openadapt-flow replay --surface``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openadapt_flow.deployment import BackendConfig, DeploymentConfig
from openadapt_flow.ir import ActionKind, Step, SurfaceSpec, Workflow
from openadapt_flow.surface_bindings import (
    SurfaceBindingError,
    build_surface_backends,
    parse_surface_flag,
    resolve_surface_configs,
    unbound_backend_surfaces,
)

_POPUP = SurfaceSpec(
    id="popup_1",
    kind="web",
    opened_by_step="step_000",
    opener="main",
    open_order=0,
    opener_link="window_opener",
    origin="http://127.0.0.1:8000",
    url_pattern=r"http://127\.0\.0\.1:8000/popup\.html(?:[?#].*)?",
)


def _workflow() -> Workflow:
    return Workflow(
        name="wf",
        surfaces=[
            SurfaceSpec(id="main", kind="web"),
            _POPUP,
            SurfaceSpec(id="windows_1", kind="windows"),
            SurfaceSpec(id="citrix_1", kind="citrix"),
        ],
        steps=[
            Step(
                id="step_000",
                intent="open",
                action=ActionKind.WAIT,
                opens=["popup_1"],
            )
        ],
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("windows_1=windows", ("windows_1", "windows", None)),
        (
            "windows_1=windows:http://10.0.0.5:5001",
            ("windows_1", "windows", "http://10.0.0.5:5001"),
        ),
        ("windows_1=http://10.0.0.5:5001", ("windows_1", None, "http://10.0.0.5:5001")),
        ("citrix_1=citrix", ("citrix_1", "citrix", None)),
        (
            "app_2=web:https://portal.example.test/",
            ("app_2", "web", "https://portal.example.test/"),
        ),
    ],
)
def test_parse_surface_flag(text, expected) -> None:
    assert parse_surface_flag(text) == expected


@pytest.mark.parametrize("text", ["windows_1", "=windows", "windows_1="])
def test_parse_surface_flag_refuses_malformed(text) -> None:
    with pytest.raises(SurfaceBindingError):
        parse_surface_flag(text)


def test_kind_defaults_from_the_bundle_and_flag_overrides_config() -> None:
    configured = {
        "windows_1": BackendConfig.model_validate(
            {"agent_url": "http://config:5001", "agent_token": "t"}
        )
    }
    resolved = resolve_surface_configs(
        _workflow(), ["windows_1=http://flag:5001", "citrix_1=citrix"], configured
    )
    assert resolved["windows_1"].kind == "windows"
    assert resolved["windows_1"].agent_url == "http://flag:5001"
    assert resolved["windows_1"].agent_token == "t"  # kept from the config
    assert resolved["citrix_1"].kind == "citrix"
    assert unbound_backend_surfaces(_workflow(), resolved) == []
    assert unbound_backend_surfaces(_workflow(), {}) == ["windows_1", "citrix_1"]


@pytest.mark.parametrize(
    ("flag", "message"),
    [
        ("nope=windows", "not declared"),
        ("main=web", "primary surface"),
        ("popup_1=web", "needs no binding"),
        ("windows_1=rdp:10.0.0.5", "declared windows"),
    ],
)
def test_resolve_refuses_a_binding_the_bundle_cannot_use(flag, message) -> None:
    with pytest.raises(SurfaceBindingError, match=message):
        resolve_surface_configs(_workflow(), [flag])


def test_config_surfaces_map_round_trips_and_is_omitted_when_empty() -> None:
    assert "surfaces" not in DeploymentConfig().model_dump()
    cfg = DeploymentConfig.model_validate(
        {"surfaces": {"windows_1": {"agent_url": "http://x:5001"}}}
    )
    resolved = resolve_surface_configs(_workflow(), [], cfg.surfaces)
    assert resolved["windows_1"].kind == "windows"


def test_build_uses_the_factory_and_opens_web_surfaces_in_the_browser() -> None:
    calls: list = []

    def fake_build(cfg, page=None):
        calls.append((cfg.kind, page))
        return object()

    pages: list[str] = []
    backends = build_surface_backends(
        {
            "windows_1": BackendConfig(kind="windows", agent_url="http://x:1"),
            "app_2": BackendConfig(kind="web", url="http://y/"),
        },
        new_page=lambda url: pages.append(url) or f"page:{url}",
        build=fake_build,
    )
    assert set(backends) == {"windows_1", "app_2"}
    assert calls == [("windows", None), ("web", "page:http://y/")]
    with pytest.raises(SurfaceBindingError, match="needs a browser"):
        build_surface_backends(
            {"app_2": BackendConfig(kind="web", url="http://y/")}, build=fake_build
        )


# -- CLI end to end ------------------------------------------------------------

pw = pytest.importorskip("playwright.sync_api")

import tests.test_multi_surface_e2e as _e2e  # noqa: E402
from openadapt_flow.__main__ import main as cli_main  # noqa: E402
from openadapt_flow.backends.playwright_backend import PlaywrightBackend  # noqa: E402
from openadapt_flow.compiler import compile_recording  # noqa: E402
from openadapt_flow.recorder import Recorder  # noqa: E402

_center = _e2e._center
_fresh_page = _e2e._fresh_page
#: The e2e module's local fixture app (served on a free loopback port).
app_url = _e2e.app_url


@pytest.fixture(scope="module")
def two_app_bundle(app_url: str, tmp_path_factory) -> Path:
    """Main app tab, then a separately opened second app (a backend surface).

    Uses its own Playwright session, closed before the CLI tests start theirs
    (the sync API cannot nest).
    """

    from openadapt_flow._browser_setup import ensure_chromium_installed

    ensure_chromium_installed()
    root = tmp_path_factory.mktemp("surface_cli")
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            _record_two_apps(browser, app_url, root)
        finally:
            browser.close()
    compile_recording(root / "recording", root / "bundle", name="two-apps")
    return root / "bundle"


def _record_two_apps(browser, app_url: str, root: Path) -> None:
    main_page = _fresh_page(browser, app_url)
    other_page = _fresh_page(browser, f"{app_url}/report.html")
    try:
        recorder = Recorder(
            PlaywrightBackend(main_page),
            root / "recording",
            app_url=app_url,
            settle_interval_s=0.05,
            settle_timeout_s=2.0,
        )
        recorder.switch_surface("app_2", backend=PlaywrightBackend(other_page))
        recorder.click(*_center(other_page, "#reviewed"))
        recorder.switch_surface("main")
        recorder.click(*_center(main_page, "#finish"))
        recorder.finish()
    finally:
        main_page.context.close()
        other_page.context.close()


def _report(run_dir: Path) -> dict:
    return json.loads((run_dir / "report.json").read_text())


def test_cli_replay_binds_a_web_backend_surface(
    two_app_bundle: Path, app_url: str, tmp_path
) -> None:
    workflow = Workflow.load(two_app_bundle)
    assert [(s.id, s.kind, s.is_page) for s in workflow.surfaces] == [
        ("main", "web", False),
        ("app_2", "web", False),
    ]
    run_dir = tmp_path / "bound"
    code = cli_main(
        [
            "replay",
            str(two_app_bundle),
            "--url",
            app_url,
            "--surface",
            f"app_2={app_url}/report.html",
            "--run-dir",
            str(run_dir),
        ]
    )
    report = _report(run_dir)
    assert code == 0, [(r["step_id"], r.get("error")) for r in report["results"]]
    assert report["model_calls"] == 0
    assert [r.get("surface") for r in report["results"]] == [
        "app_2",
        "app_2",
        "main",
        "main",
    ]


def test_cli_replay_without_a_binding_halts_at_the_switch(
    two_app_bundle: Path, app_url: str, tmp_path
) -> None:
    run_dir = tmp_path / "unbound"
    code = cli_main(
        ["replay", str(two_app_bundle), "--url", app_url, "--run-dir", str(run_dir)]
    )
    assert code == 1
    last = _report(run_dir)["results"][-1]
    assert last["step_id"] == "step_000"
    assert "--surface app_2=" in last["error"]


def test_cli_refuses_an_undeclared_surface_name(two_app_bundle: Path, app_url) -> None:
    with pytest.raises(SystemExit, match="not declared"):
        cli_main(
            [
                "replay",
                str(two_app_bundle),
                "--url",
                app_url,
                "--surface",
                "nope=web:http://127.0.0.1:1/",
            ]
        )
