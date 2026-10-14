"""Tests for evaluate_server.py setup handlers (verify_apps, install_apps)."""

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Mock WAA-internal modules that only exist inside the Docker container.
# This must happen before any import of evaluate_server.
# ---------------------------------------------------------------------------

_MOCK_MODULES = [
    "controllers",
    "controllers.python",
    "evaluators",
    "evaluators.getters",
    "evaluators.metrics",
]


@pytest.fixture(autouse=True)
def _mock_waa_internals():
    """Stub out WAA container modules so evaluate_server can be imported."""
    stubs = {}
    for mod_name in _MOCK_MODULES:
        if mod_name not in sys.modules:
            stubs[mod_name] = types.ModuleType(mod_name)
            sys.modules[mod_name] = stubs[mod_name]

    # controllers.python.PythonController must be a callable (class)
    pc_mock = MagicMock(name="PythonController")
    sys.modules["controllers.python"].PythonController = pc_mock

    yield

    # Clean up stubs AND the cached evaluate_server module so each test
    # class gets a fresh import.
    for mod_name in stubs:
        sys.modules.pop(mod_name, None)
    sys.modules.pop("openadapt_evals.waa_deploy.evaluate_server", None)


def _import_handlers():
    """Import the setup handler functions from evaluate_server."""
    from openadapt_evals.waa_deploy.evaluate_server import (
        SETUP_HANDLERS,
        _setup_install_apps,
        _setup_verify_apps,
    )
    return _setup_verify_apps, _setup_install_apps, SETUP_HANDLERS


def _import_app():
    """Import the Flask app from evaluate_server."""
    from openadapt_evals.waa_deploy.evaluate_server import app
    return app


# ---------------------------------------------------------------------------
# _setup_verify_apps
# ---------------------------------------------------------------------------


class TestVerifyApps:
    """Tests for the verify_apps setup handler."""

    def test_all_apps_present(self):
        """When all apps return True, no error is raised."""
        verify, _, _ = _import_handlers()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"output": "True"}

        with patch("requests.post", return_value=mock_resp):
            # Should not raise
            verify(apps=["notepad", "chrome"])

    def test_missing_app_raises(self):
        """When an app returns False, RuntimeError is raised."""
        verify, _, _ = _import_handlers()

        def _fake_post(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            cmd = kwargs.get("json", {}).get("command", "")
            if "scalc.exe" in cmd:
                resp.json.return_value = {"output": "False"}
            else:
                resp.json.return_value = {"output": "True"}
            return resp

        with patch("requests.post", side_effect=_fake_post):
            with pytest.raises(RuntimeError, match="Missing apps.*libreoffice_calc"):
                verify(apps=["libreoffice_calc", "notepad"])

    def test_unknown_app_skipped(self):
        """Apps not in APP_CHECKS are silently skipped (built-in)."""
        verify, _, _ = _import_handlers()

        with patch("requests.post") as mock_post:
            # Should never be called because "calculator" has no check
            verify(apps=["calculator", "settings"])
            mock_post.assert_not_called()

    def test_connection_error_counts_as_missing(self):
        """If the POST to WAA fails, the app counts as missing."""
        verify, _, _ = _import_handlers()

        with patch("requests.post", side_effect=Exception("connection refused")):
            with pytest.raises(RuntimeError, match="Missing apps.*notepad"):
                verify(apps=["notepad"])

    def test_non_200_counts_as_missing(self):
        """If the WAA server returns non-200, the app counts as missing."""
        verify, _, _ = _import_handlers()

        mock_resp = MagicMock()
        mock_resp.status_code = 500

        with patch("requests.post", return_value=mock_resp):
            with pytest.raises(RuntimeError, match="Missing apps.*chrome"):
                verify(apps=["chrome"])

    def test_empty_apps_is_noop(self):
        """Empty apps list does nothing."""
        verify, _, _ = _import_handlers()

        with patch("requests.post") as mock_post:
            verify(apps=[])
            mock_post.assert_not_called()

    def test_normalizes_hyphenated_names(self):
        """'libreoffice-calc' normalizes to 'libreoffice_calc'."""
        verify, _, _ = _import_handlers()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"output": "True"}

        with patch("requests.post", return_value=mock_resp) as mock_post:
            verify(apps=["libreoffice-calc"])
            # Should have called POST (not skipped as unknown)
            mock_post.assert_called_once()
            cmd = mock_post.call_args.kwargs.get("json", {}).get("command", "")
            assert "scalc.exe" in cmd

    def test_normalizes_space_names(self):
        """'libreoffice calc' normalizes to 'libreoffice_calc'."""
        verify, _, _ = _import_handlers()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"output": "False"}

        with patch("requests.post", return_value=mock_resp):
            with pytest.raises(RuntimeError, match="Missing apps.*libreoffice calc"):
                verify(apps=["libreoffice calc"])

    def test_normalizes_vscode_alias(self):
        """'vscode' normalizes to 'vs_code'."""
        verify, _, _ = _import_handlers()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"output": "True"}

        with patch("requests.post", return_value=mock_resp) as mock_post:
            verify(apps=["vscode"])
            mock_post.assert_called_once()
            cmd = mock_post.call_args.kwargs.get("json", {}).get("command", "")
            assert "Code.exe" in cmd


