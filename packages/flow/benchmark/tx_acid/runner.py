"""Pluggable 50,000-row / 20-worker ACID benchmark runner and Markdown/JSON reporter.

Evaluates three head-to-head arms across ``ClinicalChartServer``,
``ReplaceTaskServer``, and ``ClaimsLedgerServer`` under continuous concurrent
background load:

1. ``naive_2xx`` (``NaiveAgent2xxArm``)
2. ``legacy_whole_table`` (``LegacyWholeTableContractArm``)
3. ``tx_acid`` (auto-imported ``openadapt_flow.tx`` commit engine when merged,
   caller-supplied ``tx_arm_factory``, or ``ReferenceTxArm`` fallback)
"""

from __future__ import annotations

import argparse
import inspect
import json
import math
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

if __package__ in (None, ""):
    _FLOW_DIR = Path(__file__).resolve().parents[2]
    if str(_FLOW_DIR) not in sys.path:
        sys.path.insert(0, str(_FLOW_DIR))
    import benchmark as _bm_pkg

    _bm_dir = str(_FLOW_DIR / "benchmark")
    if hasattr(_bm_pkg, "__path__") and _bm_dir not in _bm_pkg.__path__:
        _bm_pkg.__path__.insert(0, _bm_dir)
    __package__ = "benchmark.tx_acid"

from openadapt_flow.tx.types import (  # noqa: E402
    CertifiedLensPack,
    EntityLensSpec,
    TxCommitResult,
    TxTask,
)

from .baselines import (  # noqa: E402
    ArmExecutionResult,
    BenchmarkArm,
    LegacyWholeTableContractArm,
    NaiveAgent2xxArm,
    ReferenceTxArm,
    _format_endpoint_path,
    _http_json_request,
    _ReferenceJudge,
)
from .servers import (  # noqa: E402
    ALL_BENCHMARK_FAULTS,
    BackgroundTrafficGenerator,
    BaseSorServer,
    ClaimsLedgerServer,
    ClinicalChartServer,
    FaultConfig,
    FaultInjectingProxy,
    FaultKind,
    ReplaceTaskServer,
)

TxArmFactory = Callable[..., BenchmarkArm]


@dataclass(frozen=True)
class WorkloadItem:
    """Single clean or fault-injected benchmark scenario."""

    task: TxTask
    fault_kind: Optional[FaultKind] = None
    cleanup_dup_field: Optional[str] = None
    cleanup_dup_value: Optional[str] = None


@dataclass(frozen=True)
class ArmMetricsSummary:
    """Per-arm metrics for one System of Record or aggregated across all apps."""

    arm_name: str
    app_name: str
    clean_total: int
    clean_passed: int
    false_stops: int
    clean_silent_corruptions: int
    clean_pass_rate: float
    false_stop_rate: float
    fault_total: int
    faults_caught: int
    false_accepts: int
    fault_catch_rate: float
    false_accept_rate: float
    per_fault_catch_rate: dict[str, float]
    mean_rows_read_per_tx: float
    p50_ms: float
    p95_ms: float
    background_concurrent_writes_landed: int


@dataclass(frozen=True)
class AppBenchmarkReport:
    """Benchmark results for one System-of-Record server."""

    app_name: str
    server_class: str
    seed_rows: int
    seed_duration_ms: float
    journal_mode: str
    num_background_workers: int
    background_concurrent_writes_landed: int
    background_creates_landed: int
    background_updates_landed: int
    background_errors: int
    arms: dict[str, ArmMetricsSummary]


@dataclass(frozen=True)
class BenchmarkSuiteReport:
    """Top-level report across all three Systems of Record."""

    suite_name: str
    seed_rows: int
    num_background_workers: int
    tx_backend: str
    total_background_writes_landed: int
    total_duration_s: float
    apps: dict[str, AppBenchmarkReport]
    aggregate_by_arm: dict[str, ArmMetricsSummary] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    rank = (pct / 100.0) * (len(ordered) - 1)
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return round(ordered[lo], 3)
    weight = rank - lo
    return round(ordered[lo] * (1.0 - weight) + ordered[hi] * weight, 3)


def _try_import_tx_components() -> Optional[dict[str, Any]]:
    """Auto-import merged openadapt_flow.tx components when available."""
    import openadapt_flow.tx as tx_pkg

    engine_cls = getattr(tx_pkg, "TransactionalCommitEngine", None)
    tap_cls = getattr(tx_pkg, "SessionWireTap", None)
    judge_cls = getattr(tx_pkg, "FrameAndIntentJudge", None)
    induce_fn = getattr(tx_pkg, "induce_lens_from_har", None)
    certify_fn = getattr(tx_pkg, "certify_lens_on_sandbox", None)

    if engine_cls is None:
        try:
            mod_eng = __import__(
                "openadapt_flow.tx.engine", fromlist=["TransactionalCommitEngine"]
            )
            engine_cls = getattr(mod_eng, "TransactionalCommitEngine", None)
        except Exception:
            engine_cls = None
    if tap_cls is None:
        try:
            mod_tap = __import__(
                "openadapt_flow.tx.wire_tap", fromlist=["SessionWireTap"]
            )
            tap_cls = getattr(mod_tap, "SessionWireTap", None)
        except Exception:
            tap_cls = None
    if judge_cls is None:
        try:
            mod_judge = __import__(
                "openadapt_flow.tx.judge", fromlist=["FrameAndIntentJudge"]
            )
            judge_cls = getattr(mod_judge, "FrameAndIntentJudge", None)
        except Exception:
            judge_cls = None
    if induce_fn is None:
        for mod_name in ("openadapt_flow.tx.induce", "openadapt_flow.tx.har"):
            try:
                mod = __import__(mod_name, fromlist=["induce_lens_from_har"])
                induce_fn = getattr(mod, "induce_lens_from_har", None)
                if induce_fn is not None:
                    break
            except Exception:
                continue
    if certify_fn is None:
        for mod_name in ("openadapt_flow.tx.certify", "openadapt_flow.tx.sandbox"):
            try:
                mod = __import__(mod_name, fromlist=["certify_lens_on_sandbox"])
                certify_fn = getattr(mod, "certify_lens_on_sandbox", None)
                if certify_fn is not None:
                    break
            except Exception:
                continue

    if engine_cls is None or tap_cls is None or judge_cls is None:
        return None
    return {
        "TransactionalCommitEngine": engine_cls,
        "SessionWireTap": tap_cls,
        "FrameAndIntentJudge": judge_cls,
        "induce_lens_from_har": induce_fn,
        "certify_lens_on_sandbox": certify_fn,
    }


