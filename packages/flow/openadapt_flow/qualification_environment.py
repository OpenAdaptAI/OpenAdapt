"""Exact environment observation contract for cross-surface qualification."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any, Literal, Protocol, cast, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

BACKEND_ENVIRONMENT_OBSERVER_CONTRACT_SHA256 = hashlib.sha256(
    b"openadapt.backend-qualification-environment-observer/v1:tuple4"
).hexdigest()


def qualification_environment_binding_sha256(
    *,
    target_kind: str,
    observer_id: str,
    observer_contract_sha256: str,
    application_identity_sha256: str,
    application_version_sha256: str,
    environment_digest: str,
    session_identity_sha256: str,
) -> str:
    """Bind an observer and the exact PHI-free digests retained in a report."""

    payload = {
        "schema": "openadapt.qualification-environment-binding/v1",
        "target_kind": target_kind,
        "observer_id": observer_id,
        "observer_contract_sha256": observer_contract_sha256,
        "application_identity_sha256": application_identity_sha256,
        "application_version_sha256": application_version_sha256,
        "environment_digest": environment_digest,
        "session_identity_sha256": session_identity_sha256,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class QualificationEnvironmentObservation(BaseModel):
    """One PHI-free application, version, and session observation."""

    model_config = ConfigDict(extra="forbid")

    target_kind: Literal["web", "windows", "macos", "linux", "rdp", "citrix"]
    application_identity: str = Field(min_length=1, max_length=320)
    application_version: str = Field(min_length=1, max_length=128)
    session_identity_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    environment_digest: str = Field(pattern=r"^[a-f0-9]{64}$")

    def binding_sha256(
        self,
        *,
        observer_id: str,
        observer_contract_sha256: str,
    ) -> str:
        """Bind the observer and every exact run-environment signal."""

        return qualification_environment_binding_sha256(
            target_kind=self.target_kind,
            observer_id=observer_id,
            observer_contract_sha256=observer_contract_sha256,
            application_identity_sha256=hashlib.sha256(
                self.application_identity.encode("utf-8")
            ).hexdigest(),
            application_version_sha256=hashlib.sha256(
                self.application_version.encode("utf-8")
            ).hexdigest(),
            environment_digest=self.environment_digest,
            session_identity_sha256=self.session_identity_sha256,
        )


@runtime_checkable
class QualificationEnvironmentObserver(Protocol):
    """Environment-owned observer for an application the backend cannot label."""

    @property
    def observer_id(self) -> str: ...

    @property
    def contract_sha256(self) -> str: ...

    def observe(
        self,
        backend: Any,
        target_kind: Literal["web", "windows", "macos", "linux", "rdp", "citrix"],
    ) -> QualificationEnvironmentObservation:
        """Read exact live identity without returning a configured expectation."""


class BackendQualificationEnvironmentObserver:
    """Adapt one backend's atomic four-signal observer to the public protocol."""

    def __init__(self, backend: Any) -> None:
        self._backend = backend
        qualified = f"{type(backend).__module__}.{type(backend).__qualname__}"
        self._observer_id = f"backend:{qualified}"[:128]

    @property
    def observer_id(self) -> str:
        return self._observer_id

    @property
    def contract_sha256(self) -> str:
        return BACKEND_ENVIRONMENT_OBSERVER_CONTRACT_SHA256

    def observe(
        self,
        backend: Any,
        target_kind: Literal["web", "windows", "macos", "linux", "rdp", "citrix"],
    ) -> QualificationEnvironmentObservation:
        if backend is not self._backend:
            raise ValueError("qualification environment backend changed")
        observer = getattr(backend, "qualification_environment_identity", None)
        if not callable(observer):
            raise ValueError("qualification backend has no atomic environment observer")
        value = cast(Callable[[], object], observer)()
        if not isinstance(value, tuple) or len(value) != 4:
            raise ValueError("qualification environment observation is incomplete")
        return QualificationEnvironmentObservation(
            target_kind=target_kind,
            application_identity=value[0],
            application_version=value[1],
            session_identity_sha256=value[2],
            environment_digest=value[3],
        )
