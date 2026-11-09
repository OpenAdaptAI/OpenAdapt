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
    "macos": "macos://textedit",
    "linux": "linux://patient-notes",
    "rdp": "rdp://10.0.0.5",
    "citrix": "citrix://citrix viewer",
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
    if "Jane" in str(SECTIONS[kind]):
        assert "jane" not in app.origin
    assert app.run_refusal() is None


def test_a_deployment_file_names_the_app(tmp_path: Path) -> None:
    deploy = tmp_path / "deploy.yaml"
    deploy.write_text("name: notes\nbackend:\n  kind: macos\n  macos_app: TextEdit\n")
    app = SurfaceApp.from_deployment(deploy)
    assert app.kind == "macos" and app.origin == "macos://textedit"
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
        (
            {"kind": "windows", "agent_url": "https://u:secret@vm.example:5001/"},
            "must not carry credentials",
        ),
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


def test_the_same_kind_on_another_target_is_another_origin() -> None:
    a = SurfaceApp(SECTIONS["rdp"])
    b = SurfaceApp(dict(SECTIONS["rdp"], rdp_host="10.0.0.6"))
    c = SurfaceApp(dict(SECTIONS["rdp"], rdp_port=3390))
    assert len({a.origin, b.origin, c.origin}) == 3


def test_an_admission_policy_names_a_surface_with_its_kind() -> None:
    from types import SimpleNamespace

    from openadapt_flow.jit.authority import scope_problems

    def policy(*hosts: str) -> Any:
        return SimpleNamespace(
            allowed_hosts=hosts,
            allowed_actions=(),
            allowed_effects=(),
            flow_policies=(),
        )

    empty = SimpleNamespace(steps=[])
    rdp = SurfaceApp(SECTIONS["rdp"]).origin
    # A web host entry never covers the RDP desktop on the same address.
    assert scope_problems(policy("10.0.0.5"), empty, {}, rdp)
    assert scope_problems(policy("rdp://10.0.0.5"), empty, {}, rdp) == []
    # Nor does a surface entry cover the web app on that host.
    assert scope_problems(policy("rdp://10.0.0.5"), empty, {}, "http://10.0.0.5")
    assert scope_problems(policy("10.0.0.5"), empty, {}, "http://10.0.0.5") == []
    # The kind is part of the name: a Citrix window is not a macOS app.
    citrix = SurfaceApp(SECTIONS["citrix"]).origin
    assert scope_problems(policy("macos://citrix viewer"), empty, {}, citrix)


@pytest.mark.parametrize("kind", sorted(SECTIONS))
def test_a_canary_and_a_run_drive_the_surface_through_the_replayer(
    tmp_path: Path, monkeypatch, kind: str
) -> None:
    """Everything but the gate and the replay itself is the real path."""

    from types import SimpleNamespace

    import openadapt_flow.run_gate as run_gate
    import openadapt_flow.runtime as runtime

    opened: list = []
    closed: list = []
    runs: list[dict[str, Any]] = []

    class Backend:
        def close(self) -> None:
            closed.append(kind)

    def open_backend(cfg: BackendConfig) -> Backend:
        opened.append(cfg.kind)
        return Backend()

    class Replayer:
        def __init__(self, backend: Any, **kwargs: Any) -> None:
            assert isinstance(backend, Backend)
            self.kwargs = kwargs

        def run(self, workflow: Any, **kwargs: Any) -> Any:
            runs.append({**kwargs, "replayer": self.kwargs})
            return _report()

    monkeypatch.setattr(
        run_gate,
        "evaluate_run_gate",
        lambda *a, **k: SimpleNamespace(passed=True, render=lambda: ""),
    )
    monkeypatch.setattr(run_gate, "build_runtime_authorization", lambda *a, **k: "auth")
    monkeypatch.setattr(runtime, "Replayer", Replayer)

    app = SurfaceApp(SECTIONS[kind], open_backend=open_backend)
    jit = _rerun(_jit(tmp_path), app=app)
    _review(jit)
    assert jit.canary(principal_id=REVIEWER).status == "VERIFIED"
    jit.admit_with_key(_ci(jit, monkeypatch))
    assert jit.run().status == "VERIFIED"

    assert opened == closed == [kind, kind]
    for run in runs:
        assert run["execution_target_kind"] == kind
        assert run["execution_origin"] is None and run["execution_entry_url"] is None
        assert run["execution_target_origin"] == ORIGINS[kind]
        assert run["replayer"]["deployment_surface"] is None
    assert runs[0]["replayer"]["attended_draft_by"] == REVIEWER