def _instantiate_with_matching_kwargs(
    cls_or_fn: Any, candidates: Mapping[str, Any]
) -> Any:
    sig = inspect.signature(cls_or_fn)
    params = sig.parameters
    accepts_var_kw = any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
    )
    kwargs: dict[str, Any] = {}
    for name, param in params.items():
        if name == "self":
            continue
        if name in candidates:
            kwargs[name] = candidates[name]
    if kwargs or not params:
        return cls_or_fn(**kwargs)
    if accepts_var_kw:
        return cls_or_fn(**candidates)
    return cls_or_fn()


class _AutoImportedOpenAdaptTxArm:
    """Adapter driving merged openadapt_flow.tx classes with signature introspection."""

    name: str = "tx_acid"

    def __init__(self, components: Mapping[str, Any]) -> None:
        self._components = dict(components)
        self._fallback = ReferenceTxArm()
        self._server_ctx: dict[str, tuple[Any, CertifiedLensPack, Any]] = {}

    def close(self) -> None:
        for tap, _, _ in self._server_ctx.values():
            for closer in ("close", "stop"):
                if hasattr(tap, closer):
                    try:
                        getattr(tap, closer)()
                    except Exception:
                        pass
                    break
        self._server_ctx.clear()
        self._fallback.close()

    def _get_or_create_ctx(
        self, server: BaseSorServer
    ) -> tuple[Any, CertifiedLensPack, Any]:
        cached = self._server_ctx.get(server.base_url)
        if cached is not None:
            return cached

        tap_cls = self._components["SessionWireTap"]
        judge_cls = self._components["FrameAndIntentJudge"]
        engine_cls = self._components["TransactionalCommitEngine"]
        induce_fn = self._components.get("induce_lens_from_har")
        certify_fn = self._components.get("certify_lens_on_sandbox")

        lens: EntityLensSpec = server.lens_spec()
        if callable(induce_fn):
            try:
                har_doc = {"log": {"entries": server.sample_har_entries()}}
                induced = induce_fn(har_doc)
                if (
                    isinstance(induced, EntityLensSpec)
                    and induced.duplicate_search is not None
                ):
                    lens = induced
            except Exception:
                pass

        if not callable(certify_fn):
            raise RuntimeError("transaction benchmark requires the real qualifier")
        sample_rec = server.make_background_create_payload(999, 1)
        _, sample_upd = server.make_background_update_payload(999, 1)
        pack = certify_fn(
            lens,
            sample_record=sample_rec,
            sample_update=sample_upd,
            auth_headers={"X-Actor-Id": "tx-certifier"},
        )
        if not isinstance(pack, CertifiedLensPack) or pack.admission_errors():
            raise RuntimeError("transaction benchmark qualification failed")
        lens = pack.lens

        tap = _instantiate_with_matching_kwargs(
            tap_cls,
            {
                "upstream_url": server.base_url,
                "target_url": server.base_url,
                "target_base_url": server.base_url,
                "base_url": server.base_url,
            },
        )
        if hasattr(tap, "start"):
            tap.start()
        elif hasattr(tap, "__enter__"):
            tap.__enter__()

        judge = judge_cls()
        engine = _instantiate_with_matching_kwargs(
            engine_cls,
            {
                "lens": pack,
                "lens_spec": lens,
                "pack": pack,
                "lens_pack": pack,
                "certified_pack": pack,
                "wire_tap": tap,
                "tap": tap,
                "judge": judge,
            },
        )
        ctx = (tap, pack, engine)
        self._server_ctx[server.base_url] = ctx
        return ctx

    def execute(
        self,
        server: BaseSorServer,
        task: TxTask,
        *,
        fault_proxy: Optional[FaultInjectingProxy] = None,
    ) -> ArmExecutionResult:
        tap, pack, engine = self._get_or_create_ctx(server)
        lens = pack.lens

        # Build safe merged payload from engine's own pre-read rules (never touching DB directly)
        client = engine.client

        def _agent_action(*args: Any, **kwargs: Any) -> None:
            tap_url = str(args[0]) if args else str(tap.base_url)
            if fault_proxy is not None:
                fault_proxy.set_upstream(tap_url)
                dest = fault_proxy.base_url
            else:
                dest = tap_url
            if task.operation == "create" and lens.create_endpoint is not None:
                _http_json_request(
                    dest,
                    lens.create_endpoint.method,
                    lens.create_endpoint.path_template,
                    body=dict(task.intended_delta),
                    headers={"X-Actor-Id": task.actor_id},
                )
            elif task.operation == "update" and task.entity_id is not None:
                path = _format_endpoint_path(
                    lens.write_endpoint.path_template,
                    task.entity_id,
                )
                payload = dict(task.intended_delta)
                hdrs = {"X-Actor-Id": task.actor_id}
                if lens.write_mode == "replace" or lens.write_endpoint.etag_header:
                    r0_resp, r0_now, _ = client.get_one(task.entity_id)
                    if r0_now is not None and lens.write_mode == "replace":
                        payload = {**r0_now, **task.intended_delta}
                    if r0_resp.etag:
                        hdrs["If-Match"] = r0_resp.etag
                    elif r0_now is not None and "etag" in r0_now:
                        hdrs["If-Match"] = str(r0_now["etag"])
                _http_json_request(
                    dest,
                    lens.write_endpoint.method,
                    path,
                    body=payload,
                    headers=hdrs,
                )

        if hasattr(engine, "wrap_action"):
            res = engine.wrap_action(task, _agent_action)
        else:
            res = engine.execute(task)

        if not isinstance(res, TxCommitResult):
            raise TypeError(f"Expected TxCommitResult, got {type(res)!r}")
        return ArmExecutionResult(
            arm_name=self.name,
            task_id=task.task_id,
            accepted=(res.outcome == "CONFIRMED"),
            outcome_label=res.outcome,
            rows_read=res.rows_read,
            duration_ms=res.duration_ms,
            reason=";".join(res.reasons),
            occ_status=res.occ_status,
        )


