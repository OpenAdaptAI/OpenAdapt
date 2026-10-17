"""The per-workflow part of qualification: ``verify.yaml``.

Everything Flow cannot learn from the demonstration lives here, and nothing
else does. A workflow is qualified with::

    openadapt-flow qualify <bundle> --verify verify.yaml

The file names:

- ``app``: the system the workflow runs against (address, version, how a
  trial starts, how the test system is reset between trials);
- ``system_of_record``: how to read the written record back, independently of
  the screen. It is the deployment ``effects`` section (``kind: rest``,
  ``sql``, ``fhir``, ``graphql`` or a plugin kind), so any verifier Flow
  already has works here;
- ``record``: which record fields the save writes, from which inputs, which
  fields select the record, and which field is the duplicate (idempotency)
  key;
- ``save_step``: the irreversible step, or ``auto`` to detect it;
- ``inputs``: the test inputs for the cases;
- ``campaign``: trials per case, cases, and the default certification policy.

``${NAME}`` in any string is replaced by the environment variable ``NAME``
when the file is loaded (``${NAME:-default}`` gives a default); an unset
variable without a default is an error. Secrets are never
written in the file: the read-back's ``auth`` names environment variables.

See ``docs/QUALIFY_A_WORKFLOW.md``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from openadapt_flow.deployment import EffectsConfig
from openadapt_flow.runtime.effects import ValueExpr

SCHEMA_VERSION = 1
TargetKind = Literal["web", "windows", "macos", "linux", "rdp", "citrix"]
CASE_IDS: tuple[str, ...] = (
    "representative",
    "fault-ambiguity",
    "fault-wrong-identity",
    "fault-stale-identity",
    "fault-weak-effect",
    "fault-missing-effect",
)


class QualifyConfigError(ValueError):
    """``verify.yaml`` is missing something, or does not fit the bundle."""

    def __init__(self, problems: list[str] | str) -> None:
        self.problems = [problems] if isinstance(problems, str) else list(problems)
        super().__init__(
            "verify.yaml cannot qualify this bundle:\n  - "
            + "\n  - ".join(self.problems)
        )


_HOOK = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$")


def _check_hook(value: Optional[str], where: str) -> Optional[str]:
    if value is not None and not _HOOK.match(value):
        raise ValueError(f"{where} must look like 'package.module:function'")
    return value


class HttpReset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["GET", "POST", "PUT", "DELETE"] = "POST"
    url: str
    body: Optional[Any] = None


class ResetConfig(BaseModel):
    """How the test system returns to its baseline before every trial.

    Exactly one of ``command`` (a shell command), ``http`` (one request) or
    ``python`` (``module:function``, called with no arguments).
    """

    model_config = ConfigDict(extra="forbid")

    command: Optional[str] = None
    http: Optional[HttpReset] = None
    python: Optional[str] = None
    timeout_s: float = 600.0

    @model_validator(mode="after")
    def _one(self) -> "ResetConfig":
        if sum(x is not None for x in (self.command, self.http, self.python)) != 1:
            raise ValueError("app.reset needs exactly one of command, http, python")
        _check_hook(self.python, "app.reset.python")
        return self


class AppConfig(BaseModel):
    """The system under test and how each trial reaches the start screen."""

    model_config = ConfigDict(extra="forbid")

    target: TargetKind = "web"
    name: str = Field(min_length=1, max_length=256)
    #: The application's address. Its origin is the identity the runtime
    #: checks before every input.
    url: str
    #: Where a trial starts (default: ``url``).
    entry_url: Optional[str] = None
    #: The application version you are qualifying. The page cannot report it;
    #: it is bound into the environment digest.
    version: str = Field(min_length=1, max_length=128)
    headless: bool = True
    viewport: tuple[int, int] = (1280, 800)
    #: A Playwright storage-state file for a signed-in session (web only).
    storage_state: Optional[str] = None
    #: ``module:function`` returning ``(backend, close)``, for applications
    #: whose sign-in cannot be expressed by ``storage_state``.
    session: Optional[str] = None
    reset: Optional[ResetConfig] = None

    @field_validator("session")
    @classmethod
    def _session_hook(cls, value: Optional[str]) -> Optional[str]:
        return _check_hook(value, "app.session")

    @property
    def origin(self) -> str:
        from urllib.parse import urlsplit

        parts = urlsplit(self.url)
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
        return self.url.rstrip("/")


class SystemOfRecordConfig(BaseModel):
    """The independent read-back, as a deployment ``effects`` section.

    ``session`` (``module:function``) may supply an authenticated HTTP client
    for ``kind: rest`` when ``auth`` cannot (for example an OAuth flow).
    """

    model_config = ConfigDict(extra="forbid")

    session: Optional[str] = None
    verifier: EffectsConfig

    @model_validator(mode="before")
    @classmethod
    def _split(cls, data: Any) -> Any:
        if isinstance(data, dict) and "verifier" not in data:
            data = dict(data)
            session = data.pop("session", None)
            return {"session": session, "verifier": data}
        return data

    @model_validator(mode="after")
    def _check(self) -> "SystemOfRecordConfig":
        _check_hook(self.session, "system_of_record.session")
        if self.verifier.kind in ("none", "onscreen"):
            raise ValueError(
                "system_of_record.kind must read the system of record "
                "(rest, sql, fhir, graphql, ui_readback or a plugin), not "
                f"{self.verifier.kind!r}"
            )
        if self.session is not None and self.verifier.kind != "rest":
            raise ValueError("system_of_record.session is supported for kind: rest")
        return self


FieldValue = Union[str, ValueExpr]


class RecordConfig(BaseModel):
    """What the save writes, mapped onto the run's inputs.

    ``fields`` maps a record field to an input: a bare string is a parameter
    name, ``{literal: "MA"}`` a fixed value, ``{param: x}`` a parameter.
    """

    model_config = ConfigDict(extra="forbid")

    #: A plain name for the record, used in check descriptions.
    name: str = "record"
    #: Record fields whose values select the written record.
    match: list[str] = Field(default_factory=list)
    fields: dict[str, FieldValue] = Field(default_factory=dict)
    #: The record field that must be unique per write (idempotency key).
    duplicate_key: Optional[str] = None
    expected_count: int = Field(default=1, ge=1)
    timeout_s: float = Field(default=5.0, gt=0)

    def expr(self, field: str) -> ValueExpr:
        value = self.fields[field]
        return ValueExpr(param=value) if isinstance(value, str) else value


class InputsConfig(BaseModel):
    """Test inputs. Every case and trial uses the same inputs.

    ``source: recorded`` uses the values the demonstration typed;
    ``source: file`` reads a JSON object. ``params`` overrides single values.
    ``unique`` lists parameters that get a per-campaign suffix, so a campaign
    never collides with a record left by an earlier one.
    """

    model_config = ConfigDict(extra="forbid")

    source: Literal["recorded", "file"] = "recorded"
    file: Optional[str] = None
    params: dict[str, str] = Field(default_factory=dict)
    unique: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _file(self) -> "InputsConfig":
        if (self.source == "file") != (self.file is not None):
            raise ValueError("inputs.file is required exactly when source: file")
        return self


class CampaignConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Optional[str] = None
    trials: int = Field(default=3, ge=1, le=50)
    cases: list[str] = Field(default_factory=lambda: list(CASE_IDS))
    #: Policy ``--certify`` uses when it is given without a value.
    policy: str = "clinical-write"

    @field_validator("cases")
    @classmethod
    def _cases(cls, value: list[str]) -> list[str]:
        unknown = [case for case in value if case not in CASE_IDS]
        if unknown:
            raise ValueError(f"unknown case(s) {unknown}; known: {list(CASE_IDS)}")
        if not value:
            raise ValueError("campaign.cases is empty")
        return value


class VerifyConfig(BaseModel):
    """``verify.yaml``: the only per-workflow input to qualification."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    app: AppConfig
    system_of_record: SystemOfRecordConfig
    record: RecordConfig = Field(default_factory=RecordConfig)
    save_step: str = "auto"
    inputs: InputsConfig = Field(default_factory=InputsConfig)
    campaign: CampaignConfig = Field(default_factory=CampaignConfig)
    #: Where relative paths in the file resolve (set by the loader).
    base_dir: Optional[str] = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _record(self) -> "VerifyConfig":
        record = self.record
        missing = [f for f in record.match if f not in record.fields]
        if record.fields and missing:
            raise ValueError(f"record.match names fields not in record.fields: {missing}")
        if record.fields and not record.match:
            raise ValueError("record.match must name the fields that select the record")
        if (
            record.duplicate_key is not None
            and record.fields
            and record.duplicate_key not in record.fields
        ):
            raise ValueError("record.duplicate_key must be one of record.fields")
        return self

    def resolve_path(self, value: str) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute() and self.base_dir:
            path = Path(self.base_dir) / path
        return path


