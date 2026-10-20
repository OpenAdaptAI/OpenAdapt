"""The compatibility matrix (openadapt_flow/compatibility.py, docs/COMPATIBILITY.md).

Every feature against every run profile either works or is refused with a
named reason. These tests check each cell against the runtime:

- a cell that works must engage the feature (the API step sends its request,
  the read-back runs on the side thread); a fallback fails the test;
- a refused cell must give exactly the reason the matrix names;
- every cell names tests that exist, and the published page is current.

The record system is a scripted verifier or the MockMed fault server; the
values are made up.
"""

from __future__ import annotations

import argparse
import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from openadapt_flow import compatibility as matrix
from openadapt_flow.approval_mode.gate import ApprovalGate
from openadapt_flow.approval_mode.item_approval import ItemApprovalStore
from openadapt_flow.deployment import DeploymentConfig, PolicySection
from openadapt_flow.execution_profiles import ExecutionProfile
from openadapt_flow.run_gate import build_runtime_authorization, evaluate_run_gate
from openadapt_flow.runtime.actuators.web_form import WebFormActuator
from openadapt_flow.runtime.effects.rest import RestRecordVerifier
from openadapt_flow.runtime.readback_pipeline import ReadbackPipeline
from tests.test_api_step_approval import APPROVE, Person
from tests.test_api_step_governed import PARAMS, Transport, _workflow
from tests.test_approval_mode_replayer import RecordSystem
from tests.test_execution_profiles import (
    _KEY,
    Replayer,
    _gate,
    _ReadyVision,
    _sealed,
)
from tests.test_replayer import FakeBackend

FLOW = Path(__file__).resolve().parents[1]
REPO = FLOW.parents[1]


class RestRecordSystem(RecordSystem, RestRecordVerifier):
    """The scripted record system, read the way a REST read is.

    The read-back pipeline defers only network reads of the system of record
    (REST, FHIR, GraphQL, SQL); this is the scripted system as one of them.
    """


class Run:
    """One item of the API-step program under one run profile."""

    def __init__(
        self,
        tmp_path: Path,
        profile: str,
        *,
        pipeline: Optional[ReadbackPipeline] = None,
    ) -> None:
        self.profile = profile
        self.durable = profile in {"durable", "governed", "qualified"}
        regulated = profile in {"governed", "qualified"}
        self.workflow, self.bundle = _sealed(tmp_path, _workflow(), encrypted=True)
        self.verifier = RestRecordSystem()
        if regulated:
            report = _gate(
                self.workflow,
                self.bundle,
                ExecutionProfile.REGULATED,
                verifier=self.verifier,
                durable=True,
            )
        else:
            report = evaluate_run_gate(
                self.workflow,
                bundle_dir=self.bundle,
                deployment=DeploymentConfig(policy=PolicySection(policy="permissive")),
                effect_verifier=self.verifier,
                effective_durable=self.durable,
            )
        assert report.passed, report.render()
        self.authorization = build_runtime_authorization(
            self.workflow, report, params=PARAMS
        )
        if profile == "qualified":
            # The fields a signed production admission binds into the run.
            self.authorization = self.authorization.model_copy(
                update={
                    "production_qualification_admission_id": "admission-1",
                    "production_qualification_admission_sha256": "1" * 64,
                    "production_qualification_evidence_identity_sha256": "2" * 64,
                    "production_qualification_runtime_validation_id": "validation-1",
                    "production_qualification_signer_registry_sha256": "3" * 64,
                    "production_qualification_signer_registry_revision": 1,
                    "production_qualification_signer_registry_expires_at": (
                        "2099-01-01T00:00:00+00:00"
                    ),
                    "production_qualification_authority_sha256": "4" * 64,
                }
            )
        self.run_dir = tmp_path / "run"
        self.person = Person([APPROVE])
        self.store = ItemApprovalStore(self.run_dir)
        self.person.store = self.store
        self.transport = Transport()
        self.pipeline = pipeline

    def replayer(self) -> Any:
        gate = ApprovalGate(
            "each_item",
            approval_store=self.store,
            approval_wait_s=30.0,
            approval_poll_s=1.0,
            sleep=self.person.sleep,
            monotonic=self.person.monotonic,
        )
        return Replayer(
            FakeBackend(),
            vision=_ReadyVision(),
            effect_verifier=self.verifier,
            governed_authorization=self.authorization,
            durable=self.durable,
            checkpoint_key=_KEY,
            require_settled=True,
            approval_gate=gate,
            web_form_actuator=WebFormActuator(self.transport),
            readback_pipeline=self.pipeline,
        )

    def run(self) -> Any:
        return self.replayer().run(
            self.workflow,
            params=PARAMS,
            bundle_dir=self.bundle,
            run_dir=self.run_dir,
        )