def test_the_replayer_matches_an_admitted_surface_program_by_its_origin(
    tmp_path: Path, monkeypatch
) -> None:
    from openadapt_flow.ir import Workflow
    from openadapt_flow.jit.lifecycle import DraftNotAdmitted, ensure_runnable
    from openadapt_flow.runtime.replayer import Replayer
    from tests.test_replayer import FakeBackend

    app = SurfaceApp(SECTIONS["macos"])
    jit = _rerun(_jit(tmp_path), app=app)
    _review(jit)
    _stub_runs(jit, monkeypatch, _report())
    jit.canary(principal_id=REVIEWER)
    jit.admit_with_key(_ci(jit, monkeypatch))
    workflow = Workflow.load(jit.bundle_dir)

    backend = FakeBackend()
    with pytest.raises(DraftNotAdmitted, match="this run is on linux://patient-notes"):
        Replayer(
            backend, effect_verifier=IndependentVerifier(), draft_key_dir=jit.key_dir
        ).run(
            workflow,
            bundle_dir=jit.bundle_dir,
            run_dir=tmp_path / "other",
            execution_target_kind="linux",
            execution_target_origin=ORIGINS["linux"],
        )
    assert backend.actions == []
    ensure_runnable(
        jit.bundle_dir,
        workflow=workflow,
        key_dir=jit.key_dir,
        execution_origin=ORIGINS["macos"],
        effect_verifier=IndependentVerifier(),
        at_run=True,
    )


def test_the_cli_names_a_desktop_run_for_the_admission_match() -> None:
    from openadapt_flow.__main__ import _surface_target_origin_or_none

    assert _surface_target_origin_or_none(BackendConfig(kind="web")) is None
    assert (
        _surface_target_origin_or_none(BackendConfig(**SECTIONS["macos"]))
        == ORIGINS["macos"]
    )
    leaky = BackendConfig(kind="windows", agent_url="https://u:p@vm:5001")
    assert _surface_target_origin_or_none(leaky) is None


def test_a_teach_authorization_must_name_the_desktop_app(
    tmp_path: Path, monkeypatch
) -> None:
    from tests._teach_authorization_support import authority

    app = SurfaceApp({"kind": "macos", "macos_app": "Chart"}, record_reader=lambda: [])
    task = TaskSignature("taught.chart_note", "Add {note}", {"note": str})

    def jit(origins: list[str], folder: str) -> Any:
        return compile_agent(
            task=task,
            app=app,
            verifier=IndependentVerifier(),
            policy="clinical-write",
            cache_dir=tmp_path / folder,
            teach_authorization=authority(
                tmp_path / "keys",
                tmp_path / folder / "statement",
                task.task_id,
                monkeypatch,
                origins=origins,
                record={"patient_id": "p1"},
                fields={"note": "Recheck"},
                inputs={"note": "Recheck"},
            ),
        )

    other = jit(["macos://textedit"], "other")
    with pytest.raises(ValueError, match=r"does not name this app \(macos://chart\)"):
        other._teach_scope({"note": "Recheck"})
    web = jit(["http://127.0.0.1:8765"], "web")
    with pytest.raises(ValueError, match="does not name this app"):
        web._teach_scope({"note": "Recheck"})
    scope = jit(["macos://chart"], "named")._teach_scope({"note": "Recheck"})
    assert scope is not None and scope.origins == ("macos://chart",)


