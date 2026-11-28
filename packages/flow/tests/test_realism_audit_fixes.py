"""Unit tests for REALISM_AUDIT open bug fixes (Ranks 15-16, macOS y-scale, store roots)."""

from __future__ import annotations

import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from openadapt_flow.backends.macos_backend import MacOSBackend
from openadapt_flow.backends.remote_display import WindowInfo
from openadapt_flow.ir import ActionKind, Step, StepResult, Workflow
from openadapt_flow.path_safety import (
    canonicalize_platform_root_symlinks,
    validate_no_store_symlinks,
)
from openadapt_flow.receipt_log import LocalLog, LogError, validate_receipt_store_root
from openadapt_flow.runtime.effects.effect import Verdict
from openadapt_flow.runtime.effects.onscreen import OnScreenReadbackVerifier
from openadapt_flow.runtime.effects.readback_values import (
    compare_label_bound_value,
    compare_value,
    extract_label_bound_value,
)
from openadapt_flow.runtime.replay.confirming import Confirming

# -- Rank 15: exact match on identifiers/codes/numbers + label-bound < 4 chars --


def test_rank15_exact_match_on_identifiers_numbers_and_codes():
    # Near-miss identifiers/codes/numbers must never fuzzy-pass.
    assert compare_value("MRN-12346", "MRN-12345", kind="code", source="ocr") == "mismatch"
    assert compare_value("MRN-12345", "MRN-12345", kind="code", source="ocr") == "match"
    assert compare_value("1006", "1005", kind="number", source="ocr") == "mismatch"
    assert compare_value("1005", "1005", kind="number", source="ocr") == "match"
    assert compare_value("E11.8", "E11.9", kind="code", source="ocr") == "mismatch"
    assert compare_value("E11.9", "E11.9", kind="code", source="ocr") == "match"


def test_rank15_label_bound_short_clinical_values():
    assert extract_label_bound_value("Eye: OD", "Eye") == "OD"
    assert extract_label_bound_value("Eye: OS", "Eye") == "OS"
    assert extract_label_bound_value("Sex: F  Age: 42", "Sex") == "F"

    # Label-bound < 4-char values match exactly and reject wrong codes.
    assert compare_label_bound_value("Eye: OD", "OD", label="Eye", source="ocr") == "match"
    assert compare_label_bound_value("Eye: OS", "OD", label="Eye", source="ocr") == "mismatch"
    assert compare_value("Eye: OD", "OD", label="Eye", source="ocr", exact=False) == "match"
    assert compare_value("Eye: OS", "OD", label="Eye", source="ocr", exact=False) == "mismatch"

    # Conflicting duplicate labels are ambiguous (halt, never guess).
    assert compare_label_bound_value("Eye: OD  Eye: OS", "OD", label="Eye") == "ambiguous"


def test_rank15_onscreen_readback_verifier_label_bound():
    class _FakeLine:
        def __init__(self, text: str, confidence: float = 0.99) -> None:
            self.text = text
            self.confidence = confidence

    class _FakeVision:
        def __init__(self, text: str) -> None:
            self._text = text

        def ocr(self, _png: bytes, region=None):
            del region
            return [_FakeLine(self._text)]

    class _FakeBackend:
        def screenshot(self) -> bytes:
            img = Image.new("RGB", (200, 60), (255, 255, 255))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()

    v_ok = OnScreenReadbackVerifier(_FakeBackend(), region=(0, 0, 200, 60), vision=_FakeVision("Eye: OD"))
    verdict_ok = v_ok.read_back("OD", label="Eye")
    assert verdict_ok.verdict == Verdict.CONFIRMED

    v_wrong = OnScreenReadbackVerifier(_FakeBackend(), region=(0, 0, 200, 60), vision=_FakeVision("Eye: OS"))
    verdict_wrong = v_wrong.read_back("OD", label="Eye")
    assert verdict_wrong.verdict == Verdict.REFUTED


# -- Rank 16: pre-keystroke active-window focus check before retry-after-no-change --


def test_rank16_retry_after_no_change_enforces_active_window_focus(tmp_path: Path):
    from openadapt_flow.backend import StructuralBackend

    class _FakeFocusedBackend(StructuralBackend):
        url = "https://clinic.example.test/chart"
        viewport = (1280, 800)

        def __init__(self, focused: bool) -> None:
            self._focused = focused
            self.actions: list[tuple] = []

        def verify_active_window_focus(self) -> bool:
            return self._focused

        def screenshot(self) -> bytes:
            img = Image.new("RGB", (100, 100), (255, 255, 255))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()

        def click(self, x: int, y: int, *, double: bool = False) -> None:
            self.actions.append(("click", x, y, double))

        def type_text(self, text: str) -> None:
            self.actions.append(("type_text", text))

        def press(self, key: str) -> None:
            self.actions.append(("press", key))

        def scroll(self, dx: int, dy: int) -> None:
            del dx, dy

        def find_by_selector(self, selector: str):
            del selector
            return []

        def text_value_at(self, x: int, y: int):
            del x, y
            return ""

        def focused_text_value(self):
            return ""

    class _Harness(Confirming):
        def __init__(self, backend) -> None:
            self.backend = backend
            self.vision = SimpleNamespace(
                pixels_changed=lambda *a, **kw: False,
                ocr=lambda *a, **kw: [],
            )
            self.governed_authorization = None

        def _settle_seeded(self) -> bytes:
            return self.backend.screenshot()

        def _step_needs_consequential_revalidation(self, step, workflow) -> bool:
            del step, workflow
            return False

        def _delivery_authorization_refusal(self, workflow, params, step, result):
            del workflow, params, step, result
            return None

        def _require_qualification_environment_current(self) -> None:
            return None

        def _require_locale_invariants_before_keyboard(self, workflow):
            del workflow
            return None

        def _deliver_backend_call(self, result, fn):
            del result
            return fn()

    backend = _FakeFocusedBackend(focused=False)
    harness = _Harness(backend)
    step = Step(id="s1", intent="type MRN", action=ActionKind.TYPE, text="24682")
    workflow = Workflow(name="test", steps=[step])
    result = StepResult(step_id="s1", intent="type MRN", ok=False)

    err = harness._verify_typed_input(
        step,
        "24682",
        (100, 100),
        backend.screenshot(),
        result,
        workflow=workflow,
        params={},
        bundle_dir=tmp_path,
        run_dir=tmp_path,
        resolution=None,
        baseline_field_value="",
        allow_retry=True,
    )

    assert err is not None
    assert "no longer holds active focus" in err
    assert result.safety_halt is True
    assert result.input_verified is False
    # Zero refocus clicks or retry keystrokes delivered!
    assert backend.actions == []


