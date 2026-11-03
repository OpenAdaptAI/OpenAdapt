"""The per-workflow part of qualification: ``verify.yaml``.

Everything Flow cannot learn from the demonstration lives here, and nothing
else does. A workflow is qualified with::

    openadapt-flow qualify <bundle> --verify verify.yaml

The file is ``schema_version: openadapt.verify/v1`` (RFC-0002). Its core is
the read-back contract, the same shape ``verify-run`` reads:

- ``verifier``: how to read the written record back, independently of the
  screen. Exactly the deployment ``effects`` section (``kind: rest``, ``sql``,
  ``fhir``, ``graphql`` or a plugin kind). ``none`` and ``onscreen`` are
  refused. ``session`` (``module:function``) may add an authenticated client
  for ``kind: rest``; it is not part of the verifier's identity;
- ``effects``: the effect contract list (``effect_contract.yaml``
  ``effects:``), bound to the save step. Or ``record``, a shorthand that
  expands to that list (record fields from inputs, the selecting fields, the
  duplicate key). With neither, the effects the compiler mined from the
  demonstration are used;
- ``minimum_readback``: the check level the verifier must reach,
  ``system_of_record`` or ``separate_session``
  (:mod:`openadapt_flow.readback_ladder`). The older
  ``minimum_effect_strength`` (``independent_system_of_record``, the default,
  or ``independent_session``) means the same; give one or the other;
- ``qualification_id`` / ``environment_id``: optional opaque ids.

Qualify adds what a campaign needs on top:

- ``app``: the system the workflow runs against (address, version, how a
  trial starts, how the test system is reset between trials, and the
  deployment ``surface`` it will run on in production);
- ``save_step``: the irreversible step, or ``auto`` to detect it;
- ``inputs``: the test inputs for the cases;
- ``campaign``: trials per case, cases, and the default certification policy;
- ``business_rules``: cases for a rule the application itself enforces (an
  existing record the save must not duplicate, for example). Each names a
  ``seed`` step that creates the conflicting record after the reset, the
  run ``params`` that trigger the rule, the required outcome
  (``HALTED_BEFORE_EFFECT``) and the read-back (``no_mutation``: no new or
  changed record). Certification runs each one on every trial, on the
  certified bundle, and is refused unless all pass. Their case ids are
  ``rule-<id>``.

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
from typing import Any, Literal, Mapping, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from openadapt_flow.deployment import EffectsConfig
from openadapt_flow.lookup_capabilities import DeclaredSurface
from openadapt_flow.runtime.effects import Effect, ValueExpr
from openadapt_flow.verification import VerificationTier

SCHEMA_VERSION = "openadapt.verify/v1"
EffectStrength = Literal["independent_system_of_record", "independent_session"]
_OPAQUE = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$"
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
            raise ValueError("needs exactly one of command, http, python")
        _check_hook(self.python, "python")
        return self


class AppConfig(BaseModel):
    """The system under test and how each trial reaches the start screen."""

    model_config = ConfigDict(extra="forbid")

    target: TargetKind = "web"
    #: The deployment surface the workflow will run on in production, the
    #: same vocabulary as deployment.yaml ``backend.deployment_surface``:
    #: ``local``, ``remote_in_session``, ``remote_display`` (a remote session
    #: viewed from outside, pixels only) or ``unknown`` (read as
    #: ``remote_display``). Omitted means ``local`` for a web target. On a
    #: ``remote_display`` surface the campaign runs with structure lookups
    #: disabled and that pixels-only result decides qualification.
    surface: Optional[DeclaredSurface] = None
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
        _check_hook(self.session, "verifier.session")
        if self.verifier.kind in ("none", "onscreen"):
            raise ValueError(
                "verifier.kind must read the system of record "
                "(rest, sql, fhir, graphql, ui_readback or a plugin), not "
                f"{self.verifier.kind!r}"
            )
        if self.session is not None and self.verifier.kind != "rest":
            raise ValueError("verifier.session is supported for kind: rest")
        return self


FieldValue = Union[str, ValueExpr]

#: REST query parameters that page or order a read rather than filter it.
_REST_PAGING = {
    "_limit",
    "limit",
    "_offset",
    "offset",
    "page",
    "_page",
    "per_page",
    "page_size",
    "pagesize",
    "_count",
    "count",
    "_sort",
    "sort",
    "order",
}
#: Of those, the ones that cap how many records one response returns.
_REST_LIMITS = {"_limit", "limit", "per_page", "page_size", "pagesize", "_count"}
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SQL_BINDS = re.compile(r"(?<![:\w]):([A-Za-z_]\w*)|%\(([A-Za-z_]\w*)\)s")
_SQL_LIMIT = re.compile(r"\b(LIMIT|TOP|FETCH\s+FIRST|ROWNUM)\b", re.IGNORECASE)


def composite_key_read_problems(fields: list[str], verifier: Any) -> list[str]:
    """Why the verifier's read may miss a record that carries the key.

    A composite duplicate key counts records that were already there, so the
    read must contain every record that could carry it. That holds when the
    read is filtered by key fields only and is complete. This refuses a REST
    path or SQL query that filters on another field (a pre-existing chart
    with another email would be dropped), and a row limit without a way to
    read every page (REST ``records_total_path`` or ``records_next_path``;
    SQL has none, so no ``LIMIT``). A literal condition inside a SQL
    ``WHERE`` cannot be told apart here and is the author's responsibility;
    other verifier kinds are not checked.
    """

    from urllib.parse import parse_qsl, urlsplit

    keys = set(fields)
    problems: list[str] = []
    kind = getattr(verifier, "kind", None)
    if kind == "rest":
        parts = urlsplit(verifier.records_path)
        names = [name for name, _ in parse_qsl(parts.query, keep_blank_values=True)]
        filters = sorted(
            {n for n in names if n not in keys and n.lower() not in _REST_PAGING}
            | {p for p in _PLACEHOLDER.findall(parts.path) if p not in keys}
        )
        if filters:
            problems.append(
                "a list record.duplicate_key needs a read filtered by key fields "
                f"only; verifier.records_path also filters by {filters}, which "
                "drops an existing record with the same key"
            )
        limited = sorted(n for n in names if n.lower() in _REST_LIMITS)
        if limited and not (verifier.records_total_path or verifier.records_next_path):
            problems.append(
                "a list record.duplicate_key needs the whole read; "
                f"verifier.records_path sets {limited} without "
                "records_total_path or records_next_path"
            )
    elif kind == "sql" and verifier.sql_query:
        binds = {a or b for a, b in _SQL_BINDS.findall(verifier.sql_query)}
        binds |= set(verifier.sql_query_params)
        filters = sorted(binds - keys)
        if filters:
            problems.append(
                "a list record.duplicate_key needs a read filtered by key fields "
                f"only; verifier.sql_query also filters by {filters}, which "
                "drops an existing record with the same key"
            )
        if _SQL_LIMIT.search(verifier.sql_query):
            problems.append(
                "a list record.duplicate_key needs the whole read; "
                "verifier.sql_query limits its rows"
            )
    return problems


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
    #: The record field that must be unique per write (idempotency key), or a
    #: list of fields that are unique together, such as OpenEMR's
    #: ``[lname, fname, DOB]``. A list (even of one field) is a composite key:
    #: it also counts records that were already there, so the verifier's read
    #: must cover the key (see :func:`composite_key_read_problems`).
    duplicate_key: Optional[Union[str, list[str]]] = None
    expected_count: int = Field(default=1, ge=1)
    timeout_s: float = Field(default=5.0, gt=0)
    #: Also check that the save added exactly this record and nothing else:
    #: the records added since the snapshot taken before the save must be
    #: exactly ``expected_count`` records with these fields (an
    #: ``exact_new_set`` check). ``problems_only`` needs it for a create.
    exact_new_set: bool = False
    #: The field that tells a new record from one that was already there, for
    #: ``exact_new_set`` (an id the system of record assigns).
    identity_field: str = "id"

    @field_validator("duplicate_key")
    @classmethod
    def _duplicate_key_shape(
        cls, value: Optional[Union[str, list[str]]]
    ) -> Optional[Union[str, list[str]]]:
        if not isinstance(value, list):
            return value
        if not value or any(not name.strip() for name in value):
            raise ValueError("record.duplicate_key must name at least one field")
        if len(set(value)) != len(value):
            raise ValueError("record.duplicate_key names a field twice")
        return list(value)

    @model_validator(mode="after")
    def _composite_key_count(self) -> "RecordConfig":
        if isinstance(self.duplicate_key, list) and self.expected_count > 1:
            raise ValueError(
                "a list record.duplicate_key identifies one record; it cannot "
                f"be combined with expected_count {self.expected_count}"
            )
        return self

    @property
    def duplicate_key_fields(self) -> list[str]:
        """The duplicate key's fields (empty without one)."""
        if self.duplicate_key is None:
            return []
        if isinstance(self.duplicate_key, str):
            return [self.duplicate_key]
        return list(self.duplicate_key)

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