_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(value: Any, missing: list[str]) -> Any:
    if isinstance(value, str):

        def sub(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            if name in os.environ:
                return os.environ[name]
            if default is not None:
                return default
            missing.append(name)
            return match.group(0)

        return _ENV.sub(sub, value)
    if isinstance(value, list):
        return [_expand(item, missing) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item, missing) for key, item in value.items()}
    return value


def parse_verify_config(data: Any, *, base_dir: Optional[Path] = None) -> VerifyConfig:
    if not isinstance(data, dict):
        raise QualifyConfigError("verify.yaml must be a YAML mapping")
    missing: list[str] = []
    data = _expand(data, missing)
    if missing:
        raise QualifyConfigError(
            [f"environment variable {name} is not set" for name in sorted(set(missing))]
        )
    try:
        config = VerifyConfig.model_validate(data)
    except Exception as exc:  # pydantic ValidationError -> plain problems
        errors = getattr(exc, "errors", None)
        if callable(errors):
            problems = [
                f"{'.'.join(str(p) for p in item['loc']) or '(top level)'}: {item['msg']}"
                for item in errors()
            ]
            raise QualifyConfigError(problems) from exc
        raise QualifyConfigError(str(exc)) from exc
    if base_dir is not None:
        config.base_dir = str(base_dir)
    return config


def load_verify_config(path: Path | str) -> VerifyConfig:
    import yaml

    path = Path(path)
    if not path.is_file():
        raise QualifyConfigError(f"verify file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise QualifyConfigError(f"could not parse {path}: {exc}") from exc
    return parse_verify_config(data, base_dir=path.resolve().parent)


def verify_config_schema() -> dict[str, Any]:
    return VerifyConfig.model_json_schema()


__all__ = [
    "CASE_IDS",
    "AppConfig",
    "CampaignConfig",
    "InputsConfig",
    "QualifyConfigError",
    "RecordConfig",
    "ResetConfig",
    "SystemOfRecordConfig",
    "VerifyConfig",
    "load_verify_config",
    "parse_verify_config",
    "verify_config_schema",
]
