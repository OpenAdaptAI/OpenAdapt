"""Thin shims that make the EXISTING adapters satisfy :class:`Environment`.

Nothing here re-implements an environment. Each shim is a mechanical wrapper
that (a) forwards ``reset`` / ``observe`` / ``act`` / ``close`` to an existing
adapter and (b) folds that family's native scoring -- ``BenchmarkAdapter.
evaluate`` + :class:`TaskVerifierRegistry`, or a flow ``EffectVerifier`` -- into
the single :meth:`Environment.verify` entry point.

Three conform today:

- :class:`BenchmarkAdapterEnvironment` -- wraps ANY
  :class:`~openadapt_evals.adapters.base.BenchmarkAdapter` (so ``WAAAdapter``,
  ``WAALiveAdapter``, ``WAAMockAdapter``, ``LocalAdapter`` all conform). Its
  ``verify`` prefers a registered :class:`TaskVerifierRegistry` verifier when
  ``task.verifier`` is set and falls back to the adapter's native
  ``evaluate``.
- :class:`MockMedAdapter` -- the MockMed fault server scored by the flow
  ``RestRecordVerifier`` (JSON system of record at ``/api/db``).
- :class:`OpenEMRAdapter` -- OpenEMR scored by the flow ``FhirEffectVerifier``
  (FHIR R4 system of record).

Everything heavy (``openadapt_flow``, a live browser, a live VM) is imported
lazily and every collaborator is injectable, so the shims are unit-testable
with fakes -- no flow extra, no VM, no browser, no network.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from openadapt_types import (
    BenchmarkAction,
    BenchmarkObservation,
    BenchmarkTask,
)

from openadapt_evals.evaluation.verifier_registry import (
    TaskVerifierRegistry,
    VerificationResult,
)
from openadapt_evals.evaluation.verifier_registry import (
    registry as global_registry,
)
from openadapt_evals.harness.protocol import Observation

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. Any BenchmarkAdapter -> Environment (WAA live/mock, Local, ...).
# ---------------------------------------------------------------------------


class BenchmarkAdapterEnvironment:
    """Adapt any :class:`BenchmarkAdapter` to the :class:`Environment` protocol.

    The mechanical shim from the trichotomy's adapter leg to the unified
    protocol. ``reset`` / ``act`` / ``close`` forward straight through;
    ``observe`` uses the adapter's own ``observe`` when it has one (the live/
    local adapters do) and otherwise replays the last observation captured from
    ``reset`` / ``act``.

    ``verify`` is the unification: it prefers a :class:`TaskVerifierRegistry`
    verifier (when ``task.verifier`` names one) and falls back to the adapter's
    native ``evaluate`` -- folding both legacy scoring paths into one call that
    always yields a :class:`VerificationResult`.

    Args:
        adapter: The wrapped ``BenchmarkAdapter``.
        env_name: Family label recorded on metrics rows (default: adapter.name).
        verifier_registry: Registry consulted by ``verify`` (default: the global
            registry, so built-in verifiers are available).
    """

    def __init__(
        self,
        adapter: Any,
        *,
        env_name: Optional[str] = None,
        verifier_registry: Optional[TaskVerifierRegistry] = None,
    ) -> None:
        self._adapter = adapter
        self._env_name = env_name or getattr(adapter, "name", "adapter")
        self._registry = (
            verifier_registry if verifier_registry is not None else global_registry
        )
        self._last_obs: Optional[BenchmarkObservation] = None

    @property
    def name(self) -> str:
        return self._env_name

    @property
    def adapter(self) -> Any:
        return self._adapter

    def reset(self, task: BenchmarkTask) -> Observation:
        self._last_obs = self._adapter.reset(task)
        return self._last_obs

    def observe(self) -> Observation:
        observe = getattr(self._adapter, "observe", None)
        if callable(observe):
            self._last_obs = observe()
            return self._last_obs
        if self._last_obs is None:
            raise RuntimeError(
                "no observation available: call reset() before observe(), or "
                "wrap an adapter that exposes observe()"
            )
        return self._last_obs

    def act(
        self, action: BenchmarkAction
    ) -> "tuple[Observation, bool, dict[str, Any]]":
        obs, done, info = self._adapter.step(action)
        self._last_obs = obs
        return obs, done, info

    def verify(self, task: BenchmarkTask) -> VerificationResult:
        """Unified scoring: registry verifier first, native ``evaluate`` fallback."""
        key = getattr(task, "verifier", None)
        if key and self._registry is not None and self._registry.has_verifier(key):
            result = self._registry.verify(key, self._adapter)
            result.details.setdefault("source", "verifier_registry")
            result.details.setdefault("verifier_key", key)
            return result

        # Fall back to the benchmark's own execution-based evaluator.
        result = self._adapter.evaluate(task)
        return VerificationResult(
            success=bool(getattr(result, "success", False)),
            score=float(getattr(result, "score", 0.0) or 0.0),
            details={
                "source": "adapter.evaluate",
                "reason": getattr(result, "reason", None),
                "num_steps": getattr(result, "num_steps", None),
            },
        )

    def close(self) -> None:
        close = getattr(self._adapter, "close", None)
        if callable(close):
            close()


# ---------------------------------------------------------------------------
# 2. Effect-verified web environments (MockMed, OpenEMR) via openadapt-flow.
# ---------------------------------------------------------------------------

# A verdict->result mapping shared by both web envs.
def _verdict_to_result(verdict: Any) -> VerificationResult:
    """Convert a flow ``EffectVerdict`` into a :class:`VerificationResult`.

    CONFIRMED -> success (score 1.0); REFUTED / INDETERMINATE -> failure
    (score 0.0). The three-valued verdict string is preserved in ``details``
    (``effect_verdict``) so the metrics row can distinguish "wrong write"
    (REFUTED) from "could not tell" (INDETERMINATE) -- the whole point of the
    effect-verification wedge.
    """
    verdict_str = getattr(getattr(verdict, "verdict", None), "value", None) or str(
        getattr(verdict, "verdict", "")
    )
    confirmed = bool(getattr(verdict, "confirmed", False))
    return VerificationResult(
        success=confirmed,
        score=1.0 if confirmed else 0.0,
        details={
            "source": "effect_verifier",
            "effect_verdict": verdict_str,
            "substrate": getattr(verdict, "substrate", ""),
            "reason": getattr(verdict, "reason", ""),
            "observed_count": getattr(verdict, "observed_count", None),
            "expected_count": getattr(verdict, "expected_count", None),
        },
    )


class _EffectVerifiedWebEnvironment:
    """Base for a Playwright-driven web env scored by a flow ``EffectVerifier``.

    Wraps the flow-side Playwright ``Backend`` (drives the page) and an
    ``EffectVerifier`` (reads the REAL system of record, never the screen).
    ``reset`` snapshots the pre-action system-of-record state; ``verify`` builds
    the task's :class:`Effect` and asks the verifier to rule on it.

    Both the backend and the verifier are injectable so this is testable with
    fakes; the real ones are built lazily from ``openadapt_flow`` only when not
    supplied. ``effect_builder`` maps a task to an ``Effect`` (default: validate
    ``task.evaluation_spec`` into an ``Effect`` -- bare-string selectors are
    coerced to ``ValueExpr`` by the flow model, matching v1 bundles).
    """

    substrate = "effect"

    def __init__(
        self,
        *,
        backend: Any = None,
        verifier: Any = None,
        effect_builder: Optional[Callable[[BenchmarkTask], Any]] = None,
    ) -> None:
        self._backend = backend
        self._verifier = verifier
        self._effect_builder = effect_builder or _default_effect_builder
        self._pre_state: Any = None
        self._last_obs: Optional[BenchmarkObservation] = None

    # -- lazy real collaborators (subclasses override _build_verifier) -------

    def _build_backend(self) -> Any:  # pragma: no cover - requires a live browser
        raise ImportError(
            "no backend supplied and no live Playwright backend configured; "
            "inject `backend=` (tests) or install 'openadapt-evals[flow]' and "
            "pass a launched PlaywrightBackend"
        )

    def _build_verifier(self) -> Any:  # pragma: no cover - subclass responsibility
        raise NotImplementedError

    @property
    def backend(self) -> Any:
        if self._backend is None:
            self._backend = self._build_backend()
        return self._backend

    @property
    def verifier(self) -> Any:
        if self._verifier is None:
            self._verifier = self._build_verifier()
        return self._verifier

    # -- Environment ---------------------------------------------------------

    def reset(self, task: BenchmarkTask) -> Observation:
        # Snapshot the system of record BEFORE the workflow runs so the verifier
        # can count only records this run wrote (delta / at-most-once).
        capture = getattr(self.verifier, "capture_pre_state", None)
        self._pre_state = capture() if callable(capture) else None
        self._last_obs = self.observe()
        return self._last_obs

    def observe(self) -> Observation:
        backend = self.backend
        screenshot = None
        shot = getattr(backend, "screenshot", None)
        if callable(shot):
            try:
                screenshot = shot()
            except Exception:  # noqa: BLE001 - observation is best-effort
                screenshot = None
        self._last_obs = BenchmarkObservation(
            screenshot=screenshot,
            viewport=getattr(backend, "viewport", None),
            url=getattr(backend, "url", None),
            window_title=getattr(backend, "page_title", None),
            raw_observation={"substrate": self.substrate},
        )
        return self._last_obs

    def act(
        self, action: BenchmarkAction
    ) -> "tuple[Observation, bool, dict[str, Any]]":
        """Translate a canonical action into a Playwright backend call."""
        backend = self.backend
        done = False
        if action.type in ("click", "double_click"):
            backend.click(
                int(action.x or 0),
                int(action.y or 0),
                double=action.type == "double_click",
            )
        elif action.type == "type":
            backend.type_text(action.text or "")
        elif action.type == "key":
            key = action.key or ""
            if action.modifiers:
                key = "+".join([*action.modifiers, key])
            backend.press(key)
        elif action.type == "scroll":
            amount = int(action.scroll_amount or 3)
            dy = amount if action.scroll_direction != "up" else -amount
            backend.scroll(0, dy)
        elif action.type in ("done", "answer"):
            done = True
        obs = self.observe()
        return obs, done, {"action_type": action.type}

    def verify(self, task: BenchmarkTask) -> VerificationResult:
        """Rule on the task's effect against the REAL system of record."""
        expected = self._effect_builder(task)
        verdict = self.verifier.verify(expected, self._pre_state)
        return _verdict_to_result(verdict)

    def close(self) -> None:
        close = getattr(self._backend, "close", None)
        if callable(close):
            close()


