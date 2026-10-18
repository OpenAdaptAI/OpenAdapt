"""Vision-first qualification: lookup capabilities and the pixels-only pass.

A workflow qualified with structure lookups (DOM / accessibility) must not
run where only pixels exist, such as an RDP or Citrix window viewed from
outside the session. These tests cover the qualified-lookup record, the
run-time halt, the deployment surface declaration, and the pixels-only pass
every ``openadapt-flow qualify`` runs.

The remote-display surfaces here are simulated. None of this is evidence
from a live Citrix or RDP deployment.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from PIL import Image

import openadapt_flow.qualification as qualification_mod
import openadapt_flow.qualify.campaign as campaign
import openadapt_flow.qualify.result as result_mod
from openadapt_flow.deployment import BackendConfig, DeploymentConfig
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Resolution,
    RunReport,
    Step,
    StepResult,
    StructuralLocator,
    Workflow,
)
from openadapt_flow.lookup_capabilities import (
    PixelsOnlyPass,
    QualifiedLookup,
    capabilities_used,
    lookup_capability_refusal,
    resolve_deployment_surface,
    surface_offers_structure,
)
from openadapt_flow.qualification import (
    EnvironmentBoundary,
    init_project,
    set_qualified_lookup,
)
from openadapt_flow.qualify import QualifyError, parse_verify_config, qualify
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.verification import VerificationTier

VIEWPORT = (300, 200)


def _png(size=VIEWPORT) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (240, 240, 240)).save(buffer, format="PNG")
    return buffer.getvalue()


def _environment() -> EnvironmentBoundary:
    return EnvironmentBoundary(
        target_kind="web",
        application="Demo",
        application_identity="http://127.0.0.1:9301",
        application_version="1",
        environment_digest="b" * 64,
        runtime_version="1.0.0",
        required_capabilities=["playwright_dom"],
    )


def _lookup(
    *,
    used: list[str],
    surface: str = "local",
    role: str = "informational",
    run: int = 6,
    passed: int = 6,
) -> QualifiedLookup:
    return QualifiedLookup(
        surface=surface,  # type: ignore[arg-type]
        target_kind="web",
        structure_offered=surface != "remote_display",
        capabilities_used=used,  # type: ignore[arg-type]
        pixels_only=PixelsOnlyPass(
            role=role,  # type: ignore[arg-type]
            cases=["representative"],
            trials_run=run,
            trials_passed=passed,
        ),
    )


def _save_step() -> Step:
    return Step(
        id="s3",
        intent="Create patient",
        action=ActionKind.CLICK,
        anchor=Anchor(
            template="templates/s3.png",
            region=(10, 10, 80, 20),
            click_point=(20, 20),
            structural=StructuralLocator(selector="#create", role="button"),
            context_text="Create New Patient",
        ),
        identity_armed=True,
        risk="irreversible",
    )


def _workflow() -> Workflow:
    return Workflow(
        name="register",
        steps=[
            Step(id="s1", intent="email", action=ActionKind.TYPE, param="email"),
            Step(id="s2", intent="name", action=ActionKind.TYPE, param="lname"),
            _save_step(),
        ],
        params={"email": "a@example.test", "lname": "Doe"},
    )


def _qualified(used: list[str], surface: str = "local") -> Workflow:
    workflow = _workflow()
    init_project(workflow, environment=_environment())
    set_qualified_lookup(
        workflow,
        _lookup(
            used=used,
            surface=surface,
            role="gate" if surface == "remote_display" else "informational",
        ),
    )
    return workflow


# -- the deployment surface ---------------------------------------------------


@pytest.mark.parametrize(
    ("declared", "kind", "expected"),
    [
        (None, "web", "local"),
        (None, "windows", "local"),
        (None, "rdp", "remote_display"),
        (None, "citrix", "remote_display"),
        ("unknown", "citrix", "remote_display"),
        ("unknown", "web", "remote_display"),
        ("remote_in_session", "windows", "remote_in_session"),
        ("remote_display", "web", "remote_display"),
    ],
)
def test_surface_resolution_defaults_to_pixels_for_remote_display(
    declared, kind, expected
):
    assert resolve_deployment_surface(declared, kind) == expected


def test_only_a_remote_display_surface_loses_structure():
    assert surface_offers_structure("local", "web")
    assert surface_offers_structure("remote_in_session", "windows")
    assert not surface_offers_structure("remote_display", "web")
    assert not surface_offers_structure("local", "citrix")


def test_deployment_yaml_declares_the_surface():
    cfg = DeploymentConfig.model_validate(
        {"backend": {"kind": "windows", "deployment_surface": "remote_in_session"}}
    )
    assert cfg.backend.deployment_surface == "remote_in_session"
    assert BackendConfig(kind="citrix").deployment_surface is None
    with pytest.raises(ValueError, match="contradicts backend kind 'citrix'"):
        BackendConfig(kind="citrix", deployment_surface="local")
    with pytest.raises(ValueError):
        BackendConfig(kind="web", deployment_surface="somewhere")


def test_verify_yaml_declares_the_target_surface():
    config = _config(surface="unknown")
    assert campaign.lookup_plan(config) == ("remote_display", False)
    assert campaign.lookup_plan(_config()) == ("local", True)


# -- the qualified-lookup record ---------------------------------------------


def _report(*rungs: str) -> RunReport:
    return RunReport(
        workflow_name="register",
        started_at=datetime.now(timezone.utc).isoformat(),
        results=[
            StepResult(
                step_id=f"s{index}",
                intent="x",
                ok=True,
                resolution=Resolution(
                    rung=rung,  # type: ignore[arg-type]
                    point=(1, 1),
                    confidence=1.0,
                    elapsed_ms=1.0,
                ),
            )
            for index, rung in enumerate(rungs)
        ],
    )


def test_capabilities_used_come_from_the_resolved_rungs():
    assert capabilities_used(
        [_report("structural", "template")], structure_offered=True
    ) == ["pixels", "structure"]
    assert capabilities_used([_report("ocr")], structure_offered=True) == ["pixels"]
    # Nothing resolved: assume everything that was offered.
    assert capabilities_used([_report()], structure_offered=True) == [
        "pixels",
        "structure",
    ]
    assert capabilities_used([], structure_offered=False) == ["pixels"]


def test_qualification_records_the_lookups_and_round_trips(tmp_path):
    workflow = _workflow()
    project = init_project(workflow, environment=_environment())
    contract = project.contract_sha256()
    set_qualified_lookup(workflow, _lookup(used=["pixels", "structure"]))
    # An outcome of the runs, not part of the contract the cases sign.
    assert project.contract_sha256() == contract
    templates = tmp_path / "bundle" / "templates"
    templates.mkdir(parents=True)
    (templates / "s3.png").write_bytes(_png((80, 20)))
    workflow.save(tmp_path / "bundle")
    loaded = Workflow.load(tmp_path / "bundle")
    assert loaded.qualification is not None
    lookup = loaded.qualification.lookup
    assert lookup is not None
    assert lookup.capabilities_used == ["pixels", "structure"]
    assert lookup.surface == "local"
    assert lookup.pixels_only.rate_text() == "6/6 (100%)"


def test_a_project_without_the_record_serializes_as_before():
    workflow = _workflow()
    project = init_project(workflow, environment=_environment())
    assert "lookup" not in project.model_dump(mode="json")


def test_certification_binds_the_lookup_record(tmp_path):
    from openadapt_flow.policy import load_policy

    workflow = _qualified(["pixels", "structure"])
    assert workflow.qualification is not None
    qualification_mod.certify_project(
        workflow, policy=load_policy("permissive"), evidence_root=tmp_path
    )
    certification = workflow.qualification.last_certification
    assert certification is not None
    assert certification.lookup_sha256 == (
        workflow.qualification.lookup.contract_sha256()  # type: ignore[union-attr]
    )


def test_new_case_evidence_clears_a_stale_lookup_record(tmp_path):
    workflow = _qualified(["structure"])
    qualification_mod.record_case_results(workflow, [], evidence_root=tmp_path)
    assert workflow.qualification is not None
    assert workflow.qualification.lookup is None


def test_record_rejects_structure_on_a_remote_display_surface():
    with pytest.raises(ValueError):
        _lookup(used=["structure"], surface="remote_display", role="gate")
    with pytest.raises(ValueError, match="gates"):
        QualifiedLookup(
            surface="remote_display",
            target_kind="web",
            structure_offered=False,
            capabilities_used=["pixels"],
            pixels_only=PixelsOnlyPass(
                role="informational", trials_run=1, trials_passed=1
            ),
        )


# -- the run-time halt --------------------------------------------------------


class PixelBackend:
    """A pixel-only surface: screenshots and input, no structure."""

    def __init__(self) -> None:
        self.actions: list[Any] = []
        self._frame = _png()

    @property
    def viewport(self):
        return VIEWPORT

    def screenshot(self):
        return self._frame

    def click(self, x, y, *, double=False):
        self.actions.append(("click", x, y))

    def type_text(self, text):
        self.actions.append(("type", text))

    def press(self, key):
        self.actions.append(("press", key))

    def scroll(self, dx, dy):
        self.actions.append(("scroll", dx, dy))


class StructureBackend(PixelBackend):
    def locate_structural(self, locator):  # pragma: no cover - not reached
        return None


def _bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "bundle"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "s3.png").write_bytes(_png((80, 20)))
    return bundle


def test_structure_qualified_bundle_halts_on_a_pixel_only_surface(tmp_path):
    workflow = _qualified(["pixels", "structure"])
    backend = StructureBackend()
    report = Replayer(backend, deployment_surface="remote_display").run(
        workflow,
        bundle_dir=_bundle(tmp_path),
        run_dir=tmp_path / "run",
        execution_target_kind="web",
    )
    assert report.success is False
    assert report.execution_outcome == "HALTED"
    assert report.transaction_outcome == "HALTED_BEFORE_EFFECT"
    assert backend.actions == []
    halt = report.results[0]
    assert halt.step_id == "<lookup-capabilities>"
    assert halt.safety_halt
    assert "Requalify" in (halt.error or "")
    assert "remote_display" in (halt.error or "")


@pytest.mark.parametrize("kind", ["rdp", "citrix"])
def test_remote_display_backend_halts_without_any_declaration(tmp_path, kind):
    workflow = _qualified(["structure"])
    backend = PixelBackend()
    report = Replayer(backend).run(
        workflow,
        bundle_dir=_bundle(tmp_path),
        run_dir=tmp_path / "run",
        execution_target_kind=kind,
    )
    assert report.transaction_outcome == "HALTED_BEFORE_EFFECT"
    assert backend.actions == []


def test_pixel_qualified_bundle_is_allowed_on_a_pixel_only_surface():
    workflow = _qualified(["pixels"], surface="remote_display")
    replayer = Replayer(PixelBackend(), deployment_surface="remote_display")
    assert replayer._lookup_capability_refusal(workflow, "citrix") is None


def test_in_session_remote_runtime_is_allowed_structure():
    workflow = _qualified(["pixels", "structure"])
    replayer = Replayer(StructureBackend(), deployment_surface="remote_in_session")
    assert replayer._lookup_capability_refusal(workflow, "windows") is None
    # The same bundle viewed from outside the session is refused.
    outside = Replayer(StructureBackend(), deployment_surface="remote_display")
    assert outside._lookup_capability_refusal(workflow, "windows") is not None


def test_contradictory_declaration_is_read_conservatively():
    workflow = _qualified(["structure"])
    replayer = Replayer(StructureBackend(), deployment_surface="local")
    refusal = replayer._lookup_capability_refusal(workflow, "citrix")
    assert refusal is not None and refusal.startswith("HALTED_BEFORE_EFFECT")


def test_bundles_without_the_record_are_unchanged():
    workflow = _workflow()
    assert (
        lookup_capability_refusal(
            workflow, surface="remote_display", structure_available=False
        )
        is None
    )
    replayer = Replayer(PixelBackend(), deployment_surface="remote_display")
    assert replayer._lookup_capability_refusal(workflow, "citrix") is None


def test_a_certified_bundle_without_the_record_halts_where_structure_is_missing():
    """The manual `qualification run` + `certify` path certified without a
    lookup record, and such a bundle then ran on RDP or Citrix unchecked."""
    workflow = _workflow()
    init_project(workflow, environment=_environment())
    workflow.qualification.last_certification = SimpleNamespace(passed=True)
    refusal = lookup_capability_refusal(
        workflow, surface="remote_display", structure_available=False
    )
    assert refusal is not None and "Requalify" in refusal
    assert (
        lookup_capability_refusal(workflow, surface="local", structure_available=True)
        is None
    )


# -- the pixels-only pass in qualify -----------------------------------------

APP = {"name": "Demo", "version": "1", "url": "http://127.0.0.1:9301/"}


def _config(**app_over: Any):
    return parse_verify_config(
        {
            "schema_version": "openadapt.verify/v1",
            "app": {**APP, "reset": {"command": "true"}, **app_over},
            "verifier": {
                "kind": "rest",
                "base_url": "http://127.0.0.1:9300",
                "records_path": "/api/patient",
                "records_key": "data",
            },
            "record": {
                "name": "patient",
                "match": ["email"],
                "duplicate_key": "email",
                "fields": {"email": "email", "lname": "lname"},
            },
            "campaign": {"trials": 1},
        },
        env={},
    )


class _Campaign:
    """Stand-ins for the browser, the read-back, and the signing.

    Only the orchestration under test is real: which runs happen, with which
    lookups, and what the campaign records about them.
    """

    def __init__(self, monkeypatch, *, fail: Optional[set[tuple[str, bool]]] = None):
        self.calls: list[dict[str, Any]] = []
        self.fail = fail or set()
        monkeypatch.setattr(campaign, "_run", self._run)
        monkeypatch.setattr(campaign, "_judge_trial", self._judge)
        monkeypatch.setattr(
            campaign,
            "case_result",
            lambda **kw: SimpleNamespace(case_id=kw["case_id"], detail_code="x"),
        )
        monkeypatch.setattr(campaign, "web_environment_payload", lambda *a, **k: {})
        monkeypatch.setattr(campaign, "web_environment_digest", lambda _e: "e" * 64)
        monkeypatch.setattr(
            qualification_mod,
            "record_case_results",
            lambda workflow, results, **_k: workflow.qualification,
        )
        monkeypatch.setattr(
            qualification_mod,
            "evaluate_qualification",
            lambda *_a, **_k: SimpleNamespace(
                passed=True, render=lambda: "PASS", model_dump=lambda **_k: {}
            ),
        )
        monkeypatch.setattr(result_mod, "verifier_identity_of", lambda *_a: "v")
        monkeypatch.setattr(result_mod, "verify_config_sha256", lambda *_a: "c" * 64)
        monkeypatch.setattr(result_mod, "case_entry", lambda **_k: {"passed": True})
        monkeypatch.setattr(
            result_mod,
            "build_result",
            lambda **_k: {
                "passed": True,
                "silent_incorrect_success_count": 0,
                "over_halt_count": 0,
                "executor_model_calls": 0,
                "verifier_model_calls": 0,
            },
        )
        monkeypatch.setattr(result_mod, "result_sha256", lambda _r: "f" * 64)

    def hooks(self) -> campaign.QualifyHooks:
        verifier = SimpleNamespace(
            verification_tier=VerificationTier.INDEPENDENT_SYSTEM
        )
        return campaign.QualifyHooks(
            open_session=lambda: (SimpleNamespace(), lambda: None),
            reset=lambda: None,
            make_verifier=lambda _params: verifier,
            settle_s=0.0,
        )

    def _run(self, *, ctx, lookup=None, pixels_only=False, **_kw):
        structural = bool((lookup or {}).get("use_structural")) and not pixels_only
        self.calls.append(
            {
                "case": ctx.case_id,
                "pixels_only": pixels_only,
                "use_structural": structural,
                "surface": (lookup or {}).get("deployment_surface"),
            }
        )
        outcome = ctx.expected
        if (ctx.case_id, pixels_only) in self.fail:
            outcome = "COMPLETED_UNVERIFIED"
        report = _report("structural" if structural else "template")
        report.execution_outcome = outcome
        report.success = outcome == "VERIFIED"
        return report, None

    @staticmethod
    def _judge(*, ctx, **_kw):
        return {"oracle": {"passed": True, "observed": ctx.oracle_kind}, "extra": {}}


def _source_bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "src"
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates" / "s3.png").write_bytes(_png((80, 20)))
    _workflow().save(bundle)
    return bundle


def test_qualify_records_capabilities_and_reports_the_pixels_only_rate(
    tmp_path, monkeypatch
):
    fake = _Campaign(monkeypatch, fail={("fault-ambiguity", True)})
    lines: list[str] = []
    result = qualify(
        _source_bundle(tmp_path),
        _config(),
        out=tmp_path / "out",
        hooks=fake.hooks(),
        log=lines.append,
    )
    signed = [c for c in fake.calls if not c["pixels_only"]]
    pixel = [c for c in fake.calls if c["pixels_only"]]
    assert len(signed) == 6 and all(c["use_structural"] for c in signed)
    assert len(pixel) == 6 and not any(c["use_structural"] for c in pixel)
    # The pixels-only pass is informational on a local surface: one failure
    # lowers the rate but the campaign still passes.
    assert result.passed
    assert result.lookup is not None
    assert result.lookup.surface == "local"
    assert result.lookup.capabilities_used == ["structure"]
    pixels = result.pixels_only
    assert pixels is not None and pixels.role == "informational"
    assert (pixels.trials_run, pixels.trials_passed) == (6, 5)
    assert pixels.pass_rate == pytest.approx(5 / 6)
    assert any("pixels-only pass (informational): 5/6 (83%)" in ln for ln in lines)
    assert pixels.common_failure() == "observed COMPLETED_UNVERIFIED, expected HALTED"
    assert "Pixels-only pass (for information): 5/6 (83%)" in (
        (tmp_path / "out" / "QUALIFICATION.md").read_text()
    )
    saved = Workflow.load(result.bundle_dir)
    assert saved.qualification is not None and saved.qualification.lookup is not None
    assert saved.qualification.lookup.capabilities_used == ["structure"]
    assert (tmp_path / "out" / "qualification" / "lookup.json").is_file()


def test_pixels_only_pass_gates_a_remote_display_target(tmp_path, monkeypatch):
    fake = _Campaign(monkeypatch)
    result = qualify(
        _source_bundle(tmp_path),
        _config(surface="remote_display"),
        out=tmp_path / "out",
        hooks=fake.hooks(),
        log=lambda _l: None,
    )
    # One pass, with structure disabled, whose result is the qualification.
    assert len(fake.calls) == 6
    assert not any(c["use_structural"] for c in fake.calls)
    assert {c["surface"] for c in fake.calls} == {"remote_display"}
    assert result.lookup is not None
    assert result.lookup.capabilities_used == ["pixels"]
    assert result.pixels_only is not None
    assert result.pixels_only.role == "gate"
    assert result.pixels_only.rate_text() == "6/6 (100%)"


def test_pixels_only_failure_fails_a_remote_display_qualification(
    tmp_path, monkeypatch
):
    fake = _Campaign(monkeypatch, fail={("representative", False)})
    with pytest.raises(QualifyError, match="representative trial 1"):
        qualify(
            _source_bundle(tmp_path),
            _config(surface="unknown"),
            out=tmp_path / "out",
            hooks=fake.hooks(),
            log=lambda _l: None,
        )
    assert not any(c["use_structural"] for c in fake.calls)


# -- coverage of the rule, point by point ------------------------------------


@pytest.mark.parametrize(
    ("app_over", "expected"),
    [
        ({}, ("local", True)),
        ({"target": "rdp"}, ("remote_display", False)),
        ({"target": "citrix"}, ("remote_display", False)),
        ({"surface": "remote_display"}, ("remote_display", False)),
        (
            {"target": "windows", "surface": "remote_in_session"},
            ("remote_in_session", True),
        ),
    ],
)
def test_qualification_targets_the_surface_it_will_run_on(app_over, expected):
    # Point 1: local, RDP, Citrix and a remote display viewed from outside
    # each resolve to the surface the workflow is qualified on.
    assert campaign.lookup_plan(_config(**app_over)) == expected


def test_record_keeps_the_target_and_the_lookups_used():
    pixels = campaign.summarize_pixels_only(
        [("representative", 1, None), ("fault-ambiguity", 1, "observed X")],
        role="gate",
    )
    record = campaign.qualified_lookup(
        surface="remote_display",
        target_kind="citrix",
        structure_offered=False,
        reports={"representative": [_report("template")]},
        pixels_only=pixels,
    )
    dumped = record.model_dump(mode="json")
    assert dumped["surface"] == "remote_display"
    assert dumped["target_kind"] == "citrix"
    assert dumped["capabilities_used"] == ["pixels"]
    assert dumped["pixels_only"]["role"] == "gate"
    assert pixels.pass_rate == pytest.approx(0.5)


def test_a_local_surface_without_structure_also_halts(tmp_path):
    # Point 2: "fewer capabilities" is not only remote display. A local run
    # whose backend cannot read structure, or with structure switched off,
    # halts before any action too.
    workflow = _qualified(["pixels", "structure"])
    for replayer in (
        Replayer(PixelBackend(), deployment_surface="local"),
        Replayer(StructureBackend(), deployment_surface="local", use_structural=False),
    ):
        refusal = replayer._lookup_capability_refusal(workflow, "web")
        assert refusal is not None
        assert refusal.startswith("HALTED_BEFORE_EFFECT")
        assert "local" in refusal
    backend = PixelBackend()
    report = Replayer(backend, deployment_surface="local").run(
        workflow,
        bundle_dir=_bundle(tmp_path),
        run_dir=tmp_path / "run",
        execution_target_kind="web",
    )
    assert report.transaction_outcome == "HALTED_BEFORE_EFFECT"
    assert backend.actions == []


def test_a_structure_surface_runs_a_structure_qualified_workflow():
    # Point 2, the other side: the same or more capabilities is allowed.
    workflow = _qualified(["pixels", "structure"])
    replayer = Replayer(StructureBackend(), deployment_surface="local")
    assert replayer._lookup_capability_refusal(workflow, "web") is None


def test_in_session_runtime_is_declared_with_its_own_backend():
    # Point 4: structure is allowed when Flow is installed in the session,
    # which means the session's own backend, never the rdp/citrix viewer.
    cfg = BackendConfig(kind="windows", deployment_surface="remote_in_session")
    assert surface_offers_structure("remote_in_session", cfg.kind)
    for kind in ("rdp", "citrix"):
        with pytest.raises(ValueError, match="remote_display"):
            BackendConfig(kind=kind, deployment_surface="remote_in_session")


DOCS = Path(__file__).resolve().parents[1] / "docs"


def test_docs_claim_no_real_citrix_results():
    # Point 5: the Citrix evidence is synthetic. The docs for this rule say
    # so, and none of them reports a live Citrix result.
    surfaces = (DOCS / "SURFACES.md").read_text()
    section = surfaces.split("## Lookup capabilities", 1)[1]
    assert "synthetic" in section
    assert "no counted run against a live" in section
    for name in (
        "SURFACES.md",
        "QUALIFY_A_WORKFLOW.md",
        "QUALIFICATION_TERMS.md",
        "RESOLUTION_LADDER.md",
    ):
        text = (DOCS / name).read_text().lower()
        for claim in (
            "qualified on citrix",
            "live citrix run passed",
            "real citrix result",
        ):
            assert claim not in text, (name, claim)