class _FactoryWrappedArm:
    """Adapter around a user-supplied tx_arm_factory (0-arg or 1-arg server factory)."""

    name: str = "tx_acid"

    def __init__(self, factory: TxArmFactory) -> None:
        self._factory = factory
        sig = inspect.signature(factory)
        self._takes_server = len(sig.parameters) >= 1
        self._singleton: Optional[BenchmarkArm] = None
        self._per_server: dict[str, BenchmarkArm] = {}
        if not self._takes_server:
            self._singleton = factory()

    def close(self) -> None:
        if self._singleton is not None and hasattr(self._singleton, "close"):
            self._singleton.close()
        for arm in self._per_server.values():
            if hasattr(arm, "close"):
                arm.close()
        self._per_server.clear()

    def execute(
        self,
        server: BaseSorServer,
        task: TxTask,
        *,
        fault_proxy: Optional[FaultInjectingProxy] = None,
    ) -> ArmExecutionResult:
        if self._takes_server:
            arm = self._per_server.get(server.base_url)
            if arm is None:
                arm = self._factory(server)
                self._per_server[server.base_url] = arm
        else:
            arm = self._singleton
        assert arm is not None
        return arm.execute(server, task, fault_proxy=fault_proxy)


def build_tx_arm(
    tx_arm_factory: Optional[TxArmFactory] = None,
) -> tuple[BenchmarkArm, str]:
    """Resolve the tx benchmark arm and a descriptive backend identifier."""
    if tx_arm_factory is not None:
        return _FactoryWrappedArm(tx_arm_factory), "custom_tx_arm_factory"
    auto = _try_import_tx_components()
    if auto is not None:
        return _AutoImportedOpenAdaptTxArm(auto), "openadapt_flow.tx"
    return ReferenceTxArm(), "reference_tx_types"