def _default_effect_builder(task: BenchmarkTask) -> Any:
    """Build a flow ``Effect`` from ``task.evaluation_spec`` (lazy import).

    The spec is the RFC ``Effect`` payload
    (e.g. ``{"kind": "record_written", "match": {"patient_id": "p1"}}``). Bare
    string selector values are coerced to ``ValueExpr`` by the flow model.
    """
    spec = getattr(task, "evaluation_spec", None)
    if not spec:
        raise ValueError(
            f"task {getattr(task, 'task_id', '?')!r} has no evaluation_spec to "
            "build an Effect from; set evaluation_spec or pass effect_builder="
        )
    from openadapt_flow.runtime.effects import Effect  # lazy: heavy

    return Effect.model_validate(spec)


class MockMedAdapter(_EffectVerifiedWebEnvironment):
    """MockMed (the flow reference clinic app + ``fault_server``) as an env.

    Scored by the flow :class:`RestRecordVerifier` against MockMed's JSON system
    of record (default ``/api/db``, records under the ``records`` key). The
    ``fault_server`` injects the transactional faults (phantom / partial /
    duplicate / lost-update) whose silent mishandling is exactly what the effect
    verifier catches.

    Args:
        base_url: MockMed base URL (e.g. ``http://localhost:8000``).
        records_path: System-of-record document path (default ``/api/db``).
        backend / verifier / effect_builder: Injectable for tests; real ones
            built lazily from ``openadapt_flow``.
    """

    substrate = "rest"

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        *,
        records_path: str = "/api/db",
        backend: Any = None,
        verifier: Any = None,
        effect_builder: Optional[Callable[[BenchmarkTask], Any]] = None,
    ) -> None:
        super().__init__(
            backend=backend, verifier=verifier, effect_builder=effect_builder
        )
        self._base_url = base_url
        self._records_path = records_path

    def _build_verifier(self) -> Any:  # pragma: no cover - needs flow extra
        from openadapt_flow.runtime.effects import RestRecordVerifier

        return RestRecordVerifier(self._base_url, records_path=self._records_path)