# ---------------------------------------------------------------------------
# _setup_install_apps
# ---------------------------------------------------------------------------


class TestInstallApps:
    """Tests for the install_apps setup handler."""

    def test_install_no_apps_runs_install_bat(self):
        """With no apps arg, runs install.bat from C:\\oem."""
        _, install, _ = _import_handlers()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"returncode": 0, "output": "ok"}

        with patch("requests.post", return_value=mock_resp) as mock_post:
            install()
            cmd = mock_post.call_args.kwargs.get("json", {}).get("command", "")
            assert "C:\\oem\\install.bat" in cmd

    def test_install_bat_failure_raises(self):
        """Failed install.bat execution raises RuntimeError."""
        _, install, _ = _import_handlers()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"returncode": 1, "error": "some error"}

        with patch("requests.post", return_value=mock_resp):
            with pytest.raises(RuntimeError, match="install.bat exited"):
                install()

    def test_install_specific_apps(self):
        """Targeted install downloads (if needed), writes script, and runs it."""
        _, install, _ = _import_handlers()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"returncode": 0, "output": ""}

        m_open = MagicMock()
        # Mock glob.glob so _download_libreoffice sees an existing MSI and skips download
        with patch("requests.post", return_value=mock_resp) as mock_post, \
             patch("builtins.open", m_open), \
             patch("glob.glob", return_value=["/tmp/smb/LibreOffice_25.2.1_Win_x86-64.msi"]):
            install(apps=["libreoffice-calc"])
            # Should write the install script file
            m_open.assert_called_once()
            assert "libreoffice_calc" in m_open.call_args[0][0]
            # Should execute via PowerShell
            cmd = mock_post.call_args.kwargs.get("json", {}).get("command", "")
            assert "install_libreoffice_calc.ps1" in cmd

    def test_install_writer_handled_by_calc(self):
        """libreoffice_writer install triggers libreoffice_calc MSI (same package)."""
        _, install, _ = _import_handlers()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"returncode": 0, "output": ""}

        # Mock glob.glob so _download_libreoffice sees an existing MSI
        with patch("requests.post", return_value=mock_resp) as mock_post, \
             patch("builtins.open", MagicMock()), \
             patch("glob.glob", return_value=["/tmp/smb/LibreOffice_25.2.1_Win_x86-64.msi"]):
            install(apps=["libreoffice_writer"])
            # Only one POST to execute (writer maps to calc, single install)
            mock_post.assert_called_once()

    def test_install_calls_download_when_msi_missing(self):
        """Phase 1: download function is called when MSI not on Samba share."""
        _, install, _ = _import_handlers()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"returncode": 0, "output": ""}

        # Mock the version discovery response
        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_get_resp.text = 'href="25.2.1/"'
        mock_get_resp.raise_for_status = MagicMock()

        with patch("requests.post", return_value=mock_resp), \
             patch("builtins.open", MagicMock()), \
             patch("glob.glob", return_value=[]), \
             patch("requests.get", return_value=mock_get_resp), \
             patch("subprocess.run") as mock_subprocess:
            install(apps=["libreoffice-calc"])
            # subprocess.run should have been called with curl to download
            mock_subprocess.assert_called_once()
            args = mock_subprocess.call_args[0][0]
            assert "curl" in args
            assert any("LibreOffice" in a for a in args)

    def test_install_skips_download_for_apps_without_downloader(self):
        """Apps without discover_and_download skip Phase 1 (e.g. Chrome)."""
        _, install, _ = _import_handlers()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"returncode": 0, "output": ""}

        with patch("requests.post", return_value=mock_resp) as mock_post, \
             patch("builtins.open", MagicMock()), \
             patch("subprocess.run") as mock_subprocess:
            install(apps=["chrome"])
            # No subprocess call (no Linux-side download for Chrome)
            mock_subprocess.assert_not_called()
            # But should still write script and execute
            cmd = mock_post.call_args.kwargs.get("json", {}).get("command", "")
            assert "install_chrome.ps1" in cmd

    def test_install_post_failure_raises(self):
        """Non-200 POST response raises RuntimeError."""
        _, install, _ = _import_handlers()

        mock_resp = MagicMock()
        mock_resp.status_code = 500

        with patch("requests.post", return_value=mock_resp), \
             patch("builtins.open", MagicMock()):
            with pytest.raises(RuntimeError, match="Failed to install"):
                install(apps=["chrome"])