def build_clean_workload(server: BaseSorServer, count: int = 12) -> list[WorkloadItem]:
    """Construct realistic clean update + create tasks exercising server quirks."""
    items: list[WorkloadItem] = []

    if isinstance(server, ClinicalChartServer):
        templates: list[WorkloadItem] = [
            WorkloadItem(
                task=TxTask(
                    task_id="ehr-clean-confusable-short",
                    operation="update",
                    entity_id="PT-0O1l8",
                    expected_identity={"fname": "Bo", "lname": "Li"},
                    intended_delta={
                        "street": "7 mill rd",
                        "city": "cambridge",
                        "state": "Massachusetts",
                    },
                )
            ),
            WorkloadItem(
                task=TxTask(
                    task_id="ehr-clean-amy-ca",
                    operation="update",
                    entity_id="PT-000002",
                    expected_identity={"fname": "Amy", "lname": "Wu"},
                    intended_delta={
                        "street": "250 market st",
                        "city": "san francisco",
                        "state": "California",
                    },
                )
            ),
            WorkloadItem(
                task=TxTask(
                    task_id="ehr-clean-al-ontario",
                    operation="update",
                    entity_id="PT-000003",
                    expected_identity={"fname": "Al", "lname": "Ng"},
                    intended_delta={
                        "street": "99 queen st w",
                        "city": "toronto",
                        "state": "Ontario",
                    },
                )
            ),
            WorkloadItem(
                task=TxTask(
                    task_id="ehr-clean-titlecase",
                    operation="update",
                    entity_id="PT-000004",
                    expected_identity={},
                    intended_delta={
                        "fname": "bo",
                        "lname": "chen",
                        "street": "18 elm ave",
                        "state": "New York",
                    },
                )
            ),
            WorkloadItem(
                task=TxTask(
                    task_id="ehr-clean-create-bo",
                    operation="create",
                    intended_delta={
                        "fname": "bo",
                        "lname": "li",
                        "email": "new.bo.li@clinic.example",
                        "street": "14 beacon st",
                        "city": "boston",
                        "state": "Massachusetts",
                        "allergies": "Penicillin",
                        "notes": "Clean admission intake",
                    },
                ),
                cleanup_dup_field="email",
                cleanup_dup_value="new.bo.li@clinic.example",
            ),
            WorkloadItem(
                task=TxTask(
                    task_id="ehr-clean-create-amy",
                    operation="create",
                    intended_delta={
                        "fname": "amy",
                        "lname": "wu",
                        "email": "new.amy.wu@clinic.example",
                        "street": "500 university ave",
                        "city": "toronto",
                        "state": "Ontario",
                        "allergies": "None",
                        "notes": "Clean referral intake",
                    },
                ),
                cleanup_dup_field="email",
                cleanup_dup_value="new.amy.wu@clinic.example",
            ),
        ]
        state_cycle = ("Massachusetts", "California", "Ontario", "Texas")
        for i in range(len(templates), count):
            row_idx = 5 + (i % 40)
            templates.append(
                WorkloadItem(
                    task=TxTask(
                        task_id=f"ehr-clean-upd-{i}",
                        operation="update",
                        entity_id=f"PT-{row_idx:06d}",
                        intended_delta={
                            "street": f"{10 + i} harbor way",
                            "state": state_cycle[i % len(state_cycle)],
                        },
                    )
                )
            )
        items.extend(templates[:count])
        return items

    if isinstance(server, ReplaceTaskServer):
        templates = [
            WorkloadItem(
                task=TxTask(
                    task_id="erp-clean-partial-status",
                    operation="update",
                    entity_id="TSK-0O1l8",
                    expected_identity={"external_ref": "REF-000000"},
                    intended_delta={"status": "completed"},
                )
            ),
            WorkloadItem(
                task=TxTask(
                    task_id="erp-clean-partial-short-assignee",
                    operation="update",
                    entity_id="TSK-000002",
                    expected_identity={"external_ref": "REF-000002"},
                    intended_delta={"assignee": "Bo Li", "status": "completed"},
                )
            ),
            WorkloadItem(
                task=TxTask(
                    task_id="erp-clean-partial-budget",
                    operation="update",
                    entity_id="TSK-000003",
                    expected_identity={"external_ref": "REF-000003"},
                    intended_delta={"budget_cents": 625000},
                )
            ),
            WorkloadItem(
                task=TxTask(
                    task_id="erp-clean-partial-duedate",
                    operation="update",
                    entity_id="TSK-000004",
                    intended_delta={"due_date": "2026-12-29"},
                )
            ),
            WorkloadItem(
                task=TxTask(
                    task_id="erp-clean-create-1",
                    operation="create",
                    intended_delta={
                        "external_ref": "CLEAN-ERP-CREATE-1",
                        "title": "Controlled Formulary Signoff",
                        "status": "open",
                        "description": "Audit controlled substance vault counts",
                        "assignee": "Bo Li",
                        "due_date": "2026-12-24",
                        "labels": "regulated,vault",
                        "budget_cents": 140000,
                        "department": "PHARMACY_OPS",
                    },
                ),
                cleanup_dup_field="external_ref",
                cleanup_dup_value="CLEAN-ERP-CREATE-1",
            ),
            WorkloadItem(
                task=TxTask(
                    task_id="erp-clean-create-2",
                    operation="create",
                    intended_delta={
                        "external_ref": "CLEAN-ERP-CREATE-2",
                        "title": "Biologics Freezer Qualification",
                        "status": "in_progress",
                        "description": "Validate ultra-low freezer alarm relay",
                        "assignee": "Amy Wu",
                        "due_date": "2026-12-27",
                        "labels": "qa,biologics",
                        "budget_cents": 98000,
                        "department": "CLINICAL_QA",
                    },
                ),
                cleanup_dup_field="external_ref",
                cleanup_dup_value="CLEAN-ERP-CREATE-2",
            ),
        ]
        for i in range(len(templates), count):
            row_idx = 5 + (i % 40)
            templates.append(
                WorkloadItem(
                    task=TxTask(
                        task_id=f"erp-clean-partial-{i}",
                        operation="update",
                        entity_id=f"TSK-{row_idx:06d}",
                        intended_delta={"status": "completed"},
                    )
                )
            )
        items.extend(templates[:count])
        return items

    if isinstance(server, ClaimsLedgerServer):
        templates = [
            WorkloadItem(
                task=TxTask(
                    task_id="clm-clean-phone-date",
                    operation="update",
                    entity_id="CLM-0O1l8",
                    expected_identity={"claim_number": "CN-2026-000000"},
                    intended_delta={
                        "member_phone": "(416) 555-0199",
                        "service_date": "2026-11-19",
                        "status": "approved",
                    },
                )
            ),
            WorkloadItem(
                task=TxTask(
                    task_id="clm-clean-short-claimant",
                    operation="update",
                    entity_id="CLM-000002",
                    expected_identity={"claim_number": "CN-2026-000002"},
                    intended_delta={
                        "claimant_name": "bo li",
                        "member_phone": "(617) 555-0144",
                        "service_date": "2026-11-20",
                    },
                )
            ),
            WorkloadItem(
                task=TxTask(
                    task_id="clm-clean-amount-date",
                    operation="update",
                    entity_id="CLM-000003",
                    expected_identity={"claim_number": "CN-2026-000003"},
                    intended_delta={
                        "amount_cents": 72500,
                        "service_date": "2026-11-21",
                        "status": "paid",
                    },
                )
            ),
            WorkloadItem(
                task=TxTask(
                    task_id="clm-clean-phone-e164",
                    operation="update",
                    entity_id="CLM-000004",
                    intended_delta={
                        "member_phone": "1-416-555-0177",
                        "status": "adjudicated",
                    },
                )
            ),
            WorkloadItem(
                task=TxTask(
                    task_id="clm-clean-create-1",
                    operation="create",
                    intended_delta={
                        "claim_number": "CLEAN-CLM-CREATE-1",
                        "policy_id": "POL-700001",
                        "claimant_name": "bo li",
                        "member_phone": "(416) 555-0199",
                        "service_date": "2026-11-19",
                        "status": "submitted",
                        "amount_cents": 54000,
                        "diagnosis_code": "J06.9",
                        "adjudication_note": "Clean electronic EDI submission",
                    },
                ),
                cleanup_dup_field="claim_number",
                cleanup_dup_value="CLEAN-CLM-CREATE-1",
            ),
            WorkloadItem(
                task=TxTask(
                    task_id="clm-clean-create-2",
                    operation="create",
                    intended_delta={
                        "claim_number": "CLEAN-CLM-CREATE-2",
                        "policy_id": "POL-700002",
                        "claimant_name": "amy wu",
                        "member_phone": "(617) 555-0123",
                        "service_date": "2026-11-22",
                        "status": "submitted",
                        "amount_cents": 89000,
                        "diagnosis_code": "I10",
                        "adjudication_note": "Clean specialist claim",
                    },
                ),
                cleanup_dup_field="claim_number",
                cleanup_dup_value="CLEAN-CLM-CREATE-2",
            ),
        ]
        for i in range(len(templates), count):
            row_idx = 5 + (i % 40)
            templates.append(
                WorkloadItem(
                    task=TxTask(
                        task_id=f"clm-clean-upd-{i}",
                        operation="update",
                        entity_id=f"CLM-{row_idx:06d}",
                        intended_delta={
                            "member_phone": f"(416) 555-{1000 + i:04d}",
                            "service_date": "2026-11-19",
                        },
                    )
                )
            )
        items.extend(templates[:count])
        return items

    raise TypeError(f"Unsupported server class: {type(server).__name__}")


