"""Tests for multi-layer WAA probe."""

import json
from unittest.mock import MagicMock, patch

import pytest
import requests as _requests

from openadapt_evals.infrastructure.probe import (
    MultiLayerProbeResult,
    ProbeLayerResult,
    _build_summary,
    _count_a11y_elements,
    multi_layer_probe,
    print_probe_results,
    probe_layer_a11y,
    probe_layer_action,
    probe_layer_score,
    probe_layer_screenshot,
)

# PNG magic bytes for a minimal valid PNG header
_PNG_HEADER = b"\x89PNG\r\n\x1a\n"

# Patch targets — patch on the module that imported requests
_PATCH_GET = "openadapt_evals.infrastructure.probe.requests.get"
_PATCH_POST = "openadapt_evals.infrastructure.probe.requests.post"


def _make_png_bytes(size: int = 100) -> bytes:
    """Create fake PNG data with valid magic bytes."""
    return _PNG_HEADER + b"\x00" * size


# ============================================================================
# Dataclass serialization
# ============================================================================


class TestProbeLayerResult:
    def test_to_dict(self):
        r = ProbeLayerResult(layer="screenshot", success=True, latency_ms=42.5, details={"size_bytes": 1024})
        d = r.to_dict()
        assert d["layer"] == "screenshot"
        assert d["success"] is True
        assert d["latency_ms"] == 42.5
        assert d["details"]["size_bytes"] == 1024
        assert d["error"] is None

    def test_to_json(self):
        r = ProbeLayerResult(layer="a11y", success=False, latency_ms=10, error="Timeout")
        j = r.to_json()
        data = json.loads(j)
        assert data["layer"] == "a11y"
        assert data["success"] is False
        assert data["error"] == "Timeout"

    def test_defaults(self):
        r = ProbeLayerResult(layer="test", success=True, latency_ms=0)
        assert r.details == {}
        assert r.error is None


class TestMultiLayerProbeResult:
    def test_to_dict(self):
        layer = ProbeLayerResult(layer="screenshot", success=True, latency_ms=50)
        r = MultiLayerProbeResult(
            server_url="http://localhost:5001",
            layers=[layer],
            overall_ready=True,
            summary="screenshot:PASS",
        )
        d = r.to_dict()
        assert d["server_url"] == "http://localhost:5001"
        assert len(d["layers"]) == 1
        assert d["layers"][0]["layer"] == "screenshot"
        assert d["overall_ready"] is True

    def test_to_json(self):
        r = MultiLayerProbeResult(server_url="http://test", layers=[], overall_ready=False, summary="")
        j = r.to_json()
        data = json.loads(j)
        assert data["server_url"] == "http://test"
        assert data["layers"] == []

    def test_timestamp_auto_set(self):
        r = MultiLayerProbeResult()
        assert r.timestamp  # non-empty


# ============================================================================
# Screenshot layer
# ============================================================================


class TestProbeLayerScreenshot:
    @patch(_PATCH_GET)
    def test_png_success(self, mock_get):
        resp = MagicMock()
        resp.status_code = 200
        resp.content = _make_png_bytes(500)
        mock_get.return_value = resp
        result = probe_layer_screenshot("http://localhost:5001", timeout=5)
        assert result.success is True
        assert result.layer == "screenshot"
        assert result.details["is_png"] is True
        assert result.details["size_bytes"] > 0
        assert result.error is None

    @patch(_PATCH_GET)
    def test_non_png_failure(self, mock_get):
        resp = MagicMock()
        resp.status_code = 200
        resp.content = b"<html>not a png</html>"
        mock_get.return_value = resp
        result = probe_layer_screenshot("http://localhost:5001")
        assert result.success is False
        assert result.details["is_png"] is False

    @patch(_PATCH_GET)
    def test_http_error(self, mock_get):
        resp = MagicMock()
        resp.status_code = 500
        mock_get.return_value = resp
        result = probe_layer_screenshot("http://localhost:5001")
        assert result.success is False
        assert "HTTP 500" in result.error

    @patch(_PATCH_GET)
    def test_connection_error(self, mock_get):
        mock_get.side_effect = _requests.ConnectionError("refused")
        result = probe_layer_screenshot("http://localhost:5001")
        assert result.success is False
        assert "Connection error" in result.error

    @patch(_PATCH_GET)
    def test_timeout(self, mock_get):
        mock_get.side_effect = _requests.Timeout()
        result = probe_layer_screenshot("http://localhost:5001")
        assert result.success is False
        assert result.error == "Timeout"