def test_the_identity_band_is_marked_from_the_recorded_screen(
    tmp_path: Path, monkeypatch
) -> None:
    import json

    import openadapt_flow.vision as vision
    from openadapt_flow.jit.compile_agent import mark_identity_band

    class Line:
        def __init__(self, text: str, region: tuple[int, int, int, int]) -> None:
            self.text, self.region = text, region

    lines = [
        Line("MRN RC79284 Okafor, Philip", (52, 123, 600, 37)),
        Line("born 1966-02-17", (660, 123, 170, 37)),
        Line("New note", (127, 247, 121, 30)),
    ]
    monkeypatch.setattr(vision, "ocr", lambda png: lines)
    rec = tmp_path / "recording"
    (rec / "frames").mkdir(parents=True)
    (rec / "frames" / "0000_before.png").write_bytes(b"png")
    (rec / "events.jsonl").write_text(json.dumps({"i": 0, "kind": "click"}) + "\n")
    (rec / "meta.json").write_text(json.dumps({"surface": "macos"}))

    band = mark_identity_band(rec, ["MRN RC79284 Okafor, Philip born 1966-02-17"])
    assert band == [46, 117, 790, 49]
    assert json.loads((rec / "meta.json").read_text())["identifier_region"] == band

    # Not on the screen: nothing is marked, so the run gate refuses the clicks.
    (rec / "meta.json").write_text(json.dumps({"surface": "macos"}))
    assert mark_identity_band(rec, ["MRN P9 Someone Else"]) is None
    assert "identifier_region" not in json.loads((rec / "meta.json").read_text())


TAUGHT_KINDS = ("macos", "windows", "rdp", "citrix", "linux")


def _taught(
    tmp_path: Path, monkeypatch, kind: str, folder: str, origins: list[str], scope=None
) -> Any:
    from tests._teach_authorization_support import authority

    app = SurfaceApp(SECTIONS[kind], record_reader=lambda: [])
    task = TaskSignature("taught.chart_note", "Add {note}", {"note": str})
    return compile_agent(
        task=task,
        app=app,
        verifier=IndependentVerifier(),
        policy="clinical-write",
        cache_dir=tmp_path / folder,
        teach_scope=scope,
        teach_authorization=authority(
            tmp_path / "keys",
            tmp_path / folder / "statement",
            task.task_id,
            monkeypatch,
            origins=origins,
            record={"patient_id": "p1"},
            fields={"note": "Recheck"},
            inputs={"note": "Recheck"},
        ),
    )


def _caller_scope(origins: tuple[str, ...]) -> Any:
    from openadapt_flow.teach_scope import TeachScope

    return TeachScope.for_task(
        "taught.chart_note",
        origins=origins,
        record={"patient_id": "p1"},
        inputs={"note": "Recheck"},
        fields={"note": "Recheck"},
    )


@pytest.mark.parametrize("kind", TAUGHT_KINDS)
def test_a_narrowed_signed_scope_always_names_the_taught_app(
    tmp_path: Path, monkeypatch, kind: str
) -> None:
    """The scope a teach signs contains the app taught, or teaching refuses."""

    target = ORIGINS[kind]
    other = "macos://elsewhere"
    signed = [target, other]

    # A caller scope that names no app is bound to the app taught.
    jit = _taught(tmp_path, monkeypatch, kind, "empty", signed, _caller_scope(()))
    scope = jit._teach_scope({"note": "Recheck"})
    assert scope is not None and scope.origins == (target,)
    assert scope.authorization is not None

    # The signed scope alone is bound to the app taught, not its other apps.
    scope = _taught(tmp_path, monkeypatch, kind, "signed", signed)._teach_scope(
        {"note": "Recheck"}
    )
    assert scope is not None and scope.origins == (target,)

    # A narrowing that drops the app taught is refused, nothing returned.
    narrowed_away = _taught(
        tmp_path, monkeypatch, kind, "away", signed, _caller_scope((other,))
    )
    with pytest.raises(ValueError, match=r"teach scope does not name this app"):
        narrowed_away._teach_scope({"note": "Recheck"})

    # A signature for another app only is refused.
    unsigned_app = _taught(tmp_path, monkeypatch, kind, "unsigned", [other])
    with pytest.raises(ValueError, match=r"authorization does not name this app"):
        unsigned_app._teach_scope({"note": "Recheck"})