def build_fault_workload(
    server: BaseSorServer,
    fault_repeats: int = 2,
) -> list[WorkloadItem]:
    """Construct planted fault workload covering all 6 write-path faults + OCC race."""
    items: list[WorkloadItem] = []

    for rep in range(fault_repeats):
        for fk in ALL_BENCHMARK_FAULTS:
            if fk == "duplicate_post_on_retry":
                if isinstance(server, ClinicalChartServer):
                    dup_val = f"fault.dup.{rep}@clinic.example"
                    items.append(
                        WorkloadItem(
                            task=TxTask(
                                task_id=f"ehr-fault-{fk}-{rep}",
                                operation="create",
                                intended_delta={
                                    "fname": "Elena",
                                    "lname": "Patel",
                                    "email": dup_val,
                                    "street": "22 oak st",
                                    "city": "boston",
                                    "state": "Massachusetts",
                                    "allergies": "None",
                                    "notes": "Retry duplicate probe",
                                },
                            ),
                            fault_kind=fk,
                            cleanup_dup_field="email",
                            cleanup_dup_value=dup_val,
                        )
                    )
                elif isinstance(server, ReplaceTaskServer):
                    dup_val = f"FAULT-DUP-REF-{rep}"
                    items.append(
                        WorkloadItem(
                            task=TxTask(
                                task_id=f"erp-fault-{fk}-{rep}",
                                operation="create",
                                intended_delta={
                                    "external_ref": dup_val,
                                    "title": "Retry Duplicate Probe Task",
                                    "status": "open",
                                    "description": "Ensure POST retry duplicate caught",
                                    "assignee": "Elena Chen",
                                    "due_date": "2026-12-28",
                                    "labels": "probe",
                                    "budget_cents": 50000,
                                    "department": "CLINICAL_QA",
                                },
                            ),
                            fault_kind=fk,
                            cleanup_dup_field="external_ref",
                            cleanup_dup_value=dup_val,
                        )
                    )
                elif isinstance(server, ClaimsLedgerServer):
                    dup_val = f"FAULT-DUP-CN-{rep}"
                    items.append(
                        WorkloadItem(
                            task=TxTask(
                                task_id=f"clm-fault-{fk}-{rep}",
                                operation="create",
                                intended_delta={
                                    "claim_number": dup_val,
                                    "policy_id": "POL-888888",
                                    "claimant_name": "Elena Chen",
                                    "member_phone": "(416) 555-0199",
                                    "service_date": "2026-11-19",
                                    "status": "submitted",
                                    "amount_cents": 41000,
                                    "diagnosis_code": "J06.9",
                                    "adjudication_note": "Duplicate POST probe",
                                },
                            ),
                            fault_kind=fk,
                            cleanup_dup_field="claim_number",
                            cleanup_dup_value=dup_val,
                        )
                    )
                continue

            # Update-path faults target confusable primary ID or low-index target rows
            if isinstance(server, ClinicalChartServer):
                target_id = (
                    "PT-0O1l8" if fk == "wrong_target_record" else f"PT-{10 + rep:06d}"
                )
                items.append(
                    WorkloadItem(
                        task=TxTask(
                            task_id=f"ehr-fault-{fk}-{rep}",
                            operation="update",
                            entity_id=target_id,
                            intended_delta={
                                "street": f"{300 + rep} beacon st",
                                "state": "Massachusetts",
                            },
                        ),
                        fault_kind=fk,
                    )
                )
            elif isinstance(server, ReplaceTaskServer):
                target_id = (
                    "TSK-0O1l8"
                    if fk == "wrong_target_record"
                    else f"TSK-{10 + rep:06d}"
                )
                items.append(
                    WorkloadItem(
                        task=TxTask(
                            task_id=f"erp-fault-{fk}-{rep}",
                            operation="update",
                            entity_id=target_id,
                            intended_delta={"status": "completed"},
                        ),
                        fault_kind=fk,
                    )
                )
            elif isinstance(server, ClaimsLedgerServer):
                target_id = (
                    "CLM-0O1l8"
                    if fk == "wrong_target_record"
                    else f"CLM-{10 + rep:06d}"
                )
                items.append(
                    WorkloadItem(
                        task=TxTask(
                            task_id=f"clm-fault-{fk}-{rep}",
                            operation="update",
                            entity_id=target_id,
                            intended_delta={
                                "status": "approved",
                                "member_phone": "(416) 555-0199",
                            },
                        ),
                        fault_kind=fk,
                    )
                )
    return items


