"""Typed, local qualification cases for business judgment.

The case set records what a reviewed qualification contract permits.  It does
not learn a rule from an example and it does not place free-form reasons on an
execution path.  A case can retain human authority indefinitely.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import Enum
from pathlib import PureWindowsPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256_RE = r"^[a-f0-9]{64}$"


class JudgmentFactType(str, Enum):
    BOOLEAN = "boolean"
    INTEGER = "integer"
    NUMBER = "number"
    STRING = "string"
    ENUM = "enum"


class JudgmentFactFieldV1(BaseModel):
    """One typed local fact. Values have no implied policy meaning."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: JudgmentFactType
    allowed_values: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _enum_is_closed(self) -> "JudgmentFactFieldV1":
        if self.type is JudgmentFactType.ENUM:
            if len(self.allowed_values) < 2 or len(set(self.allowed_values)) != len(
                self.allowed_values
            ):
                raise ValueError("an enum fact requires at least two unique values")
        elif self.allowed_values:
            raise ValueError("allowed_values applies only to enum facts")
        return self


class JudgmentFactSchemaV1(BaseModel):
    """The closed fact vocabulary for one decision state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.judgment-fact-schema/v1"] = (
        "openadapt.judgment-fact-schema/v1"
    )
    fields: dict[str, JudgmentFactFieldV1] = Field(min_length=1)

    @field_validator("fields")
    @classmethod
    def _names_are_safe(cls, value: dict[str, JudgmentFactFieldV1]):
        if any(_ID_RE.fullmatch(name) is None for name in value):
            raise ValueError("judgment fact names must be stable identifiers")
        return dict(sorted(value.items()))

    def contract_sha256(self) -> str:
        return _sha256(self.model_dump(mode="json"))


class JudgmentFactSchemaBindingV1(BaseModel):
    """One fact schema bound to one exact graph decision node."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    graph_id: str = Field(
        pattern=r"^(?:__program__|[A-Za-z0-9][A-Za-z0-9._:-]{0,127})$"
    )
    state_id: str = Field(pattern=_ID_RE.pattern)
    fact_schema: JudgmentFactSchemaV1


class LocalEvidenceRefV1(BaseModel):
    """A local artifact reference. No remote URL or artifact body is allowed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    relative_path: str = Field(min_length=1, max_length=512)
    sha256: str = Field(pattern=_SHA256_RE)
    kind: Literal["frame", "recording", "report", "document", "system_read"]

    @field_validator("relative_path")
    @classmethod
    def _local_relative_path(cls, value: str) -> str:
        windows_path = PureWindowsPath(value)
        if (
            value.startswith("/")
            or "://" in value
            or "\\" in value
            or windows_path.drive
            or windows_path.is_absolute()
        ):
            raise ValueError("judgment evidence must use a local relative path")
        parts = value.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError("judgment evidence path cannot escape its local root")
        return value


class JudgmentCaseProvenanceV1(BaseModel):
    """The bounded origin of a local case, without an unbounded rationale."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal["demonstration", "counterfactual", "policy_review", "fault"]
    source_ref_sha256: str = Field(pattern=_SHA256_RE)
    reviewer_role: str = Field(pattern=_ID_RE.pattern)
    reviewer_principal_ref_sha256: str = Field(pattern=_SHA256_RE)


class JudgmentDisposition(str, Enum):
    AUTOMATIC_RULE = "automatic_rule"
    HUMAN_NODE = "human_node"
    MORE_EVIDENCE_REQUIRED = "more_evidence_required"