# ============================================================================
# Accessibility layer
# ============================================================================


class TestProbeLayerA11y:
    @patch(_PATCH_GET)
    def test_json_success(self, mock_get):
        resp = MagicMock()
        resp.status_code = 200
        resp.text = json.dumps([{"Name": "Desktop"}, {"Name": "Taskbar"}])
        mock_get.return_value = resp
        result = probe_layer_a11y("http://localhost:5001")
        assert result.success is True
        assert result.details["element_count"] == 2

    @patch(_PATCH_GET)
    def test_xml_success(self, mock_get):
        resp = MagicMock()
        resp.status_code = 200
        resp.text = 'Name="Desktop" Role="Window"\nName="Taskbar" Role="Pane"'
        mock_get.return_value = resp
        result = probe_layer_a11y("http://localhost:5001")
        assert result.success is True
        assert result.details["element_count"] == 2

    @patch(_PATCH_GET)
    def test_empty_tree_failure(self, mock_get):
        resp = MagicMock()
        resp.status_code = 200
        resp.text = ""
        mock_get.return_value = resp
        result = probe_layer_a11y("http://localhost:5001")
        assert result.success is False
        assert "Empty" in result.error

    @patch(_PATCH_GET)
    def test_http_error(self, mock_get):
        resp = MagicMock()
        resp.status_code = 503
        mock_get.return_value = resp
        result = probe_layer_a11y("http://localhost:5001")
        assert result.success is False
        assert "HTTP 503" in result.error

    @patch(_PATCH_GET)
    def test_passes_uia_backend(self, mock_get):
        resp = MagicMock()
        resp.status_code = 200
        resp.text = json.dumps([{"Name": "Test"}])
        mock_get.return_value = resp
        probe_layer_a11y("http://localhost:5001")
        mock_get.assert_called_once()
        call_kwargs = mock_get.call_args
        assert call_kwargs[1]["params"] == {"backend": "uia"}


# ============================================================================
# Action layer
# ============================================================================


class TestProbeLayerAction:
    @patch(_PATCH_POST)
    def test_point_output_success(self, mock_post):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"output": "Point(x=500, y=400)"}
        mock_post.return_value = resp
        result = probe_layer_action("http://localhost:5001")
        assert result.success is True
        assert "Point" in result.details["output"]

    @patch(_PATCH_POST)
    def test_bad_output_failure(self, mock_post):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"output": "error: display not found"}
        mock_post.return_value = resp
        result = probe_layer_action("http://localhost:5001")
        assert result.success is False
        assert "Expected 'Point'" in result.error

    @patch(_PATCH_POST)
    def test_correct_command_sent(self, mock_post):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"output": "Point(x=0, y=0)"}
        mock_post.return_value = resp
        probe_layer_action("http://localhost:5001")
        call_kwargs = mock_post.call_args
        payload = call_kwargs[1]["json"]
        assert "python -c" in payload["command"]
        assert "pyautogui.position()" in payload["command"]

    @patch(_PATCH_POST)
    def test_http_error(self, mock_post):
        resp = MagicMock()
        resp.status_code = 500
        mock_post.return_value = resp
        result = probe_layer_action("http://localhost:5001")
        assert result.success is False

    @patch(_PATCH_POST)
    def test_non_json_response(self, mock_post):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.side_effect = ValueError("not json")
        resp.text = "Point(x=100, y=200)"
        mock_post.return_value = resp
        result = probe_layer_action("http://localhost:5001")
        # Falls back to resp.text, data = {"output": resp.text}
        assert result.success is True


# ============================================================================
# Score layer
# ============================================================================