def _collateral_and_wrong_ids(server: BaseSorServer) -> tuple[str, str]:
    if isinstance(server, ClinicalChartServer):
        return "PT-000050", "PT-0O118"
    if isinstance(server, ReplaceTaskServer):
        return "TSK-000050", "TSK-0O118"
    if isinstance(server, ClaimsLedgerServer):
        return "CLM-000050", "CLM-0O118"
    return "ENT-000050", "ENT-0O118"


def _verify_ground_truth_state(
    server: BaseSorServer,
    item: WorkloadItem,
    pre_snapshot: Mapping[str, Optional[dict[str, Any]]],
    collateral_id: str,
    wrong_id: str,
    oracle_judge: _ReferenceJudge,
) -> bool:
    """Check directly against SQLite whether the intended transaction truly & cleanly landed."""
    lens = server.lens_spec()

    # Neither collateral_id nor wrong_id (unless wrong_id IS target) may change!
    for bystander_id in (collateral_id, wrong_id):
        if bystander_id == item.task.entity_id:
            continue
        if server.get_record_direct(bystander_id) != pre_snapshot.get(bystander_id):
            return False

    if item.task.operation == "update":
        assert item.task.entity_id is not None
        r0 = pre_snapshot.get(item.task.entity_id)
        r1 = server.get_record_direct(item.task.entity_id)
        if r0 is None or r1 is None:
            return False
        # Version must increment by exactly 1 (version jump >1 means a concurrent race was overwritten)
        if int(r1.get("version", 0)) != int(r0.get("version", 0)) + 1:
            return False
        verdict = oracle_judge.evaluate(
            r0,
            r1,
            item.task.intended_delta,
            volatile_fields=lens.volatile_fields,
            field_codecs=lens.field_codecs,
        )
        return verdict.passed

    if item.cleanup_dup_field and item.cleanup_dup_value is not None:
        matches = server.find_records_direct(
            item.cleanup_dup_field, item.cleanup_dup_value
        )
        if len(matches) != 1:
            return False
        verdict = oracle_judge.evaluate(
            None,
            matches[0],
            item.task.intended_delta,
            volatile_fields=lens.volatile_fields,
            field_codecs=lens.field_codecs,
        )
        return verdict.passed

    return False


def _summarize_arm_metrics(
    *,
    arm_name: str,
    app_name: str,
    clean_outcomes: Sequence[tuple[ArmExecutionResult, bool]],
    fault_outcomes: Sequence[tuple[FaultKind, ArmExecutionResult]],
    background_writes: int,
) -> ArmMetricsSummary:
    clean_total = len(clean_outcomes)
    clean_passed = sum(1 for res, gt_ok in clean_outcomes if res.accepted and gt_ok)
    false_stops = sum(1 for res, _ in clean_outcomes if not res.accepted)
    silent_corruptions = sum(
        1 for res, gt_ok in clean_outcomes if res.accepted and not gt_ok
    )

    fault_total = len(fault_outcomes)
    faults_caught = sum(1 for _, res in fault_outcomes if not res.accepted)
    false_accepts = sum(1 for _, res in fault_outcomes if res.accepted)

    by_fault_totals: dict[str, int] = {}
    by_fault_caught: dict[str, int] = {}
    for fk, res in fault_outcomes:
        by_fault_totals[fk] = by_fault_totals.get(fk, 0) + 1
        if not res.accepted:
            by_fault_caught[fk] = by_fault_caught.get(fk, 0) + 1
    per_fault_rates = {
        fk: round(by_fault_caught.get(fk, 0) / tot, 4)
        for fk, tot in by_fault_totals.items()
        if tot > 0
    }

    all_results = [r for r, _ in clean_outcomes] + [r for _, r in fault_outcomes]
    mean_rows = (
        round(sum(r.rows_read for r in all_results) / len(all_results), 3)
        if all_results
        else 0.0
    )
    latencies = [r.duration_ms for r in all_results]

    return ArmMetricsSummary(
        arm_name=arm_name,
        app_name=app_name,
        clean_total=clean_total,
        clean_passed=clean_passed,
        false_stops=false_stops,
        clean_silent_corruptions=silent_corruptions,
        clean_pass_rate=round(clean_passed / clean_total, 4) if clean_total else 0.0,
        false_stop_rate=round(false_stops / clean_total, 4) if clean_total else 0.0,
        fault_total=fault_total,
        faults_caught=faults_caught,
        false_accepts=false_accepts,
        fault_catch_rate=round(faults_caught / fault_total, 4) if fault_total else 0.0,
        false_accept_rate=round(false_accepts / fault_total, 4) if fault_total else 0.0,
        per_fault_catch_rate=per_fault_rates,
        mean_rows_read_per_tx=mean_rows,
        p50_ms=_percentile(latencies, 50.0),
        p95_ms=_percentile(latencies, 95.0),
        background_concurrent_writes_landed=background_writes,
    )