# ---------------------------------------------------------------------------
# SETUP_HANDLERS registry
# ---------------------------------------------------------------------------


class TestSetupHandlersRegistry:
    """Verify the new handlers are registered in SETUP_HANDLERS."""

    def test_verify_apps_registered(self):
        _, _, handlers = _import_handlers()
        assert "verify_apps" in handlers
        assert callable(handlers["verify_apps"])

    def test_install_apps_registered(self):
        _, _, handlers = _import_handlers()
        assert "install_apps" in handlers
        assert callable(handlers["install_apps"])


# ---------------------------------------------------------------------------
# /setup endpoint returns 422 on handler errors
# ---------------------------------------------------------------------------


class TestSetupEndpointErrorStatus:
    """Test that the /setup endpoint returns 422 when a handler fails."""

    def test_setup_returns_422_on_verify_failure(self):
        """POST /setup with verify_apps that fails should return 422."""
        app = _import_app()
        client = app.test_client()

        with patch("requests.post") as mock_post:
            # Make notepad check fail
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"output": "False"}
            mock_post.return_value = mock_resp

            resp = client.post(
                "/setup",
                json={"config": [{"type": "verify_apps", "parameters": {"apps": ["notepad"]}}]},
                content_type="application/json",
            )
            assert resp.status_code == 422
            data = resp.get_json()
            assert data["status"] == "error"
            assert data["results"][0]["status"] == "error"
            assert "Missing apps" in data["results"][0]["error"]

    def test_setup_returns_200_on_success(self):
        """POST /setup with verify_apps that passes should return 200."""
        app = _import_app()
        client = app.test_client()

        with patch("requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"output": "True"}
            mock_post.return_value = mock_resp

            resp = client.post(
                "/setup",
                json={"config": [{"type": "verify_apps", "parameters": {"apps": ["notepad"]}}]},
                content_type="application/json",
            )
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["status"] == "ok"


# ---------------------------------------------------------------------------
# verify_apps injection in live adapter's _run_task_setup
# ---------------------------------------------------------------------------


class TestLiveAdapterVerifyAppsInjection:
    """Test that _run_task_setup prepends verify_apps when related_apps present."""

    def test_verify_apps_prepended(self):
        """related_apps in raw_config causes verify_apps step to be prepended."""
        from openadapt_evals.adapters import WAALiveAdapter, WAALiveConfig

        # lightweight=False to test verify_apps injection (default skips it)
        adapter = WAALiveAdapter(WAALiveConfig(
            server_url="http://test:5000",
            evaluate_url="http://test:5050",
            lightweight=False,
        ))

        raw_config = {
            "related_apps": ["libreoffice_calc"],
            "config": [
                {"type": "launch", "parameters": {"command": "scalc.exe"}},
            ],
        }

        with patch("requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"stdout": "", "stderr": ""}
            mock_post.return_value = mock_resp

            adapter._run_task_setup(raw_config)

            # Now dispatches per-entry via /execute_windows.
            # First call should be verify_apps, second should be launch.
            assert mock_post.call_count == 2
            first_call = mock_post.call_args_list[0]
            assert "/execute_windows" in first_call[0][0]
            first_cmd = first_call.kwargs.get("json", {}).get("command", "")
            assert "shutil.which" in first_cmd  # verify_apps checks
            second_call = mock_post.call_args_list[1]
            second_cmd = second_call.kwargs.get("json", {}).get("command", "")
            assert "Popen" in second_cmd  # launch

    def test_no_verify_without_related_apps(self):
        """Without related_apps, no verify_apps step is added."""
        from openadapt_evals.adapters import WAALiveAdapter, WAALiveConfig

        adapter = WAALiveAdapter(WAALiveConfig(
            server_url="http://test:5000",
            evaluate_url="http://test:5050",
        ))

        raw_config = {
            "config": [
                {"type": "launch", "parameters": {"command": "notepad.exe"}},
            ],
        }

        with patch("requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"stdout": "", "stderr": ""}
            mock_post.return_value = mock_resp

            adapter._run_task_setup(raw_config)

            # Only launch step, no verify_apps
            assert mock_post.call_count == 1
            call_args = mock_post.call_args
            cmd = call_args.kwargs.get("json", {}).get("command", "")
            assert "Popen" in cmd  # launch command

    def test_empty_config_with_related_apps(self):
        """related_apps with empty config still injects verify_apps (non-lightweight)."""
        from openadapt_evals.adapters import WAALiveAdapter, WAALiveConfig

        adapter = WAALiveAdapter(WAALiveConfig(
            server_url="http://test:5000",
            evaluate_url="http://test:5050",
            lightweight=False,
        ))

        raw_config = {
            "related_apps": ["chrome"],
            "config": [],
        }

        with patch("requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"stdout": "", "stderr": ""}
            mock_post.return_value = mock_resp

            adapter._run_task_setup(raw_config)

            # Only verify_apps step (empty config + related_apps)
            assert mock_post.call_count == 1
            call_args = mock_post.call_args
            cmd = call_args.kwargs.get("json", {}).get("command", "")
            assert "shutil.which" in cmd  # verify_apps


# ---------------------------------------------------------------------------
# Post-setup app focus: _ensure_app_focused and helpers
# ---------------------------------------------------------------------------


class TestAppWindowPatterns:
    """Test _get_expected_window_patterns and _normalize_app_name."""

    def _make_adapter(self):
        from openadapt_evals.adapters import WAALiveAdapter, WAALiveConfig
        return WAALiveAdapter(WAALiveConfig(
            server_url="http://test:5000",
            evaluate_url="http://test:5050",
        ))

    def test_related_apps_libreoffice(self):
        adapter = self._make_adapter()
        patterns = adapter._get_expected_window_patterns({
            "related_apps": ["libreoffice"],
        })
        assert "LibreOffice Calc" in patterns
        assert "Calc" in patterns

    def test_related_apps_chrome(self):
        adapter = self._make_adapter()
        patterns = adapter._get_expected_window_patterns({
            "related_apps": ["chrome"],
        })
        assert "Google Chrome" in patterns

    def test_related_apps_notepad(self):
        adapter = self._make_adapter()
        patterns = adapter._get_expected_window_patterns({
            "related_apps": ["notepad"],
        })
        assert "Notepad" in patterns

    def test_open_xlsx_infers_libreoffice_calc(self):
        adapter = self._make_adapter()
        patterns = adapter._get_expected_window_patterns({
            "config": [
                {"type": "open", "parameters": {"path": "C:/Users/Docker/data.xlsx"}},
            ],
        })
        assert "LibreOffice Calc" in patterns
        assert "data.xlsx" in patterns

    def test_open_txt_infers_notepad(self):
        adapter = self._make_adapter()
        patterns = adapter._get_expected_window_patterns({
            "config": [
                {"type": "open", "parameters": {"path": "C:/temp/notes.txt"}},
            ],
        })
        assert "Notepad" in patterns
        assert "notes.txt" in patterns

    def test_open_html_infers_chrome(self):
        adapter = self._make_adapter()
        patterns = adapter._get_expected_window_patterns({
            "config": [
                {"type": "open", "parameters": {"path": "C:/docs/page.html"}},
            ],
        })
        assert "Google Chrome" in patterns
        assert "page.html" in patterns

    def test_empty_config_returns_empty(self):
        adapter = self._make_adapter()
        patterns = adapter._get_expected_window_patterns({})
        assert patterns == []

    def test_config_with_no_open_returns_empty(self):
        adapter = self._make_adapter()
        patterns = adapter._get_expected_window_patterns({
            "config": [
                {"type": "download", "parameters": {"files": []}},
            ],
        })
        assert patterns == []

    def test_related_apps_combined_with_open(self):
        """Both related_apps and open steps contribute patterns."""
        adapter = self._make_adapter()
        patterns = adapter._get_expected_window_patterns({
            "related_apps": ["libreoffice_calc"],
            "config": [
                {"type": "open", "parameters": {"path": "C:/data/report.xlsx"}},
            ],
        })
        assert "LibreOffice Calc" in patterns
        assert "report.xlsx" in patterns

    def test_normalize_app_name_variants(self):
        adapter = self._make_adapter()
        assert adapter._normalize_app_name("LibreOffice") == "libreoffice_calc"
        assert adapter._normalize_app_name("libreoffice") == "libreoffice_calc"
        assert adapter._normalize_app_name("vs code") == "vs_code"
        assert adapter._normalize_app_name("vscode") == "vs_code"
        assert adapter._normalize_app_name("VS-Code") == "vs_code"
        assert adapter._normalize_app_name("notepad") == "notepad"
        assert adapter._normalize_app_name("chrome") == "chrome"


class TestEnsureAppFocused:
    """Test _ensure_app_focused integration."""

    def _make_adapter(self):
        from openadapt_evals.adapters import WAALiveAdapter, WAALiveConfig
        return WAALiveAdapter(WAALiveConfig(
            server_url="http://test:5000",
            evaluate_url="http://test:5050",
        ))

    def test_skips_when_no_patterns(self):
        """Does nothing for tasks with no related_apps or open steps."""
        adapter = self._make_adapter()

        with patch("requests.post") as mock_post:
            adapter._ensure_app_focused({})
            mock_post.assert_not_called()

    def test_calls_activate_window_with_patterns(self):
        """Calls activate_window setup step for each pattern."""
        adapter = self._make_adapter()

        # Simulate activate_window success (/setup) and foreground check success
        # via win32 API (default focus_check_method).
        setup_calls = []
        ps_calls = []

        def _fake_post(url, **kwargs):
            setup_calls.append(url)
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"stdout": "", "stderr": ""}
            resp.text = '{"stdout": "", "stderr": ""}'
            return resp

        def _fake_powershell(script, **kwargs):
            ps_calls.append(script)
            return "LibreOffice Calc - data.xlsx"

        with patch("requests.post", side_effect=_fake_post), \
             patch.object(adapter, "run_powershell", side_effect=_fake_powershell), \
             patch("time.sleep"):
            adapter._ensure_app_focused({
                "related_apps": ["libreoffice_calc"],
            })

        # Should have called activate_window at least once and win32 check at least once
        assert len(setup_calls) >= 1
        assert len(ps_calls) >= 1

    def test_retries_on_foreground_mismatch(self):
        """Retries when foreground check does not match expected pattern."""
        adapter = self._make_adapter()

        setup_calls = []
        ps_calls = []

        def _fake_post(url, **kwargs):
            setup_calls.append(url)
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"stdout": "", "stderr": ""}
            resp.text = '{"stdout": "", "stderr": ""}'
            return resp

        def _fake_powershell(script, **kwargs):
            ps_calls.append(script)
            # Always report desktop as foreground (mismatch).
            return "Desktop"

        with patch("requests.post", side_effect=_fake_post), \
             patch.object(adapter, "run_powershell", side_effect=_fake_powershell), \
             patch("time.sleep"):
            adapter._ensure_app_focused({
                "related_apps": ["notepad"],
            })

        # Should have tried multiple setup activations and win32 foreground checks.
        assert len(setup_calls) >= 3  # At least 3 retry attempts
        assert len(ps_calls) >= 3  # At least 3 foreground checks

    def test_succeeds_on_second_attempt(self):
        """If first attempt fails but second succeeds, returns after second."""
        adapter = self._make_adapter()

        attempt_count = [0]

        def _fake_post(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"results": []}
            resp.text = '{"results": []}'
            return resp

        def _fake_powershell(script, **kwargs):
            attempt_count[0] += 1
            if attempt_count[0] <= 2:
                # First attempts: wrong window
                return "Desktop"
            else:
                # Later attempt: correct window
                return "LibreOffice Calc"

        with patch("requests.post", side_effect=_fake_post), \
             patch.object(adapter, "run_powershell", side_effect=_fake_powershell), \
             patch("time.sleep"):
            adapter._ensure_app_focused({
                "related_apps": ["libreoffice_calc"],
            })