def test_a_common_name_without_its_identifier_marks_no_identity_band(
    tmp_path: Path, monkeypatch
) -> None:
    """A name alone identifies no record: the band needs every declared id raw."""

    import json

    import openadapt_flow.vision as vision
    from openadapt_flow.jit.compile_agent import mark_identity_band

    class Line:
        def __init__(self, text: str, region: tuple[int, int, int, int]) -> None:
            self.text, self.region = text, region

    screens: dict[str, list[Line]] = {}
    monkeypatch.setattr(vision, "ocr", lambda png: screens["now"])
    rec = tmp_path / "recording"
    (rec / "frames").mkdir(parents=True)
    (rec / "frames" / "0000_before.png").write_bytes(b"png")
    (rec / "events.jsonl").write_text(json.dumps({"i": 0, "kind": "click"}) + "\n")
    declared = ["MRN RC79284 Okafor, Philip born 1966-02-17"]

    def band(lines: list[Line], on_screen: list[str] = declared) -> Any:
        (rec / "meta.json").write_text(json.dumps({"surface": "macos"}))
        screens["now"] = lines
        out = mark_identity_band(rec, on_screen)
        stamped = json.loads((rec / "meta.json").read_text())
        assert ("identifier_region" in stamped) == (out is not None)
        return out

    # The common name is on screen; the declared MRN is not.
    assert band([Line("Okafor, Philip", (52, 123, 300, 37))]) is None
    assert band([Line("MRN Okafor, Philip born", (52, 123, 600, 37))]) is None
    # Another record's MRN beside the same name.
    assert band([Line("MRN RC11111 Okafor, Philip", (52, 123, 600, 37))]) is None
    # The MRN only inside a longer token is not a raw match.
    assert band([Line("MRN RC792840 Okafor, Philip", (52, 123, 600, 37))]) is None
    # One declared id present, the other missing.
    assert band([Line("MRN RC79284 Okafor, Philip", (52, 123, 600, 37))]) is None
    # A declaration with no identifier at all: a name never counts.
    assert (
        band([Line("Okafor, Philip", (52, 123, 300, 37))], ["Okafor, Philip"]) is None
    )
    # The full identity is still marked.
    assert band(
        [
            Line("MRN RC79284 Okafor, Philip", (52, 123, 600, 37)),
            Line("born 1966-02-17", (660, 123, 170, 37)),
            Line("Okafor, Philip", (52, 400, 300, 37)),
        ]
    ) == [46, 117, 790, 49]


def test_a_linux_app_whose_name_is_escaped_is_still_taught_on_it(
    tmp_path: Path, monkeypatch
) -> None:
    """An escaped app name (``%2F``) normalizes to the origin a scope names."""

    from openadapt_flow.teach_scope import scope_origin
    from tests._teach_authorization_support import authority

    app = SurfaceApp(
        {"kind": "linux", "linux_app": "org/chart", "linux_window_title": "Chart"},
        record_reader=lambda: [],
    )
    target = scope_origin(app.origin)
    assert target == "linux://org%2fchart"
    task = TaskSignature("taught.chart_note", "Add {note}", {"note": str})
    jit = compile_agent(
        task=task,
        app=app,
        verifier=IndependentVerifier(),
        policy="clinical-write",
        cache_dir=tmp_path / "escaped",
        teach_authorization=authority(
            tmp_path / "keys",
            tmp_path / "escaped" / "statement",
            task.task_id,
            monkeypatch,
            origins=[target],
            record={"patient_id": "p1"},
            fields={"note": "Recheck"},
            inputs={"note": "Recheck"},
        ),
    )
    scope = jit._teach_scope({"note": "Recheck"})
    assert scope is not None and scope.origins == (target,)