def run_tx_acid_benchmark(
    *,
    seed_rows: int = 50_000,
    num_workers: int = 20,
    clean_tasks_per_app: int = 12,
    fault_repeats: int = 2,
    bg_interval_s: float = 0.002,
    tx_arm_factory: Optional[TxArmFactory] = None,
) -> BenchmarkSuiteReport:
    """Run the 3-app x 3-arm ACID benchmark suite with concurrent background writers."""
    t_suite_start = time.perf_counter()
    oracle_judge = _ReferenceJudge()
    tx_arm, tx_backend = build_tx_arm(tx_arm_factory)
    arms: list[BenchmarkArm] = [
        NaiveAgent2xxArm(),
        LegacyWholeTableContractArm(),
        tx_arm,
    ]

    server_classes: tuple[type[BaseSorServer], ...] = (
        ClinicalChartServer,
        ReplaceTaskServer,
        ClaimsLedgerServer,
    )

    app_reports: dict[str, AppBenchmarkReport] = {}
    agg_clean: dict[str, list[tuple[ArmExecutionResult, bool]]] = {
        a.name: [] for a in arms
    }
    agg_fault: dict[str, list[tuple[FaultKind, ArmExecutionResult]]] = {
        a.name: [] for a in arms
    }
    total_bg_writes = 0

    try:
        for srv_cls in server_classes:
            with srv_cls(seed_rows=seed_rows) as server:
                collateral_id, wrong_id = _collateral_and_wrong_ids(server)
                clean_items = build_clean_workload(server, count=clean_tasks_per_app)
                fault_items = build_fault_workload(server, fault_repeats=fault_repeats)

                per_arm_clean: dict[str, list[tuple[ArmExecutionResult, bool]]] = {
                    a.name: [] for a in arms
                }
                per_arm_fault: dict[str, list[tuple[FaultKind, ArmExecutionResult]]] = {
                    a.name: [] for a in arms
                }

                with (
                    FaultInjectingProxy(server.base_url, server=server) as fault_proxy,
                    BackgroundTrafficGenerator(
                        server,
                        num_workers=num_workers,
                        interval_s=bg_interval_s,
                    ) as bg,
                ):
                    # Ensure background workers are actively landing concurrent commits
                    if num_workers > 0:
                        deadline = time.perf_counter() + 0.5
                        while (
                            bg.ops_completed < num_workers
                            and time.perf_counter() < deadline
                        ):
                            time.sleep(0.005)

                    for item in clean_items:
                        watch_ids = [collateral_id, wrong_id]
                        if item.task.entity_id:
                            watch_ids.append(item.task.entity_id)
                        for arm in arms:
                            snap = server.snapshot_records(watch_ids)
                            if item.cleanup_dup_field and item.cleanup_dup_value:
                                server.delete_where_field(
                                    item.cleanup_dup_field, item.cleanup_dup_value
                                )
                            res = arm.execute(server, item.task, fault_proxy=None)
                            gt_ok = _verify_ground_truth_state(
                                server,
                                item,
                                snap,
                                collateral_id,
                                wrong_id,
                                oracle_judge,
                            )
                            per_arm_clean[arm.name].append((res, gt_ok))
                            agg_clean[arm.name].append((res, gt_ok))

                            server.restore_records(snap)
                            if item.cleanup_dup_field and item.cleanup_dup_value:
                                server.delete_where_field(
                                    item.cleanup_dup_field, item.cleanup_dup_value
                                )

                    for item in fault_items:
                        assert item.fault_kind is not None
                        watch_ids = [collateral_id, wrong_id]
                        if item.task.entity_id:
                            watch_ids.append(item.task.entity_id)
                        fault_proxy.set_fault(
                            FaultConfig(
                                fault_kind=item.fault_kind,
                                collateral_id=collateral_id,
                                wrong_target_id=wrong_id,
                            )
                        )
                        for arm in arms:
                            snap = server.snapshot_records(watch_ids)
                            if item.cleanup_dup_field and item.cleanup_dup_value:
                                server.delete_where_field(
                                    item.cleanup_dup_field, item.cleanup_dup_value
                                )
                            res = arm.execute(
                                server, item.task, fault_proxy=fault_proxy
                            )
                            per_arm_fault[arm.name].append((item.fault_kind, res))
                            agg_fault[arm.name].append((item.fault_kind, res))

                            server.restore_records(snap)
                            if item.cleanup_dup_field and item.cleanup_dup_value:
                                server.delete_where_field(
                                    item.cleanup_dup_field, item.cleanup_dup_value
                                )

                bg_landed = bg.ops_completed
                total_bg_writes += bg_landed
                arm_summaries = {
                    arm.name: _summarize_arm_metrics(
                        arm_name=arm.name,
                        app_name=server.app_name,
                        clean_outcomes=per_arm_clean[arm.name],
                        fault_outcomes=per_arm_fault[arm.name],
                        background_writes=bg_landed,
                    )
                    for arm in arms
                }
                app_reports[server.app_name] = AppBenchmarkReport(
                    app_name=server.app_name,
                    server_class=srv_cls.__name__,
                    seed_rows=server.seed_rows,
                    seed_duration_ms=round(server.seed_duration_ms, 2),
                    journal_mode=server.journal_mode(),
                    num_background_workers=num_workers,
                    background_concurrent_writes_landed=bg_landed,
                    background_creates_landed=bg.creates_completed,
                    background_updates_landed=bg.updates_completed,
                    background_errors=len(bg.errors),
                    arms=arm_summaries,
                )
    finally:
        if hasattr(tx_arm, "close"):
            tx_arm.close()

    aggregate_by_arm = {
        arm.name: _summarize_arm_metrics(
            arm_name=arm.name,
            app_name="ALL_3_APPS",
            clean_outcomes=agg_clean[arm.name],
            fault_outcomes=agg_fault[arm.name],
            background_writes=total_bg_writes,
        )
        for arm in arms
    }

    return BenchmarkSuiteReport(
        suite_name="openadapt-tx-acid-50k-20w",
        seed_rows=seed_rows,
        num_background_workers=num_workers,
        tx_backend=tx_backend,
        total_background_writes_landed=total_bg_writes,
        total_duration_s=round(time.perf_counter() - t_suite_start, 3),
        apps=app_reports,
        aggregate_by_arm=aggregate_by_arm,
    )


