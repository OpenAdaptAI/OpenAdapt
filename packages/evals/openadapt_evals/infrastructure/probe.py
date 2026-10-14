"""Multi-layer WAA probe for per-layer diagnostics.

Probes 4 layers of the WAA stack using existing endpoints:
1. Screenshot — GET /screenshot (PNG capture working?)
2. Accessibility — GET /accessibility?backend=uia (a11y tree populated?)
3. Action — POST /execute with pyautogui.position() (action pipeline working?)
4. Score — POST /evaluate with echo probe_test (full getter→metric→score path?)

All probes are read-only and safe to run at any time.

Usage:
    from openadapt_evals.infrastructure.probe import multi_layer_probe

    result = multi_layer_probe("http://localhost:5001")
    print_probe_results(result)
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime

import requests

# Layer names in data-flow order
LAYER_SCREENSHOT = "screenshot"
LAYER_A11Y = "a11y"
LAYER_ACTION = "action"
LAYER_SCORE = "score"

ALL_LAYERS = [LAYER_SCREENSHOT, LAYER_A11Y, LAYER_ACTION, LAYER_SCORE]

# PNG magic bytes
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@dataclass
class ProbeLayerResult:
    """Result from probing a single WAA layer."""

    layer: str
    success: bool
    latency_ms: float
    details: dict = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict())


@dataclass
class MultiLayerProbeResult:
    """Aggregated result from probing multiple WAA layers."""

    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    server_url: str = ""
    layers: list[ProbeLayerResult] = field(default_factory=list)
    overall_ready: bool = False
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "server_url": self.server_url,
            "layers": [layer.to_dict() for layer in self.layers],
            "overall_ready": self.overall_ready,
            "summary": self.summary,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


def _count_a11y_elements(text: str) -> int:
    """Count elements in an accessibility tree response.

    Handles both JSON arrays and XML-like text.
    """
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return len(data)
        if isinstance(data, dict):
            # Single element or wrapper
            children = data.get("children", data.get("elements", []))
            if isinstance(children, list):
                return len(children) + 1
            return 1
    except (json.JSONDecodeError, TypeError):
        pass
    # XML/text fallback: count opening tags or Name= occurrences
    count = text.count("Name=") + text.count("<Name")
    if count == 0:
        # Try counting lines with content as a rough proxy
        count = sum(1 for line in text.splitlines() if line.strip())
    return count


def _build_summary(layers: list[ProbeLayerResult]) -> str:
    """Build a one-line summary from layer results."""
    parts = []
    for layer in layers:
        status = "PASS" if layer.success else ("SKIP" if layer.error == "Skipped" else "FAIL")
        parts.append(f"{layer.layer}:{status}")
    return " | ".join(parts)


def probe_layer_screenshot(url: str, timeout: float = 10) -> ProbeLayerResult:
    """Probe the screenshot layer via GET /screenshot.

    Validates that the response contains PNG data.
    """
    start = time.monotonic()
    try:
        resp = requests.get(f"{url}/screenshot", timeout=timeout)
        latency = (time.monotonic() - start) * 1000
        if resp.status_code != 200:
            return ProbeLayerResult(
                layer=LAYER_SCREENSHOT,
                success=False,
                latency_ms=latency,
                error=f"HTTP {resp.status_code}",
                details={"status_code": resp.status_code},
            )
        content = resp.content
        is_png = content[:8] == _PNG_MAGIC
        size = len(content)
        return ProbeLayerResult(
            layer=LAYER_SCREENSHOT,
            success=is_png and size > 0,
            latency_ms=latency,
            details={"is_png": is_png, "size_bytes": size},
            error=None if (is_png and size > 0) else "Response is not a valid PNG",
        )
    except requests.ConnectionError as e:
        latency = (time.monotonic() - start) * 1000
        return ProbeLayerResult(
            layer=LAYER_SCREENSHOT, success=False, latency_ms=latency,
            error=f"Connection error: {e}",
        )
    except requests.Timeout:
        latency = (time.monotonic() - start) * 1000
        return ProbeLayerResult(
            layer=LAYER_SCREENSHOT, success=False, latency_ms=latency,
            error="Timeout",
        )
    except Exception as e:
        latency = (time.monotonic() - start) * 1000
        return ProbeLayerResult(
            layer=LAYER_SCREENSHOT, success=False, latency_ms=latency,
            error=str(e),
        )


def probe_layer_a11y(url: str, timeout: float = 10) -> ProbeLayerResult:
    """Probe the accessibility layer via GET /accessibility?backend=uia.

    Validates that the response is non-empty and contains element data.
    """
    start = time.monotonic()
    try:
        resp = requests.get(f"{url}/accessibility", params={"backend": "uia"}, timeout=timeout)
        latency = (time.monotonic() - start) * 1000
        if resp.status_code != 200:
            return ProbeLayerResult(
                layer=LAYER_A11Y,
                success=False,
                latency_ms=latency,
                error=f"HTTP {resp.status_code}",
                details={"status_code": resp.status_code},
            )
        text = resp.text.strip()
        if not text:
            return ProbeLayerResult(
                layer=LAYER_A11Y,
                success=False,
                latency_ms=latency,
                error="Empty accessibility tree",
                details={"element_count": 0},
            )
        element_count = _count_a11y_elements(text)
        return ProbeLayerResult(
            layer=LAYER_A11Y,
            success=element_count > 0,
            latency_ms=latency,
            details={"element_count": element_count, "response_length": len(text)},
            error=None if element_count > 0 else "No elements found in accessibility tree",
        )
    except requests.ConnectionError as e:
        latency = (time.monotonic() - start) * 1000
        return ProbeLayerResult(
            layer=LAYER_A11Y, success=False, latency_ms=latency,
            error=f"Connection error: {e}",
        )
    except requests.Timeout:
        latency = (time.monotonic() - start) * 1000
        return ProbeLayerResult(
            layer=LAYER_A11Y, success=False, latency_ms=latency,
            error="Timeout",
        )
    except Exception as e:
        latency = (time.monotonic() - start) * 1000
        return ProbeLayerResult(
            layer=LAYER_A11Y, success=False, latency_ms=latency,
            error=str(e),
        )


def probe_layer_action(url: str, timeout: float = 10) -> ProbeLayerResult:
    """Probe the action layer via POST /execute with pyautogui.position().

    Uses python -c wrapper per WAA /execute command format requirements.
    Validates that output contains "Point" (pyautogui.position() returns Point(x, y)).
    """
    command = 'python -c "import pyautogui; print(pyautogui.position())"'
    start = time.monotonic()
    try:
        resp = requests.post(
            f"{url}/execute",
            json={"command": command},
            timeout=timeout,
        )
        latency = (time.monotonic() - start) * 1000
        if resp.status_code != 200:
            return ProbeLayerResult(
                layer=LAYER_ACTION,
                success=False,
                latency_ms=latency,
                error=f"HTTP {resp.status_code}",
                details={"status_code": resp.status_code},
            )
        try:
            data = resp.json()
        except (ValueError, json.JSONDecodeError):
            data = {"output": resp.text}
        output = str(data.get("output", data.get("result", "")))
        has_point = "Point" in output
        return ProbeLayerResult(
            layer=LAYER_ACTION,
            success=has_point,
            latency_ms=latency,
            details={"output": output, "command": command},
            error=None if has_point else f"Expected 'Point' in output, got: {output[:200]}",
        )
    except requests.ConnectionError as e:
        latency = (time.monotonic() - start) * 1000
        return ProbeLayerResult(
            layer=LAYER_ACTION, success=False, latency_ms=latency,
            error=f"Connection error: {e}",
        )
    except requests.Timeout:
        latency = (time.monotonic() - start) * 1000
        return ProbeLayerResult(
            layer=LAYER_ACTION, success=False, latency_ms=latency,
            error="Timeout",
        )
    except Exception as e:
        latency = (time.monotonic() - start) * 1000
        return ProbeLayerResult(
            layer=LAYER_ACTION, success=False, latency_ms=latency,
            error=str(e),
        )


def probe_layer_score(
    url: str,
    evaluate_url: str | None = None,
    timeout: float = 10,
) -> ProbeLayerResult:
    """Probe the scoring layer via POST /evaluate.

    Sends ``echo probe_test`` with ``exact_match`` metric to validate the
    full getter -> metric -> score path. Uses a separate ``evaluate_url``
    if provided (evaluate server typically runs on port 5051).
    """
    target = evaluate_url or url
    payload = {
        "command": "echo probe_test",
        "expected": "probe_test",
        "metric": "exact_match",
    }
    start = time.monotonic()
    try:
        resp = requests.post(f"{target}/evaluate", json=payload, timeout=timeout)
        latency = (time.monotonic() - start) * 1000
        if resp.status_code != 200:
            return ProbeLayerResult(
                layer=LAYER_SCORE,
                success=False,
                latency_ms=latency,
                error=f"HTTP {resp.status_code}",
                details={"status_code": resp.status_code, "evaluate_url": target},
            )
        try:
            data = resp.json()
        except (ValueError, json.JSONDecodeError):
            data = {}
        # Look for a numeric score in common response shapes
        score = data.get("score", data.get("result", data.get("value")))
        has_score = score is not None and isinstance(score, (int, float))
        return ProbeLayerResult(
            layer=LAYER_SCORE,
            success=has_score,
            latency_ms=latency,
            details={"score": score, "response": data, "evaluate_url": target},
            error=None if has_score else f"No numeric score in response: {data}",
        )
    except requests.ConnectionError as e:
        latency = (time.monotonic() - start) * 1000
        return ProbeLayerResult(
            layer=LAYER_SCORE, success=False, latency_ms=latency,
            error=f"Connection error: {e}",
            details={"evaluate_url": target},
        )
    except requests.Timeout:
        latency = (time.monotonic() - start) * 1000
        return ProbeLayerResult(
            layer=LAYER_SCORE, success=False, latency_ms=latency,
            error="Timeout",
            details={"evaluate_url": target},
        )
    except Exception as e:
        latency = (time.monotonic() - start) * 1000
        return ProbeLayerResult(
            layer=LAYER_SCORE, success=False, latency_ms=latency,
            error=str(e),
            details={"evaluate_url": target},
        )


# Map layer names to probe functions
_LAYER_FUNCS = {
    LAYER_SCREENSHOT: lambda url, timeout, **kw: probe_layer_screenshot(url, timeout),
    LAYER_A11Y: lambda url, timeout, **kw: probe_layer_a11y(url, timeout),
    LAYER_ACTION: lambda url, timeout, **kw: probe_layer_action(url, timeout),
    LAYER_SCORE: lambda url, timeout, **kw: probe_layer_score(url, kw.get("evaluate_url"), timeout),
}


def multi_layer_probe(
    server_url: str,
    timeout: float = 10,
    layers: list[str] | None = None,
    bail_early: bool = True,
    evaluate_url: str | None = None,
) -> MultiLayerProbeResult:
    """Run a multi-layer probe against a WAA server.

    Args:
        server_url: Base URL of the WAA server (e.g. http://localhost:5001).
        timeout: Per-layer timeout in seconds.
        layers: Subset of layers to probe. Defaults to all 4 layers.
        bail_early: If True, skip remaining layers after first failure.
        evaluate_url: Separate URL for the score layer (e.g. http://localhost:5051).

    Returns:
        MultiLayerProbeResult with per-layer results.
    """
    selected = layers or list(ALL_LAYERS)
    # Validate layer names
    for name in selected:
        if name not in _LAYER_FUNCS:
            raise ValueError(f"Unknown layer: {name!r}. Valid layers: {ALL_LAYERS}")

    result = MultiLayerProbeResult(server_url=server_url)
    failed = False

    for name in ALL_LAYERS:
        if name not in selected:
            continue
        if failed and bail_early:
            result.layers.append(ProbeLayerResult(
                layer=name, success=False, latency_ms=0, error="Skipped",
            ))
            continue
        layer_result = _LAYER_FUNCS[name](server_url, timeout, evaluate_url=evaluate_url)
        result.layers.append(layer_result)
        if not layer_result.success:
            failed = True

    result.overall_ready = all(lr.success for lr in result.layers)
    result.summary = _build_summary(result.layers)
    return result


def print_probe_results(result: MultiLayerProbeResult) -> None:
    """Print probe results in a terminal-friendly format."""
    print(f"\nWAA Multi-Layer Probe: {result.server_url}")
    print(f"Timestamp: {result.timestamp}")
    print("-" * 60)

    for layer in result.layers:
        if layer.success:
            status = "PASS"
        elif layer.error == "Skipped":
            status = "SKIP"
        else:
            status = "FAIL"
        print(f"  [{status}] {layer.layer:<12} {layer.latency_ms:>8.1f}ms", end="")
        if layer.error and layer.error != "Skipped":
            print(f"  error: {layer.error}")
        elif layer.details:
            # Show key details inline
            detail_parts = []
            for k, v in layer.details.items():
                if k in ("command", "response", "evaluate_url"):
                    continue
                detail_parts.append(f"{k}={v}")
            if detail_parts:
                print(f"  ({', '.join(detail_parts)})")
            else:
                print()
        else:
            print()

    print("-" * 60)
    overall = "READY" if result.overall_ready else "NOT READY"
    print(f"Overall: {overall}  [{result.summary}]")
    print()
