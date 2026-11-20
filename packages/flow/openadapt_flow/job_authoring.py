"""Digest-covered job attribution, never approval or execution authority."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Literal, TypeVar, cast
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializationInfo,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_serializer,
    model_validator,
)

JOB_AUTHORING_SCHEMA = "openadapt.job-authoring-binding/v1"
JOB_AUTHORING_BUNDLE_SCHEMA = 3
SUPPORTED_BUNDLE_SCHEMAS = frozenset({1, 2, 3})
PRIVATE_JOB_INTENT_DOMAIN = b"openadapt.private-job-intent/v1\0"
MAX_PRIVATE_JOB_INTENT_BYTES = 8192
_BINDING_FIELDS = frozenset(
    {"binding_schema", "job_id", "org_id", "owner_principal_id", "intent_sha256"}
)


_JSON_PARSE_CHECKED: ContextVar[bool] = ContextVar(
    "authoring_json_parse", default=False
)
_JSON_PROJECTION_CHECKED: ContextVar[bool] = ContextVar(
    "authoring_json_projection", default=False
)
_ParsedModel = TypeVar("_ParsedModel", bound=BaseModel)


class JobAuthoringError(ValueError):
    """Closed attribution/schema refusal; messages contain no private values."""


def validate_authoring_model_json(
    model: type[_ParsedModel],
    data: str | bytes | bytearray,
    *,
    strict_profile: bool = False,
    **kwargs: Any,
) -> _ParsedModel:
    """Check original bytes before entering Pydantic's already-decoded schema.

    TypeAdapter/core JSON entrypoints cannot expose duplicate keys or numeric
    tokens to model validators. Refuse those entrypoints for the new format;
    supported model methods enter this bounded scope only after checking bytes.
    This scope is an encoding check, never verified-load or admission authority.
    """
    # Bind the real BaseModel method to this class, preserving JSON-specific
    # strict enum/date semantics. Never accept an arbitrary scoped callback.
    parse_model = getattr(BaseModel.model_validate_json, "__func__")

    def validate() -> _ParsedModel:
        return cast(_ParsedModel, parse_model(model, data, **kwargs))

    try:
        parse_authoring_json(data, strict=strict_profile)
    except JobAuthoringError:
        raise
    except (ValueError, TypeError):
        if strict_profile:
            raise
        # Preserve the original Pydantic parser's legacy error semantics.
        return validate()
    token = _JSON_PARSE_CHECKED.set(True)
    try:
        return validate()
    finally:
        _JSON_PARSE_CHECKED.reset(token)


def require_checked_json_parser(info: ValidationInfo) -> None:
    if info.mode == "json" and not _JSON_PARSE_CHECKED.get():
        raise JobAuthoringError(
            "job-authoring JSON requires the supported model parser"
        )


def dump_authoring_model(model: BaseModel, **kwargs: Any) -> dict[str, Any]:
    """Produce checked Python values for the shared canonical JSON writer."""
    token = _JSON_PROJECTION_CHECKED.set(True)
    try:
        return BaseModel.model_dump(model, **kwargs)
    finally:
        _JSON_PROJECTION_CHECKED.reset(token)


def require_checked_json_projection(info: SerializationInfo) -> None:
    if info.mode == "json" and not _JSON_PROJECTION_CHECKED.get():
        raise JobAuthoringError("schema 3 JSON requires the supported model writer")


class JobAuthoringBinding(BaseModel):
    """Immutable identity/intent only; all five wire fields are required."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, hide_input_in_errors=True
    )

    binding_schema: Literal["openadapt.job-authoring-binding/v1"] = Field(
        alias="schema"
    )
    job_id: str
    org_id: str
    owner_principal_id: str
    intent_sha256: str

    @classmethod
    def model_validate_json(
        cls, json_data: str | bytes | bytearray, **kwargs: Any
    ) -> "JobAuthoringBinding":
        return validate_authoring_model_json(
            cls, json_data, strict_profile=True, **kwargs
        )

    @model_validator(mode="before")
    @classmethod
    def _checked_json_source(cls, value: Any, info: ValidationInfo) -> Any:
        require_checked_json_parser(info)
        return value

    @field_validator("job_id", "org_id", "owner_principal_id")
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        try:
            canonical = str(UUID(value))
        except (ValueError, AttributeError):
            raise ValueError(
                "job binding identifier must be a canonical UUID"
            ) from None
        if value != canonical:
            raise ValueError("job binding identifier must be a canonical UUID")
        return value

    @field_validator("intent_sha256")
    @classmethod
    def _bare_digest(cls, value: str) -> str:
        if re.fullmatch(r"[a-f0-9]{64}", value) is None:
            raise ValueError("job binding intent digest must be bare lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def _validate_actual_fields(self) -> "JobAuthoringBinding":
        self._checked_wire()
        return self

    def _checked_wire(self) -> dict[str, str]:
        # model_copy/model_construct do not validate updates. Check before any
        # serialization, rather than silently losing an extra or invalid field.
        if set(self.__dict__) != _BINDING_FIELDS:
            raise JobAuthoringError("job binding model is malformed")
        wire = {
            "schema": self.binding_schema,
            "job_id": self.job_id,
            "org_id": self.org_id,
            "owner_principal_id": self.owner_principal_id,
            "intent_sha256": self.intent_sha256,
        }
        if (
            any(type(value) is not str for value in wire.values())
            or self.binding_schema != JOB_AUTHORING_SCHEMA
        ):
            raise JobAuthoringError("job binding model is malformed")
        for name in ("job_id", "org_id", "owner_principal_id"):
            self._canonical_uuid(wire[name])
        self._bare_digest(self.intent_sha256)
        return wire

    @model_serializer(mode="plain")
    def _wire(self) -> dict[str, str]:
        return self._checked_wire()


def private_job_intent_sha256(intent: str | bytes) -> str:
    """Hash exact valid UTF-8 intent bytes without trimming or normalization."""
    try:
        raw = intent.encode("utf-8") if isinstance(intent, str) else intent
        if not isinstance(raw, bytes) or len(raw) > MAX_PRIVATE_JOB_INTENT_BYTES:
            raise JobAuthoringError("private job intent bytes are invalid")
        raw.decode("utf-8")
    except UnicodeError:
        raise JobAuthoringError("private job intent bytes are invalid") from None
    return hashlib.sha256(PRIVATE_JOB_INTENT_DOMAIN + raw).hexdigest()


def checked_bundle_schema(value: object) -> int:
    if type(value) is not int or value not in SUPPORTED_BUNDLE_SCHEMAS:
        raise JobAuthoringError("unsupported bundle schema version")
    return value


def parse_job_authoring(value: object) -> JobAuthoringBinding:
    try:
        wire = value.model_dump() if isinstance(value, JobAuthoringBinding) else value
        return JobAuthoringBinding.model_validate(wire)
    except (ValidationError, ValueError, TypeError, AttributeError):
        raise JobAuthoringError("job authoring binding is invalid") from None


def checked_job_authoring(
    schema_version: object,
    value: object,
    *,
    manifest_version: object = None,
) -> JobAuthoringBinding | None:
    version = checked_bundle_schema(schema_version)
    if manifest_version is not None:
        manifest_schema = checked_bundle_schema(manifest_version)
        if (
            JOB_AUTHORING_BUNDLE_SCHEMA in (version, manifest_schema)
            and version != manifest_schema
        ):
            raise JobAuthoringError("job binding workflow and manifest schemas differ")
    if version != JOB_AUTHORING_BUNDLE_SCHEMA:
        if value is not None:
            raise JobAuthoringError("job authoring binding requires bundle schema 3")
        return None
    if value is None:
        raise JobAuthoringError("bundle schema 3 requires a job authoring binding")
    return parse_job_authoring(value)


def _check_schema3_values(value: object) -> None:
    """Schema 3 has finite numbers, valid Unicode and no cyclic JSON values."""
    pending: list[tuple[object, bool]] = [(value, False)]
    active: set[int] = set()
    while pending:
        current, leaving = pending.pop()
        if isinstance(current, str):
            try:
                current.encode("utf-8")
            except UnicodeError:
                raise JobAuthoringError(
                    "bundle schema 3 contains invalid Unicode"
                ) from None
        if isinstance(current, float) and not math.isfinite(current):
            raise JobAuthoringError("bundle schema 3 contains a non-finite number")
        if isinstance(current, BaseModel):
            children = [
                getattr(current, name)
                for name in type(current).model_fields
                if hasattr(current, name)
            ]
        elif isinstance(current, Mapping):
            if any(not isinstance(key, str) for key in current):
                raise JobAuthoringError(
                    "bundle schema 3 contains a non-string object key"
                )
            children = [*current.keys(), *current.values()]
        elif isinstance(current, (list, tuple)):
            children = list(current)
        else:
            continue
        identity = id(current)
        if leaving:
            active.remove(identity)
            continue
        if identity in active:
            raise JobAuthoringError("bundle schema 3 contains a cyclic value")
        active.add(identity)
        pending.append((current, True))
        pending.extend((child, False) for child in children)


def dump_schema3_json(model: BaseModel, **kwargs: Any) -> str:
    """Use the digest's Python number encoding for schema-3 wire bytes."""
    _check_schema3_values(model)
    indent = kwargs.pop("indent", None)
    ensure_ascii = kwargs.pop("ensure_ascii", False)
    rendered = model.model_dump(mode="json", **kwargs)
    return json.dumps(
        rendered,
        indent=indent,
        ensure_ascii=ensure_ascii,
        allow_nan=False,
        separators=(",", ":") if indent is None else (",", ": "),
    )


def parse_authoring_json(data: str | bytes | bytearray, *, strict: bool = False) -> Any:
    """Read the schema-3 numeric/object format without changing legacy JSON.

    Python's typed digest distinguishes integral floats and uses its numeric
    representation, e.g. 1e-07 rather than Pydantic's 1e-7. Schema 3 writes and
    checks that representation so lossless independent consumers agree.
    """
    duplicate = False
    noncanonical_number = False
    root_pairs: list[tuple[str, Any]] = []

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        nonlocal duplicate, root_pairs
        duplicate |= len({key for key, _ in pairs}) != len(pairs)
        root_pairs = pairs
        return dict(pairs)

    def integer(token: str) -> int:
        nonlocal noncanonical_number
        value = int(token)
        noncanonical_number |= str(value) != token
        return value

    def floating(token: str) -> float:
        nonlocal noncanonical_number
        value = float(token)
        noncanonical_number |= not math.isfinite(value) or json.dumps(value) != token
        return value

    parsed = json.loads(
        data, object_pairs_hook=object_pairs, parse_int=integer, parse_float=floating
    )
    declared = strict or (
        isinstance(parsed, dict)
        and any(
            (key == "schema_version" and type(value) is int and value == 3)
            or (key == "job_authoring" and value is not None)
            for key, value in root_pairs
        )
    )
    if declared:
        if duplicate or noncanonical_number:
            raise JobAuthoringError("job-authoring JSON is not canonical")
        if isinstance(data, (bytes, bytearray)):
            try:
                text = bytes(data).decode("utf-8")
            except UnicodeError:
                raise JobAuthoringError("job-authoring JSON must be UTF-8") from None
        else:
            text = data
        if text.startswith("\ufeff") or "\x00" in text:
            raise JobAuthoringError(
                "job-authoring JSON must be UTF-8 without a byte-order mark"
            )
        _check_schema3_values(parsed)
    return parsed


def check_workflow_input(
    raw: object,
    *,
    default_version: int,
    allowed_fields: Iterable[str] | None = None,
) -> None:
    """Check before a parser can drop unknown attribution or coerce a version."""
    if not isinstance(raw, Mapping):
        return
    version = checked_bundle_schema(raw.get("schema_version", default_version))
    if version == JOB_AUTHORING_BUNDLE_SCHEMA:
        _check_schema3_values(raw)
    if version == JOB_AUTHORING_BUNDLE_SCHEMA and allowed_fields is not None:
        if set(raw) - set(allowed_fields):
            raise JobAuthoringError("bundle schema 3 contains unknown workflow fields")
    manifest = raw.get("manifest")
    if (
        version == JOB_AUTHORING_BUNDLE_SCHEMA
        and manifest is not None
        and not isinstance(manifest, Mapping)
    ):
        raise JobAuthoringError("job binding manifest is invalid")
    checked_job_authoring(
        version,
        raw.get("job_authoring"),
        manifest_version=manifest.get("schema_version", 2)
        if isinstance(manifest, Mapping)
        else None,
    )


def match_job_authoring(actual: object, expected: object) -> JobAuthoringBinding:
    """Exact matching only; the caller must obtain expected identity authoritatively."""
    binding = parse_job_authoring(actual)
    if binding.model_dump() != parse_job_authoring(expected).model_dump():
        raise JobAuthoringError(
            "job authoring binding does not match expected identity"
        )
    return binding


def require_supported_workflow(
    workflow: Any,
    *,
    for_execution: bool = False,
    bundle_dir: Path | str | None = None,
    expected_binding: object = None,
) -> JobAuthoringBinding | None:
    """Validate actual model/bytes, including preconstructed or mutated inputs.

    Schemas 1/2 retain their normal authority rules. Schema 3 execution must
    originate in this supported consumer's actual integrity-verified load,
    never a copied manifest, old direct model or claimed capability string.
    This check supplies no qualification, admission or owner permission.
    """
    version = checked_bundle_schema(getattr(workflow, "schema_version", None))
    manifest = getattr(workflow, "manifest", None)
    if version == JOB_AUTHORING_BUNDLE_SCHEMA:
        _check_schema3_values(workflow)
    binding = checked_job_authoring(
        version,
        getattr(workflow, "job_authoring", None),
        manifest_version=getattr(manifest, "schema_version", None),
    )
    if expected_binding is not None:
        binding = match_job_authoring(binding, expected_binding)
    if version != JOB_AUTHORING_BUNDLE_SCHEMA or not for_execution:
        return binding
    from openadapt_flow import bundle_validation as bundles
    from openadapt_flow.ir import Workflow

    if (
        not isinstance(workflow, Workflow)
        or "job_authoring" not in type(workflow).model_fields
        or not isinstance(workflow.job_authoring, JobAuthoringBinding)
        or workflow._unverified_load
        or not bundles.has_verified_load_capability(workflow)
        or workflow._loaded_seal is None
        or manifest is None
        or not manifest.content_digest
        or bundle_dir is None
    ):
        raise JobAuthoringError(
            "schema 3 execution requires a supported verified bundle load"
        )
    source = Path(workflow._loaded_seal.source_bundle_dir)
    if source.resolve() != Path(bundle_dir).resolve():
        raise JobAuthoringError("job binding bundle source does not match execution")
    bundles.verify_integrity(
        workflow,
        source,
        manifest,
        decrypted_assets=workflow.decrypted_templates() if manifest.encrypted else None,
    )
    return binding


def verified_job_authoring(
    workflow: Any,
    bundle_dir: Path | str,
    *,
    expected_binding: object = None,
) -> JobAuthoringBinding:
    """Read attribution from the verified loaded snapshot, not a fresh disk load.

    A later export/registration must verify its exact artifact against this
    snapshot's content identity. This check supplies no admission authority.
    """
    binding = require_supported_workflow(
        workflow,
        for_execution=True,
        bundle_dir=bundle_dir,
        expected_binding=expected_binding,
    )
    if binding is None:
        raise JobAuthoringError("verified program has no job authoring binding")
    return binding