class OpenEMRAdapter(_EffectVerifiedWebEnvironment):
    """OpenEMR (the flagship real EMR) as an env.

    Scored by the flow :class:`FhirEffectVerifier` against OpenEMR's FHIR R4 API
    -- the primary, real-EMR system of record from the OpenEMR flagship
    benchmark. (An OCR fallback verifier can be swapped in via ``verifier=`` when
    FHIR is unavailable; FHIR is the default and primary.)

    Args:
        base_url: OpenEMR FHIR base URL (e.g. ``https://host/apis/default/fhir``).
        resource_type: FHIR resource searched (default ``Observation``).
        access_token: OAuth2 bearer token for the FHIR API.
        backend / verifier / effect_builder: Injectable for tests; real ones
            built lazily from ``openadapt_flow``.
    """

    substrate = "fhir"

    def __init__(
        self,
        base_url: str = "http://localhost/apis/default/fhir",
        *,
        resource_type: str = "Observation",
        access_token: Optional[str] = None,
        search_params: Optional[dict] = None,
        backend: Any = None,
        verifier: Any = None,
        effect_builder: Optional[Callable[[BenchmarkTask], Any]] = None,
    ) -> None:
        super().__init__(
            backend=backend, verifier=verifier, effect_builder=effect_builder
        )
        self._base_url = base_url
        self._resource_type = resource_type
        self._access_token = access_token
        self._search_params = search_params

    def _build_verifier(self) -> Any:  # pragma: no cover - needs flow extra
        from openadapt_flow.runtime.effects import FhirEffectVerifier

        return FhirEffectVerifier(
            self._base_url,
            resource_type=self._resource_type,
            search_params=self._search_params,
            access_token=self._access_token,
        )


__all__ = [
    "BenchmarkAdapterEnvironment",
    "MockMedAdapter",
    "OpenEMRAdapter",
]