def _cells(feature: str, status: str) -> list[str]:
    return [
        c.profile for c in matrix.CELLS if c.feature == feature and c.status == status
    ]


# -- the matrix is complete and backed by tests -------------------------------------


def test_every_feature_has_exactly_one_cell_per_profile():
    pairs = [(c.feature, c.profile) for c in matrix.CELLS]
    assert len(pairs) == len(set(pairs))
    assert set(pairs) == {
        (f.key, p.key) for f in matrix.FEATURES for p in matrix.PROFILES
    }
    features = {f.key for f in matrix.FEATURES}
    for item in matrix.COMBINATIONS:
        assert item.feature in features and item.profile in features


@pytest.mark.parametrize(
    "item",
    [*matrix.CELLS, *matrix.COMBINATIONS],
    ids=lambda c: f"{c.feature}-{c.profile}",
)
def test_every_cell_names_a_reason_or_works_and_has_evidence(item):
    if item.status == "refused":
        assert item.reason, "a refused cell names its reason"
    else:
        assert item.reason is None
    assert item.evidence, "every cell names the tests that show it"
    for node in item.evidence:
        path, _, name = node.partition("::")
        source = FLOW / path
        assert source.is_file(), node
        names = {
            n.name
            for n in ast.walk(ast.parse(source.read_text()))
            if isinstance(n, ast.FunctionDef)
        }
        assert name in names, node


def test_the_published_page_is_current_and_linked():
    page = FLOW / "docs" / "COMPATIBILITY.md"
    assert page.read_text(encoding="utf-8") == matrix.render_markdown(), (
        "run python scripts/build_compatibility_doc.py"
    )
    assert (
        "packages/flow/docs/COMPATIBILITY.md" in (REPO / "CAPABILITIES.md").read_text()
    )


# -- the API step ---------------------------------------------------------------------


@pytest.mark.parametrize("profile", _cells("api_step", "works"))
def test_api_step_runs_where_the_matrix_says(tmp_path, profile):
    run = Run(tmp_path, profile)
    report = run.run()

    assert [r.actuation for r in report.results] == ["api_folded", "api"], [
        r.api_fallback for r in report.results
    ]
    assert all(r.api_fallback is None for r in report.results)
    assert [path for path, _ in run.transport.posts] == ["/save.php"]
    assert report.success is True
    (request,) = run.store.requests()
    assert request.form_state_kind == "api_request"
    if profile == "governed":
        assert report.execution_outcome == "VERIFIED"


@pytest.mark.parametrize("profile", _cells("api_step", "refused"))
def test_api_step_refuses_where_the_matrix_says(tmp_path, profile):
    reason = matrix.cell("api_step", profile).reason
    run = Run(tmp_path, "qualified" if profile == "qualified" else "default")
    if profile == "managed":
        replayer = run.replayer()
        replayer.managed_dispatch_binding = object()
        _end, _save, why = replayer._web_form_span_at(run.workflow, 0, durable=True)
        assert why == reason
        return
    report = run.run()
    assert run.transport.posts == []
    assert report.results[0].api_fallback == reason


def test_api_step_refuses_in_a_multi_surface_run(tmp_path):
    run = Run(tmp_path, "default")
    replayer = run.replayer()
    replayer._surface_router = object()
    _end, _save, why = replayer._web_form_span_at(run.workflow, 0, durable=False)
    assert why == matrix.API_MULTI_SURFACE


def test_the_repair_reason_is_the_runtimes():
    from openadapt_flow.repair.self_repair import REPAIR_GUI_ONLY

    assert matrix.API_REPAIR_PROBE == REPAIR_GUI_ONLY


# -- pipelined read-back ---------------------------------------------------------------