class JudgmentDecisionBindingV1(BaseModel):
    """Exact executable decision revision that a local case describes."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    graph_id: str = Field(
        pattern=r"^(?:__program__|[A-Za-z0-9][A-Za-z0-9._:-]{0,127})$"
    )
    state_id: str = Field(pattern=_ID_RE)
    workflow_contract_sha256: str = Field(pattern=_SHA256_RE)
    decision_contract_sha256: str = Field(pattern=_SHA256_RE)


class JudgmentCaseV1(BaseModel):
    """One reviewed example or counterfactual for a typed decision state.

    ``automatic_rule`` names a reviewed rule and a finite compiled option. It
    never embeds an expression, prompt, natural-language reason, or generated
    executable program. The runtime continues to use ``BusinessDecisionSpec``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.judgment-case/v1"] = "openadapt.judgment-case/v1"
    id: str = Field(pattern=_ID_RE)
    decision: JudgmentDecisionBindingV1
    fact_schema_sha256: str = Field(pattern=_SHA256_RE)
    facts: dict[str, bool | int | float | str]
    local_evidence: tuple[LocalEvidenceRefV1, ...] = Field(min_length=1)
    review_note_ref: LocalEvidenceRefV1 | None = None
    provenance: JudgmentCaseProvenanceV1
    disposition: JudgmentDisposition
    reviewed_rule_id: str | None = Field(default=None, pattern=_ID_RE)
    option_id: str | None = Field(default=None, pattern=_ID_RE)
    contrast_case_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _closed_disposition(self) -> "JudgmentCaseV1":
        if not self.facts:
            raise ValueError("a judgment case requires typed facts")
        if any(_ID_RE.fullmatch(name) is None for name in self.facts):
            raise ValueError("judgment case facts must use stable identifiers")
        if len({item.relative_path for item in self.local_evidence}) != len(
            self.local_evidence
        ):
            raise ValueError("judgment evidence paths must be unique")
        if self.review_note_ref is not None and self.review_note_ref.kind != "document":
            raise ValueError(
                "a judgment review note must be a local document reference"
            )
        if self.id in self.contrast_case_ids or len(set(self.contrast_case_ids)) != len(
            self.contrast_case_ids
        ):
            raise ValueError(
                "judgment contrasts must be unique and cannot self-reference"
            )
        automatic = self.disposition is JudgmentDisposition.AUTOMATIC_RULE
        if automatic and (self.reviewed_rule_id is None or self.option_id is None):
            raise ValueError("automatic_rule requires a reviewed rule id and option id")
        if not automatic and (
            self.reviewed_rule_id is not None or self.option_id is not None
        ):
            raise ValueError(
                "human_node and more_evidence_required cannot contain rule or option"
            )
        return self

    def facts_sha256(self) -> str:
        return _sha256(self.facts)


class JudgmentCaseSetV1(BaseModel):
    """Scriptable local file format for reviewed schemas and cases."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.judgment-case-set/v1"] = (
        "openadapt.judgment-case-set/v1"
    )
    schemas: tuple[JudgmentFactSchemaBindingV1, ...] = ()
    cases: tuple[JudgmentCaseV1, ...] = ()


class JudgmentCaseFindingCode(str, Enum):
    BINDING_MISMATCH = "binding_mismatch"
    FACT_SCHEMA_MISMATCH = "fact_schema_mismatch"
    CONFLICT = "conflict"
    MISSING_CONTRAST_COVERAGE = "missing_contrast_coverage"
    MORE_EVIDENCE_REQUIRED = "more_evidence_required"
    RETAINED_HUMAN_AUTHORITY = "retained_human_authority"


class JudgmentCaseFindingV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: JudgmentCaseFindingCode
    case_id: str | None = None
    message: str


class JudgmentCaseQualificationReportV1(BaseModel):
    """Deterministic result of local judgment-case validation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.judgment-case-report/v1"] = (
        "openadapt.judgment-case-report/v1"
    )
    workflow_contract_sha256: str = Field(pattern=_SHA256_RE)
    passed: bool
    case_count: int
    automatic_case_count: int
    retained_human_authority_count: int
    findings: tuple[JudgmentCaseFindingV1, ...] = ()