def render_markdown_report(report: BenchmarkSuiteReport) -> str:
    """Render crisp GitHub-flavored Markdown comparison tables from BenchmarkSuiteReport."""
    lines: list[str] = [
        "# OpenAdapt Transactional Commit Layer (`tx`) — Production Realism Benchmark",
        "",
        f"- **Rows Seeded per System of Record:** `{report.seed_rows:,}` (SQLite WAL mode)",
        f"- **Concurrent Background Writers per App:** `{report.num_background_workers}`",
        f"- **Concurrent Background Writes Landed:** `{report.total_background_writes_landed:,}`",
        f"- **`tx` Engine Backend:** `{report.tx_backend}`",
        f"- **Total Suite Wall Time:** `{report.total_duration_s:.2f}s`",
        "",
        "## Aggregate Comparison Across All 3 Systems of Record",
        "",
        "| Arm | Clean Pass Rate | False Stop Rate | Fault Catch Rate | False Accept Rate | Mean Rows Read / Tx | p50 (ms) | p95 (ms) |",
        "| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]

    for arm_name, m in report.aggregate_by_arm.items():
        lines.append(
            f"| `{arm_name}` | {m.clean_pass_rate * 100:.1f}% | "
            f"{m.false_stop_rate * 100:.1f}% | {m.fault_catch_rate * 100:.1f}% | "
            f"{m.false_accept_rate * 100:.1f}% | {m.mean_rows_read_per_tx:.2f} | "
            f"{m.p50_ms:.2f} | {m.p95_ms:.2f} |"
        )

    lines.extend(
        [
            "",
            "## Per-Application Breakdown",
            "",
            "| System of Record | Seed Time | BG Writes | Arm | Clean Pass | False Stop | Fault Catch | False Accept | Mean Rows Read | p50 / p95 (ms) |",
            "| :--- | ---: | ---: | :--- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )

    for app_name, app_rep in report.apps.items():
        for arm_name, m in app_rep.arms.items():
            lines.append(
                f"| `{app_name}` | {app_rep.seed_duration_ms:.0f} ms | "
                f"{app_rep.background_concurrent_writes_landed:,} | `{arm_name}` | "
                f"{m.clean_pass_rate * 100:.1f}% | {m.false_stop_rate * 100:.1f}% | "
                f"{m.fault_catch_rate * 100:.1f}% | {m.false_accept_rate * 100:.1f}% | "
                f"{m.mean_rows_read_per_tx:.2f} | {m.p50_ms:.2f} / {m.p95_ms:.2f} |"
            )

    lines.extend(
        [
            "",
            "## Per-Fault-Class Catch Rate Matrix (Aggregate)",
            "",
            "| Fault Class | "
            + " | ".join(f"`{a}`" for a in report.aggregate_by_arm)
            + " |",
            "| :--- | " + " | ".join("---:" for _ in report.aggregate_by_arm) + " |",
        ]
    )

    for fk in ALL_BENCHMARK_FAULTS:
        cells = []
        for arm_m in report.aggregate_by_arm.values():
            rate = arm_m.per_fault_catch_rate.get(fk, 0.0)
            cells.append(f"{rate * 100:.1f}%")
        lines.append(f"| `{fk}` | " + " | ".join(cells) + " |")

    lines.append("")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        allow_abbrev=False,
        description="Run the 50k-row / 20-worker OpenAdapt tx ACID benchmark suite.",
    )
    parser.add_argument("--seed-rows", type=int, default=50_000)
    parser.add_argument("--workers", type=int, default=20)
    parser.add_argument("--clean-tasks", type=int, default=12)
    parser.add_argument("--fault-repeats", type=int, default=2)
    parser.add_argument("--json-out", type=str, default="")
    parser.add_argument("--md-out", type=str, default="")
    args = parser.parse_args(argv)

    report = run_tx_acid_benchmark(
        seed_rows=args.seed_rows,
        num_workers=args.workers,
        clean_tasks_per_app=args.clean_tasks,
        fault_repeats=args.fault_repeats,
    )
    md = render_markdown_report(report)
    print(md)

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report.to_dict(), fh, indent=2, sort_keys=True)
    if args.md_out:
        with open(args.md_out, "w", encoding="utf-8") as fh:
            fh.write(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
