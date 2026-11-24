"""Full Real-App Verification Matrix harness, fault proxy, and Wave 1 app adapters."""

from benchmarks.real_apps.fault_proxy import FaultMode, FaultProxy, FaultRule
from benchmarks.real_apps.harness import (
    AppRunResult,
    ReadbackResult,
    RealAppAdapter,
    RealAppHarness,
    RealAppSpec,
    RunMode,
    SurfaceKind,
    TierKind,
    WorkflowSpec,
    render_scoreboard_markdown,
)
from benchmarks.real_apps.registry import get_app_spec, list_app_specs, load_adapter

__all__ = [
    "AppRunResult",
    "FaultMode",
    "FaultProxy",
    "FaultRule",
    "ReadbackResult",
    "RealAppAdapter",
    "RealAppHarness",
    "RealAppSpec",
    "RunMode",
    "SurfaceKind",
    "TierKind",
    "WorkflowSpec",
    "get_app_spec",
    "list_app_specs",
    "load_adapter",
    "render_scoreboard_markdown",
]
