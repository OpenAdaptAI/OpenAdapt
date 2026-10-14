"""Tests for evaluate_server.py deployment integrity.

Validates that the evaluate_server.py source file and Dockerfile are
correctly configured to avoid the symlink-to-stdin bug where
/evaluate_server.py in the container becomes a symlink to /proc/self/fd/0
instead of a real file.
"""

import os
import re
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Path to the waa_deploy directory (source files for Docker build context)
WAA_DEPLOY_DIR = Path(__file__).resolve().parents[1] / "openadapt_evals" / "waa_deploy"
EVALUATE_SERVER_PATH = WAA_DEPLOY_DIR / "evaluate_server.py"
DOCKERFILE_PATH = WAA_DEPLOY_DIR / "Dockerfile"
ENTRYPOINT_SCRIPT_PATH = WAA_DEPLOY_DIR / "start_with_evaluate.sh"


class TestEvaluateServerSourceFile:
    """Validate the evaluate_server.py source file exists and has expected content."""

    def test_file_exists(self):
        """evaluate_server.py must exist in the waa_deploy directory."""
        assert EVALUATE_SERVER_PATH.exists(), (
            f"evaluate_server.py not found at {EVALUATE_SERVER_PATH}. "
            "This file is required for the Docker build context."
        )

    def test_file_is_not_empty(self):
        """evaluate_server.py must have content (not be a 0-byte file)."""
        assert EVALUATE_SERVER_PATH.stat().st_size > 0, (
            "evaluate_server.py is empty (0 bytes). Expected a Python Flask server."
        )

    def test_file_is_not_symlink(self):
        """evaluate_server.py must be a regular file, not a symlink."""
        assert not EVALUATE_SERVER_PATH.is_symlink(), (
            f"evaluate_server.py is a symlink to {os.readlink(EVALUATE_SERVER_PATH)}. "
            "This was the root cause of the deployment bug."
        )

    def test_minimum_size(self):
        """evaluate_server.py should be a substantial file (not a stub)."""
        size = EVALUATE_SERVER_PATH.stat().st_size
        assert size > 1000, (
            f"evaluate_server.py is only {size} bytes. "
            "Expected a full Flask server (typically 20KB+)."
        )

    def test_has_probe_route(self):
        """evaluate_server.py must define the /probe health check route."""
        content = EVALUATE_SERVER_PATH.read_text()
        assert "'/probe'" in content or '"/probe"' in content, (
            "evaluate_server.py is missing the /probe route. "
            "This is the health check endpoint."
        )

    def test_has_task_route(self):
        """evaluate_server.py must define the /task/<task_id> route."""
        content = EVALUATE_SERVER_PATH.read_text()
        assert "/task/" in content, (
            "evaluate_server.py is missing the /task/ route. "
            "This serves task configurations."
        )

    def test_has_setup_route(self):
        """evaluate_server.py must define the /setup route."""
        content = EVALUATE_SERVER_PATH.read_text()
        assert "'/setup'" in content or '"/setup"' in content, (
            "evaluate_server.py is missing the /setup route. "
            "This handles task setup."
        )

    def test_has_evaluate_route(self):
        """evaluate_server.py must define the /evaluate route."""
        content = EVALUATE_SERVER_PATH.read_text()
        assert "'/evaluate'" in content or '"/evaluate"' in content, (
            "evaluate_server.py is missing the /evaluate route. "
            "This is the core evaluation endpoint."
        )

    def test_has_flask_app(self):
        """evaluate_server.py must create a Flask application."""
        content = EVALUATE_SERVER_PATH.read_text()
        assert "Flask(__name__)" in content, (
            "evaluate_server.py is missing Flask(__name__) app creation."
        )

    def test_listens_on_port_5050(self):
        """evaluate_server.py must listen on port 5050."""
        content = EVALUATE_SERVER_PATH.read_text()
        assert "5050" in content, (
            "evaluate_server.py does not reference port 5050. "
            "The evaluate server must listen on port 5050."
        )