# -- macos_backend.py y-click scaling bug --


def test_macos_backend_click_uses_y_scale_not_x_scale():
    win = WindowInfo(
        window_id=42,
        owner="ClinicApp",
        title="Chart",
        pid=1234,
        bounds=(100.0, 200.0, 400.0, 300.0),
        on_screen=True,
    )

    class _FakeMacClient:
        def __init__(self) -> None:
            self.moves: list[tuple[float, float]] = []
            self.clicks: list[tuple[float, float]] = []

        def find_windows(self, owner_substr, title_substr=None):
            del owner_substr, title_substr
            return [win]

        def list_windows(self):
            return [win]

        def capture_trusted(self) -> bool:
            return True

        def input_trusted(self) -> bool:
            return True

        def ax_trusted(self) -> bool:
            return True

        def window_is_minimized(self, window) -> bool:
            del window
            return False

        def frontmost_pid(self):
            return 1234

        def focused_application_pid(self):
            return 1234

        def frontmost_window_id(self):
            return 42

        def frontmost_window_at(self, x: float, y: float):
            del x, y
            return 42

        def window_id_at_point(self, x: float, y: float):
            del x, y
            return 42

        def window_has_keyboard_focus(self, window):
            del window
            return True

        def exact_window_focused_main(self, window) -> bool:
            del window
            return True

        def activate(self, pid: int) -> None:
            del pid

        def raise_window(self, window) -> bool:
            del window
            return True

        def mouse_down(self, x: float, y: float, *, click_count: int = 1) -> None:
            del click_count
            self.clicks.append((x, y))

        def mouse_up(self, x: float, y: float, *, click_count: int = 1) -> None:
            del x, y, click_count

        def capture(self, window_id: int) -> tuple[bytes, int, int]:
            del window_id
            # 800x604 framebuffer over 400x300 logical bounds -> scale_x=2.0, scale_y=604/300 (within 1% tolerance).
            img = Image.new("RGB", (800, 604), (240, 240, 240))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue(), 800, 604

        def mouse_move(self, x: float, y: float) -> None:
            self.moves.append((x, y))

        def mouse(
            self, x: float, y: float, *, button: str = "left", down: bool, click_count: int = 1
        ) -> None:
            del button, click_count
            if down:
                self.clicks.append((x, y))

    client = _FakeMacClient()
    backend = MacOSBackend(client, app="ClinicApp", window_title="Chart", settle_s=0.0)
    backend.screenshot()
    assert backend._scale_x == pytest.approx(2.0)
    assert backend._scale_y == pytest.approx(604.0 / 300.0)

    backend.click(200, 300)
    expected_sx = 100.0 + 200.0 / 2.0
    expected_sy = 200.0 + 300.0 / (604.0 / 300.0)
    # With the old bug using scale_x=2.0 for y, sy would wrongly be 350.0 instead of ~349.0066.
    assert expected_sy != pytest.approx(350.0, abs=0.5)
    assert client.moves[-1] == (pytest.approx(expected_sx), pytest.approx(expected_sy))
    assert client.clicks[-1] == (pytest.approx(expected_sx), pytest.approx(expected_sy))


# -- Receipt store root symlink canonicalization --


def test_receipt_store_canonicalizes_platform_roots_and_refuses_internal_symlinks(tmp_path: Path):
    # 1. Platform root canonicalization: on macOS /tmp -> /private/tmp.
    if sys.platform == "darwin" and Path("/tmp").is_symlink():
        canonical = canonicalize_platform_root_symlinks("/tmp/openadapt-unit-test-probe")
        assert str(canonical).startswith("/private/tmp/")
        canonical_var = canonicalize_platform_root_symlinks("/var/tmp/openadapt-unit-test-probe")
        assert str(canonical_var).startswith("/private/var/")

    # 2. A normal store inside tmp_path passes validation (even if tmp_path is under /var/folders).
    store_root = tmp_path / "receipts"
    store_root.mkdir()
    validated = validate_receipt_store_root(store_root)
    assert validated == canonicalize_platform_root_symlinks(store_root)
    log = LocalLog(home=store_root)
    assert log.dir.name == "log"

    # 3. A symlink INSIDE the store is still refused!
    evil_target = tmp_path / "outside.txt"
    evil_target.write_text("evil", encoding="utf-8")
    (store_root / "leak").symlink_to(evil_target)
    with pytest.raises(LogError, match="must not contain a symlink"):
        validate_receipt_store_root(store_root)
    with pytest.raises(ValueError, match="must not contain a symlink"):
        validate_no_store_symlinks(store_root)

    # 4. A symlink AS the store root itself is still refused!
    linked_store = tmp_path / "linked-store"
    linked_store.symlink_to(tmp_path)
    with pytest.raises(LogError, match="must not traverse a symlink"):
        validate_receipt_store_root(linked_store)
