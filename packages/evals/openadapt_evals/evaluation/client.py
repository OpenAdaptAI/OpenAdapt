"""Client-side evaluator for WAA benchmarks.

Runs WAA evaluators locally, making HTTP calls to the WAA server's /execute endpoint.
This approach follows WAA's own design pattern and eliminates the need for a sidecar service.

Fallback metrics are provided by ``evaluation.metrics`` (shared with
``server/evaluate_endpoint.py``).

**Why this module refuses to degrade silently.** Every value produced here ends
up as a published benchmark number. A broken WAA evaluators install, an
unreachable VM, and a task the agent genuinely failed all used to collapse into
the same ``score=0.0, success=False`` shape, so a caller could not tell a
measured failure from an unmeasured one. Two things now keep the distinction
representable:

* ``EvaluatorClient(require_waa_evaluators=True)`` (the default) raises
  :class:`EvaluatorsUnavailableError` at construction rather than quietly
  substituting the built-in fallback evaluator for the rest of the run.
* Every :class:`EvaluationResult` carries ``error_type`` and
  ``evaluator_source``. ``result.scored`` is ``False`` whenever the number in
  ``score`` was not produced by a working evaluator, and aggregators must
  exclude those rows rather than count them as zeros.
"""

import logging
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from openadapt_evals.evaluation.receipts import (
    ReceiptValidationError,
    parse_strict_json_response,
    require_successful_receipt,
)

logger = logging.getLogger(__name__)


class EvaluatorsUnavailableError(RuntimeError):
    """The WAA evaluators package could not be loaded.

    Raised instead of silently falling back to the built-in evaluator, because
    a fallback-scored run and a WAA-scored run are not comparable and used to
    be indistinguishable.
    """


class EvaluationError(RuntimeError):
    """An evaluation could not be carried out (as opposed to failing)."""


class EvaluationInfrastructureError(EvaluationError):
    """The evaluator could not run because its remote dependency failed."""


@dataclass
class EvaluationResult:
    """Result of evaluating a benchmark task.

    ``error_type`` is the tri-state that separates "measured" from "could not
    measure":

    * ``None`` -- ``score``/``success`` are a real measurement.
    * ``"infrastructure"`` -- the VM or server could not be reached; ``score``
      is not a measurement and must not be aggregated.
    * ``"evaluation"`` -- the VM answered but the evaluator itself could not
      run (missing getter, metric raised, unknown metric name); ``score`` is
      likewise not a measurement.
    """

    success: bool
    score: float
    actual: Any = None
    expected: Any = None
    reason: str = ""
    metrics: Dict[str, Any] = field(default_factory=dict)
    error_type: Optional[str] = None
    evaluator_source: str = "waa"

    @property
    def scored(self) -> bool:
        """True only when ``score`` is a real measurement."""
        return self.error_type is None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "success": self.success,
            "score": self.score,
            "actual": str(self.actual)[:500] if self.actual is not None else None,
            "expected": (
                str(self.expected)[:500] if self.expected is not None else None
            ),
            "reason": self.reason,
            "metrics": self.metrics,
            "error_type": self.error_type,
            "evaluator_source": self.evaluator_source,
            "scored": self.scored,
        }


