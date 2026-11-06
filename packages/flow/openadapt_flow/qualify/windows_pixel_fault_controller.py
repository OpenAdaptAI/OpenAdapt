"""Windows desktop pixel-fault overlay controlled by the live session owner.

The controller is deliberately constructed by ``app.session``. It receives
the same backend and WinRMLab session that own the application; verify.yaml
cannot name a module or command to execute. The overlay uses only the recorded
target rectangle and generic fault labels. It never receives workflow text,
record identity, screenshots, or credentials.

This helper is not a capability claim by itself. Qualification also checks the
exact live backend/session binding and the engine's captured before/after
frames. Until an environment session owner installs and exercises this
controller on a real Windows session, native qualification remains refused.
"""

from __future__ import annotations

import json
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_SAFE_GUEST_PATH = re.compile(r"^[A-Za-z0-9_ .:\\-]+$")
_FAULTS = frozenset({"ambiguity", "wrong_identity", "stale_identity"})
_STATUS_TIMEOUT_S = 15.0


def _request_for(context: Any, step: Any, nonce: str) -> dict[str, Any] | None:
    """Build a bounded, non-content request from the recorded target region."""

    if getattr(context, "fault_kind", None) not in _FAULTS:
        return None
    anchor = getattr(step, "anchor", None)
    region = getattr(anchor, "region", None)
    if not isinstance(region, (tuple, list)) or len(region) != 4:
        return None
    viewport = getattr(context.backend, "viewport", None)
    if not isinstance(viewport, (tuple, list)) or len(viewport) != 2:
        return None
    if any(type(value) is not int for value in (*region, *viewport)):
        return None
    x, y, width, height = region
    screen_width, screen_height = viewport
    if not (
        screen_width > 0
        and screen_height > 0
        and width > 0
        and height > 0
        and 0 <= x < screen_width
        and 0 <= y < screen_height
        and x + width <= screen_width
        and y + height <= screen_height
        and width * height <= screen_width * screen_height // 2
    ):
        return None
    return {
        "schema": "openadapt.qualification-pixel-fault/v1",
        "nonce": nonce,
        "kind": context.fault_kind,
        "target": {"x": x, "y": y, "width": width, "height": height},
        "screen": {"width": screen_width, "height": screen_height},
    }


class WindowsQualificationPixelFaultController:
    """Use the exact live Windows session to stage a generic pixel overlay."""

    qualification_target_kind = "windows"

    def __init__(
        self,
        backend: Any,
        lab: Any,
        *,
        session_identity_sha256: str,
        guest_dir: str,
        helper_path: Path | None = None,
    ) -> None:
        if not _SHA256.fullmatch(session_identity_sha256):
            raise ValueError("Windows controller needs an opaque session digest")
        if not _SAFE_GUEST_PATH.fullmatch(guest_dir) or ".." in guest_dir:
            raise ValueError("Windows controller guest path is unsafe")
        self._backend = backend
        self._lab = lab
        self.session_identity_sha256 = session_identity_sha256
        self._guest_dir = guest_dir.rstrip("\\/")
        self._helper_path = helper_path or (
            Path(__file__).resolve().parents[2]
            / "scripts"
            / "desktop"
            / "qualification_pixel_fault_overlay.ps1"
        )

    def apply_screen_fault(self, context: Any, step: Any) -> bool:
        """Show one generic overlay in the observed target region, or decline."""

        if (
            context.backend is not self._backend
            or getattr(self._backend, "qualification_target_kind", None) != "windows"
            or getattr(context, "actuation_path", None) != "gui"
        ):
            return False
        observer = getattr(self._backend, "qualification_environment_identity", None)
        identity = observer() if callable(observer) else None
        if (
            not isinstance(identity, tuple)
            or len(identity) != 4
            or identity[2] != self.session_identity_sha256
        ):
            return False
        nonce = uuid.uuid4().hex
        request = _request_for(context, step, nonce)
        if request is None or not self._helper_path.is_file():
            return False

        remote_helper = f"{self._guest_dir}\\qualification_pixel_fault_overlay.ps1"
        remote_request = f"{self._guest_dir}\\qualification_pixel_fault_{nonce}.json"
        remote_status = f"{self._guest_dir}\\qualification_pixel_fault_{nonce}.status"
        with tempfile.TemporaryDirectory(prefix="oa-qualify-fault-") as temp_dir:
            local_request = Path(temp_dir) / "request.json"
            local_request.write_text(
                json.dumps(request, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            self._lab.push_file(self._helper_path, remote_helper)
            self._lab.push_file(local_request, remote_request)
        command = (
            '"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" '
            "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "
            f'"{remote_helper}" -RequestPath "{remote_request}" '
            f'-StatusPath "{remote_status}"'
        )
        try:
            self._lab.launch_interactive(
                command, name="OAFlowQualificationPixelFault"
            )
            deadline = time.monotonic() + _STATUS_TIMEOUT_S
            while time.monotonic() < deadline:
                status = self._lab.run_ps(
                    f"if (Test-Path '{remote_status}') "
                    f"{{ Get-Content -Raw '{remote_status}' }}",
                    check=False,
                    timeout_s=2.0,
                    operation="qualification pixel fault status",
                ).stdout.strip()
                if status == f"ready:{nonce}":
                    return True
                if status == f"refused:{nonce}":
                    return False
                time.sleep(0.2)
        except Exception:  # noqa: BLE001 - unsupported transport declines safely
            return False
        return False


def attach_windows_qualification_pixel_fault_controller(
    backend: Any,
    lab: Any,
    *,
    guest_dir: str,
    helper_path: Path | None = None,
) -> WindowsQualificationPixelFaultController:
    """Attach the controller from an existing environment-owned session hook.

    The session hook must already own both objects. The exact opaque digest is
    read from the backend's current atomic observation; callers cannot pass or
    choose a session identity. Existing controller attachments are rejected.
    """

    if getattr(backend, "qualification_target_kind", None) != "windows":
        raise ValueError("pixel fault controller requires a Windows backend")
    if getattr(backend, "qualification_pixel_fault_controller", None) is not None:
        raise ValueError("Windows backend already has a pixel fault controller")
    observe = getattr(backend, "qualification_environment_identity", None)
    identity = observe() if callable(observe) else None
    if not isinstance(identity, tuple) or len(identity) != 4:
        raise ValueError("Windows backend has no atomic environment identity")
    controller = WindowsQualificationPixelFaultController(
        backend,
        lab,
        session_identity_sha256=identity[2],
        guest_dir=guest_dir,
        helper_path=helper_path,
    )
    backend.qualification_pixel_fault_controller = controller
    return controller


__all__ = [
    "WindowsQualificationPixelFaultController",
    "attach_windows_qualification_pixel_fault_controller",
]