class TestDockerfileCopyIntegrity:
    """Validate the Dockerfile correctly copies and verifies evaluate_server.py."""

    def test_dockerfile_exists(self):
        """Dockerfile must exist in waa_deploy directory."""
        assert DOCKERFILE_PATH.exists(), (
            f"Dockerfile not found at {DOCKERFILE_PATH}"
        )

    def test_dockerfile_copies_evaluate_server(self):
        """Dockerfile must COPY evaluate_server.py."""
        content = DOCKERFILE_PATH.read_text()
        assert "COPY evaluate_server.py /evaluate_server.py" in content, (
            "Dockerfile missing 'COPY evaluate_server.py /evaluate_server.py'. "
            "The evaluate server must be copied into the image."
        )

    def test_dockerfile_does_not_copy_from_stdin(self):
        """Dockerfile must not use stdin COPY syntax (COPY - /path)."""
        content = DOCKERFILE_PATH.read_text()
        # Match 'COPY - /...' or 'COPY - /...' patterns (stdin copy)
        stdin_copies = re.findall(r'COPY\s+- \s*/\S+', content)
        assert not stdin_copies, (
            f"Dockerfile uses stdin COPY syntax which can create symlinks "
            f"to /proc/self/fd/0: {stdin_copies}"
        )

    def test_dockerfile_has_verification_step(self):
        """Dockerfile must verify evaluate_server.py after COPY."""
        content = DOCKERFILE_PATH.read_text()
        # Look for the verification RUN that checks for symlinks
        assert "-L /evaluate_server.py" in content, (
            "Dockerfile is missing the post-COPY symlink verification step. "
            "Add a RUN that checks /evaluate_server.py is not a symlink."
        )

    def test_dockerfile_verifies_routes(self):
        """Dockerfile verification must check for expected routes."""
        content = DOCKERFILE_PATH.read_text()
        assert "/probe" in content and "/evaluate" in content, (
            "Dockerfile verification should check for expected Flask routes."
        )


class TestEntrypointScript:
    """Validate the entrypoint script validates evaluate_server.py at startup."""

    def test_entrypoint_exists(self):
        """start_with_evaluate.sh must exist."""
        assert ENTRYPOINT_SCRIPT_PATH.exists(), (
            f"start_with_evaluate.sh not found at {ENTRYPOINT_SCRIPT_PATH}"
        )

    def test_entrypoint_checks_symlink(self):
        """Entrypoint must check if evaluate_server.py is a symlink."""
        content = ENTRYPOINT_SCRIPT_PATH.read_text()
        assert "-L" in content, (
            "start_with_evaluate.sh does not check for symlinks. "
            "It must validate /evaluate_server.py is not a symlink before running it."
        )

    def test_entrypoint_checks_empty_file(self):
        """Entrypoint must check if evaluate_server.py is empty."""
        content = ENTRYPOINT_SCRIPT_PATH.read_text()
        assert "-s" in content, (
            "start_with_evaluate.sh does not check for empty files. "
            "It must validate /evaluate_server.py has content before running it."
        )

    def test_entrypoint_checks_routes(self):
        """Entrypoint must check for expected routes."""
        content = ENTRYPOINT_SCRIPT_PATH.read_text()
        assert "/probe" in content or "/evaluate" in content, (
            "start_with_evaluate.sh does not verify Flask routes. "
            "It should check that the file contains expected route definitions."
        )

    def test_entrypoint_exec_passthrough(self):
        """Entrypoint must exec $@ to pass through to the main process."""
        content = ENTRYPOINT_SCRIPT_PATH.read_text()
        assert 'exec "$@"' in content, (
            "start_with_evaluate.sh missing 'exec \"$@\"'. "
            "It must pass through to the main entry point."
        )