class EvaluatorClient:
    """Client-side evaluator that uses WAA's evaluators directly.

    This client imports WAA's evaluator modules (getters, metrics) and runs them
    locally. The getters make HTTP calls to the WAA server's /execute endpoint
    to retrieve values from the Windows VM.

    Example:
        client = EvaluatorClient()  # Auto-detects VM IP
        result = client.evaluate(task_config)
    """

    def __init__(
        self,
        vm_ip: Optional[str] = None,
        port: int = 5000,
        waa_evaluators_path: Optional[Path] = None,
        timeout: int = 30,
        require_waa_evaluators: bool = True,
    ):
        """Initialize the evaluator client.

        Args:
            vm_ip: VM IP address. If None, auto-detects from multiple sources.
            port: WAA server port (default 5000).
            waa_evaluators_path: Path to WAA evaluators. If None, searches common locations.
            timeout: HTTP request timeout in seconds.
            require_waa_evaluators: When True (the default), a missing or
                broken WAA evaluators package raises
                :class:`EvaluatorsUnavailableError` instead of degrading to the
                built-in fallback evaluator. Pass False only when the built-in
                evaluator is genuinely what you want; results are then stamped
                ``evaluator_source="fallback"`` so the choice stays visible in
                every downstream record.

        Raises:
            ValueError: If the VM IP cannot be determined.
            EvaluatorsUnavailableError: If ``require_waa_evaluators`` is True
                and the WAA evaluators package is missing or unimportable.
        """
        from .discovery import discover_vm_ip

        self.vm_ip = vm_ip or discover_vm_ip()
        if not self.vm_ip:
            raise ValueError(
                "Could not auto-detect VM IP. Please provide vm_ip explicitly or "
                "set WAA_VM_IP environment variable."
            )

        self.port = port
        self.timeout = timeout
        self.base_url = f"http://{self.vm_ip}:{self.port}"

        # Find and load WAA evaluators
        self._searched_paths: List[Path] = []
        self._evaluators_path = waa_evaluators_path or self._find_evaluators_path()
        self._getters = None
        self._metrics = None
        self._evaluators_error: Optional[str] = None
        self._load_evaluators()

        if self._evaluators_error is not None:
            if require_waa_evaluators:
                raise EvaluatorsUnavailableError(
                    f"{self._evaluators_error} Every task this client evaluated "
                    "would be scored by the built-in fallback evaluator instead "
                    "of WAA's, and the resulting numbers are not comparable with "
                    "WAA-scored runs. Install the WAA evaluators, pass "
                    "waa_evaluators_path explicitly, or construct the client "
                    "with require_waa_evaluators=False to accept fallback "
                    "scoring deliberately."
                )
            logger.warning(
                "%s Continuing with the built-in fallback evaluator because "
                "require_waa_evaluators=False; results are stamped "
                "evaluator_source='fallback'.",
                self._evaluators_error,
            )

    @property
    def evaluator_source(self) -> str:
        """``"waa"`` when WAA's own evaluators are loaded, else ``"fallback"``."""
        return "waa" if self._getters is not None else "fallback"

    @property
    def evaluators_error(self) -> Optional[str]:
        """Why the WAA evaluators are unavailable, or None if they loaded."""
        return self._evaluators_error

    def _find_evaluators_path(self) -> Optional[Path]:
        """Find WAA evaluators in common locations."""
        search_paths = [
            # Relative to openadapt-ml
            Path(__file__).parent.parent.parent.parent / "openadapt-ml" / "vendor" / "WindowsAgentArena" / "src" / "win-arena-container" / "client" / "desktop_env",
            # Relative to current file in openadapt-evals
            Path(__file__).parent.parent.parent.parent / "vendor" / "WindowsAgentArena" / "src" / "win-arena-container" / "client" / "desktop_env",
            # Absolute common locations
            Path.home() / "WindowsAgentArena" / "src" / "win-arena-container" / "client" / "desktop_env",
            Path("/opt/waa/client/desktop_env"),
        ]
        self._searched_paths = list(search_paths)

        for path in search_paths:
            evaluators_dir = path / "evaluators"
            if evaluators_dir.exists() and (evaluators_dir / "getters.py").exists():
                return path

        return None

    def _load_evaluators(self) -> None:
        """Load WAA evaluator modules, recording why if they cannot be loaded.

        Sets ``self._evaluators_error`` to a non-None string on failure. The two
        failure modes are kept distinct in the message: nothing was found at any
        candidate path (a legitimately absent optional install) versus something
        was found but would not import (a broken install). Both used to leave
        ``_getters``/``_metrics`` as None with no record, which is what made a
        broken install indistinguishable from a healthy one.
        """
        if not self._evaluators_path:
            searched = ", ".join(str(p) for p in self._searched_paths) or "<none>"
            self._evaluators_error = (
                f"WAA evaluators package not found; searched: {searched}."
            )
            return

        if not (self._evaluators_path / "evaluators").is_dir():
            # An explicitly-supplied path that does not hold an evaluators
            # package is "absent", not "broken" -- keep the two apart.
            self._evaluators_error = (
                f"WAA evaluators package not found under the supplied path "
                f"{self._evaluators_path}."
            )
            return

        # Add to sys.path if not already there
        path_str = str(self._evaluators_path)
        if path_str not in sys.path:
            sys.path.insert(0, path_str)

        # Also add parent for absolute imports
        parent_str = str(self._evaluators_path.parent)
        if parent_str not in sys.path:
            sys.path.insert(0, parent_str)

        try:
            from evaluators import getters, metrics
            self._getters = getters
            self._metrics = metrics
        except ImportError as e:
            self._evaluators_error = (
                f"WAA evaluators found at {self._evaluators_path} but not "
                f"importable ({e}) -- the install is broken, not absent."
            )

    def evaluate(self, task_config: Dict[str, Any]) -> EvaluationResult:
        """Evaluate a benchmark task.

        Args:
            task_config: Task configuration with 'evaluator' section containing:
                - result: Dict with 'type' specifying the getter function
                - expected: Dict with 'value' or 'rules' specifying expected result
                - func: Metric function name (default: 'exact_match')

        Returns:
            EvaluationResult. Check ``result.scored`` before treating
            ``result.score`` as a measurement: an unreachable VM or a broken
            evaluator yields ``score=0.0`` with ``error_type`` set, which is not
            the same thing as a task the agent failed.
        """
        if not isinstance(task_config, dict):
            return EvaluationResult(
                success=False,
                score=0.0,
                reason="Task config must be an object",
                error_type="evaluation",
                evaluator_source=self.evaluator_source,
            )
        evaluator_config = task_config.get("evaluator", {})

        if not isinstance(evaluator_config, dict) or not evaluator_config:
            # A task with no evaluator spec cannot be scored at all. This is a
            # configuration error, not a task the agent failed.
            return EvaluationResult(
                success=False,
                score=0.0,
                reason="No evaluator configuration in task",
                error_type="evaluation",
                evaluator_source=self.evaluator_source,
            )

        try:
            # Get actual value from VM
            actual = self._get_actual_value(evaluator_config)

            # Get expected value from config
            expected = self._get_expected_value(evaluator_config)

            # Run metric comparison
            score = self._run_metric(evaluator_config, actual, expected)

            return EvaluationResult(
                success=score >= 1.0,
                score=score,
                actual=actual,
                expected=expected,
                reason=f"Metric returned score {score}",
                metrics={"raw_score": score},
                evaluator_source=self.evaluator_source,
            )

        except (requests.RequestException, EvaluationInfrastructureError) as e:
            # The VM never answered. Scoring this 0.0 with no marker is how an
            # unreachable backend gets published as a legitimate 0%.
            return EvaluationResult(
                success=False,
                score=0.0,
                reason=f"VM unreachable during evaluation: {e}",
                error_type="infrastructure",
                evaluator_source=self.evaluator_source,
            )
        except Exception as e:
            return EvaluationResult(
                success=False,
                score=0.0,
                reason=f"Evaluation error: {str(e)}",
                error_type="evaluation",
                evaluator_source=self.evaluator_source,
            )

    def _get_actual_value(self, evaluator_config: Dict[str, Any]) -> Any:
        """Get actual value from VM using getter function."""
        result_spec = evaluator_config.get("result")
        if not isinstance(result_spec, dict) or not result_spec:
            raise EvaluationError(
                "evaluator result contract must be a non-empty object"
            )
        getter_type = result_spec.get("type")

        if not isinstance(getter_type, str) or not getter_type.strip():
            raise EvaluationError("No 'type' specified in evaluator.result")
        getter_type = getter_type.strip()

        required_fields = {
            "file_content": ("path",),
            "registry_value": ("key", "value"),
            "process_running": ("process",),
            "window_exists": ("title",),
            "vm_command_line": ("command",),
            "vm_file": ("path",),
        }.get(getter_type, ())
        for field_name in required_fields:
            self._require_getter_parameter(result_spec, field_name, getter_type)

        # Create a mock env object that the getters expect
        class HttpEnv:
            def __init__(self, vm_ip: str, port: int, timeout: int):
                self.vm_ip = vm_ip
                self.port = port
                self.timeout = timeout

            def execute(self, command: str) -> Dict[str, Any]:
                """Execute command on VM via HTTP.

                Propagates the transport error rather than returning an empty
                ``output``: an unreachable VM that returns ``""`` compares equal
                to nothing and scores 0.0, which is indistinguishable from the
                command having genuinely produced no output.
                """
                url = f"http://{self.vm_ip}:{self.port}/execute"
                response = requests.post(
                    url,
                    json={"command": command},
                    timeout=self.timeout,
                )
                response.raise_for_status()
                try:
                    payload = parse_strict_json_response(
                        response,
                        context="VM command",
                    )
                    return require_successful_receipt(
                        payload,
                        context="VM command",
                        require_output=True,
                    )
                except ReceiptValidationError as exc:
                    raise EvaluationInfrastructureError(str(exc)) from exc

        env = HttpEnv(self.vm_ip, self.port, self.timeout)

        # Try WAA getter if available
        if self._getters:
            getter_func = getattr(self._getters, f"get_{getter_type}", None)
            if getter_func:
                return getter_func(env, result_spec)

        # Fallback: direct HTTP call
        return self._fallback_getter(env, getter_type, result_spec)

    def _fallback_getter(self, env: Any, getter_type: str, spec: Dict[str, Any]) -> Any:
        """Fallback getter implementation when WAA evaluators not available."""
        # Common getter types
        if getter_type == "file_content":
            path = self._require_getter_parameter(spec, "path", getter_type)
            result = env.execute(f"type {path}")
            return self._require_output(result, getter_type)

        elif getter_type == "registry_value":
            key = self._require_getter_parameter(spec, "key", getter_type)
            value = self._require_getter_parameter(spec, "value", getter_type)
            result = env.execute(f'reg query "{key}" /v "{value}"')
            return self._require_output(result, getter_type)

        elif getter_type == "process_running":
            process = self._require_getter_parameter(spec, "process", getter_type)
            result = env.execute(f'tasklist /FI "IMAGENAME eq {process}"')
            return process.lower() in self._require_output(result, getter_type).lower()

        elif getter_type == "window_exists":
            title = self._require_getter_parameter(spec, "title", getter_type)
            result = env.execute(f'powershell "Get-Process | Where-Object {{$_.MainWindowTitle -like \'*{title}*\'}}"')
            return bool(self._require_output(result, getter_type).strip())

        else:
            raise ValueError(f"Unknown getter type: {getter_type}")

    @staticmethod
    def _require_output(result: Dict[str, Any], getter_type: str) -> str:
        """Return the command output, or raise if the VM reported an error.

        ``{"error": ..., "output": ""}`` and ``{"output": ""}`` are not the same
        thing; treating them alike is how a failed command becomes a measured
        empty result.
        """
        try:
            result = require_successful_receipt(
                result,
                context=f"getter {getter_type!r}",
                require_output=True,
            )
        except ReceiptValidationError as exc:
            raise EvaluationInfrastructureError(str(exc)) from exc
        output = result["output"]
        assert isinstance(output, str)
        return output

    @staticmethod
    def _require_getter_parameter(
        spec: Dict[str, Any], name: str, getter_type: str
    ) -> str:
        """Return one required non-empty string getter parameter."""
        value = spec.get(name)
        if not isinstance(value, str) or not value.strip():
            raise EvaluationError(
                f"getter '{getter_type}' requires non-empty '{name}'"
            )
        return value.strip()

    def _get_expected_value(self, evaluator_config: Dict[str, Any]) -> Any:
        """Extract expected value from evaluator config."""
        if "expected" not in evaluator_config:
            raise EvaluationError("evaluator has no expected-value contract")
        expected_spec = evaluator_config["expected"]
        if not isinstance(expected_spec, dict) or not expected_spec:
            raise EvaluationError("expected-value contract must be a non-empty object")

        # Direct value
        if "value" in expected_spec:
            value = expected_spec["value"]
            if value is None:
                raise EvaluationError("expected-value contract cannot be null")
            return value

        # Rules-based
        rules = expected_spec.get("rules")
        if isinstance(rules, dict) and "match" in rules and rules["match"] is not None:
            return rules["match"]

        raise EvaluationError("expected-value contract is malformed")

    def _run_metric(self, evaluator_config: Dict[str, Any], actual: Any, expected: Any) -> float:
        """Run metric function to compare actual vs expected.

        A WAA metric that raises is an evaluation failure, not a reason to
        quietly re-score the task with a different (built-in) metric: the two
        metrics do not agree, so substituting one for the other publishes a
        number nobody asked for.
        """
        func_name = evaluator_config.get("func", "exact_match")
        if not isinstance(func_name, str) or not func_name:
            raise EvaluationError("metric name must be a non-empty string")
        options = evaluator_config.get("options", {})
        if not isinstance(options, dict):
            raise EvaluationError("metric options must be an object")
        if evaluator_config.get("conj", "and") != "and":
            raise EvaluationError(
                "EvaluatorClient supports only one metric with conj='and'"
            )

        # Try WAA metric if available
        if self._metrics:
            metric_func = getattr(self._metrics, func_name, None)
            if metric_func:
                try:
                    score = metric_func(actual, expected, **options)
                except Exception as e:
                    raise EvaluationError(
                        f"WAA metric '{func_name}' raised while scoring: {e}"
                    ) from e
                return self._require_metric_score(score, func_name)

        # Fallback metrics
        return self._fallback_metric(func_name, actual, expected, options)

    def _fallback_metric(
        self,
        func_name: str,
        actual: Any,
        expected: Any,
        options: dict[str, Any] | None = None,
    ) -> float:
        """Fallback metric — delegates to shared evaluation.metrics module.

        An unknown metric name raises. Silently substituting ``exact_match``
        scores the task with the wrong comparison and reports the result as if
        the requested metric had run.
        """
        from openadapt_evals.evaluation.metrics import get_metric

        metric_fn = get_metric(func_name)
        if metric_fn is None:
            raise EvaluationError(
                f"metric '{func_name}' is not implemented by the built-in "
                f"fallback evaluator; scoring with a different metric would "
                f"publish a number the task config did not ask for"
            )
        return self._require_metric_score(
            metric_fn(actual, expected, **(options or {})),
            func_name,
        )

    @staticmethod
    def _require_metric_score(value: Any, metric_name: str) -> float:
        """Return one finite score in the closed unit interval."""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EvaluationError(
                f"metric '{metric_name}' returned a non-numeric score"
            )
        score = float(value)
        if not math.isfinite(score) or not 0.0 <= score <= 1.0:
            raise EvaluationError(
                f"metric '{metric_name}' returned an invalid score: {value!r}"
            )
        return score

    def health_check(self) -> bool:
        """Check if WAA server is reachable.

        A False here means "probed and did not get a healthy answer", which is
        the honest answer to the question this method asks; it is not used as a
        task score.
        """
        try:
            response = requests.get(
                f"{self.base_url}/probe",
                timeout=5
            )
            return response.status_code == 200
        except requests.RequestException:
            return False
