"""CLI wiring for the new-architecture commands: worklist / effects / actuator
/ durable resume + approve / deployment ``run``.

Arg-parsing + delegation only — the heavy browser/vision stack is faked, and
the durable paths run against a real ``CheckpointStore`` on disk (import-light,
no backend). The point is to prove the CLI CONSTRUCTS and INJECTS the library
objects; the objects' own behavior is covered by their unit suites.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from openadapt_flow.__main__ import (
    _deployment_runtime,
    _load_worklist_file,
    _resolve_worklists,
    build_parser,
    main,
)
from openadapt_flow.compiler.induction import induce_program
from openadapt_flow.ir import ActionKind, BackendHints, Step, Workflow
from openadapt_flow.runner.dispatch_envelope import (
    ManagedDispatchEnvelope,
    write_managed_dispatch_envelope_value,
)
from openadapt_flow.runner.protocol import dispatch_binding_sha256
from openadapt_flow.runtime.authorization import (
    GovernedRunAuthorization,
    runtime_inputs_digest,
)
from openadapt_flow.runtime.durable.approval import approval_pause_digest
from openadapt_flow.runtime.durable.authority import DurableAuthority
from openadapt_flow.runtime.durable.checkpoint import (
    CheckpointStore,
    PendingEscalation,
    RunCheckpoint,
    RunManifest,
)
from openadapt_flow.runtime.durable.program_checkpoint import bundle_version

# ---------------------------------------------------------------------------
# worklist loading + relation binding
# ---------------------------------------------------------------------------


def test_load_worklist_csv(tmp_path: Path) -> None:
    f = tmp_path / "wl.csv"
    f.write_text("patient,dose\nAlice,5mg\nBob,10mg\n")
    assert _load_worklist_file(f) == [
        {"patient": "Alice", "dose": "5mg"},
        {"patient": "Bob", "dose": "10mg"},
    ]


def test_load_worklist_json_list_and_object(tmp_path: Path) -> None:
    lst = tmp_path / "l.json"
    lst.write_text('[{"patient":"Cy","dose":"2mg"}]')
    assert _load_worklist_file(lst) == [{"patient": "Cy", "dose": "2mg"}]
    obj = tmp_path / "o.json"
    obj.write_text('{"patient":"Dee"}')
    assert _load_worklist_file(obj) == [{"patient": "Dee"}]


def test_load_worklist_bad_suffix(tmp_path: Path) -> None:
    f = tmp_path / "wl.txt"
    f.write_text("x")
    with pytest.raises(SystemExit, match="must be .csv or .json"):
        _load_worklist_file(f)


def _loop_program() -> Workflow:
    """An induced program with exactly one loop relation."""

    def trace(vals: list[str]) -> Workflow:
        steps = [Step(id="open", intent="click open", action=ActionKind.CLICK)]
        for i, v in enumerate(vals):
            steps.append(
                Step(id=f"r{i}", intent="type row", action=ActionKind.TYPE, text=v)
            )
        return Workflow(name="t", steps=steps)

    result = induce_program([trace(["a", "b"]), trace(["a", "b", "c"])])
    assert result.workflow is not None and result.workflow.program is not None
    return result.workflow


def test_resolve_worklists_bare_and_named(tmp_path: Path) -> None:
    wf = _loop_program()
    (rel,) = tuple(wf.data_sources)
    csv = tmp_path / "wl.csv"
    csv.write_text("row\nX\nY\n")
    expected = {rel: [{"row": "X"}, {"row": "Y"}]}
    assert _resolve_worklists([str(csv)], wf) == expected
    assert _resolve_worklists([f"{rel}={csv}"], wf) == expected


def test_resolve_worklists_unknown_relation(tmp_path: Path) -> None:
    wf = _loop_program()
    with pytest.raises(SystemExit, match="not one of this program"):
        _resolve_worklists(["nope=/x"], wf)


def test_resolve_worklists_linear_bundle_refused(tmp_path: Path) -> None:
    lin = Workflow(
        name="lin", steps=[Step(id="a", intent="c", action=ActionKind.CLICK)]
    )
    with pytest.raises(SystemExit, match="only to a PROGRAM bundle"):
        _resolve_worklists(["/x.csv"], lin)


# ---------------------------------------------------------------------------
# deployment runtime resolution (the effect/actuator/durable injection seam)
# ---------------------------------------------------------------------------


def _replay_ns(**over) -> argparse.Namespace:
    base = dict(
        config=None,
        effects_kind=None,
        effects_base_url=None,
        effects_root=None,
        api_actuator=False,
        api_base_url=None,
        durable=False,
        allow_model_grounding=False,
    )
    base.update(over)
    return argparse.Namespace(**base)


def test_deployment_runtime_defaults_are_off() -> None:
    cfg, ev, act, durable, egress = _deployment_runtime(_replay_ns())
    assert (ev, act, durable, egress) == (None, None, False, False)


def test_deployment_runtime_flags_wire_objects() -> None:
    ns = _replay_ns(
        effects_kind="rest",
        effects_base_url="http://sor",
        api_base_url="http://api",
        durable=True,
    )
    cfg, ev, act, durable, egress = _deployment_runtime(ns)
    assert type(ev).__name__ == "RestRecordVerifier"
    assert type(act).__name__ == "ApiActuator"  # api_base_url implies enabled
    assert durable is True


def test_deployment_runtime_reads_config_file(tmp_path: Path) -> None:
    cfg_file = tmp_path / "d.yaml"
    cfg_file.write_text(
        "effects:\n  kind: document-hash\n  root: /tmp/store\n"
        "runtime:\n  durable: true\n"
    )
    cfg, ev, act, durable, egress = _deployment_runtime(
        _replay_ns(config=str(cfg_file))
    )
    assert type(ev).__name__ == "DocumentHashVerifier"
    assert durable is True


def test_deployment_runtime_bad_effects_config_exits() -> None:
    with pytest.raises(SystemExit, match="base_url"):
        _deployment_runtime(_replay_ns(effects_kind="rest"))  # no base_url


# ---------------------------------------------------------------------------
# approve / resume durable paths
# ---------------------------------------------------------------------------


def _paused_run(
    tmp_path: Path,
    *,
    verified_first: bool = True,
    backend_hints: BackendHints | None = None,
    managed: bool = False,
) -> Path:
    run = tmp_path / "run"
    bundle = tmp_path / "b"
    run_id = "cli-test-run"
    workflow = Workflow(
        name="w",
        backend_hints=backend_hints,
        steps=[
            Step(id="s0", intent="first", action=ActionKind.KEY, key="A"),
            Step(id="s1", intent="second", action=ActionKind.KEY, key="B"),
        ],
    )
    workflow.save(bundle)
    workflow = Workflow.load(bundle)
    remote_run_id = "018f6c0a-4cce-4f47-8d71-c3d63bf1c001"
    authorization = None
    managed_binding_sha256 = None
    if managed:
        assert workflow.manifest is not None
        authorization = GovernedRunAuthorization(
            bundle_content_digest=workflow.manifest.content_digest,
            runtime_inputs_digest=runtime_inputs_digest(workflow, {}, {}),
            admitted_policy_name="managed-test",
            execution_profile="standard",
            approval_source="hosted:test",
        )
        managed_binding_sha256 = dispatch_binding_sha256(remote_run_id, authorization)
    store = CheckpointStore(run)
    manifest = RunManifest(
        run_id=run_id,
        namespace_id=f"namespace-{run_id}",
        canonical_run_dir=str(run.resolve()),
        workflow_name="w",
        bundle_dir=str(bundle),
        params={},
        governed_authorization=authorization,
        delivery_authority_kind=("cloud_runner" if managed else "customer_local"),
        remote_delivery_run_id=(remote_run_id if managed else None),
        managed_dispatch_binding_sha256=managed_binding_sha256,
    )
    store.write_fresh_manifest(manifest)
    authority = DurableAuthority(run, store)
    active = authority.validate(manifest)
    if verified_first:
        store.write_checkpoint(
            RunCheckpoint(
                run_id=run_id,
                bundle_version=bundle_version(bundle),
                workflow_name="w",
                step_index=0,
                step_id="s0",
                next_step_index=1,
            )
        )
    pending = PendingEscalation(
        run_id=run_id,
        workflow_name="w",
        step_index=1,
        step_id="s1",
        category="identity",
        reason="boom",
        resume_from_index=1,
    )
    store.write_pending(pending)
    authority.advance(
        manifest,
        expected_progress_digest=active.progress_digest,
        phase="paused",
        pause_binding_sha256=approval_pause_digest(pending),
    )
    return run


def test_approve_flips_status_and_is_idempotent(tmp_path: Path, capsys) -> None:
    run = _paused_run(tmp_path)
    assert main(["approve", str(run)]) == 0
    assert CheckpointStore(run).read_pending().status == "approved"
    # Second approve is a no-op success.
    assert main(["approve", str(run)]) == 0
    assert "already approved" in capsys.readouterr().out


def test_approve_no_pending(tmp_path: Path, capsys) -> None:
    run = tmp_path / "empty"
    run.mkdir()
    assert main(["approve", str(run)]) == 1
    assert "nothing to approve" in capsys.readouterr().out


def test_resume_no_pending_returns_1(tmp_path: Path) -> None:
    run = tmp_path / "empty"
    run.mkdir()
    assert main(["resume", str(run)]) == 1


def test_resume_require_approval_refuses_pending(tmp_path: Path, capsys) -> None:
    run = _paused_run(tmp_path)
    rc = main(["resume", str(run), "--require-approval"])
    assert rc == 3
    output = capsys.readouterr().out
    assert "not 'approved'" in output
    assert "openadapt flow approve" in output
    assert "without --require-approval" not in output


def test_resume_without_url_exits(tmp_path: Path) -> None:
    run = _paused_run(tmp_path)
    # Approved + no --url and no backend.url -> a clear config error.
    main(["approve", str(run)])
    with pytest.raises(SystemExit, match="needs the target app URL"):
        main(["resume", str(run), "--require-approval"])


# ---------------------------------------------------------------------------
# parser wiring for every new command
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv,func_name",
    [
        (["induce", "r1", "r2", "--out", "o"], "_cmd_induce"),
        (
            [
                "compose",
                "--child",
                "a=./a",
                "--child",
                "b=./b",
                "--out",
                "o",
            ],
            "_cmd_compose",
        ),
        (["replay", "b", "--worklist", "w.csv"], "_cmd_replay"),
        (["run", "b", "--config", "d.yaml"], "_cmd_run"),
        (["resume", "run_dir"], "_cmd_resume"),
        (["approve", "run_dir"], "_cmd_approve"),
    ],
)
def test_parser_dispatches(argv, func_name) -> None:
    args = build_parser().parse_args(argv)
    assert args.func.__name__ == func_name


def test_replay_parser_has_deployment_flags() -> None:
    args = build_parser().parse_args(
        [
            "replay",
            "b",
            "--config",
            "d.yaml",
            "--effects-kind",
            "fhir",
            "--effects-base-url",
            "http://sor",
            "--api-actuator",
            "--durable",
            "--worklist",
            "rel=w.csv",
        ]
    )
    assert args.config == "d.yaml"
    assert args.effects_kind == "fhir"
    assert args.api_actuator is True
    assert args.durable is True
    assert args.worklist == ["rel=w.csv"]


# ---------------------------------------------------------------------------
# replay/run integration: prove the deployment objects reach the Replayer
# ---------------------------------------------------------------------------


class _FakePage:
    video = None

    def goto(self, url):  # noqa: D401 - fake
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


def test_replay_injects_effects_actuator_durable_and_worklist(
    tmp_path: Path, monkeypatch
) -> None:
    # A program bundle with one loop relation, saved to disk.
    wf = _loop_program()
    bundle = tmp_path / "bundle"
    wf.save(bundle)
    (rel,) = tuple(wf.data_sources)
    csv = tmp_path / "wl.csv"
    csv.write_text("row\nX\nY\n")

    captured: dict = {}
    _install_fake_browser(monkeypatch, captured)

    rc = main(
        [
            "replay",
            str(bundle),
            "--url",
            "http://app.example",
            "--effects-kind",
            "rest",
            "--effects-base-url",
            "http://sor",
            "--api-base-url",
            "http://api",
            "--durable",
            "--worklist",
            f"{rel}={csv}",
            "--run-dir",
            str(tmp_path / "run"),
        ]
    )
    assert rc == 0
    ctor = captured["ctor"]
    assert type(ctor["effect_verifier"]).__name__ == "RestRecordVerifier"
    assert type(ctor["api_actuator"]).__name__ == "ApiActuator"
    assert ctor["durable"] is True
    assert captured["run"]["worklists"] == {rel: [{"row": "X"}, {"row": "Y"}]}


def test_run_delegates_to_replay_with_config(tmp_path: Path, monkeypatch) -> None:
    # ``run`` is now FAIL-CLOSED (openadapt_flow.run_gate): it executes only an
    # ADMISSIBLE bundle. So this delegation/wiring test uses a bundle that passes
    # every gate (certified clinical-write, armed, effect-covered, encrypted,
    # sealed) and asserts the config wiring reaches the shared executor.
    from openadapt_flow.ir import Anchor, Postcondition, PostconditionKind
    from openadapt_flow.runtime.effects import Effect, EffectKind

    key = "cli-wiring-key"
    monkeypatch.setenv("OPENADAPT_BUNDLE_KEY", key)

    def _armed_click(sid, ocr, *, risk="reversible", effects=None):
        return Step(
            id=sid,
            intent=f"click {ocr}",
            action=ActionKind.CLICK,
            risk=risk,
            anchor=Anchor(
                template=f"{sid}.png",
                region=(0, 0, 10, 10),
                click_point=(5, 5),
                ocr_text=ocr,
                context_text="Row 42 Jane Doe",
            ),
            identity_armed=True,
            effects=list(effects or []),
            expect=[Postcondition(kind=PostconditionKind.TEXT_PRESENT, text="OK")],
        )

    wf = Workflow(
        name="lin",
        steps=[
            _armed_click("a", "Open"),
            _armed_click(
                "b",
                "Save",
                risk="irreversible",
                effects=[
                    Effect(
                        kind=EffectKind.RECORD_WRITTEN,
                        match={"patient_id": "p1"},
                        idempotency_key="run-1",
                        risk="irreversible",
                    )
                ],
            ),
        ],
    )
    bundle = tmp_path / "bundle"
    wf.save(bundle, encrypt=True, key=key)
    cfg = tmp_path / "d.yaml"
    cfg.write_text(
        "backend:\n  url: http://from-config\n"
        "effects:\n  kind: rest\n  base_url: http://sor\n"
        "runtime:\n  durable: true\n"
        "policy:\n  policy: clinical-write\n"
    )

    captured: dict = {}
    _install_fake_browser(monkeypatch, captured)

    rc = main(
        ["run", str(bundle), "--config", str(cfg), "--run-dir", str(tmp_path / "r")]
    )
    assert rc == 0
    # The bundle was ADMITTED and delegated: backend.url from config was used;
    # effects + durable wired from config.
    assert type(captured["ctor"]["effect_verifier"]).__name__ == "RestRecordVerifier"
    assert captured["ctor"]["durable"] is True


# ---------------------------------------------------------------------------
# resume routed through the backend factory (openadapt-flow#115 seam)
# ---------------------------------------------------------------------------


def test_resume_parser_has_backend_flags() -> None:
    args = build_parser().parse_args(
        ["resume", "run_dir", "--backend", "windows", "--agent-url", "http://a:5001"]
    )
    assert args.backend == "windows"
    assert args.agent_url == "http://a:5001"


def test_resume_web_backend_still_requires_url(tmp_path: Path) -> None:
    # The web default is unchanged: with no --url and no backend.url the web
    # path still fails loud on the missing target URL (it must launch a browser
    # and navigate a page). --backend web is the explicit form of the default.
    run = _paused_run(tmp_path)
    main(["approve", str(run)])
    with pytest.raises(SystemExit, match="needs the target app URL"):
        main(["resume", str(run), "--require-approval", "--backend", "web"])


def test_resume_web_default_builds_playwright_backend(
    tmp_path: Path, monkeypatch
) -> None:
    # The default (web / no --backend) still drives the browser: it launches
    # Playwright, builds the PlaywrightBackend via the factory, and hands it to
    # the durable resume entrypoint — byte-for-byte the historical path.
    run = _paused_run(tmp_path)
    main(["approve", str(run)])

    captured: dict = {}
    _install_fake_browser(monkeypatch, captured)

    import openadapt_flow.runtime.durable as durable_mod

    seen: dict = {}

    def _fake_resume(run_dir, replayer, key=None, execution_target_kind=None):
        seen["called"] = True
        seen["execution_target_kind"] = execution_target_kind
        return _FakeReport()

    monkeypatch.setattr(durable_mod, "resume", _fake_resume)

    rc = main(
        [
            "resume",
            str(run),
            "--require-approval",
            "--url",
            "http://app.example",
        ]
    )
    assert rc == 0
    assert seen["called"] is True
    assert seen["execution_target_kind"] == "web"
    # The factory built the browser-backed backend (the fake returns "backend").
    assert captured["ctor"]  # Replayer was constructed for the web path


def test_resume_wires_all_configured_appliance_tiers(
    tmp_path: Path, monkeypatch
) -> None:
    """Resume uses the shared deployment constructor, including VLM handles."""
    run = _paused_run(tmp_path)
    main(["approve", str(run)])
    config = tmp_path / "deployment.yaml"
    config.write_text("runtime:\n  allow_model_grounding: true\n")

    captured: dict = {}
    _install_fake_browser(monkeypatch, captured)

    class _Appliance:
        grounder = object()
        identity_vlm = object()
        state_verifier = object()

    appliance = _Appliance()

    import openadapt_flow.runtime.durable as durable_mod
    import openadapt_flow.runtime.grounder as grounder_mod
    import openadapt_flow.runtime.remote_vlm as remote_mod

    def _grounder(*, fallback=None):
        captured["fallback"] = fallback
        return fallback

    monkeypatch.setattr(remote_mod, "appliance_from_env", lambda: appliance)
    monkeypatch.setattr(grounder_mod, "build_grounder", _grounder)
    monkeypatch.setattr(
        durable_mod,
        "resume",
        lambda run_dir, replayer, key=None, execution_target_kind=None: _FakeReport(),
    )

    rc = main(
        [
            "resume",
            str(run),
            "--require-approval",
            "--url",
            "http://app.example",
            "--config",
            str(config),
        ]
    )

    assert rc == 0
    assert captured["fallback"] is appliance.grounder
    assert captured["ctor"]["grounder"] is appliance.grounder
    assert captured["ctor"]["identity_vlm"] is appliance.identity_vlm
    assert captured["ctor"]["state_verifier"] is appliance.state_verifier
    assert captured["ctor"]["allow_model_grounding"] is True
    assert captured["ctor"]["durable"] is True


def test_managed_resume_refuses_model_grounding_profile(tmp_path: Path, capsys) -> None:
    """A managed resume preserves its local-only evidence contract."""
    run = _paused_run(tmp_path, managed=True)
    main(["approve", str(run)])
    config = tmp_path / "deployment.yaml"
    config.write_text("runtime:\n  allow_model_grounding: true\n")
    manifest = CheckpointStore(run).read_manifest()
    assert manifest is not None
    assert manifest.governed_authorization is not None
    assert manifest.remote_delivery_run_id is not None
    assert manifest.managed_dispatch_binding_sha256 is not None
    dispatch_file = write_managed_dispatch_envelope_value(
        tmp_path / "managed-dispatch.json",
        ManagedDispatchEnvelope(
            run_id=manifest.remote_delivery_run_id,
            bundle_content_digest=(
                manifest.governed_authorization.bundle_content_digest
            ),
            runtime_inputs_digest=(
                manifest.governed_authorization.runtime_inputs_digest
            ),
            authorization=manifest.governed_authorization,
            dispatch_binding_sha256=manifest.managed_dispatch_binding_sha256,
        ),
    )

    rc = main(
        [
            "resume",
            str(run),
            "--require-approval",
            "--config",
            str(config),
            "--managed-dispatch-file",
            str(dispatch_file),
        ]
    )

    assert rc == 3
    output = capsys.readouterr().out
    assert "managed runner doesn't permit model grounding" in output
    assert "Nothing was executed" in output


def test_resume_windows_config_builds_windows_backend(
    tmp_path: Path, monkeypatch
) -> None:
    # A windows-kind resume builds a real WindowsBackend from the factory with
    # NO browser and NO --url, and hands it to the durable resume entrypoint.
    run = _paused_run(tmp_path)
    main(["approve", str(run)])

    captured: dict = {}

    class _CapturingReplayer:
        def __init__(self, backend, **kwargs):
            captured["backend"] = backend

    import openadapt_flow.report as report_mod
    import openadapt_flow.runtime as runtime_mod
    import openadapt_flow.runtime.durable as durable_mod

    monkeypatch.setattr(runtime_mod, "Replayer", _CapturingReplayer)
    monkeypatch.setattr(
        durable_mod,
        "resume",
        lambda run_dir, replayer, key=None, execution_target_kind=None: (
            captured.update(execution_target_kind=execution_target_kind)
            or _FakeReport()
        ),
    )
    monkeypatch.setattr(report_mod, "render_run_report", lambda run_dir: "REPORT.md")

    rc = main(
        [
            "resume",
            str(run),
            "--require-approval",
            "--backend",
            "windows",
            "--agent-url",
            "http://localhost:5001",
        ]
    )
    assert rc == 0
    backend = captured["backend"]
    assert type(backend).__name__ == "WindowsBackend"
    assert backend.server_url == "http://localhost:5001"
    assert captured["execution_target_kind"] == "windows"


def test_resume_uses_recorded_citrix_target_and_readiness(
    tmp_path: Path, monkeypatch
) -> None:
    run = _paused_run(
        tmp_path,
        backend_hints=BackendHints(
            backend="citrix",
            rdp_window="wfica32",
            rdp_window_title="Claims - Citrix Workspace",
            rdp_readiness_text="Claims queue",
        ),
    )
    main(["approve", str(run)])

    captured: dict = {}

    class FakeBackend:
        def close(self):
            pass

    class CapturingReplayer:
        def __init__(self, backend, **kwargs):
            captured["backend"] = backend

    import openadapt_flow.backends.factory as factory
    import openadapt_flow.report as report_mod
    import openadapt_flow.runtime as runtime_mod
    import openadapt_flow.runtime.durable as durable_mod

    def fake_build(cfg, **kwargs):
        captured["config"] = cfg
        return FakeBackend()

    monkeypatch.setattr(factory, "build_backend", fake_build)
    monkeypatch.setattr(runtime_mod, "Replayer", CapturingReplayer)
    monkeypatch.setattr(
        durable_mod,
        "resume",
        lambda run_dir, replayer, key=None, execution_target_kind=None: (
            captured.update(execution_target_kind=execution_target_kind)
            or _FakeReport()
        ),
    )
    monkeypatch.setattr(report_mod, "render_run_report", lambda run_dir: "REPORT.md")

    assert main(["resume", str(run), "--require-approval"]) == 0
    cfg = captured["config"]
    assert cfg.kind == "citrix"
    assert cfg.rdp_window == "wfica32"
    assert cfg.rdp_window_title == "Claims - Citrix Workspace"
    assert cfg.rdp_readiness_text == "Claims queue"
    assert captured["execution_target_kind"] == "citrix"


def test_resume_refuses_blank_citrix_readiness_before_backend(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    run = _paused_run(tmp_path)
    main(["approve", str(run)])
    config = tmp_path / "citrix.yaml"
    config.write_text(
        "backend:\n  kind: citrix\n  rdp_window: wfica32\n  rdp_readiness_text: '   '\n"
    )

    import openadapt_flow.backends.factory as factory

    monkeypatch.setattr(
        factory,
        "build_backend",
        lambda *args, **kwargs: pytest.fail("backend must not be constructed"),
    )
    assert (
        main(
            [
                "resume",
                str(run),
                "--require-approval",
                "--config",
                str(config),
            ]
        )
        == 2
    )
    out = capsys.readouterr().out
    assert "Resume REFUSED" in out
    assert "Nothing was executed" in out


def test_resume_bundle_load_refusal_has_safe_constant_guidance(
    tmp_path: Path, capsys
) -> None:
    run = _paused_run(tmp_path)
    main(["approve", str(run)])
    (tmp_path / "b" / "workflow.json").unlink()

    assert main(["resume", str(run), "--require-approval"]) == 3
    out = capsys.readouterr().out
    assert "paused workflow bundle could not be loaded safely" in out
    assert "OPENADAPT_BUNDLE_KEY" in out
    assert "Nothing was executed" in out


# ---------------------------------------------------------------------------
# --version
# ---------------------------------------------------------------------------


def test_version_flag_prints_version_and_exits_zero(capsys) -> None:
    """``openadapt-flow --version`` prints the version and exits 0 (no subcommand)."""
    from openadapt_flow.__main__ import _package_version

    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out.strip()
    assert out == f"openadapt-flow {_package_version()}"
    prog, _, ver = out.partition(" ")
    assert ver and ver[0].isdigit()
