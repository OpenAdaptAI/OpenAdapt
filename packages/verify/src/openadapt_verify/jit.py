"""Local JIT compiler for verified UI workflows (``openadapt_verify.jit``).

Compiles the first verified run of an agent-driven UI function into a
parameterized, deterministic ``CompiledPlan`` cached under
``.openadapt/compiled/<name>.json``. Subsequent runs skip the agent, replay
the compiled steps directly with new parameters, and verify persistence via
independent read-back. If a selector breaks, bounded step-level self-healing
recovers from the current page state and updates the cache only when final
read-back passes ``VERIFIED``.
"""

from __future__ import annotations

import functools
import inspect
import json
import os
import re
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional, Union

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openadapt_verify.mcpify import (
    ReadbackResult,
    SafetyPolicy,
    SignedReceipt,
    WorkflowToolSpec,
    issue_mcpify_receipt,
)

COMPILED_PLAN_SCHEMA = "openadapt.jit.plan/v1"
DEFAULT_CACHE_DIR = ".openadapt/compiled"

JitMode = Literal["cold", "warm", "healed"]
JitStatus = Literal["VERIFIED", "UNCONFIRMED_WRITE", "STOPPED"]
StepAction = Literal["goto", "click", "fill", "select", "press"]

_SECRET_NAME_TOKENS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "auth",
    "credential",
    "ssn",
    "cvv",
    "pin",
)
_TEMPLATE_VAR_RE = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")
_SAFE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")


class StepReplayError(RuntimeError):
    """Raised when every fallback selector for a compiled step fails."""

    def __init__(self, step_index: int, step: CompiledStep, detail: str) -> None:
        self.step_index = step_index
        self.step = step
        self.detail = detail
        super().__init__(f"Step {step_index} ({step.action}) failed: {detail}")


class ParameterBinder:
    """Bind concrete runtime parameter values into ``{{param_name}}`` templates.

    Matches longer parameter values before shorter ones, refuses empty
    parameter values, and protects secret-named parameters so raw secret values
    are never persisted in compiled plans.
    """

    @staticmethod
    def is_secret_param(name: str) -> bool:
        lowered = name.strip().lower()
        return any(token in lowered for token in _SECRET_NAME_TOKENS)

    @classmethod
    def validate_params(cls, params: Mapping[str, Any]) -> dict[str, str]:
        cleaned: dict[str, str] = {}
        for key, raw_val in params.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("Parameter names must be non-empty strings.")
            if raw_val is None:
                raise ValueError(f"Parameter {key!r} must not be None.")
            val_str = str(raw_val)
            if not val_str.strip():
                raise ValueError(f"Parameter {key!r} must not be empty.")
            cleaned[key.strip()] = val_str
        return cleaned

    @classmethod
    def bind_value(
        cls,
        value: str,
        params: Mapping[str, Any],
    ) -> tuple[str, tuple[str, ...]]:
        """Replace concrete parameter values in ``value`` with ``{{name}}``."""
        validated = cls.validate_params(params)
        if not value:
            return value, ()

        # Sort by value length descending so longer values match before substrings.
        ordered = sorted(
            validated.items(),
            key=lambda kv: (-len(kv[1]), kv[0]),
        )
        templated = value
        bound_names: list[str] = []
        for pname, pval in ordered:
            if pval in templated:
                templated = templated.replace(pval, f"{{{{{pname}}}}}")
                if pname not in bound_names:
                    bound_names.append(pname)
        return templated, tuple(bound_names)

    @classmethod
    def bind_mapping(
        cls,
        mapping: Mapping[str, Any],
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Recursively template string values inside a JSON-compatible mapping."""
        out: dict[str, Any] = {}
        for key, val in mapping.items():
            if isinstance(val, str):
                templated, _ = cls.bind_value(val, params)
                out[str(key)] = templated
            elif isinstance(val, Mapping):
                out[str(key)] = cls.bind_mapping(val, params)
            elif isinstance(val, list):
                out[str(key)] = [
                    cls.bind_value(item, params)[0] if isinstance(item, str) else item
                    for item in val
                ]
            else:
                out[str(key)] = val
        return out

    @classmethod
    def substitute(cls, template: str, params: Mapping[str, Any]) -> str:
        """Substitute ``{{param_name}}`` placeholders using concrete ``params``."""
        validated = cls.validate_params(params)

        def _replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in validated:
                raise KeyError(
                    f"Missing required parameter {name!r} for template {template!r}"
                )
            return validated[name]

        return _TEMPLATE_VAR_RE.sub(_replace, template)

    @classmethod
    def substitute_mapping(
        cls,
        mapping: Mapping[str, Any],
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Recursively substitute ``{{param_name}}`` placeholders in a mapping."""
        out: dict[str, Any] = {}
        for key, val in mapping.items():
            if isinstance(val, str):
                out[str(key)] = cls.substitute(val, params)
            elif isinstance(val, Mapping):
                out[str(key)] = cls.substitute_mapping(val, params)
            elif isinstance(val, list):
                out[str(key)] = [
                    cls.substitute(item, params) if isinstance(item, str) else item
                    for item in val
                ]
            else:
                out[str(key)] = val
        return out

    @classmethod
    def assert_no_secrets_leaked(
        cls,
        serialized_plan: str,
        params: Mapping[str, Any],
    ) -> None:
        """Raise ValueError if any secret-named parameter value appears in plaintext."""
        for key, raw_val in params.items():
            if not cls.is_secret_param(key):
                continue
            val_str = str(raw_val)
            if val_str and val_str in serialized_plan:
                raise ValueError(
                    f"Secret parameter {key!r} leaked into compiled plan; refusing to persist."
                )


@dataclass(frozen=True)
class CompiledStep:
    """One deterministic browser step in a compiled plan."""

    action: StepAction
    selectors: tuple[str, ...] = ()
    value: Optional[str] = None
    post_condition: Optional[str] = None
    healed_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "action": self.action,
            "selectors": list(self.selectors),
            "value": self.value,
            "post_condition": self.post_condition,
            "healed_count": self.healed_count,
        }
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CompiledStep:
        action = str(data["action"])
        if action not in ("goto", "click", "fill", "select", "press"):
            raise ValueError(f"Unsupported compiled step action: {action!r}")
        raw_selectors = data.get("selectors") or ()
        selectors = tuple(str(s) for s in raw_selectors)
        val = data.get("value")
        post = data.get("post_condition")
        return cls(
            action=action,  # type: ignore[arg-type]
            selectors=selectors,
            value=str(val) if val is not None else None,
            post_condition=str(post) if post is not None else None,
            healed_count=int(data.get("healed_count", 0)),
        )