@pytest.mark.parametrize(
    "profile", [p.key for p in matrix.PROFILES if p.key != "managed"]
)
def test_pipelined_readback_where_the_matrix_says(tmp_path, profile):
    item = matrix.cell("pipelined_readback", profile)
    pipeline = ReadbackPipeline()
    try:
        run = Run(tmp_path, profile, pipeline=pipeline)
        report = run.run()
        handoff = pipeline.take()
        settled = handoff.settled.result(timeout=30) if handoff is not None else None
    finally:
        pipeline.close()

    assert report.results[-1].effect_verified is True or settled is not None
    if item.status == "works":
        assert settled is report, pipeline.inline_reason
        assert report.success is True
        assert report.results[-1].effect_verified is True
    else:
        assert settled is None
        assert pipeline.inline_reason == item.reason


# -- the worklist pool -------------------------------------------------------------------


@pytest.mark.parametrize("profile", [p.key for p in matrix.PROFILES])
def test_the_worklist_pool_where_the_matrix_says(tmp_path, profile):
    from openadapt_flow.cli_worklist_pool import pool_profile_refusal
    from openadapt_flow.runtime.worklist_pool import (
        FunctionWorker,
        PoolPolicy,
        run_worklist_pool,
    )

    refusal = pool_profile_refusal(
        managed=profile == "managed", qualification_case=False
    )
    workers = matrix.cell("parallel_workers", profile)
    if workers.status == "refused":
        assert refusal == workers.reason
        assert matrix.cell("pipelined_readback", profile).reason == refusal
        return
    assert refusal is None
    ran: list[int] = []
    pool = [
        FunctionWorker(f"w{i}", lambda idx, row, d: ran.append(idx) or _Done())
        for i in range(2)
    ]
    report = run_worklist_pool(
        [{"n": str(i)} for i in range(4)],
        pool,
        run_dir=tmp_path,
        policy=PoolPolicy(workers=2, items_independent=True),
    )
    assert sorted(ran) == [0, 1, 2, 3] and report.success


class _Done:
    success = True
    execution_outcome = "VERIFIED"
    transaction_outcome = "VERIFIED"
    halt = None
    total_ms = 1.0


def test_workers_run_governed_durable_api_items(tmp_path):
    """Two workers, each item a Regulated, durable, gated API-step run."""

    from openadapt_flow.runtime.worklist_pool import (
        VERIFIED,
        FunctionWorker,
        PoolPolicy,
        run_worklist_pool,
    )

    runs: dict[int, Run] = {}

    def item(idx: int, row: dict[str, Any], item_dir: Path) -> Any:
        runs[idx] = Run(item_dir, "governed")
        return runs[idx].run()

    report = run_worklist_pool(
        [{"n": str(i)} for i in range(4)],
        [FunctionWorker(f"w{i}", item) for i in range(2)],
        run_dir=tmp_path,
        policy=PoolPolicy(workers=2, items_independent=True),
    )
    assert {i["state"] for i in report.items} == {VERIFIED}, [
        i.get("detail") or i for i in report.items
    ]
    for run in runs.values():
        assert [path for path, _ in run.transport.posts] == ["/save.php"]


# -- self-repair and standard changes ----------------------------------------------------


def _repair_args(profile: str) -> argparse.Namespace:
    return argparse.Namespace(
        managed_dispatch_file="dispatch.json" if profile == "managed" else None,
        qualification_authority_file=(
            "authority.json" if profile == "qualified" else None
        ),
        _qualification_case_execution=None,
        pin_digest="a" * 64 if profile == "pinned" else None,
        hold_on_halt=False,
        save_healed_to=None,
        record_video=False,
        repair_agent="mod:factory",
        repair_screen_access_by="reviewer",
        repair_requalify="verify.yaml" if profile == "pinned" else None,
        durable=profile == "durable",
    )


@pytest.mark.parametrize("feature", ["self_repair", "standard_changes"])
@pytest.mark.parametrize("profile", [p.key for p in matrix.PROFILES])
def test_self_repair_where_the_matrix_says(feature, profile):
    from openadapt_flow.cli_self_repair import _refusal

    backend = SimpleNamespace(
        kind="web", browser_cdp_endpoint=None, browser_page_url=None
    )
    refusal = _refusal(_repair_args(profile), backend, "http://app")
    item = matrix.cell(feature, profile)
    if item.status == "refused":
        assert refusal == item.reason
    elif profile == "governed":
        # The CLI's Standard profile needs a signed admission (the
        # "qualified" column); the Standard contract runs through the
        # Python API, which these cells' evidence tests exercise.
        assert item.evidence
    else:
        assert refusal is None
