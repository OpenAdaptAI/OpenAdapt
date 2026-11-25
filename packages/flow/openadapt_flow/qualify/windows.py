"""Windows campaign observations and real, opt-in native fault staging.

The observer retains the native backend's machine/OS and executable binding.
It does not make qualifications portable to another Windows installation.
For runtime-hosted applications the executable digest covers the host, not
scripts or application libraries; the application version remains declared.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from openadapt_flow.qualification_environment import (
    BackendQualificationEnvironmentObserver,
    QualificationEnvironmentObservation,
)
from openadapt_flow.qualify.faults import WebFaultDriver

WINDOWS_OBSERVER_ID = "openadapt.qualify.windows-environment"
WINDOWS_OBSERVER_CONTRACT_SHA256 = hashlib.sha256(
    b"openadapt.qualify.windows-environment/v1\n"
    b"atomic native application, executable digest, session and machine/OS digest; "
    b"declared application version bound with observed executable and environment"
).hexdigest()
WINDOWS_FAULT_DRIVER_ID = "openadapt.qualify.windows-faults"
WINDOWS_FAULT_DRIVER_CONTRACT_SHA256 = hashlib.sha256(
    b"openadapt.qualify.windows-faults/v2\n"
    b"opt-in authenticated real UIA/physical overlays from the recorded locator; "
    b"ambiguity@target_resolution; wrong_identity@recorded_identifier_region; "
    b"stale_identity@actuation_revalidation; weak_effect@effect_strength; "
    b"missing_effect@effect_verifier; owned cleanup"
).hexdigest()


class WindowsEnvironmentObserver:
    """Observe each newly opened native backend without trusting config identity."""

    observer_id = WINDOWS_OBSERVER_ID
    contract_sha256 = WINDOWS_OBSERVER_CONTRACT_SHA256

    def __init__(self, *, application_version: str) -> None:
        self._version = application_version

    def observe(
        self,
        backend: Any,
        target_kind: Literal["web", "windows", "macos", "linux", "rdp", "citrix"],
    ) -> QualificationEnvironmentObservation:
        if target_kind != "windows":
            raise ValueError("the Windows campaign observer needs a Windows target")
        observed = BackendQualificationEnvironmentObserver(backend).observe(
            backend, target_kind
        )
        binding = {
            "native_environment": observed.environment_digest,
            "application": observed.application_identity,
            "executable": observed.application_version,
            "declared_version": self._version,
        }
        digest = hashlib.sha256(
            json.dumps(binding, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return observed.model_copy(
            update={
                "application_version": self._version,
                "environment_digest": digest,
            }
        )


class WindowsFaultDriver(WebFaultDriver):
    """Reuse surface-independent receipt/effect faults, stage native screen faults."""

    driver_id = WINDOWS_FAULT_DRIVER_ID
    contract_sha256 = WINDOWS_FAULT_DRIVER_CONTRACT_SHA256

    def __init__(self, workflow: Any, **kwargs: Any) -> None:
        super().__init__(workflow, **kwargs)
        self._staged: list[tuple[Any, str]] = []

    @staticmethod
    def mutation_bytes(kind: str) -> bytes:
        return f"{WINDOWS_FAULT_DRIVER_ID}/{kind}/v2\n".encode("ascii")

    def _screen_fault(self, context: Any) -> str | None:
        from openadapt_flow.qualification_drills import _settled_screenshot
        from openadapt_flow.qualification_faults import sha256_bytes

        step = self._steps.get(context.step_id)
        anchor = step.anchor if step is not None else None
        locator = anchor.structural if anchor is not None else None
        stage = getattr(context.backend, "stage_qualification_fault", None)
        if context.actuation_path != "gui" or not callable(stage):
            self.declined = "native_fault_staging_unavailable"
            return None
        if (
            anchor is None
            or locator is None
            or not (locator.automation_id or (locator.role and locator.name))
        ):
            self.declined = "native_save_locator_unavailable"
            return None
        if context.fault_kind == "wrong_identity" and anchor.identifier_region is None:
            self.declined = "native_identity_region_unavailable"
            return None
        try:
            if context.fault_kind == "wrong_identity":
                assert anchor.identifier_region is not None
                x, y, width, height = anchor.identifier_region
                click_x, click_y = anchor.click_point
                mutation_id = stage(
                    context.fault_kind,
                    locator,
                    identity_offset=(x - click_x, y - click_y, width, height),
                )
            else:
                mutation_id = stage(context.fault_kind, locator)
            self._staged.append((context.backend, mutation_id))
            capture = (
                getattr(context, "capture_frame", None) or context.backend.screenshot
            )
            frame = _settled_screenshot(capture)
            if frame is None and capture is not context.backend.screenshot:
                frame = _settled_screenshot(context.backend.screenshot)
            if frame is None:
                self.declined = "native_fault_did_not_settle"
                return None
            return sha256_bytes(frame)
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError):
            self.declined = "native_fault_staging_failed"
            return None

    def cleanup(self, backend: Any) -> None:
        """Clear only overlays this campaign staged; cleanup failure is a refusal."""
        owned = [item for item in self._staged if item[0] is backend]
        for _, mutation_id in owned:
            backend.clear_qualification_fault(mutation_id)
            self._staged.remove((backend, mutation_id))
