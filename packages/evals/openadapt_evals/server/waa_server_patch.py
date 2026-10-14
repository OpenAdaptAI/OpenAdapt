#!/usr/bin/env python3
"""WAA Server Patch - Add /evaluate endpoint to existing WAA Flask server.

This script adds the /evaluate endpoint to the Windows Agent Arena Flask
server. Run it on the VM where WAA is installed.

Usage (on WAA VM):
    # Option 1: Direct execution
    python waa_server_patch.py

    # Option 2: Import and use in existing server
    from waa_server_patch import patch_waa_server
    patch_waa_server(app)  # Flask app

    # Option 3: Copy patch code into main.py
    # See the patch_waa_server() function below

Requirements:
    - WAA server must be running
    - WAA evaluators must be installed at standard path
    - Python 3.10+

The /evaluate endpoint:
    POST /evaluate
    Request: {task config with "evaluator" field}
    Response: {"success": bool, "score": float, "reason": str}
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from flask import Flask

logger = logging.getLogger(__name__)

# WAA evaluator paths to search
WAA_EVALUATOR_PATHS = [
    "/client/desktop_env",
    "/home/azureuser/WindowsAgentArena/src/win-arena-container/client/desktop_env",
    "C:/WAA/client/desktop_env",
    "/waa/client/desktop_env",
    Path(__file__).parent.parent.parent / "WindowsAgentArena/src/win-arena-container/client/desktop_env",
]


def find_waa_evaluators() -> tuple[Any, Any] | None:
    """Find and import WAA evaluator modules.

    Returns:
        Tuple of (getters, metrics) modules, or None if not found.
    """
    for path in WAA_EVALUATOR_PATHS:
        path = Path(path)
        if path.exists() and (path / "evaluators").exists():
            # Add parent to sys.path so `desktop_env.evaluators` is importable
            parent = str(path.parent)
            if parent not in sys.path:
                sys.path.insert(0, parent)
            # Also add path itself for direct `evaluators` imports
            if str(path) not in sys.path:
                sys.path.insert(0, str(path))
            try:
                from evaluators import getters, metrics
                logger.info(f"Found WAA evaluators at {path}")
                return getters, metrics
            except ImportError as e:
                logger.debug(f"Import failed from {path}: {e}")
                continue
    return None


def create_evaluate_routes(app: "Flask", getters: Any, metrics: Any) -> None:
    """Add /evaluate endpoint to Flask app.

    Args:
        app: Flask application.
        getters: WAA getters module.
        metrics: WAA metrics module.
    """
    from flask import jsonify, request

    class MockEnv:
        """Mock environment for getter calls."""
        vm_ip = "localhost"
        port = 5000

    @app.route("/evaluate", methods=["POST"])
    def evaluate():
        """Evaluate current VM state against task criteria."""
        task_config = request.json

        if not task_config:
            return jsonify({"error": "No task config provided"}), 400

        evaluator_config = task_config.get("evaluator", {})

        if not evaluator_config:
            return jsonify({
                "success": False,
                "score": 0.0,
                "reason": "No evaluator configuration in task",
            })

        env = MockEnv()

        try:
            # Handle infeasible tasks
            if evaluator_config.get("infeasible"):
                agent_last_action = task_config.get("agent_last_action", "")
                if agent_last_action.upper() in ("FAIL", "INFEASIBLE", "IMPOSSIBLE"):
                    return jsonify({
                        "success": True,
                        "score": 1.0,
                        "reason": "Correctly identified infeasible task",
                    })

            # Get actual value
            result_spec = evaluator_config.get("result", {})
            result_type = result_spec.get("type", "vm_command_line")
            getter_func = getattr(getters, f"get_{result_type}", None)

            actual = None
            if getter_func:
                try:
                    actual = getter_func(env, result_spec)
                except Exception as e:
                    logger.error(f"Getter failed: {e}")

            # Get expected value
            expected_spec = evaluator_config.get("expected", {})
            expected_type = expected_spec.get("type")

            if expected_type == "rule":
                expected = expected_spec.get("rules", {}).get("match")
            elif expected_type == "literal" or "value" in expected_spec:
                expected = expected_spec.get("value")
            elif expected_type:
                expected_getter = getattr(getters, f"get_{expected_type}", None)
                if expected_getter:
                    try:
                        expected = expected_getter(env, expected_spec)
                    except Exception as e:
                        logger.error(f"Expected getter failed: {e}")
                        expected = None
                else:
                    expected = None
            else:
                expected = None

            # Run metric
            func_name = evaluator_config.get("func", "exact_match")
            options = evaluator_config.get("options", {})

            if isinstance(func_name, list):
                # Multiple metrics
                scores = []
                for fn in func_name:
                    metric_func = getattr(metrics, fn, metrics.exact_match)
                    try:
                        scores.append(metric_func(actual, expected, **options))
                    except Exception as e:
                        logger.error(f"Metric {fn} failed: {e}")
                        scores.append(0.0)

                conj = evaluator_config.get("conj", "and")
                if conj == "or":
                    score = max(scores)
                else:
                    score = min(scores)
            else:
                metric_func = getattr(metrics, func_name, metrics.exact_match)
                try:
                    score = metric_func(actual, expected, **options)
                except Exception as e:
                    logger.error(f"Metric failed: {e}")
                    score = 0.0

            score = float(score)
            success = score >= 1.0

            # Truncate large values
            actual_str = str(actual)[:500] if actual is not None else None
            expected_str = str(expected)[:500] if expected is not None else None

            return jsonify({
                "success": success,
                "score": score,
                "actual": actual_str,
                "expected": expected_str,
                "reason": f"Evaluation complete (score={score:.2f})",
            })

        except Exception as e:
            logger.exception("Evaluation error")
            return jsonify({
                "success": False,
                "score": 0.0,
                "error": str(e),
                "reason": f"Evaluation error: {e}",
            }), 500

    @app.route("/evaluate/health", methods=["GET"])
    def evaluate_health():
        """Health check for evaluate endpoint."""
        return jsonify({
            "status": "ok",
            "evaluators_loaded": True,
            "getters_available": dir(getters),
            "metrics_available": dir(metrics),
        })

    logger.info("Added /evaluate endpoint to WAA server")


def patch_waa_server(app: "Flask") -> bool:
    """Patch a Flask app with the /evaluate endpoint.

    Args:
        app: Flask application to patch.

    Returns:
        True if successful, False otherwise.
    """
    result = find_waa_evaluators()
    if result is None:
        logger.error(
            "Could not find WAA evaluators. "
            f"Searched: {[str(p) for p in WAA_EVALUATOR_PATHS]}"
        )
        return False

    getters, metrics = result
    create_evaluate_routes(app, getters, metrics)
    return True


def main():
    """Main entry point for standalone execution."""
    import argparse

    parser = argparse.ArgumentParser(description="WAA Server Evaluate Endpoint")
    parser.add_argument(
        "--port", type=int, default=5050,
        help="Port to run standalone server (default: 5050)"
    )
    parser.add_argument(
        "--host", type=str, default="0.0.0.0",
        help="Host to bind to (default: 0.0.0.0)"
    )
    args = parser.parse_args()

    # Try to import Flask
    try:
        from flask import Flask
    except ImportError:
        print("ERROR: Flask not installed. Run: pip install flask")
        sys.exit(1)

    # Find evaluators
    result = find_waa_evaluators()
    if result is None:
        print("ERROR: Could not find WAA evaluators.")
        print(f"Searched: {[str(p) for p in WAA_EVALUATOR_PATHS]}")
        sys.exit(1)

    getters, metrics = result

    # Create and run server
    app = Flask(__name__)
    create_evaluate_routes(app, getters, metrics)

    @app.route("/")
    def index():
        return "WAA Evaluate Server - POST to /evaluate"

    print(f"Starting WAA evaluate server on {args.host}:{args.port}")
    print("Endpoints:")
    print(f"  POST http://{args.host}:{args.port}/evaluate")
    print(f"  GET  http://{args.host}:{args.port}/evaluate/health")
    print()

    app.run(host=args.host, port=args.port, debug=False)


# Code to copy into WAA's main.py
PATCH_CODE = '''
# === BEGIN EVALUATE ENDPOINT PATCH ===
# Add this code to WindowsAgentArena/src/win-arena-container/vm/setup/server/main.py
# Place after the Flask app creation and before app.run()

import sys
from pathlib import Path

# Add evaluators to path
evaluators_path = Path(__file__).parent.parent.parent.parent / "client/desktop_env"
if evaluators_path.exists():
    sys.path.insert(0, str(evaluators_path))

try:
    from evaluators import getters, metrics

    class MockEnv:
        vm_ip = "localhost"
        port = 5000

    @app.route("/evaluate", methods=["POST"])
    def evaluate():
        """Evaluate current VM state against task criteria."""
        task_config = request.json
        if not task_config:
            return jsonify({"error": "No task config provided"}), 400

        evaluator_config = task_config.get("evaluator", {})
        if not evaluator_config:
            return jsonify({"success": False, "score": 0.0, "reason": "No evaluator config"})

        env = MockEnv()

        # Get actual value
        result_spec = evaluator_config.get("result", {})
        result_type = result_spec.get("type", "vm_command_line")
        getter_func = getattr(getters, f"get_{result_type}", None)
        actual = getter_func(env, result_spec) if getter_func else None

        # Get expected value
        expected_spec = evaluator_config.get("expected", {})
        if expected_spec.get("type") == "rule":
            expected = expected_spec.get("rules", {}).get("match")
        else:
            expected = expected_spec.get("value")

        # Run metric
        func_name = evaluator_config.get("func", "exact_match")
        metric_func = getattr(metrics, func_name, metrics.exact_match)
        score = float(metric_func(actual, expected))
        success = score >= 1.0

        return jsonify({
            "success": success,
            "score": score,
            "actual": str(actual)[:500] if actual else None,
            "expected": str(expected)[:500] if expected else None,
            "reason": f"Score: {score:.2f}",
        })

    print("Evaluate endpoint registered at /evaluate")
except ImportError as e:
    print(f"WARNING: Could not load evaluators: {e}")
    print("The /evaluate endpoint will not be available")

# === END EVALUATE ENDPOINT PATCH ===
'''


if __name__ == "__main__":
    main()