def evaluate_judgment_cases(
    *,
    workflow_contract_sha256: str,
    decisions: dict[tuple[str, str], tuple[str, tuple[str, ...]]],
    fact_schemas: dict[tuple[str, str], JudgmentFactSchemaV1],
    cases: list[JudgmentCaseV1],
) -> JudgmentCaseQualificationReportV1:
    """Validate reviewed cases without inferring a policy from them.

    A reviewed automatic rule needs a reciprocal counterfactual pair. This is
    only a coverage requirement. It does not synthesize a predicate or convert
    examples into a program. A human-node case is reported as retained human
    authority, not as a certification refusal.
    """

    findings: list[JudgmentCaseFindingV1] = []
    by_id = {case.id: case for case in cases}
    automatic = 0
    human = 0
    automatic_key: dict[tuple[str, str, str, str], str] = {}
    for case in cases:
        key = (case.decision.graph_id, case.decision.state_id)
        executable = decisions.get(key)
        schema = fact_schemas.get(key)
        if (
            executable is None
            or case.decision.workflow_contract_sha256 != workflow_contract_sha256
            or case.decision.decision_contract_sha256 != executable[0]
        ):
            findings.append(
                JudgmentCaseFindingV1(
                    code=JudgmentCaseFindingCode.BINDING_MISMATCH,
                    case_id=case.id,
                    message="case is not bound to the current workflow and decision contract",
                )
            )
            continue
        if schema is None or case.fact_schema_sha256 != schema.contract_sha256():
            findings.append(
                JudgmentCaseFindingV1(
                    code=JudgmentCaseFindingCode.FACT_SCHEMA_MISMATCH,
                    case_id=case.id,
                    message="case facts are not bound to the current reviewed fact schema",
                )
            )
            continue
        error = _fact_value_error(schema, case.facts)
        if error:
            findings.append(
                JudgmentCaseFindingV1(
                    code=JudgmentCaseFindingCode.FACT_SCHEMA_MISMATCH,
                    case_id=case.id,
                    message=error,
                )
            )
            continue
        if case.disposition is JudgmentDisposition.HUMAN_NODE:
            human += 1
            findings.append(
                JudgmentCaseFindingV1(
                    code=JudgmentCaseFindingCode.RETAINED_HUMAN_AUTHORITY,
                    case_id=case.id,
                    message="this case retains a human decision node",
                )
            )
        elif case.disposition is JudgmentDisposition.MORE_EVIDENCE_REQUIRED:
            findings.append(
                JudgmentCaseFindingV1(
                    code=JudgmentCaseFindingCode.MORE_EVIDENCE_REQUIRED,
                    case_id=case.id,
                    message="this case requires more local evidence before an answer can proceed",
                )
            )
        elif case.disposition is JudgmentDisposition.AUTOMATIC_RULE:
            automatic += 1
            assert case.reviewed_rule_id is not None and case.option_id is not None
            if case.option_id not in executable[1]:
                findings.append(
                    JudgmentCaseFindingV1(
                        code=JudgmentCaseFindingCode.BINDING_MISMATCH,
                        case_id=case.id,
                        message="automatic case names an option outside the compiled decision",
                    )
                )
                continue
            conflict_key = (*key, case.reviewed_rule_id, case.facts_sha256())
            prior = automatic_key.get(conflict_key)
            if prior is not None and by_id[prior].option_id != case.option_id:
                findings.append(
                    JudgmentCaseFindingV1(
                        code=JudgmentCaseFindingCode.CONFLICT,
                        case_id=case.id,
                        message="identical facts select different options for one reviewed rule",
                    )
                )
            automatic_key[conflict_key] = case.id
            contrast_ok = any(
                other_id in by_id
                and case.id in by_id[other_id].contrast_case_ids
                and by_id[other_id].decision == case.decision
                and by_id[other_id].facts_sha256() != case.facts_sha256()
                for other_id in case.contrast_case_ids
            )
            if not contrast_ok:
                findings.append(
                    JudgmentCaseFindingV1(
                        code=JudgmentCaseFindingCode.MISSING_CONTRAST_COVERAGE,
                        case_id=case.id,
                        message="automatic_rule needs a reciprocal counterfactual with different facts",
                    )
                )

    blocking = {
        JudgmentCaseFindingCode.BINDING_MISMATCH,
        JudgmentCaseFindingCode.FACT_SCHEMA_MISMATCH,
        JudgmentCaseFindingCode.CONFLICT,
        JudgmentCaseFindingCode.MISSING_CONTRAST_COVERAGE,
        JudgmentCaseFindingCode.MORE_EVIDENCE_REQUIRED,
    }
    return JudgmentCaseQualificationReportV1(
        workflow_contract_sha256=workflow_contract_sha256,
        passed=not any(item.code in blocking for item in findings),
        case_count=len(cases),
        automatic_case_count=automatic,
        retained_human_authority_count=human,
        findings=tuple(findings),
    )


def _fact_value_error(
    schema: JudgmentFactSchemaV1, facts: dict[str, Any]
) -> str | None:
    if set(facts) != set(schema.fields):
        return "case facts must contain exactly the reviewed fact schema fields"
    for name, field in schema.fields.items():
        value = facts[name]
        valid = (
            (field.type is JudgmentFactType.BOOLEAN and type(value) is bool)
            or (field.type is JudgmentFactType.INTEGER and type(value) is int)
            or (field.type is JudgmentFactType.NUMBER and type(value) in {int, float})
            or (field.type is JudgmentFactType.STRING and type(value) is str)
            or (field.type is JudgmentFactType.ENUM and value in field.allowed_values)
        )
        if not valid:
            return f"fact {name!r} does not match its reviewed type"
    return None


def _sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()
