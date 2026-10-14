"""WAA /evaluate endpoint implementation.

This module provides the /evaluate endpoint for the Windows Agent Arena Flask server.
It calls WAA's existing evaluator logic (getters and metrics) to determine task success.

Deployment:
    This code should be added to or imported by the WAA server's main.py file.
    The WAA server runs inside the Windows VM at:
    WindowsAgentArena/src/win-arena-container/vm/setup/server/main.py

Usage (on WAA server):
    from openadapt_evals.server.evaluate_endpoint import create_evaluate_blueprint

    # Register the blueprint
    evaluate_bp = create_evaluate_blueprint(evaluators_path="/path/to/evaluators")
    app.register_blueprint(evaluate_bp)

    # Or import and call directly
    from openadapt_evals.server.evaluate_endpoint import evaluate_task_state
    result = evaluate_task_state(task_config, vm_env)

See also:
    - docs/research/waa-evaluator-integration.md for full documentation
    - WindowsAgentArena/src/win-arena-container/client/desktop_env/evaluators/
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any, Callable

import requests

from openadapt_evals.evaluation.receipts import (
    parse_strict_json_response,
    require_successful_receipt,
)

logger = logging.getLogger(__name__)


class EvaluationNotRunError(RuntimeError):
    """The evaluation could not be carried out, as opposed to failing.

    Every path that used to answer a broken getter or a raising metric with
    ``score=0.0`` raises this instead. The endpoint turns it into a response
    carrying ``scored: false`` and an ``error_type``, so an unreachable VM is
    never accepted upstream as a legitimate 0% benchmark result.
    """

    def __init__(self, message: str, error_type: str = "evaluation") -> None:
        super().__init__(message)
        self.error_type = error_type


# Try to import Flask for blueprint creation
try:
    from flask import Blueprint, jsonify, request

    FLASK_AVAILABLE = True
except ImportError:
    FLASK_AVAILABLE = False

# WAA evaluator modules (lazy loaded when on the server)
_getters_module = None
_metrics_module = None


def _load_waa_evaluators(evaluators_path: str | None = None) -> tuple:
    """Load WAA evaluator modules.

    Args:
        evaluators_path: Path to WAA evaluators directory. If None, attempts
            to find it in standard locations.

    Returns:
        Tuple of (getters_module, metrics_module).

    Raises:
        ImportError: If evaluators cannot be loaded.
    """
    global _getters_module, _metrics_module

    if _getters_module is not None and _metrics_module is not None:
        return _getters_module, _metrics_module

    import sys

    # Standard paths to check
    search_paths = []
    if evaluators_path:
        search_paths.append(evaluators_path)

    # Common WAA installation paths
    search_paths.extend([
        "/home/azureuser/WindowsAgentArena/src/win-arena-container/client/desktop_env",
        "C:/WAA/client/desktop_env",
        "/waa/client/desktop_env",
    ])

    for path in search_paths:
        client_path = Path(path)
        evaluators_dir = client_path / "evaluators"

        if evaluators_dir.exists():
            # Add to path
            if str(client_path) not in sys.path:
                sys.path.insert(0, str(client_path))

            try:
                from evaluators import getters, metrics

                _getters_module = getters
                _metrics_module = metrics
                logger.info(f"Loaded WAA evaluators from {evaluators_dir}")
                return _getters_module, _metrics_module
            except ImportError as e:
                logger.warning(f"Failed to import evaluators from {path}: {e}")
                continue

    raise ImportError(
        "Could not load WAA evaluators. Ensure WindowsAgentArena is installed "
        f"and evaluators are available. Searched: {search_paths}"
    )


class MockEnv:
    """Minimal environment object for WAA getters.

    WAA getter functions expect an 'env' object with vm_ip attribute
    for making HTTP calls to the VM. This mock provides that interface.
    """

    def __init__(self, vm_ip: str = "localhost", port: int = 5000):
        self.vm_ip = vm_ip
        self.port = port


def get_actual_value(
    evaluator_config: dict,
    env: MockEnv | None = None,
    getters: Any | None = None,
) -> Any:
    """Get the actual value from the VM using WAA getters.

    Args:
        evaluator_config: Task evaluator configuration with 'result' spec.
        env: Environment object with vm_ip for getter calls.
        getters: WAA getters module (loaded automatically if None).

    Returns:
        The actual value retrieved from the VM.
    """
    if env is None:
        env = MockEnv()

    if getters is None:
        getters, _ = _load_waa_evaluators()

    if "result" not in evaluator_config:
        raise EvaluationNotRunError("evaluator has no result contract")
    result_spec = evaluator_config["result"]
    if not isinstance(result_spec, dict) or not result_spec:
        raise EvaluationNotRunError("evaluator result contract must be a non-empty object")
    result_type = result_spec.get("type")
    if not isinstance(result_type, str) or not result_type:
        raise EvaluationNotRunError("evaluator result contract requires an explicit type")
    required_field = {"vm_command_line": "command", "vm_file": "path"}.get(
        result_type
    )
    if required_field is not None:
        required_value = result_spec.get(required_field)
        if not isinstance(required_value, str) or not required_value.strip():
            raise EvaluationNotRunError(
                f"evaluator result type {result_type!r} requires {required_field}"
            )

    # Get the getter function
    getter_name = f"get_{result_type}"
    getter_func = getattr(getters, getter_name, None)

    if getter_func is None:
        # A getter the deployed evaluators do not implement cannot score this
        # task. Returning None made it compare unequal to `expected` and score
        # 0.0, which reads downstream as "the agent failed".
        raise EvaluationNotRunError(
            f"getter '{getter_name}' is not implemented by the deployed WAA "
            f"evaluators; the task was not scored"
        )

    try:
        return getter_func(env, result_spec)
    except Exception as e:
        logger.error(f"Getter {getter_name} failed: {e}")
        raise EvaluationNotRunError(
            f"getter '{getter_name}' raised while reading the VM: {e}",
            error_type="infrastructure",
        ) from e


def get_expected_value(
    evaluator_config: dict,
    env: MockEnv | None = None,
    getters: Any | None = None,
) -> Any:
    """Get the expected value for comparison.

    The expected value can be:
    - A literal value in the config
    - A rule-based match criteria
    - Retrieved via a getter function

    Args:
        evaluator_config: Task evaluator configuration with 'expected' spec.
        env: Environment object for getter calls.
        getters: WAA getters module.

    Returns:
        The expected value or match criteria.
    """
    if env is None:
        env = MockEnv()

    if getters is None:
        getters, _ = _load_waa_evaluators()

    if "expected" not in evaluator_config:
        raise EvaluationNotRunError("evaluator has no expected-value contract")

    expected_spec = evaluator_config["expected"]
    if not isinstance(expected_spec, dict):
        if expected_spec is None:
            raise EvaluationNotRunError("expected-value contract cannot be null")
        return expected_spec
    if not expected_spec:
        raise EvaluationNotRunError("expected-value contract is empty")

    # Handle different expected value formats
    expected_type = expected_spec.get("type")

    if expected_type == "rule":
        # Rule-based matching - return the rules dict
        rules = expected_spec.get("rules")
        if not isinstance(rules, dict) or not rules:
            raise EvaluationNotRunError("expected rule contract is missing its rules")
        return rules

    if expected_type == "literal" or "value" in expected_spec:
        # Direct literal value
        if "value" not in expected_spec or expected_spec["value"] is None:
            raise EvaluationNotRunError("expected literal contract has no value")
        return expected_spec["value"]

    if expected_type:
        # Get via getter function
        getter_name = f"get_{expected_type}"
        getter_func = getattr(getters, getter_name, None)

        if getter_func is None:
            raise EvaluationNotRunError(
                f"expected getter '{getter_name}' is not implemented"
            )
        try:
            value = getter_func(env, expected_spec)
        except Exception as e:
            logger.error(f"Expected getter {getter_name} failed: {e}")
            raise EvaluationNotRunError(
                f"expected getter '{getter_name}' raised: {e}",
                error_type="infrastructure",
            ) from e
        if value is None:
            raise EvaluationNotRunError(
                f"expected getter '{getter_name}' returned no value"
            )
        return value

    raise EvaluationNotRunError("expected-value contract is malformed")


def run_metric(
    metric_name: str,
    actual: Any,
    expected: Any,
    options: dict | None = None,
    metrics: Any | None = None,
) -> float:
    """Run a WAA metric function to compare actual vs expected.

    Args:
        metric_name: Name of the metric function (e.g., "exact_match").
        actual: The actual value from the VM.
        expected: The expected value.
        options: Additional options to pass to the metric.
        metrics: WAA metrics module (loaded automatically if None).

    Returns:
        Score from 0.0 to 1.0.
    """
    if not isinstance(metric_name, str) or not metric_name:
        raise EvaluationNotRunError("metric name must be a non-empty string")
    if metrics is None:
        _, metrics = _load_waa_evaluators()

    if options is None:
        options = {}

    # Get the metric function
    metric_func = getattr(metrics, metric_name, None)

    if metric_func is None:
        # Substituting exact_match for a metric the task asked for scores the
        # task with a different comparison and reports it as if the requested
        # metric had run. "Metric not implemented" is not "the agent failed".
        raise EvaluationNotRunError(
            f"metric '{metric_name}' is not implemented by the deployed WAA "
            f"evaluators; refusing to substitute a different metric"
        )

    try:
        score = metric_func(actual, expected, **options)
    except Exception as e:
        logger.error(f"Metric {metric_name} failed: {e}")
        raise EvaluationNotRunError(
            f"metric '{metric_name}' raised while scoring: {e}"
        ) from e
    if (
        isinstance(score, bool)
        or not isinstance(score, (int, float))
        or not math.isfinite(float(score))
        or not 0.0 <= float(score) <= 1.0
    ):
        raise EvaluationNotRunError(
            f"metric '{metric_name}' returned an invalid score: {score!r}"
        )
    return float(score)


def evaluate_task_state(
    task_config: dict,
    env: MockEnv | None = None,
    evaluators_path: str | None = None,
) -> dict:
    """Evaluate the current VM state against task success criteria.

    This is the main evaluation function that orchestrates:
    1. Running any postconfig setup (e.g., activating windows)
    2. Getting the actual value from the VM via getters
    3. Getting the expected value
    4. Running metric comparison(s)
    5. Combining results if multiple metrics

    Args:
        task_config: Full task configuration including 'evaluator' spec.
        env: Environment object for VM access.
        evaluators_path: Path to WAA evaluators directory.

    Returns:
        Dict with evaluation results:
        {
            "success": bool,
            "score": float,  # 0.0 to 1.0
            "actual": Any,   # Truncated for response size
            "expected": Any, # Truncated for response size
            "reason": str,   # Explanation
            "metrics": list, # Per-metric results if multiple
        }
    """
    if not isinstance(task_config, dict):
        return _not_scored("Task config must be an object", "evaluation")
    if env is None:
        env = MockEnv()

    # Load evaluators
    try:
        getters, metrics = _load_waa_evaluators(evaluators_path)
    except ImportError as e:
        return _not_scored(f"Failed to load evaluators: {e}", "evaluation")

    evaluator_config = task_config.get("evaluator", {})

    if not isinstance(evaluator_config, dict) or not evaluator_config:
        return _not_scored("No evaluator configuration in task", "evaluation")

    # Handle infeasible tasks. The marker is part of the scoring contract, so
    # truthy strings or numbers must not turn an agent refusal into success.
    infeasible = evaluator_config.get("infeasible", False)
    if not isinstance(infeasible, bool):
        return _not_scored("Evaluator infeasible must be a boolean", "evaluation")
    if infeasible:
        # Check if agent's last action was a FAIL/infeasible signal
        agent_last_action = task_config.get("agent_last_action", "")
        if not isinstance(agent_last_action, str):
            return _not_scored("Agent last action must be a string", "evaluation")
        if agent_last_action.upper() in ("FAIL", "INFEASIBLE", "IMPOSSIBLE"):
            return {
                "success": True,
                "score": 1.0,
                "reason": "Correctly identified infeasible task",
                "scored": True,
                "error_type": None,
            }

    # Run postconfig if present (e.g., activate windows for inspection)
    postconfig = evaluator_config.get("postconfig", [])
    if "postconfig" in evaluator_config and not isinstance(postconfig, list):
        return _not_scored("Evaluator postconfig must be a list", "evaluation")
    if postconfig:
        try:
            _run_postconfig(postconfig, env)
        except EvaluationNotRunError as e:
            return _not_scored(str(e), e.error_type)

    # Get actual value from VM
    try:
        actual = get_actual_value(evaluator_config, env, getters)

        # Get expected value
        expected = get_expected_value(evaluator_config, env, getters)
    except EvaluationNotRunError as e:
        return _not_scored(str(e), e.error_type)

    # Get metric function(s)
    func_spec = evaluator_config.get("func", "exact_match")
    options = evaluator_config.get("options", {})
    conjunction = evaluator_config.get("conj", "and")  # "and" or "or"
    if not isinstance(options, dict):
        return _not_scored("Metric options must be an object", "evaluation")
    if conjunction not in ("and", "or"):
        return _not_scored("Metric conjunction must be 'and' or 'or'", "evaluation")

    # Handle single or multiple metrics
    if isinstance(func_spec, str):
        func_names = [func_spec]
    elif isinstance(func_spec, list) and all(
        isinstance(name, str) and name for name in func_spec
    ):
        func_names = func_spec
    else:
        return _not_scored("Metric func must name one or more metrics", "evaluation")

    # Run each metric
    metric_results = []
    for func_name in func_names:
        try:
            score = run_metric(func_name, actual, expected, options, metrics)
        except EvaluationNotRunError as e:
            return _not_scored(str(e), e.error_type)
        metric_results.append({
            "metric": func_name,
            "score": score,
            "success": score >= 1.0,
        })

    # Guard empty metric_results (e.g. func_spec was [])
    if not metric_results:
        return _not_scored(
            "No metrics could be computed (empty func spec)", "evaluation",
        )

    # Combine results based on conjunction
    if conjunction == "or":
        final_score = max(r["score"] for r in metric_results)
        success = any(r["success"] for r in metric_results)
    else:  # "and"
        final_score = min(r["score"] for r in metric_results)
        success = all(r["success"] for r in metric_results)

    # Truncate large values for response
    actual_str = _truncate_value(actual, max_len=500)
    expected_str = _truncate_value(expected, max_len=500)

    # Build reason string
    if success:
        reason = f"Task completed successfully (score={final_score:.2f})"
    else:
        reason = f"Task not completed (score={final_score:.2f})"
        if actual is None:
            reason += " - could not retrieve actual value"
        elif expected is None:
            reason += " - could not determine expected value"

    return {
        "success": success,
        "score": final_score,
        "actual": actual_str,
        "expected": expected_str,
        "reason": reason,
        "metrics": metric_results if len(metric_results) > 1 else None,
        # This one IS a measurement.
        "scored": True,
        "error_type": None,
    }


def _not_scored(reason: str, error_type: str) -> dict:
    """Build a response for a task that was NOT scored.

    ``score``/``success`` are present for schema compatibility with old
    consumers, but ``scored: false`` is the field that matters: nothing here
    was measured, and aggregating this row as a zero publishes a wrong number.
    """
    logger.error("evaluation not run (%s): %s", error_type, reason)
    return {
        "success": False,
        "score": 0.0,
        "reason": reason,
        "scored": False,
        "error_type": error_type,
    }


def _run_postconfig(postconfig: list, env: MockEnv) -> None:
    """Run postconfig commands before evaluation.

    Postconfig typically includes things like:
    - Activating specific windows
    - Waiting for operations to complete
    - Opening files for inspection

    Args:
        postconfig: List of postconfig command specs.
        env: Environment object.
    """

    for index, cmd in enumerate(postconfig):
        if not isinstance(cmd, dict):
            raise EvaluationNotRunError(
                f"postconfig item {index} must be an object"
            )
        cmd_type = cmd.get("type")
        if cmd_type not in {"activate_window", "wait", "execute"}:
            raise EvaluationNotRunError(
                f"postconfig item {index} has unsupported type: {cmd_type!r}"
            )

        if cmd_type == "wait":
            seconds = cmd.get("seconds", 1.0)
            if (
                isinstance(seconds, bool)
                or not isinstance(seconds, (int, float))
                or not math.isfinite(float(seconds))
                or seconds < 0
            ):
                raise EvaluationNotRunError(
                    f"postconfig wait item {index} has invalid seconds"
                )
            import time

            time.sleep(float(seconds))
            continue

        if cmd_type == "activate_window":
            field = "name"
            endpoint = "setup/activate_window"
            timeout = 10.0
        else:
            field = "command"
            endpoint = "execute_windows"
            timeout = 30.0
        value = cmd.get(field)
        if not isinstance(value, str) or not value.strip():
            raise EvaluationNotRunError(
                f"postconfig {cmd_type} item {index} requires {field}"
            )

        try:
            response = requests.post(
                f"http://{env.vm_ip}:{env.port}/{endpoint}",
                json={field: value},
                timeout=timeout,
            )
        except Exception as e:
            raise EvaluationNotRunError(
                f"postconfig {cmd_type} item {index} request failed: {e}",
                error_type="infrastructure",
            ) from e
        if not 200 <= response.status_code < 300:
            raise EvaluationNotRunError(
                f"postconfig {cmd_type} item {index} failed with "
                f"HTTP {response.status_code}",
                error_type="infrastructure",
            )
        try:
            payload = parse_strict_json_response(
                response,
                context=f"postconfig {cmd_type} item {index}",
            )
            require_successful_receipt(
                payload,
                context=f"postconfig {cmd_type} item {index}",
            )
        except (TypeError, ValueError) as e:
            raise EvaluationNotRunError(
                f"postconfig {cmd_type} item {index} returned an invalid "
                f"success receipt: {e}",
                error_type="infrastructure",
            ) from e


def _truncate_value(value: Any, max_len: int = 500) -> str:
    """Truncate a value for JSON response."""
    if value is None:
        return None

    s = str(value)
    if len(s) > max_len:
        return s[:max_len] + "..."
    return s


def create_evaluate_blueprint(
    evaluators_path: str | None = None,
) -> "Blueprint":
    """Create a Flask Blueprint with the /evaluate endpoint.

    This function creates a Blueprint that can be registered with the
    WAA Flask server to add evaluation capabilities.

    Args:
        evaluators_path: Path to WAA evaluators directory.

    Returns:
        Flask Blueprint with /evaluate endpoint.

    Raises:
        ImportError: If Flask is not available.

    Example:
        ```python
        from flask import Flask
        from openadapt_evals.server.evaluate_endpoint import create_evaluate_blueprint

        app = Flask(__name__)
        evaluate_bp = create_evaluate_blueprint("/path/to/evaluators")
        app.register_blueprint(evaluate_bp)
        ```
    """
    if not FLASK_AVAILABLE:
        raise ImportError("Flask is required to create the evaluate blueprint")

    bp = Blueprint("evaluate", __name__)

    @bp.route("/evaluate", methods=["POST"])
    def evaluate():
        """Evaluate current VM state against task criteria.

        Request JSON:
        {
            "evaluator": {
                "func": "exact_match",
                "result": {"type": "vm_file", "path": "..."},
                "expected": {"type": "rule", "rules": {...}}
            },
            "agent_last_action": "DONE"  # Optional
        }

        Response JSON:
        {
            "success": true/false,
            "score": 0.0-1.0,
            "actual": "...",
            "expected": "...",
            "reason": "..."
        }
        """
        task_config = request.json

        if not task_config:
            return jsonify({"error": "No task config provided"}), 400

        env = MockEnv()
        result = evaluate_task_state(task_config, env, evaluators_path)

        return jsonify(result)

    @bp.route("/evaluate/health", methods=["GET"])
    def evaluate_health():
        """Health check for evaluate endpoint."""
        try:
            _load_waa_evaluators(evaluators_path)
            return jsonify({"status": "ok", "evaluators_loaded": True})
        except ImportError as e:
            return jsonify({"status": "degraded", "error": str(e)}), 503

    return bp


# Standalone metrics implementations — delegates to shared evaluation.metrics module


class StandaloneMetrics:
    """Standalone metric implementations.

    Thin wrapper around ``openadapt_evals.evaluation.metrics`` so that
    existing callers (and tests) that reference ``StandaloneMetrics.method``
    continue to work.
    """

    from openadapt_evals.evaluation import metrics as _metrics

    contains = staticmethod(_metrics.contains)
    exact_match = staticmethod(_metrics.exact_match)
    file_exists = staticmethod(_metrics.file_exists)
    fuzzy_match = staticmethod(_metrics.fuzzy_match)


class StandaloneGetters:
    """Standalone getter implementations for basic evaluation.

    These provide basic file and command output retrieval without
    requiring the full WAA evaluator infrastructure.
    """

    def __init__(self, server_url: str = "http://localhost:5000"):
        self.server_url = server_url

    def get_vm_file(self, env: MockEnv, config: dict) -> str | None:
        """Get file contents from VM."""
        import requests

        path = config.get("path", "")
        resp = requests.post(
            f"{self.server_url}/execute_windows",
            json={"command": f"Get-Content -Path '{path}'", "shell": "powershell"},
            timeout=30.0,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"file getter returned HTTP {resp.status_code} for {path!r}"
            )
        payload = require_successful_receipt(
            parse_strict_json_response(resp, context="standalone file getter"),
            context="standalone file getter",
            require_output=True,
        )
        return payload["output"]

    def get_vm_command_line(self, env: MockEnv, config: dict) -> str | None:
        """Execute command on VM and return output."""
        import requests

        command = config.get("command", "")
        shell = config.get("shell", "powershell")

        resp = requests.post(
            f"{self.server_url}/execute_windows",
            json={"command": command, "shell": shell},
            timeout=60.0,
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"command getter returned HTTP {resp.status_code}"
            )
        payload = require_successful_receipt(
            parse_strict_json_response(resp, context="standalone command getter"),
            context="standalone command getter",
            require_output=True,
        )
        return payload["output"]


def create_standalone_evaluator(
    server_url: str = "http://localhost:5000",
) -> Callable[[dict], dict]:
    """Create a standalone evaluator function.

    This creates an evaluator that uses standalone implementations
    of basic getters and metrics, useful when WAA evaluators are
    not available.

    Args:
        server_url: URL of the WAA server for VM access.

    Returns:
        Callable that takes task_config and returns evaluation result.

    Example:
        ```python
        evaluate = create_standalone_evaluator("http://vm:5000")
        result = evaluate(task_config)
        print(f"Success: {result['success']}")
        ```
    """
    getters = StandaloneGetters(server_url)
    metrics = StandaloneMetrics()

    def evaluate(task_config: dict) -> dict:
        if not isinstance(task_config, dict):
            return _not_scored("Standalone task config must be an object", "evaluation")
        evaluator_config = task_config.get("evaluator", {})

        if not isinstance(evaluator_config, dict) or not evaluator_config:
            return _not_scored("No evaluator configuration", "evaluation")

        # Get result spec
        result_spec = evaluator_config.get("result")
        if not isinstance(result_spec, dict) or not result_spec:
            return _not_scored(
                "standalone result contract must be a non-empty object",
                "evaluation",
            )
        result_type = result_spec.get("type")
        if not isinstance(result_type, str) or not result_type:
            return _not_scored("standalone result type is invalid", "evaluation")
        required_result_field = {
            "vm_command_line": "command",
            "vm_file": "path",
        }.get(result_type)
        if required_result_field is None:
            return _not_scored(
                f"standalone result type {result_type!r} is not supported",
                "evaluation",
            )
        required_result_value = result_spec.get(required_result_field)
        if (
            not isinstance(required_result_value, str)
            or not required_result_value.strip()
        ):
            return _not_scored(
                f"standalone result type {result_type!r} requires "
                f"{required_result_field}",
                "evaluation",
            )

        postconfig = evaluator_config.get("postconfig", [])
        if "postconfig" in evaluator_config and not isinstance(postconfig, list):
            return _not_scored("standalone postconfig must be a list", "evaluation")
        if postconfig:
            return _not_scored(
                "standalone evaluator does not implement postconfig",
                "evaluation",
            )

        # Get actual value
        getter_name = f"get_{result_type}"
        getter_func = getattr(getters, getter_name, None)
        if getter_func is None:
            return _not_scored(
                f"standalone getter '{getter_name}' is not implemented",
                "evaluation",
            )
        try:
            actual = getter_func(MockEnv(), result_spec)
        except Exception as e:
            return _not_scored(
                f"standalone getter '{getter_name}' raised: {e}",
                "infrastructure",
            )
        if actual is None:
            return _not_scored(
                f"standalone getter '{getter_name}' returned no value",
                "evaluation",
            )

        # Get expected value
        expected_spec = evaluator_config.get("expected")
        if not isinstance(expected_spec, dict):
            return _not_scored("standalone expected contract is missing", "evaluation")
        if expected_spec.get("type") == "rule":
            rules = expected_spec.get("rules")
            if not isinstance(rules, dict) or "match" not in rules:
                return _not_scored(
                    "standalone expected rule requires rules.match", "evaluation"
                )
            expected = rules["match"]
        elif "value" in expected_spec and expected_spec["value"] is not None:
            expected = expected_spec["value"]
        else:
            return _not_scored(
                "standalone expected literal requires value", "evaluation"
            )

        # Run metric
        func_name = evaluator_config.get("func", "exact_match")
        if not isinstance(func_name, str) or not func_name:
            return _not_scored("standalone metric name is invalid", "evaluation")
        options = evaluator_config.get("options", {})
        if not isinstance(options, dict):
            return _not_scored("standalone metric options must be an object", "evaluation")
        conjunction = evaluator_config.get("conj", "and")
        if conjunction != "and":
            return _not_scored(
                "standalone evaluator supports only one metric with conj='and'",
                "evaluation",
            )
        try:
            score = run_metric(
                func_name,
                actual,
                expected,
                options=options,
                metrics=metrics,
            )
        except EvaluationNotRunError as e:
            return _not_scored(str(e), e.error_type)

        success = score >= 1.0

        return {
            "success": success,
            "score": float(score),
            "actual": _truncate_value(actual),
            "expected": _truncate_value(expected),
            "reason": "Standalone evaluation",
            "scored": True,
            "error_type": None,
        }

    return evaluate