class TestProbeLayerScore:
    @patch(_PATCH_POST)
    def test_success(self, mock_post):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"score": 1.0}
        mock_post.return_value = resp
        result = probe_layer_score("http://localhost:5001")
        assert result.success is True
        assert result.details["score"] == 1.0

    @patch(_PATCH_POST)
    def test_evaluate_url_routing(self, mock_post):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"score": 0.5}
        mock_post.return_value = resp
        result = probe_layer_score("http://localhost:5001", evaluate_url="http://localhost:5051")
        assert result.success is True
        call_args = mock_post.call_args
        assert "localhost:5051" in call_args[0][0]
        assert result.details["evaluate_url"] == "http://localhost:5051"

    @patch(_PATCH_POST)
    def test_http_error(self, mock_post):
        resp = MagicMock()
        resp.status_code = 502
        mock_post.return_value = resp
        result = probe_layer_score("http://localhost:5001")
        assert result.success is False
        assert "HTTP 502" in result.error

    @patch(_PATCH_POST)
    def test_no_score_in_response(self, mock_post):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"error": "metric not found"}
        mock_post.return_value = resp
        result = probe_layer_score("http://localhost:5001")
        assert result.success is False
        assert "No numeric score" in result.error

    @patch(_PATCH_POST)
    def test_sends_correct_payload(self, mock_post):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"score": 1.0}
        mock_post.return_value = resp
        probe_layer_score("http://localhost:5001")
        call_kwargs = mock_post.call_args
        payload = call_kwargs[1]["json"]
        assert payload["command"] == "echo probe_test"
        assert payload["expected"] == "probe_test"
        assert payload["metric"] == "exact_match"


# ============================================================================
# Multi-layer orchestrator
# ============================================================================


class TestMultiLayerProbe:
    @patch("openadapt_evals.infrastructure.probe.probe_layer_score")
    @patch("openadapt_evals.infrastructure.probe.probe_layer_action")
    @patch("openadapt_evals.infrastructure.probe.probe_layer_a11y")
    @patch("openadapt_evals.infrastructure.probe.probe_layer_screenshot")
    def test_all_pass(self, mock_ss, mock_a11y, mock_action, mock_score):
        mock_ss.return_value = ProbeLayerResult(layer="screenshot", success=True, latency_ms=50)
        mock_a11y.return_value = ProbeLayerResult(layer="a11y", success=True, latency_ms=30)
        mock_action.return_value = ProbeLayerResult(layer="action", success=True, latency_ms=40)
        mock_score.return_value = ProbeLayerResult(layer="score", success=True, latency_ms=60)

        result = multi_layer_probe("http://localhost:5001")
        assert result.overall_ready is True
        assert len(result.layers) == 4
        assert all(lr.success for lr in result.layers)

    @patch("openadapt_evals.infrastructure.probe.probe_layer_score")
    @patch("openadapt_evals.infrastructure.probe.probe_layer_action")
    @patch("openadapt_evals.infrastructure.probe.probe_layer_a11y")
    @patch("openadapt_evals.infrastructure.probe.probe_layer_screenshot")
    def test_bail_early(self, mock_ss, mock_a11y, mock_action, mock_score):
        mock_ss.return_value = ProbeLayerResult(layer="screenshot", success=False, latency_ms=50, error="HTTP 500")

        result = multi_layer_probe("http://localhost:5001", bail_early=True)
        assert result.overall_ready is False
        assert len(result.layers) == 4
        assert result.layers[0].success is False
        for lr in result.layers[1:]:
            assert lr.error == "Skipped"
            assert lr.success is False
        mock_a11y.assert_not_called()
        mock_action.assert_not_called()
        mock_score.assert_not_called()

    @patch("openadapt_evals.infrastructure.probe.probe_layer_score")
    @patch("openadapt_evals.infrastructure.probe.probe_layer_action")
    @patch("openadapt_evals.infrastructure.probe.probe_layer_a11y")
    @patch("openadapt_evals.infrastructure.probe.probe_layer_screenshot")
    def test_no_bail_early(self, mock_ss, mock_a11y, mock_action, mock_score):
        mock_ss.return_value = ProbeLayerResult(layer="screenshot", success=False, latency_ms=50, error="HTTP 500")
        mock_a11y.return_value = ProbeLayerResult(layer="a11y", success=True, latency_ms=30)
        mock_action.return_value = ProbeLayerResult(layer="action", success=True, latency_ms=40)
        mock_score.return_value = ProbeLayerResult(layer="score", success=True, latency_ms=60)

        result = multi_layer_probe("http://localhost:5001", bail_early=False)
        assert result.overall_ready is False
        assert len(result.layers) == 4
        mock_a11y.assert_called_once()
        mock_action.assert_called_once()
        mock_score.assert_called_once()

    @patch("openadapt_evals.infrastructure.probe.probe_layer_a11y")
    @patch("openadapt_evals.infrastructure.probe.probe_layer_screenshot")
    def test_subset_layers(self, mock_ss, mock_a11y):
        mock_ss.return_value = ProbeLayerResult(layer="screenshot", success=True, latency_ms=50)
        mock_a11y.return_value = ProbeLayerResult(layer="a11y", success=True, latency_ms=30)

        result = multi_layer_probe("http://localhost:5001", layers=["screenshot", "a11y"])
        assert result.overall_ready is True
        assert len(result.layers) == 2
        assert result.layers[0].layer == "screenshot"
        assert result.layers[1].layer == "a11y"

    def test_invalid_layer_name(self):
        with pytest.raises(ValueError, match="Unknown layer"):
            multi_layer_probe("http://localhost:5001", layers=["bogus"])

    @patch("openadapt_evals.infrastructure.probe.probe_layer_score")
    @patch("openadapt_evals.infrastructure.probe.probe_layer_action")
    @patch("openadapt_evals.infrastructure.probe.probe_layer_a11y")
    @patch("openadapt_evals.infrastructure.probe.probe_layer_screenshot")
    def test_evaluate_url_passed_to_score(self, mock_ss, mock_a11y, mock_action, mock_score):
        mock_ss.return_value = ProbeLayerResult(layer="screenshot", success=True, latency_ms=10)
        mock_a11y.return_value = ProbeLayerResult(layer="a11y", success=True, latency_ms=10)
        mock_action.return_value = ProbeLayerResult(layer="action", success=True, latency_ms=10)
        mock_score.return_value = ProbeLayerResult(layer="score", success=True, latency_ms=10)

        multi_layer_probe("http://localhost:5001", evaluate_url="http://localhost:5051")
        mock_score.assert_called_once_with("http://localhost:5001", "http://localhost:5051", 10)