RULE_CASE_PREFIX = "rule-"


class BusinessRuleCase(BaseModel):
    """A rule the application enforces, which the workflow must not break.

    The fault cases inject faults into the run. This case injects nothing:
    after the reset, ``seed`` creates a record the save would conflict with
    (for a registration, an existing record for the same person), and the run
    uses ``params`` that trigger the application's rule. The workflow must
    stop before the save (``HALTED_BEFORE_EFFECT``), and the read-back must
    show no new or changed record (``no_mutation``). A bundle that clicks
    through the application's warning saves a record and fails the case.
    """

    model_config = ConfigDict(extra="forbid")

    #: The case is ``rule-<id>``.
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    description: str = Field(min_length=1, max_length=512)
    #: Creates the conflicting record, after the reset and before the run.
    seed: ResetConfig
    #: Run inputs that trigger the rule. Values given here replace the
    #: campaign's inputs; the rest are the campaign's.
    params: dict[str, str] = Field(min_length=1)
    #: The only outcome a rule case accepts: halted before the save.
    expect: Literal["HALTED_BEFORE_EFFECT"]
    #: The only read-back a rule case accepts: no new or changed record.
    readback: Literal["no_mutation"] = "no_mutation"
    #: Where the run must stop. ``empty_result_guard`` (the default): at a
    #: step that checks a dialog's empty search result the compiler mined
    #: (the opener's check or the Confirm click's guard). ``before_save``: at
    #: any step before the save, with ``halt_reason`` in the halt. Or a step
    #: id. A halt anywhere else fails the trial: the run did not stop for the
    #: application's rule.
    halt_at: str = Field(
        default="empty_result_guard",
        pattern=r"^(empty_result_guard|before_save|[A-Za-z0-9_.-]{1,128})$",
    )
    #: Text the halt reason must contain (required with ``before_save``).
    halt_reason: Optional[str] = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def _halt(self) -> "BusinessRuleCase":
        if self.halt_at == "before_save" and not self.halt_reason:
            raise ValueError("halt_at: before_save needs the halt_reason it names")
        return self

    @property
    def case_id(self) -> str:
        return f"{RULE_CASE_PREFIX}{self.id}"


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
        # ``rule-<id>`` names a business-rule case; VerifyConfig checks it.
        unknown = [
            case
            for case in value
            if case not in CASE_IDS and not case.startswith(RULE_CASE_PREFIX)
        ]
        if unknown:
            raise ValueError(f"unknown case(s) {unknown}; known: {list(CASE_IDS)}")
        if not value:
            raise ValueError("campaign.cases is empty")
        return value


