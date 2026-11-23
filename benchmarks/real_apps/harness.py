"""Standardized Real-App Verification Matrix contract and scoreboard renderer."""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol

SurfaceKind = Literal["web", "desktop", "electron"]
TierKind = Literal["pr", "nightly"]
RunMode = Literal["contract", "live"]


@dataclass(frozen=True)
class WorkflowSpec:
    """Single deterministic workflow evaluated against a real application."""

    workflow_id: str
    description: str
    expected_writes: int = 1
    fault_mode: str = "drop_write_return_200"


@dataclass(frozen=True)
class RealAppSpec:
    """Declarative specification for one app in the Real-App Verification Matrix."""

    app_id: str
    display_name: str
    vertical: str
    surface: SurfaceKind
    ui_architecture: str
    persistence_oracle: str
    compose_file: Path | None
    tier: TierKind
    workflows: tuple[WorkflowSpec, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "app_id": self.app_id,
            "display_name": self.display_name,
            "vertical": self.vertical,
            "surface": self.surface,
            "ui_architecture": self.ui_architecture,
            "persistence_oracle": self.persistence_oracle,
            "compose_file": str(self.compose_file) if self.compose_file else None,
            "tier": self.tier,
            "workflows": [
                {
                    "workflow_id": w.workflow_id,
                    "description": w.description,
                    "expected_writes": w.expected_writes,
                    "fault_mode": w.fault_mode,
                }
                for w in self.workflows
            ],
        }


@dataclass(frozen=True)
class ReadbackResult:
    """Result of an independent persistence oracle read-back check."""

    confirmed: bool
    oracle: str
    record_id: str | None = None
    observed_fields: dict[str, Any] = field(default_factory=dict)
    reason: str = ""


@dataclass(frozen=True)
class AppRunResult:
    """Outcome of running both happy-path and silent-fault verification for one app."""

    app_id: str
    display_name: str
    vertical: str
    surface: SurfaceKind
    tier: TierKind
    mode: RunMode
    happy_path_verified: bool
    silent_fault_caught: bool
    fault_mode_tested: str
    elapsed_s: float
    happy_readback: ReadbackResult
    fault_readback: ReadbackResult
    notes: str = ""

    @property
    def passed(self) -> bool:
        """Both happy-path VERIFIED and silent-fault HALT must succeed."""
        return self.happy_path_verified and self.silent_fault_caught

    def to_dict(self) -> dict[str, Any]:
        return {
            "app_id": self.app_id,
            "display_name": self.display_name,
            "vertical": self.vertical,
            "surface": self.surface,
            "tier": self.tier,
            "mode": self.mode,
            "passed": self.passed,
            "happy_path_verified": self.happy_path_verified,
            "silent_fault_caught": self.silent_fault_caught,
            "fault_mode_tested": self.fault_mode_tested,
            "elapsed_s": round(self.elapsed_s, 3),
            "happy_readback": {
                "confirmed": self.happy_readback.confirmed,
                "oracle": self.happy_readback.oracle,
                "record_id": self.happy_readback.record_id,
                "reason": self.happy_readback.reason,
            },
            "fault_readback": {
                "confirmed": self.fault_readback.confirmed,
                "oracle": self.fault_readback.oracle,
                "record_id": self.fault_readback.record_id,
                "reason": self.fault_readback.reason,
            },
            "notes": self.notes,
        }


class RealAppAdapter(Protocol):
    """Uniform adapter contract implemented by every app in ``benchmarks/real_apps/apps/``."""

    spec: RealAppSpec

    def run_contract_verification(self, work_dir: Path) -> AppRunResult:
        """Run fast, self-contained in-process contract verification (no Docker pull)."""
        ...

    def run_live_verification(self, work_dir: Path) -> AppRunResult:
        """Run verification against a live container or desktop instance."""
        ...


class RealAppHarness:
    """Executes one or more :class:`RealAppAdapter` instances and emits scoreboards."""

    def __init__(self, adapters: Sequence[RealAppAdapter]) -> None:
        self._adapters = list(adapters)

    def run_all(self, *, mode: RunMode, work_dir: Path) -> list[AppRunResult]:
        work_dir.mkdir(parents=True, exist_ok=True)
        results: list[AppRunResult] = []
        for adapter in self._adapters:
            app_dir = work_dir / adapter.spec.app_id
            app_dir.mkdir(parents=True, exist_ok=True)
            t0 = time.monotonic()
            if mode == "contract":
                res = adapter.run_contract_verification(app_dir)
            else:
                res = adapter.run_live_verification(app_dir)
            if res.elapsed_s <= 0.0:
                dt = time.monotonic() - t0
                res = AppRunResult(
                    app_id=res.app_id,
                    display_name=res.display_name,
                    vertical=res.vertical,
                    surface=res.surface,
                    tier=res.tier,
                    mode=res.mode,
                    happy_path_verified=res.happy_path_verified,
                    silent_fault_caught=res.silent_fault_caught,
                    fault_mode_tested=res.fault_mode_tested,
                    elapsed_s=dt,
                    happy_readback=res.happy_readback,
                    fault_readback=res.fault_readback,
                    notes=res.notes,
                )
            results.append(res)
        return results


def render_scoreboard_json(results: Sequence[AppRunResult], *, mode: RunMode) -> str:
    """Serialize scoreboard results to canonical JSON."""
    payload = {
        "schema": "openadapt.real-apps-scoreboard/v1",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "mode": mode,
        "total": len(results),
        "passed": sum(1 for r in results if r.passed),
        "results": [r.to_dict() for r in results],
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def render_scoreboard_markdown(results: Sequence[AppRunResult], *, mode: RunMode) -> str:
    """Render a concise GitHub-flavored Markdown scoreboard table."""
    passed_count = sum(1 for r in results if r.passed)
    lines = [
        f"# OpenAdapt Real-App Verification Matrix ({mode} mode)",
        "",
        f"**Summary:** {passed_count}/{len(results)} apps passed both happy-path `VERIFIED` and silent-fault `HALT`.",
        "",
        "| App | Vertical | Surface | Tier | Happy Path (`VERIFIED`) | Silent Fault (`HALT`) | Fault Mode | Time |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        happy = "PASS" if r.happy_path_verified else "FAIL"
        fault = "PASS" if r.silent_fault_caught else "FAIL"
        lines.append(
            f"| {r.display_name} (`{r.app_id}`) | {r.vertical} | {r.surface} | "
            f"{r.tier} | {happy} | {fault} | `{r.fault_mode_tested}` | {r.elapsed_s:.2f}s |"
        )
    lines.append("")
    return "\n".join(lines)