# ============================================================================
# Helpers
# ============================================================================


class TestCountElements:
    def test_json_array(self):
        assert _count_a11y_elements('[{"Name": "A"}, {"Name": "B"}]') == 2

    def test_json_dict_with_children(self):
        assert _count_a11y_elements('{"children": [{"Name": "A"}, {"Name": "B"}]}') == 3

    def test_xml_name_equals(self):
        assert _count_a11y_elements('Name="A"\nName="B"\nName="C"') == 3

    def test_empty_string(self):
        assert _count_a11y_elements("") == 0

    def test_plain_text_lines(self):
        text = "line1\nline2\nline3"
        assert _count_a11y_elements(text) == 3


class TestBuildSummary:
    def test_all_pass(self):
        layers = [
            ProbeLayerResult(layer="screenshot", success=True, latency_ms=0),
            ProbeLayerResult(layer="a11y", success=True, latency_ms=0),
        ]
        summary = _build_summary(layers)
        assert "screenshot:PASS" in summary
        assert "a11y:PASS" in summary

    def test_fail_and_skip(self):
        layers = [
            ProbeLayerResult(layer="screenshot", success=False, latency_ms=0, error="HTTP 500"),
            ProbeLayerResult(layer="a11y", success=False, latency_ms=0, error="Skipped"),
        ]
        summary = _build_summary(layers)
        assert "screenshot:FAIL" in summary
        assert "a11y:SKIP" in summary


# ============================================================================
# Print utility
# ============================================================================


class TestPrintProbeResults:
    def test_smoke_no_crash(self, capsys):
        result = MultiLayerProbeResult(
            server_url="http://localhost:5001",
            layers=[
                ProbeLayerResult(layer="screenshot", success=True, latency_ms=42, details={"size_bytes": 1024}),
                ProbeLayerResult(layer="a11y", success=False, latency_ms=100, error="Timeout"),
                ProbeLayerResult(layer="action", success=False, latency_ms=0, error="Skipped"),
            ],
            overall_ready=False,
            summary="screenshot:PASS | a11y:FAIL | action:SKIP",
        )
        print_probe_results(result)
        captured = capsys.readouterr()
        assert "PASS" in captured.out
        assert "FAIL" in captured.out
        assert "SKIP" in captured.out
        assert "NOT READY" in captured.out

    def test_all_pass_output(self, capsys):
        result = MultiLayerProbeResult(
            server_url="http://test",
            layers=[ProbeLayerResult(layer="screenshot", success=True, latency_ms=10)],
            overall_ready=True,
            summary="screenshot:PASS",
        )
        print_probe_results(result)
        captured = capsys.readouterr()
        assert "READY" in captured.out