@dataclass(frozen=True)
class CompiledPlanStats:
    """Cumulative execution and savings counters for a compiled plan."""

    cold_runs: int = 0
    warm_runs: int = 0
    heals: int = 0
    total_ms_saved: float = 0.0
    total_usd_saved: float = 0.0
    last_cold_ms: float = 0.0
    last_cold_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "cold_runs": self.cold_runs,
            "warm_runs": self.warm_runs,
            "heals": self.heals,
            "total_ms_saved": round(self.total_ms_saved, 3),
            "total_usd_saved": round(self.total_usd_saved, 6),
            "last_cold_ms": round(self.last_cold_ms, 3),
            "last_cold_usd": round(self.last_cold_usd, 6),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CompiledPlanStats:
        return cls(
            cold_runs=int(data.get("cold_runs", 0)),
            warm_runs=int(data.get("warm_runs", 0)),
            heals=int(data.get("heals", 0)),
            total_ms_saved=float(data.get("total_ms_saved", 0.0)),
            total_usd_saved=float(data.get("total_usd_saved", 0.0)),
            last_cold_ms=float(data.get("last_cold_ms", 0.0)),
            last_cold_usd=float(data.get("last_cold_usd", 0.0)),
        )


@dataclass(frozen=True)
class CompiledPlan:
    """JSON-serializable compiled workflow plan stored on disk."""

    name: str
    description: str
    param_names: tuple[str, ...]
    secret_params: tuple[str, ...]
    steps: tuple[CompiledStep, ...]
    readback_mode: str = "fresh_reload"
    readback_expectations: dict[str, Any] = field(default_factory=dict)
    stats: CompiledPlanStats = field(default_factory=CompiledPlanStats)
    schema_version: str = COMPILED_PLAN_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "description": self.description,
            "param_names": list(self.param_names),
            "secret_params": list(self.secret_params),
            "steps": [step.to_dict() for step in self.steps],
            "readback_mode": self.readback_mode,
            "readback_expectations": dict(self.readback_expectations),
            "stats": self.stats.to_dict(),
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CompiledPlan:
        schema = str(data.get("schema_version", ""))
        if schema != COMPILED_PLAN_SCHEMA:
            raise ValueError(
                f"Expected schema_version {COMPILED_PLAN_SCHEMA!r}, got {schema!r}"
            )
        name = str(data["name"])
        if not _SAFE_NAME_RE.match(name):
            raise ValueError(f"Invalid compiled plan name: {name!r}")
        raw_steps = data.get("steps")
        if not isinstance(raw_steps, list):
            raise ValueError("Compiled plan 'steps' must be a list.")
        return cls(
            name=name,
            description=str(data.get("description") or f"Compiled JIT workflow {name}"),
            param_names=tuple(str(p) for p in data.get("param_names", ())),
            secret_params=tuple(str(p) for p in data.get("secret_params", ())),
            steps=tuple(CompiledStep.from_dict(s) for s in raw_steps),
            readback_mode=str(data.get("readback_mode", "fresh_reload")),
            readback_expectations=dict(data.get("readback_expectations") or {}),
            stats=CompiledPlanStats.from_dict(data.get("stats") or {}),
            schema_version=schema,
        )

    def save(
        self,
        path: Union[str, Path],
        *,
        params_to_audit: Optional[Mapping[str, Any]] = None,
    ) -> Path:
        """Atomically persist this compiled plan to ``path``."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = self.to_json(indent=2) + "\n"
        if params_to_audit:
            ParameterBinder.assert_no_secrets_leaked(payload, params_to_audit)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=str(target.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp_name, target)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return target

    @classmethod
    def load(cls, path: Union[str, Path]) -> CompiledPlan:
        target = Path(path)
        raw = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(raw, Mapping):
            raise ValueError(f"Compiled plan at {target} must be a JSON object.")
        return cls.from_dict(raw)

    def to_workflow_tool_spec(self) -> WorkflowToolSpec:
        """Expose this compiled plan as a verified ``mcpify.WorkflowToolSpec``."""
        properties: dict[str, Any] = {}
        for pname in self.param_names:
            desc = (
                f"Secret parameter '{pname}' (never persisted in compiled plan)."
                if pname in self.secret_params
                else f"Workflow parameter '{pname}'."
            )
            properties[pname] = {"type": "string", "description": desc}

        input_schema: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "required": list(self.param_names),
            "additionalProperties": False,
        }
        mcp_steps = tuple(
            {
                "op": f"jit.{step.action}",
                "selectors": list(step.selectors),
                "value": step.value,
                "post_condition": step.post_condition,
            }
            for step in self.steps
        )
        readback_contract: dict[str, Any] = {
            "kind": f"jit_{self.readback_mode}",
            "expected": dict(self.readback_expectations),
        }
        return WorkflowToolSpec(
            name=self.name,
            description=self.description,
            input_schema=input_schema,
            steps=mcp_steps,
            readback_contract=readback_contract,
            safety_policy=SafetyPolicy(),
            schema_version=COMPILED_PLAN_SCHEMA,
        )


@dataclass(frozen=True)
class JitResult:
    """Structured result returned by a ``jit_wrap``-decorated workflow."""

    mode: JitMode
    status: JitStatus
    latency_ms: float
    estimated_usd: float
    receipt: Optional[SignedReceipt] = None
    healed_steps: tuple[int, ...] = ()
    plan_path: Optional[Path] = None
    reason: str = ""
    value: Any = None
    stats: Optional[CompiledPlanStats] = None


def format_scorecard(result: JitResult) -> str:
    """Format a compact terminal scorecard for a ``JitResult``."""
    receipt_label = (
        f"signed ({result.receipt.receipt_digest[:12]})"
        if result.receipt is not None
        else "unsigned"
    )
    lines = [
        f"[openadapt-jit] mode={result.mode} status={result.status} "
        f"latency={result.latency_ms:.1f}ms cost=${result.estimated_usd:.4f} "
        f"receipt={receipt_label}"
    ]
    if result.healed_steps:
        lines.append(f"  healed_steps={list(result.healed_steps)}")
    if result.stats is not None:
        lines.append(
            f"  totals: cold={result.stats.cold_runs} warm={result.stats.warm_runs} "
            f"heals={result.stats.heals} "
            f"saved={result.stats.total_ms_saved:.1f}ms / ${result.stats.total_usd_saved:.4f}"
        )
    if result.reason:
        lines.append(f"  reason: {result.reason}")
    return "\n".join(lines)


@dataclass
class _RecordedAction:
    action: StepAction
    selectors: tuple[str, ...] = ()
    value: Optional[str] = None
    post_condition: Optional[str] = None


class _RecordingLocatorProxy:
    """Thin proxy over Playwright-style locators (``locator``, ``get_by_role``, etc.)."""

    def __init__(
        self,
        raw_locator: Any,
        selectors: tuple[str, ...],
        sink: list[_RecordedAction],
    ) -> None:
        self._raw = raw_locator
        self._selectors = selectors
        self._sink = sink

    def click(self, *args: Any, **kwargs: Any) -> Any:
        self._sink.append(_RecordedAction(action="click", selectors=self._selectors))
        return self._raw.click(*args, **kwargs)

    def fill(self, value: str, *args: Any, **kwargs: Any) -> Any:
        self._sink.append(
            _RecordedAction(action="fill", selectors=self._selectors, value=str(value))
        )
        return self._raw.fill(value, *args, **kwargs)

    def select_option(self, value: str, *args: Any, **kwargs: Any) -> Any:
        self._sink.append(
            _RecordedAction(
                action="select", selectors=self._selectors, value=str(value)
            )
        )
        return self._raw.select_option(value, *args, **kwargs)

    def press(self, key: str, *args: Any, **kwargs: Any) -> Any:
        self._sink.append(
            _RecordedAction(action="press", selectors=self._selectors, value=str(key))
        )
        return self._raw.press(key, *args, **kwargs)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._raw, item)


class RecordingPageProxy:
    """Thin page wrapper that records UI actions during Cold Run and Healing.

    Supports both sync and async page objects (Playwright ``Page`` or test
    doubles) without importing Playwright.
    """

    def __init__(self, page: Any) -> None:
        self._page = page
        self.recorded: list[_RecordedAction] = []

    def _selectors_for(
        self,
        primary: str,
        *,
        fallback_selectors: Optional[tuple[str, ...]] = None,
    ) -> tuple[str, ...]:
        chain: list[str] = [primary]
        if fallback_selectors:
            for item in fallback_selectors:
                if item and item not in chain:
                    chain.append(item)
        # Ask the page if it can report extra semantic fallback selectors (role/label/text).
        hint_fn = getattr(self._page, "fallback_selectors_for", None)
        if callable(hint_fn):
            extra = hint_fn(primary)
            if isinstance(extra, (list, tuple)):
                for item in extra:
                    s = str(item)
                    if s and s not in chain:
                        chain.append(s)
        return tuple(chain)

    def goto(self, url: str, *args: Any, **kwargs: Any) -> Any:
        self.recorded.append(_RecordedAction(action="goto", value=str(url)))
        return self._page.goto(url, *args, **kwargs)

    def click(self, selector: str, *args: Any, **kwargs: Any) -> Any:
        self.recorded.append(
            _RecordedAction(action="click", selectors=self._selectors_for(selector))
        )
        return self._page.click(selector, *args, **kwargs)

    def fill(self, selector: str, value: str, *args: Any, **kwargs: Any) -> Any:
        self.recorded.append(
            _RecordedAction(
                action="fill",
                selectors=self._selectors_for(selector),
                value=str(value),
            )
        )
        return self._page.fill(selector, value, *args, **kwargs)

    def select_option(
        self, selector: str, value: str, *args: Any, **kwargs: Any
    ) -> Any:
        self.recorded.append(
            _RecordedAction(
                action="select",
                selectors=self._selectors_for(selector),
                value=str(value),
            )
        )
        return self._page.select_option(selector, value, *args, **kwargs)

    def select(self, selector: str, value: str, *args: Any, **kwargs: Any) -> Any:
        self.recorded.append(
            _RecordedAction(
                action="select",
                selectors=self._selectors_for(selector),
                value=str(value),
            )
        )
        if hasattr(self._page, "select"):
            return self._page.select(selector, value, *args, **kwargs)
        return self._page.select_option(selector, value, *args, **kwargs)

    def press(self, selector: str, key: str, *args: Any, **kwargs: Any) -> Any:
        self.recorded.append(
            _RecordedAction(
                action="press",
                selectors=self._selectors_for(selector),
                value=str(key),
            )
        )
        return self._page.press(selector, key, *args, **kwargs)

    def locator(
        self, selector: str, *args: Any, **kwargs: Any
    ) -> _RecordingLocatorProxy:
        return _RecordingLocatorProxy(
            self._page.locator(selector, *args, **kwargs),
            self._selectors_for(selector),
            self.recorded,
        )

    def get_by_role(
        self, role: str, *, name: Optional[str] = None, **kwargs: Any
    ) -> _RecordingLocatorProxy:
        sel = f"role={role}[name={name}]" if name else f"role={role}"
        return _RecordingLocatorProxy(
            self._page.get_by_role(role, name=name, **kwargs),
            (sel,),
            self.recorded,
        )

    def get_by_label(self, text: str, **kwargs: Any) -> _RecordingLocatorProxy:
        return _RecordingLocatorProxy(
            self._page.get_by_label(text, **kwargs),
            (f"label={text}",),
            self.recorded,
        )

    def get_by_text(self, text: str, **kwargs: Any) -> _RecordingLocatorProxy:
        return _RecordingLocatorProxy(
            self._page.get_by_text(text, **kwargs),
            (f"text={text}",),
            self.recorded,
        )

    def __getattr__(self, item: str) -> Any:
        return getattr(self._page, item)


def _bind_recorded_actions(
    recorded: list[_RecordedAction],
    params: Mapping[str, Any],
) -> tuple[CompiledStep, ...]:
    steps: list[CompiledStep] = []
    for item in recorded:
        bound_selectors = tuple(
            ParameterBinder.bind_value(sel, params)[0] for sel in item.selectors
        )
        bound_val = (
            ParameterBinder.bind_value(item.value, params)[0]
            if item.value is not None
            else None
        )
        bound_post = (
            ParameterBinder.bind_value(item.post_condition, params)[0]
            if item.post_condition is not None
            else None
        )
        steps.append(
            CompiledStep(
                action=item.action,
                selectors=bound_selectors,
                value=bound_val,
                post_condition=bound_post,
            )
        )
    return tuple(steps)


def _dispatch_selector_sync(
    page: Any,
    action: StepAction,
    selector: str,
    value: Optional[str],
) -> None:
    if selector.startswith("role="):
        body = selector[len("role=") :]
        if "[name=" in body and body.endswith("]"):
            role, rest = body.split("[name=", 1)
            target = page.get_by_role(role, name=rest[:-1])
        else:
            target = page.get_by_role(body)
        if action == "click":
            target.click()
        elif action == "fill":
            target.fill(value or "")
        elif action == "select":
            target.select_option(value or "")
        elif action == "press":
            target.press(value or "")
        return

    if selector.startswith("label="):
        target = page.get_by_label(selector[len("label=") :])
        if action == "click":
            target.click()
        elif action == "fill":
            target.fill(value or "")
        elif action == "select":
            target.select_option(value or "")
        elif action == "press":
            target.press(value or "")
        return

    if selector.startswith("text="):
        target = page.get_by_text(selector[len("text=") :])
        if action == "click":
            target.click()
        elif action == "fill":
            target.fill(value or "")
        elif action == "select":
            target.select_option(value or "")
        elif action == "press":
            target.press(value or "")
        return

    if action == "click":
        page.click(selector)
    elif action == "fill":
        page.fill(selector, value or "")
    elif action == "select":
        if hasattr(page, "select_option"):
            page.select_option(selector, value or "")
        else:
            page.select(selector, value or "")
    elif action == "press":
        page.press(selector, value or "")


async def _dispatch_selector_async(
    page: Any,
    action: StepAction,
    selector: str,
    value: Optional[str],
) -> None:
    if selector.startswith("role="):
        body = selector[len("role=") :]
        if "[name=" in body and body.endswith("]"):
            role, rest = body.split("[name=", 1)
            target = page.get_by_role(role, name=rest[:-1])
        else:
            target = page.get_by_role(body)
        if action == "click":
            await target.click()
        elif action == "fill":
            await target.fill(value or "")
        elif action == "select":
            await target.select_option(value or "")
        elif action == "press":
            await target.press(value or "")
        return

    if selector.startswith("label="):
        target = page.get_by_label(selector[len("label=") :])
        if action == "click":
            await target.click()
        elif action == "fill":
            await target.fill(value or "")
        elif action == "select":
            await target.select_option(value or "")
        elif action == "press":
            await target.press(value or "")
        return

    if selector.startswith("text="):
        target = page.get_by_text(selector[len("text=") :])
        if action == "click":
            await target.click()
        elif action == "fill":
            await target.fill(value or "")
        elif action == "select":
            await target.select_option(value or "")
        elif action == "press":
            await target.press(value or "")
        return

    if action == "click":
        await page.click(selector)
    elif action == "fill":
        await page.fill(selector, value or "")
    elif action == "select":
        if hasattr(page, "select_option"):
            await page.select_option(selector, value or "")
        else:
            await page.select(selector, value or "")
    elif action == "press":
        await page.press(selector, value or "")


def replay_step_sync(
    page: Any,
    step_index: int,
    step: CompiledStep,
    params: Mapping[str, Any],
) -> None:
    """Replay one compiled step synchronously using its fallback selector chain."""
    resolved_val = (
        ParameterBinder.substitute(step.value, params)
        if step.value is not None
        else None
    )
    if step.action == "goto":
        page.goto(resolved_val or "")
        return

    if not step.selectors:
        raise StepReplayError(step_index, step, "Step has no selectors.")

    last_err = "no selectors matched"
    for raw_sel in step.selectors:
        selector = ParameterBinder.substitute(raw_sel, params)
        try:
            _dispatch_selector_sync(page, step.action, selector, resolved_val)
            return
        except Exception as exc:  # noqa: BLE001 - try next fallback selector
            last_err = f"{selector!r}: {exc}"
    raise StepReplayError(step_index, step, last_err)


async def replay_step_async(
    page: Any,
    step_index: int,
    step: CompiledStep,
    params: Mapping[str, Any],
) -> None:
    """Replay one compiled step asynchronously using its fallback selector chain."""
    resolved_val = (
        ParameterBinder.substitute(step.value, params)
        if step.value is not None
        else None
    )
    if step.action == "goto":
        await page.goto(resolved_val or "")
        return

    if not step.selectors:
        raise StepReplayError(step_index, step, "Step has no selectors.")

    last_err = "no selectors matched"
    for raw_sel in step.selectors:
        selector = ParameterBinder.substitute(raw_sel, params)
        try:
            await _dispatch_selector_async(page, step.action, selector, resolved_val)
            return
        except Exception as exc:  # noqa: BLE001 - try next fallback selector
            last_err = f"{selector!r}: {exc}"
    raise StepReplayError(step_index, step, last_err)


def _default_fresh_reload_readback_sync(
    page: Any,
    params: Mapping[str, Any],
    expectations: Mapping[str, Any],
) -> tuple[bool, str]:
    """Reload the page and check that non-secret expected values appear in the reloaded DOM."""
    reload_fn = getattr(page, "reload", None)
    if not callable(reload_fn):
        return False, "Page does not expose reload() for fresh_reload read-back."
    reload_fn()
    content_fn = getattr(page, "content", None)
    text_fn = getattr(page, "inner_text", None)
    if callable(content_fn):
        body = str(content_fn())
    elif callable(text_fn):
        body = str(text_fn("body"))
    else:
        return False, "Page does not expose content() or inner_text() for read-back."

    expected_map = (
        ParameterBinder.substitute_mapping(expectations, params)
        if expectations
        else {
            k: str(v)
            for k, v in params.items()
            if not ParameterBinder.is_secret_param(k)
        }
    )
    if not expected_map:
        return False, "No verifiable read-back expectations were supplied."

    missing = [f"{k}={v!r}" for k, v in expected_map.items() if str(v) not in body]
    if missing:
        return False, f"Fresh reload missing expected value(s): {', '.join(missing)}"
    return True, "Fresh reload confirmed expected values."


async def _default_fresh_reload_readback_async(
    page: Any,
    params: Mapping[str, Any],
    expectations: Mapping[str, Any],
) -> tuple[bool, str]:
    reload_fn = getattr(page, "reload", None)
    if not callable(reload_fn):
        return False, "Page does not expose reload() for fresh_reload read-back."
    await reload_fn()
    content_fn = getattr(page, "content", None)
    text_fn = getattr(page, "inner_text", None)
    if callable(content_fn):
        body = str(await content_fn())
    elif callable(text_fn):
        body = str(await text_fn("body"))
    else:
        return False, "Page does not expose content() or inner_text() for read-back."

    expected_map = (
        ParameterBinder.substitute_mapping(expectations, params)
        if expectations
        else {
            k: str(v)
            for k, v in params.items()
            if not ParameterBinder.is_secret_param(k)
        }
    )
    if not expected_map:
        return False, "No verifiable read-back expectations were supplied."

    missing = [f"{k}={v!r}" for k, v in expected_map.items() if str(v) not in body]
    if missing:
        return False, f"Fresh reload missing expected value(s): {', '.join(missing)}"
    return True, "Fresh reload confirmed expected values."


def _interpret_readback_outcome(outcome: Any) -> tuple[bool, str]:
    if isinstance(outcome, bool):
        return outcome, "readback confirmed" if outcome else "readback returned False"
    if (
        isinstance(outcome, tuple)
        and len(outcome) == 2
        and isinstance(outcome[0], bool)
    ):
        return outcome[0], str(outcome[1])
    status = getattr(outcome, "status", None)
    if isinstance(status, str):
        reason = str(
            getattr(outcome, "reason", "") or getattr(outcome, "detail", "") or status
        )
        return status.upper() == "VERIFIED", reason
    verified = getattr(outcome, "verified", None)
    if isinstance(verified, bool):
        reason = str(getattr(outcome, "reason", "") or getattr(outcome, "detail", ""))
        return verified, reason
    return False, f"Unsupported read-back return type: {type(outcome).__name__}"


def _maybe_sign_receipt(
    *,
    plan: CompiledPlan,
    params: Mapping[str, Any],
    verified: bool,
    reason: str,
    signing_key: Optional[Ed25519PrivateKey],
    llm_calls: int = 0,
) -> Optional[SignedReceipt]:
    if signing_key is None or not verified:
        return None
    redacted_args = {
        k: ("<redacted>" if ParameterBinder.is_secret_param(k) else str(v))
        for k, v in params.items()
    }
    rb = ReadbackResult(
        verdict="VERIFIED",
        reason=reason,
        expected={
            k: v
            for k, v in plan.readback_expectations.items()
            if k not in plan.secret_params
        },
        observed={"mode": plan.readback_mode},
    )
    spec = plan.to_workflow_tool_spec()
    return issue_mcpify_receipt(
        tool_name=plan.name,
        arguments=redacted_args,
        steps=spec.steps,
        readback=rb,
        signing_key=signing_key,
        llm_calls=llm_calls,
    )


def _build_plan_from_recording(
    *,
    name: str,
    description: str,
    params: Mapping[str, Any],
    recorded: list[_RecordedAction],
    readback_mode: str,
    readback_expectations: Optional[Mapping[str, Any]],
    cold_ms: float,
    estimated_cold_usd: float,
) -> CompiledPlan:
    validated = ParameterBinder.validate_params(params)
    secret_params = tuple(
        sorted(k for k in validated if ParameterBinder.is_secret_param(k))
    )
    param_names = tuple(sorted(validated.keys()))
    steps = _bind_recorded_actions(recorded, validated)
    if readback_expectations is not None:
        templated_rb = ParameterBinder.bind_mapping(readback_expectations, validated)
    else:
        templated_rb = {
            k: f"{{{{{k}}}}}" for k in param_names if k not in secret_params
        }
    return CompiledPlan(
        name=name,
        description=description or f"Compiled JIT workflow {name}",
        param_names=param_names,
        secret_params=secret_params,
        steps=steps,
        readback_mode=readback_mode,
        readback_expectations=templated_rb,
        stats=CompiledPlanStats(
            cold_runs=1,
            warm_runs=0,
            heals=0,
            total_ms_saved=0.0,
            total_usd_saved=0.0,
            last_cold_ms=cold_ms,
            last_cold_usd=estimated_cold_usd,
        ),
    )


def _invoke_healer_sync(
    healer: Callable[..., Any],
    proxy: RecordingPageProxy,
    params: Mapping[str, Any],
    *,
    failed_index: int,
    failed_step: CompiledStep,
) -> Any:
    sig = inspect.signature(healer)
    kwargs: dict[str, Any] = {}
    if "failed_step_index" in sig.parameters:
        kwargs["failed_step_index"] = failed_index
    if "failed_step" in sig.parameters:
        kwargs["failed_step"] = failed_step
    return healer(proxy, dict(params), **kwargs)


async def _invoke_healer_async(
    healer: Callable[..., Awaitable[Any]],
    proxy: RecordingPageProxy,
    params: Mapping[str, Any],
    *,
    failed_index: int,
    failed_step: CompiledStep,
) -> Any:
    sig = inspect.signature(healer)
    kwargs: dict[str, Any] = {}
    if "failed_step_index" in sig.parameters:
        kwargs["failed_step_index"] = failed_index
    if "failed_step" in sig.parameters:
        kwargs["failed_step"] = failed_step
    return await healer(proxy, dict(params), **kwargs)


def jit_wrap(
    name: str,
    *,
    readback: str = "fresh_reload",
    cache_dir: Union[str, Path] = DEFAULT_CACHE_DIR,
    max_heal_steps: int = 5,
    signing_key: Optional[Ed25519PrivateKey] = None,
    readback_fn: Optional[Callable[..., Any]] = None,
    healer_fn: Optional[Callable[..., Any]] = None,
    estimated_cold_usd: float = 0.05,
    description: str = "",
    readback_expectations: Optional[Mapping[str, Any]] = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorate a sync or async ``fn(page, params)`` with local JIT compilation.

    - **Cold Run (#1):** records actions through ``RecordingPageProxy``, runs
      independent read-back, and saves ``<cache_dir>/<name>.json`` only when
      read-back returns ``VERIFIED``.
    - **Warm Run (#2+):** skips ``fn``, replays the cached ``CompiledPlan`` with
      substituted ``params``, and runs read-back against the new ``params``.
    - **Bounded Self-Healing:** if a step fails during Warm Run, invokes
      ``healer_fn`` (or ``fn``) capped at ``max_heal_steps`` and updates the
      cached plan on disk only if final read-back passes ``VERIFIED``.
    """
    if not _SAFE_NAME_RE.match(name):
        raise ValueError(f"Invalid JIT workflow name: {name!r}")
    if max_heal_steps < 0:
        raise ValueError("max_heal_steps must be >= 0")

    plan_path = Path(cache_dir) / f"{name}.json"

    def _decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        is_async = inspect.iscoroutinefunction(fn)

        if not is_async:

            @functools.wraps(fn)
            def _sync_wrapper(page: Any, params: Mapping[str, Any]) -> JitResult:
                t0 = time.perf_counter()
                validated = ParameterBinder.validate_params(params)

                # --- COLD RUN ---
                if not plan_path.exists():
                    proxy = RecordingPageProxy(page)
                    ret_val = fn(proxy, dict(validated))
                    rb_ok, rb_reason = (
                        _interpret_readback_outcome(readback_fn(page, validated))
                        if readback_fn is not None
                        else _default_fresh_reload_readback_sync(
                            page, validated, readback_expectations or {}
                        )
                    )
                    elapsed_ms = (time.perf_counter() - t0) * 1000.0
                    if not rb_ok:
                        return JitResult(
                            mode="cold",
                            status="UNCONFIRMED_WRITE",
                            latency_ms=elapsed_ms,
                            estimated_usd=estimated_cold_usd,
                            plan_path=None,
                            reason=rb_reason,
                            value=ret_val,
                        )
                    plan = _build_plan_from_recording(
                        name=name,
                        description=description,
                        params=validated,
                        recorded=proxy.recorded,
                        readback_mode=readback,
                        readback_expectations=readback_expectations,
                        cold_ms=elapsed_ms,
                        estimated_cold_usd=estimated_cold_usd,
                    )
                    plan.save(plan_path, params_to_audit=validated)
                    receipt = _maybe_sign_receipt(
                        plan=plan,
                        params=validated,
                        verified=True,
                        reason=rb_reason,
                        signing_key=signing_key,
                    )
                    return JitResult(
                        mode="cold",
                        status="VERIFIED",
                        latency_ms=elapsed_ms,
                        estimated_usd=estimated_cold_usd,
                        receipt=receipt,
                        plan_path=plan_path,
                        reason=rb_reason,
                        value=ret_val,
                        stats=plan.stats,
                    )

                # --- WARM RUN (+ BOUNDED SELF-HEAL) ---
                plan = CompiledPlan.load(plan_path)
                candidate_steps = list(plan.steps)
                healed_indices: list[int] = []
                heal_actions_used = 0
                idx = 0
                while idx < len(candidate_steps):
                    step = candidate_steps[idx]
                    try:
                        replay_step_sync(page, idx, step, validated)
                        idx += 1
                    except StepReplayError as err:
                        if heal_actions_used >= max_heal_steps:
                            elapsed_ms = (time.perf_counter() - t0) * 1000.0
                            return JitResult(
                                mode="healed",
                                status="STOPPED",
                                latency_ms=elapsed_ms,
                                estimated_usd=estimated_cold_usd,
                                healed_steps=tuple(healed_indices),
                                plan_path=plan_path,
                                reason=f"Exceeded max_heal_steps={max_heal_steps}: {err}",
                                stats=plan.stats,
                            )
                        heal_proxy = RecordingPageProxy(page)
                        active_healer = healer_fn if healer_fn is not None else fn
                        try:
                            _invoke_healer_sync(
                                active_healer,
                                heal_proxy,
                                validated,
                                failed_index=idx,
                                failed_step=step,
                            )
                        except Exception as heal_exc:  # noqa: BLE001 - stop without poisoning cache
                            elapsed_ms = (time.perf_counter() - t0) * 1000.0
                            return JitResult(
                                mode="healed",
                                status="STOPPED",
                                latency_ms=elapsed_ms,
                                estimated_usd=estimated_cold_usd,
                                healed_steps=tuple(healed_indices),
                                plan_path=plan_path,
                                reason=f"Self-heal raised at step {idx}: {heal_exc}",
                                stats=plan.stats,
                            )

                        healed_recorded = heal_proxy.recorded
                        if not healed_recorded or (
                            heal_actions_used + len(healed_recorded) > max_heal_steps
                        ):
                            elapsed_ms = (time.perf_counter() - t0) * 1000.0
                            return JitResult(
                                mode="healed",
                                status="STOPPED",
                                latency_ms=elapsed_ms,
                                estimated_usd=estimated_cold_usd,
                                healed_steps=tuple(healed_indices),
                                plan_path=plan_path,
                                reason=(
                                    f"Self-heal recorded {len(healed_recorded)} actions "
                                    f"(budget remaining: {max_heal_steps - heal_actions_used})."
                                ),
                                stats=plan.stats,
                            )

                        heal_actions_used += len(healed_recorded)
                        bound_healed = _bind_recorded_actions(
                            healed_recorded, validated
                        )
                        healed_with_counts = [
                            CompiledStep(
                                action=s.action,
                                selectors=s.selectors,
                                value=s.value,
                                post_condition=s.post_condition,
                                healed_count=step.healed_count + 1,
                            )
                            for s in bound_healed
                        ]
                        healed_indices.append(idx)
                        if healer_fn is not None:
                            # Step-level healer replaced just step `idx`; continue remaining steps.
                            candidate_steps[idx : idx + 1] = healed_with_counts
                            idx += len(healed_with_counts)
                        else:
                            # Full wrapped agent recovered from step `idx` to completion.
                            candidate_steps[idx:] = healed_with_counts
                            break

                rb_ok, rb_reason = (
                    _interpret_readback_outcome(readback_fn(page, validated))
                    if readback_fn is not None
                    else _default_fresh_reload_readback_sync(
                        page, validated, plan.readback_expectations
                    )
                )
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                mode: JitMode = "healed" if healed_indices else "warm"
                est_usd = estimated_cold_usd if healed_indices else 0.0

                if not rb_ok:
                    # Broken heal or failed warm read-back NEVER overwrites the cached plan.
                    return JitResult(
                        mode=mode,
                        status="UNCONFIRMED_WRITE",
                        latency_ms=elapsed_ms,
                        estimated_usd=est_usd,
                        healed_steps=tuple(healed_indices),
                        plan_path=plan_path,
                        reason=rb_reason,
                        stats=plan.stats,
                    )

                ms_saved = (
                    max(0.0, plan.stats.last_cold_ms - elapsed_ms)
                    if not healed_indices
                    else 0.0
                )
                usd_saved = plan.stats.last_cold_usd if not healed_indices else 0.0
                new_stats = CompiledPlanStats(
                    cold_runs=plan.stats.cold_runs,
                    warm_runs=plan.stats.warm_runs + (0 if healed_indices else 1),
                    heals=plan.stats.heals + len(healed_indices),
                    total_ms_saved=plan.stats.total_ms_saved + ms_saved,
                    total_usd_saved=plan.stats.total_usd_saved + usd_saved,
                    last_cold_ms=plan.stats.last_cold_ms,
                    last_cold_usd=plan.stats.last_cold_usd,
                )
                updated_plan = CompiledPlan(
                    name=plan.name,
                    description=plan.description,
                    param_names=plan.param_names,
                    secret_params=plan.secret_params,
                    steps=tuple(candidate_steps),
                    readback_mode=plan.readback_mode,
                    readback_expectations=plan.readback_expectations,
                    stats=new_stats,
                    schema_version=plan.schema_version,
                )
                updated_plan.save(plan_path, params_to_audit=validated)
                receipt = _maybe_sign_receipt(
                    plan=updated_plan,
                    params=validated,
                    verified=True,
                    reason=rb_reason,
                    signing_key=signing_key,
                )
                return JitResult(
                    mode=mode,
                    status="VERIFIED",
                    latency_ms=elapsed_ms,
                    estimated_usd=est_usd,
                    receipt=receipt,
                    healed_steps=tuple(healed_indices),
                    plan_path=plan_path,
                    reason=rb_reason,
                    stats=new_stats,
                )

            return _sync_wrapper

        @functools.wraps(fn)
        async def _async_wrapper(page: Any, params: Mapping[str, Any]) -> JitResult:
            t0 = time.perf_counter()
            validated = ParameterBinder.validate_params(params)

            if not plan_path.exists():
                proxy = RecordingPageProxy(page)
                ret_val = await fn(proxy, dict(validated))
                if readback_fn is not None:
                    rb_raw = readback_fn(page, validated)
                    if inspect.isawaitable(rb_raw):
                        rb_raw = await rb_raw
                    rb_ok, rb_reason = _interpret_readback_outcome(rb_raw)
                else:
                    rb_ok, rb_reason = await _default_fresh_reload_readback_async(
                        page, validated, readback_expectations or {}
                    )
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                if not rb_ok:
                    return JitResult(
                        mode="cold",
                        status="UNCONFIRMED_WRITE",
                        latency_ms=elapsed_ms,
                        estimated_usd=estimated_cold_usd,
                        plan_path=None,
                        reason=rb_reason,
                        value=ret_val,
                    )
                plan = _build_plan_from_recording(
                    name=name,
                    description=description,
                    params=validated,
                    recorded=proxy.recorded,
                    readback_mode=readback,
                    readback_expectations=readback_expectations,
                    cold_ms=elapsed_ms,
                    estimated_cold_usd=estimated_cold_usd,
                )
                plan.save(plan_path, params_to_audit=validated)
                receipt = _maybe_sign_receipt(
                    plan=plan,
                    params=validated,
                    verified=True,
                    reason=rb_reason,
                    signing_key=signing_key,
                )
                return JitResult(
                    mode="cold",
                    status="VERIFIED",
                    latency_ms=elapsed_ms,
                    estimated_usd=estimated_cold_usd,
                    receipt=receipt,
                    plan_path=plan_path,
                    reason=rb_reason,
                    value=ret_val,
                    stats=plan.stats,
                )

            plan = CompiledPlan.load(plan_path)
            candidate_steps = list(plan.steps)
            healed_indices: list[int] = []
            heal_actions_used = 0
            idx = 0
            while idx < len(candidate_steps):
                step = candidate_steps[idx]
                try:
                    await replay_step_async(page, idx, step, validated)
                    idx += 1
                except StepReplayError as err:
                    if heal_actions_used >= max_heal_steps:
                        elapsed_ms = (time.perf_counter() - t0) * 1000.0
                        return JitResult(
                            mode="healed",
                            status="STOPPED",
                            latency_ms=elapsed_ms,
                            estimated_usd=estimated_cold_usd,
                            healed_steps=tuple(healed_indices),
                            plan_path=plan_path,
                            reason=f"Exceeded max_heal_steps={max_heal_steps}: {err}",
                            stats=plan.stats,
                        )
                    heal_proxy = RecordingPageProxy(page)
                    active_healer = healer_fn if healer_fn is not None else fn
                    try:
                        await _invoke_healer_async(
                            active_healer,
                            heal_proxy,
                            validated,
                            failed_index=idx,
                            failed_step=step,
                        )
                    except Exception as heal_exc:  # noqa: BLE001
                        elapsed_ms = (time.perf_counter() - t0) * 1000.0
                        return JitResult(
                            mode="healed",
                            status="STOPPED",
                            latency_ms=elapsed_ms,
                            estimated_usd=estimated_cold_usd,
                            healed_steps=tuple(healed_indices),
                            plan_path=plan_path,
                            reason=f"Self-heal raised at step {idx}: {heal_exc}",
                            stats=plan.stats,
                        )

                    healed_recorded = heal_proxy.recorded
                    if not healed_recorded or (
                        heal_actions_used + len(healed_recorded) > max_heal_steps
                    ):
                        elapsed_ms = (time.perf_counter() - t0) * 1000.0
                        return JitResult(
                            mode="healed",
                            status="STOPPED",
                            latency_ms=elapsed_ms,
                            estimated_usd=estimated_cold_usd,
                            healed_steps=tuple(healed_indices),
                            plan_path=plan_path,
                            reason=(
                                f"Self-heal recorded {len(healed_recorded)} actions "
                                f"(budget remaining: {max_heal_steps - heal_actions_used})."
                            ),
                            stats=plan.stats,
                        )

                    heal_actions_used += len(healed_recorded)
                    bound_healed = _bind_recorded_actions(healed_recorded, validated)
                    healed_with_counts = [
                        CompiledStep(
                            action=s.action,
                            selectors=s.selectors,
                            value=s.value,
                            post_condition=s.post_condition,
                            healed_count=step.healed_count + 1,
                        )
                        for s in bound_healed
                    ]
                    healed_indices.append(idx)
                    if healer_fn is not None:
                        candidate_steps[idx : idx + 1] = healed_with_counts
                        idx += len(healed_with_counts)
                    else:
                        candidate_steps[idx:] = healed_with_counts
                        break

            if readback_fn is not None:
                rb_raw = readback_fn(page, validated)
                if inspect.isawaitable(rb_raw):
                    rb_raw = await rb_raw
                rb_ok, rb_reason = _interpret_readback_outcome(rb_raw)
            else:
                rb_ok, rb_reason = await _default_fresh_reload_readback_async(
                    page, validated, plan.readback_expectations
                )
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            mode: JitMode = "healed" if healed_indices else "warm"
            est_usd = estimated_cold_usd if healed_indices else 0.0

            if not rb_ok:
                return JitResult(
                    mode=mode,
                    status="UNCONFIRMED_WRITE",
                    latency_ms=elapsed_ms,
                    estimated_usd=est_usd,
                    healed_steps=tuple(healed_indices),
                    plan_path=plan_path,
                    reason=rb_reason,
                    stats=plan.stats,
                )

            ms_saved = (
                max(0.0, plan.stats.last_cold_ms - elapsed_ms)
                if not healed_indices
                else 0.0
            )
            usd_saved = plan.stats.last_cold_usd if not healed_indices else 0.0
            new_stats = CompiledPlanStats(
                cold_runs=plan.stats.cold_runs,
                warm_runs=plan.stats.warm_runs + (0 if healed_indices else 1),
                heals=plan.stats.heals + len(healed_indices),
                total_ms_saved=plan.stats.total_ms_saved + ms_saved,
                total_usd_saved=plan.stats.total_usd_saved + usd_saved,
                last_cold_ms=plan.stats.last_cold_ms,
                last_cold_usd=plan.stats.last_cold_usd,
            )
            updated_plan = CompiledPlan(
                name=plan.name,
                description=plan.description,
                param_names=plan.param_names,
                secret_params=plan.secret_params,
                steps=tuple(candidate_steps),
                readback_mode=plan.readback_mode,
                readback_expectations=plan.readback_expectations,
                stats=new_stats,
                schema_version=plan.schema_version,
            )
            updated_plan.save(plan_path, params_to_audit=validated)
            receipt = _maybe_sign_receipt(
                plan=updated_plan,
                params=validated,
                verified=True,
                reason=rb_reason,
                signing_key=signing_key,
            )
            return JitResult(
                mode=mode,
                status="VERIFIED",
                latency_ms=elapsed_ms,
                estimated_usd=est_usd,
                receipt=receipt,
                healed_steps=tuple(healed_indices),
                plan_path=plan_path,
                reason=rb_reason,
                stats=new_stats,
            )

        return _async_wrapper

    return _decorator


__all__ = [
    "COMPILED_PLAN_SCHEMA",
    "CompiledPlan",
    "CompiledPlanStats",
    "CompiledStep",
    "DEFAULT_CACHE_DIR",
    "JitMode",
    "JitResult",
    "JitStatus",
    "ParameterBinder",
    "RecordingPageProxy",
    "StepAction",
    "StepReplayError",
    "format_scorecard",
    "jit_wrap",
    "replay_step_async",
    "replay_step_sync",
]