class VerifyConfig(BaseModel):
    """``verify.yaml`` (``openadapt.verify/v1``): the per-workflow input."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["openadapt.verify/v1"]
    qualification_id: Optional[str] = Field(default=None, pattern=_OPAQUE)
    environment_id: Optional[str] = Field(default=None, pattern=_OPAQUE)
    minimum_effect_strength: EffectStrength = "independent_system_of_record"
    #: The check level the verifier must reach (``separate_session`` or
    #: ``system_of_record``; :mod:`openadapt_flow.readback_ladder`). Written
    #: to the qualified workflow, so runs enforce the same minimum. Replaces
    #: ``minimum_effect_strength``, which stays as an alias; give one, not both.
    minimum_readback: Optional[Literal["separate_session", "system_of_record"]] = None
    verifier: SystemOfRecordConfig
    effects: Optional[list[Effect]] = None
    app: AppConfig
    record: RecordConfig = Field(default_factory=RecordConfig)
    save_step: str = "auto"
    inputs: InputsConfig = Field(default_factory=InputsConfig)
    campaign: CampaignConfig = Field(default_factory=CampaignConfig)
    business_rules: list[BusinessRuleCase] = Field(default_factory=list)
    #: Where relative paths in the file resolve (set by the loader).
    base_dir: Optional[str] = Field(default=None, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def _migrate_hint(cls, data: Any) -> Any:
        if (
            isinstance(data, dict)
            and "schema_version" not in data
            and ("version" in data or "system_of_record" in data)
        ):
            raise ValueError(
                "this is the pre-RFC verify.yaml shape: replace `version: 1` with "
                "`schema_version: openadapt.verify/v1` and rename "
                "`system_of_record` to `verifier`"
            )
        return data

    @property
    def system_of_record(self) -> SystemOfRecordConfig:
        return self.verifier

    @model_validator(mode="before")
    @classmethod
    def _minimum_readback(cls, data: Any) -> Any:
        if not isinstance(data, dict) or data.get("minimum_readback") is None:
            return data
        if "minimum_effect_strength" in data:
            raise ValueError(
                "give minimum_readback or its older alias minimum_effect_strength, "
                "not both"
            )
        from openadapt_flow.readback_ladder import parse_minimum_readback

        data = dict(data)
        data["minimum_readback"] = parse_minimum_readback(data["minimum_readback"])
        return data

    def minimum_tier(self) -> VerificationTier:
        """The weakest verifier tier this file accepts."""

        from openadapt_flow.readback_ladder import (
            minimum_from_effect_strength,
            tier_floor,
        )

        if self.minimum_readback is not None:
            return tier_floor(self.minimum_readback)
        return minimum_from_effect_strength(self.minimum_effect_strength)

    @model_validator(mode="after")
    def _record(self) -> "VerifyConfig":
        record = self.record
        if self.effects is not None and (record.fields or record.duplicate_key):
            raise ValueError(
                "give the save's checks as either effects or record, not both"
            )
        if self.effects is not None and not self.effects:
            raise ValueError("effects is empty")
        missing = [f for f in record.match if f not in record.fields]
        if record.fields and missing:
            raise ValueError(
                f"record.match names fields not in record.fields: {missing}"
            )
        if record.fields and not record.match:
            raise ValueError("record.match must name the fields that select the record")
        if record.fields and any(
            name not in record.fields for name in record.duplicate_key_fields
        ):
            raise ValueError("record.duplicate_key must name fields in record.fields")
        if isinstance(record.duplicate_key, list):
            problems = composite_key_read_problems(
                record.duplicate_key, self.verifier.verifier
            )
            if problems:
                raise ValueError("; ".join(problems))
        return self

    @model_validator(mode="after")
    def _business_rules(self) -> "VerifyConfig":
        ids = [rule.case_id for rule in self.business_rules]
        if len(ids) != len(set(ids)):
            raise ValueError("business_rules ids must be unique")
        unknown = [
            case
            for case in self.campaign.cases
            if case.startswith(RULE_CASE_PREFIX) and case not in ids
        ]
        if unknown:
            raise ValueError(f"campaign.cases names no business rule: {unknown}")
        return self

    def all_case_ids(self) -> list[str]:
        """The six cases, then every business-rule case."""

        return [*CASE_IDS, *(rule.case_id for rule in self.business_rules)]

    def resolve_path(self, value: str) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute() and self.base_dir:
            path = Path(self.base_dir) / path
        return path


_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(
    value: Any, missing: list[str], env: Optional[Mapping[str, str]] = None
) -> Any:
    source = os.environ if env is None else env
    if isinstance(value, str):

        def sub(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            if name in source:
                return source[name]
            if default is not None:
                return default
            missing.append(name)
            return match.group(0)

        return _ENV.sub(sub, value)
    if isinstance(value, list):
        return [_expand(item, missing, env) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item, missing, env) for key, item in value.items()}
    return value


#: ``openadapt.verify/v1`` sections only ``verify-run`` reads. Qualify accepts
#: and ignores them, so one file serves both (the mirror of verify-run's
#: ``QUALIFY_ONLY_SECTIONS``).
VERIFY_RUN_ONLY_SECTIONS: tuple[str, ...] = ("frame", "admission")


def parse_verify_config(
    data: Any,
    *,
    base_dir: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> VerifyConfig:
    """Validate a parsed verify.yaml. ``env`` replaces ``os.environ`` for ``${}``."""

    if not isinstance(data, dict):
        raise QualifyConfigError("verify.yaml must be a YAML mapping")
    data = {k: v for k, v in data.items() if k not in VERIFY_RUN_ONLY_SECTIONS}
    missing: list[str] = []
    data = _expand(data, missing, env)
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


def load_verify_config(
    path: Path | str, *, env: Optional[Mapping[str, str]] = None
) -> VerifyConfig:
    import yaml

    path = Path(path)
    if not path.is_file():
        raise QualifyConfigError(f"verify file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise QualifyConfigError(f"could not parse {path}: {exc}") from exc
    return parse_verify_config(data, base_dir=path.resolve().parent, env=env)


def verify_config_schema() -> dict[str, Any]:
    return VerifyConfig.model_json_schema()


__all__ = [
    "CASE_IDS",
    "RULE_CASE_PREFIX",
    "AppConfig",
    "BusinessRuleCase",
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