class TestPoolManagerEntrypoint:
    """Validate pool.py WAA_START_SCRIPT_TEMPLATE uses the Dockerfile entrypoint."""

    def test_waa_start_script_no_entrypoint_override(self):
        """WAA_START_SCRIPT_TEMPLATE must not override --entrypoint to /bin/bash."""
        from openadapt_evals.infrastructure.pool import WAA_START_SCRIPT_TEMPLATE

        assert "--entrypoint /bin/bash" not in WAA_START_SCRIPT_TEMPLATE, (
            "WAA_START_SCRIPT_TEMPLATE overrides --entrypoint to /bin/bash, bypassing "
            "start_with_evaluate.sh and its validation. Remove --entrypoint to "
            "use the Dockerfile's ENTRYPOINT which includes startup checks."
        )

    def test_waa_start_script_uses_entry_sh(self):
        """WAA_START_SCRIPT_TEMPLATE must pass /entry.sh as command (not inline python)."""
        from openadapt_evals.infrastructure.pool import WAA_START_SCRIPT_TEMPLATE

        assert "/entry.sh" in WAA_START_SCRIPT_TEMPLATE, (
            "WAA_START_SCRIPT_TEMPLATE does not reference /entry.sh. "
            "The container should run /entry.sh via the Dockerfile entrypoint."
        )


class TestDockerBuildContextFiles:
    """Validate all files needed for Docker build context exist."""

    REQUIRED_FILES = [
        "Dockerfile",
        "evaluate_server.py",
        "start_with_evaluate.sh",
        "start_waa_server.bat",
        "api_agent.py",
    ]

    def test_all_build_context_files_exist(self):
        """All files referenced in pool.py setup_docker must exist."""
        missing = []
        for fname in self.REQUIRED_FILES:
            path = WAA_DEPLOY_DIR / fname
            if not path.exists():
                missing.append(fname)
        assert not missing, (
            f"Missing Docker build context files: {missing}. "
            "These files are SCP'd to the VM during pool-create."
        )

    def test_all_build_context_files_nonempty(self):
        """All Docker build context files must have content."""
        empty = []
        for fname in self.REQUIRED_FILES:
            path = WAA_DEPLOY_DIR / fname
            if path.exists() and path.stat().st_size == 0:
                empty.append(fname)
        assert not empty, (
            f"Empty Docker build context files: {empty}. "
            "These files must have content for the Docker build to succeed."
        )


class TestEvaluateServerImport:
    """Test that evaluate_server.py can be imported (with mocked WAA internals)."""

    @pytest.fixture(autouse=True)
    def _mock_waa_internals(self):
        """Stub WAA container modules so evaluate_server can be imported."""
        mock_modules = [
            "controllers",
            "controllers.python",
            "evaluators",
            "evaluators.getters",
            "evaluators.metrics",
        ]
        stubs = {}
        for mod_name in mock_modules:
            if mod_name not in sys.modules:
                stubs[mod_name] = types.ModuleType(mod_name)
                sys.modules[mod_name] = stubs[mod_name]

        pc_mock = MagicMock(name="PythonController")
        sys.modules["controllers.python"].PythonController = pc_mock

        yield

        for mod_name in stubs:
            sys.modules.pop(mod_name, None)
        sys.modules.pop("openadapt_evals.waa_deploy.evaluate_server", None)

    def test_flask_app_has_expected_routes(self):
        """Flask app must register all expected routes."""
        from openadapt_evals.waa_deploy.evaluate_server import app

        rules = [rule.rule for rule in app.url_map.iter_rules()]
        assert "/probe" in rules, "Missing /probe route"
        assert "/setup" in rules, "Missing /setup route"
        assert "/evaluate" in rules, "Missing /evaluate route"
        # /task/<task_id> shows as /task/<task_id> in url_map
        task_routes = [r for r in rules if r.startswith("/task/")]
        assert task_routes, "Missing /task/<task_id> route"

    def test_flask_app_probe_returns_ok(self):
        """The /probe endpoint must return status ok."""
        from openadapt_evals.waa_deploy.evaluate_server import app

        client = app.test_client()
        resp = client.get("/probe")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
