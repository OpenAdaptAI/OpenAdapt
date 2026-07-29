"""Protected local handoff for one verified managed dispatch.

The runner must not put a Cloud authorization in argv.  It writes this small
mode-0600 envelope in the customer-controlled run directory, and the child
CLI reopens it with no-follow and ownership checks before it admits a run.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from openadapt_flow.runner.protocol import dispatch_binding_sha256
from openadapt_flow.runtime.authorization import GovernedRunAuthorization

if TYPE_CHECKING:  # pragma: no cover
    from openadapt_flow.runner.verify import VerifiedDispatch


class ManagedDispatchEnvelopeError(ValueError):
    """A managed dispatch envelope is unsafe or does not bind exactly."""


_BINDING_FACTORY = object()


class ManagedDispatchBinding:
    """Opaque capability produced only after strict envelope validation."""

    __slots__ = ("run_id", "authorization", "binding_sha256")

    def __setattr__(self, name: str, value: object) -> None:
        if hasattr(self, name):
            raise AttributeError("managed dispatch binding is immutable")
        object.__setattr__(self, name, value)

    def __init__(
        self,
        factory: object,
        *,
        run_id: str,
        authorization: GovernedRunAuthorization,
        binding_sha256: str,
    ) -> None:
        if factory is not _BINDING_FACTORY:
            raise ManagedDispatchEnvelopeError("managed dispatch binding is internal")
        if binding_sha256 != dispatch_binding_sha256(run_id, authorization):
            raise ManagedDispatchEnvelopeError(
                "managed dispatch binding does not match its authorization"
            )
        self.run_id = run_id
        self.authorization = authorization
        self.binding_sha256 = binding_sha256


class ManagedDispatchEnvelope(BaseModel):
    """The exact Cloud authority that one managed child process consumes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1] = 1
    run_id: str = Field(
        pattern=(
            "^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
        )
    )
    bundle_content_digest: str = Field(pattern="^[a-f0-9]{64}$")
    runtime_inputs_digest: str = Field(pattern="^[a-f0-9]{64}$")
    authorization: GovernedRunAuthorization
    dispatch_binding_sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")

    @model_validator(mode="after")
    def _binding_commits_to_exact_dispatch(self) -> "ManagedDispatchEnvelope":
        if self.dispatch_binding_sha256 != dispatch_binding_sha256(
            self.run_id, self.authorization
        ):
            raise ValueError("managed dispatch binding does not match authorization")
        return self

    def exact_authorization(self) -> GovernedRunAuthorization:
        if (
            self.authorization.bundle_content_digest != self.bundle_content_digest
            or self.authorization.runtime_inputs_digest != self.runtime_inputs_digest
        ):
            raise ManagedDispatchEnvelopeError(
                "managed dispatch envelope does not bind its authorization"
            )
        return self.authorization


def write_managed_dispatch_envelope(path: Path, verified: "VerifiedDispatch") -> Path:
    """Write one already verified dispatch without following a path."""

    run_id = verified.payload.run_id
    authorization = verified.payload.authorization
    envelope = ManagedDispatchEnvelope(
        run_id=run_id,
        bundle_content_digest=authorization.bundle_content_digest,
        runtime_inputs_digest=authorization.runtime_inputs_digest,
        authorization=authorization,
        dispatch_binding_sha256=verified.payload.dispatch_binding_sha256,
    )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ManagedDispatchEnvelopeError(
            "could not create a private managed dispatch envelope"
        ) from exc
    try:
        raw = json.dumps(
            envelope.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        written = 0
        while written < len(raw):
            count = os.write(descriptor, raw[written:])
            if count <= 0:
                raise ManagedDispatchEnvelopeError(
                    "could not write the managed dispatch envelope"
                )
            written += count
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return path


def _read_managed_dispatch_envelope(path: Path) -> ManagedDispatchEnvelope:
    """Read only a regular, private file owned by this effective user."""

    path = Path(path)
    lstat_before = None
    if os.name == "nt":
        try:
            lstat_before = os.lstat(path)
        except OSError as exc:
            raise ManagedDispatchEnvelopeError(
                "managed dispatch envelope could not be inspected safely"
            ) from exc
        if stat.S_ISLNK(lstat_before.st_mode):
            raise ManagedDispatchEnvelopeError(
                "managed dispatch envelope must not be a link"
            )
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ManagedDispatchEnvelopeError(
            "managed dispatch envelope could not be opened safely"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        windows_path_changed = False
        if lstat_before is not None:
            try:
                lstat_after = os.lstat(path)
                windows_path_changed = (
                    stat.S_ISLNK(lstat_after.st_mode)
                    or lstat_after.st_ino != lstat_before.st_ino
                    or lstat_after.st_dev != lstat_before.st_dev
                )
            except OSError:
                windows_path_changed = True
        unsafe_permissions = os.name != "nt" and (
            not hasattr(os, "geteuid")
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        )
        if (
            not stat.S_ISREG(metadata.st_mode)
            or unsafe_permissions
            or windows_path_changed
            or metadata.st_size > 64 * 1024
        ):
            raise ManagedDispatchEnvelopeError(
                "managed dispatch envelope is not a private regular file"
            )
        raw = os.read(descriptor, 64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            raise ManagedDispatchEnvelopeError("managed dispatch envelope is too large")
    finally:
        os.close(descriptor)
    try:
        raw_object = json.loads(raw.decode("utf-8"))
        if (
            not isinstance(raw_object, dict)
            or not isinstance(raw_object.get("authorization"), dict)
            or set(raw_object["authorization"]).difference(
                GovernedRunAuthorization.model_fields
            )
        ):
            raise ManagedDispatchEnvelopeError(
                "managed dispatch envelope has unknown authorization fields"
            )
        envelope = ManagedDispatchEnvelope.model_validate(raw_object)
        envelope.exact_authorization()
        # Pydantic models in the shared authorization tree retain compatibility
        # with older callers and may ignore unknown nested keys.  This private
        # managed handoff is stricter: it accepts only a byte-for-byte schema
        # shape after JSON normalization, at every nested level.
        if envelope.model_dump(mode="json") != raw_object:
            raise ManagedDispatchEnvelopeError(
                "managed dispatch envelope has unknown nested fields"
            )
        return envelope
    except (ValidationError, ValueError) as exc:
        raise ManagedDispatchEnvelopeError(
            "managed dispatch envelope has an invalid exact binding"
        ) from exc


def read_managed_dispatch_envelope(path: Path) -> ManagedDispatchBinding:
    """Strictly load the private file and mint its internal capability."""

    envelope = _read_managed_dispatch_envelope(path)
    return ManagedDispatchBinding(
        _BINDING_FACTORY,
        run_id=envelope.run_id,
        authorization=envelope.exact_authorization(),
        binding_sha256=envelope.dispatch_binding_sha256,
    )
