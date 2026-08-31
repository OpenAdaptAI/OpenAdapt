"""ProcessContract parent artifact over independently admitted capabilities.

A ProcessContract is not compose. Compose (`composition.json`) sequences
compiled recordings and copies those children. V0 sequences Flow capabilities
that each present a valid `QualificationAdmissionEnvelope`. V1 adds admitted
code, typed human tasks, and verified artifact edges without creating a second
parent runtime.

Schema names: ``openadapt.process-contract/v0`` and
``openadapt.process-contract/v1``. Filename: process-contract.json.

This module owns the on-disk document and authoring checks. Runtime lives in
:mod:`openadapt_flow.runtime.admitted_composition`. There is no
``process_contract.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, Mapping, Optional, Sequence
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from openadapt_flow.compiler.compose_authoring import (
    effect_bound_param_names,
    workflow_param_names,
)
from openadapt_flow.composition import (
    TERMINAL_OUTCOMES,
    AllowedHaltClass,
    HandoffBinding,
)
from openadapt_flow.ir import Workflow
from openadapt_flow.qualification_admission import QualificationAdmissionEnvelope

PROCESS_CONTRACT_FILENAME = "process-contract.json"
PROCESS_CONTRACT_SCHEMA: Literal["openadapt.process-contract/v0"] = (
    "openadapt.process-contract/v0"
)
PROCESS_CONTRACT_V1_SCHEMA: Literal["openadapt.process-contract/v1"] = (
    "openadapt.process-contract/v1"
)
_NAME_RE = r"^[A-Za-z][A-Za-z0-9_-]*$"
_SHA256_RE = r"^[a-f0-9]{64}$"


class ProcessContractError(ValueError):
    """Authoring or load refusal for a process-contract artifact."""


def _uuid(value: str, *, field: str) -> str:
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be a canonical UUID") from exc
    if str(parsed) != value:
        raise ValueError(f"{field} must be a canonical UUID")
    return value


class AdmittedChildSpec(BaseModel):
    """One independently admitted capability named by the parent."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128, pattern=_NAME_RE)
    admission_id: str
    workflow_version_id: str
    bundle_content_digest: str = Field(pattern=_SHA256_RE)
    envelope: str = Field(
        min_length=1,
        description="Pointer to the child's QualificationAdmissionEnvelope file",
    )
    bundle: str = Field(
        min_length=1,
        description="Pointer to the admitted bundle directory (not a copy)",
    )
    surface: Optional[str] = Field(
        default=None,
        description="Recorded surface of the admitted bundle, if known",
    )

    @field_validator("admission_id", "workflow_version_id")
    @classmethod
    def _canonical_uuid(cls, value: str, info: object) -> str:
        field = getattr(info, "field_name", "id")
        return _uuid(value, field=str(field))


class CodeChildSpec(BaseModel):
    """One sealed code capability admitted outside the ProcessContract."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128, pattern=_NAME_RE)
    kind: Literal["code"] = "code"
    manifest: str = Field(min_length=1)
    admission: str = Field(min_length=1)
    source_archive: str = Field(min_length=1)
    role: Literal["transform", "verifier"] = "transform"
    storage_boundary: Literal[
        "local_protected", "customer_controlled", "hosted_private"
    ] = "local_protected"
    data_classification: Literal["public", "internal", "sensitive", "regulated"] = (
        "regulated"
    )

    @field_validator("manifest", "admission", "source_archive")
    @classmethod
    def _portable_pointer(cls, value: str) -> str:
        if "\\" in value or "\x00" in value:
            raise ValueError("code pointers must be portable relative paths")
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or value in {"", "."}:
            raise ValueError("code pointers must be portable relative paths")
        return value


class HumanChildSpec(BaseModel):
    """One typed human task issued by the existing attended-task contract."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128, pattern=_NAME_RE)
    kind: Literal["human"] = "human"
    task_kind: Literal["authenticate", "actuate", "decide", "review"]
    substrate: Literal["browser", "windows", "macos", "linux", "rdp", "citrix", "mixed"]
    risk_class: Literal["read_only", "state_changing", "consequential", "irreversible"]
    required_authn: Literal["local_session", "aal2", "webauthn"]
    authentication_template: Optional[str] = None

    @model_validator(mode="after")
    def _authentication_boundary(self) -> "HumanChildSpec":
        if self.task_kind == "authenticate" and not self.authentication_template:
            raise ValueError(
                "an authenticate child requires a value-free authentication template"
            )
        if self.task_kind != "authenticate" and self.authentication_template:
            raise ValueError(
                "only an authenticate child can name an authentication template"
            )
        if self.authentication_template:
            value = self.authentication_template
            path = Path(value)
            if (
                "\\" in value
                or "\x00" in value
                or path.is_absolute()
                or ".." in path.parts
                or value in {"", "."}
            ):
                raise ValueError("authentication_template must be a relative path")
        return self


class ArtifactEdgeV1(BaseModel):
    """A verified, content-addressed artifact transfer between capabilities."""

    model_config = ConfigDict(extra="forbid")

    from_child: str = Field(pattern=_NAME_RE)
    from_output: str = Field(pattern=_NAME_RE)
    to_child: str = Field(pattern=_NAME_RE)
    to_input: str = Field(pattern=_NAME_RE)
    verifier_child: str = Field(pattern=_NAME_RE)


class ProcessContract(BaseModel):
    """Parent sequencer of independently admitted capabilities.

    Not a Workflow, not a ProgramGraph, and not a composition of recordings.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[
        "openadapt.process-contract/v0", "openadapt.process-contract/v1"
    ] = PROCESS_CONTRACT_SCHEMA
    name: str = Field(min_length=1, max_length=256)
    children: list[AdmittedChildSpec] = Field(default_factory=list)
    code_children: list[CodeChildSpec] = Field(default_factory=list)
    human_children: list[HumanChildSpec] = Field(default_factory=list)
    after: dict[str, list[str]] = Field(default_factory=dict)
    handoffs: list[HandoffBinding] = Field(default_factory=list)
    artifact_edges: list[ArtifactEdgeV1] = Field(default_factory=list)
    allow_halt: dict[str, list[AllowedHaltClass]] = Field(default_factory=dict)
    inputs: list[str] = Field(
        default_factory=list,
        description="Parent-level parameter names. Values arrive at run time.",
    )

    @field_validator("after")
    @classmethod
    def _unique_after(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        cleaned: dict[str, list[str]] = {}
        for name, preds in value.items():
            if len(preds) != len(set(preds)):
                raise ValueError(f"after list for {name!r} must not repeat names")
            cleaned[name] = list(preds)
        return cleaned

    @field_validator("allow_halt")
    @classmethod
    def _unique_halts(
        cls, value: dict[str, list[AllowedHaltClass]]
    ) -> dict[str, list[AllowedHaltClass]]:
        cleaned: dict[str, list[AllowedHaltClass]] = {}
        for name, outcomes in value.items():
            if len(outcomes) != len(set(outcomes)):
                raise ValueError(f"allow_halt for {name!r} must not repeat classes")
            cleaned[name] = list(outcomes)
        return cleaned

    @field_validator("inputs")
    @classmethod
    def _unique_inputs(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("process inputs must not repeat names")
        return value

    @model_validator(mode="after")
    def _closed_graph(self) -> "ProcessContract":
        if self.schema_version == PROCESS_CONTRACT_SCHEMA:
            if len(self.children) < 2:
                raise ValueError("process v0 requires at least two admitted children")
            if self.code_children or self.human_children or self.artifact_edges:
                raise ValueError(
                    "process v0 cannot contain v1 capabilities or artifacts"
                )
        names = self.capability_names
        if self.schema_version == PROCESS_CONTRACT_V1_SCHEMA and not names:
            raise ValueError("process v1 requires at least one capability")
        if self.schema_version == PROCESS_CONTRACT_V1_SCHEMA and self.allow_halt:
            raise ValueError(
                "process v1 preserves exact terminal outcomes and cannot use allow_halt"
            )
        if self.schema_version == PROCESS_CONTRACT_V1_SCHEMA:
            for child in self.children:
                for pointer in (child.envelope, child.bundle):
                    path = Path(pointer)
                    if (
                        "\\" in pointer
                        or "\x00" in pointer
                        or path.is_absolute()
                        or ".." in path.parts
                        or pointer in {"", "."}
                    ):
                        raise ValueError(
                            "process v1 Flow pointers must be portable relative paths"
                        )
        if len(names) != len(set(names)):
            raise ValueError("process capability names must be unique")
        known = set(names)
        for child_name, preds in self.after.items():
            if child_name not in known:
                raise ValueError(f"after refers to unknown child {child_name!r}")
            unknown = [name for name in preds if name not in known]
            if unknown:
                raise ValueError(
                    f"after {child_name!r} names unknown children {unknown}"
                )
            if child_name in preds:
                raise ValueError(f"child {child_name!r} cannot follow itself")
        for child_name in self.allow_halt:
            if child_name not in known:
                raise ValueError(f"allow_halt refers to unknown child {child_name!r}")
        for handoff in self.handoffs:
            if self.schema_version == PROCESS_CONTRACT_V1_SCHEMA:
                raise ValueError(
                    "process v1 uses verified artifact_edges instead of scalar handoffs"
                )
            if handoff.from_child not in known:
                raise ValueError(f"handoff from unknown child {handoff.from_child!r}")
            if handoff.to_child not in known:
                raise ValueError(f"handoff to unknown child {handoff.to_child!r}")
            if handoff.from_child == handoff.to_child:
                raise ValueError(
                    f"handoff {handoff.source} cannot target the same child "
                    f"{handoff.from_child!r}"
                )
        verifier_names = {
            child.name for child in self.code_children if child.role == "verifier"
        }
        transform_names = {
            child.name for child in self.code_children if child.role == "transform"
        }
        edge_keys: set[tuple[str, str, str, str]] = set()
        consumer_inputs: set[tuple[str, str]] = set()
        for edge in self.artifact_edges:
            if edge.from_child not in known or edge.to_child not in known:
                raise ValueError("an artifact edge refers to an unknown capability")
            if edge.from_child == edge.to_child:
                raise ValueError("an artifact edge cannot target its producer")
            if edge.verifier_child not in verifier_names:
                raise ValueError(
                    "an artifact edge requires a declared code verifier child"
                )
            if edge.from_child not in transform_names:
                raise ValueError(
                    "the built-in v1 artifact store requires a code transform producer"
                )
            edge_key = (
                edge.from_child,
                edge.from_output,
                edge.to_child,
                edge.to_input,
            )
            if edge_key in edge_keys:
                raise ValueError("process artifact edges must be unique")
            edge_keys.add(edge_key)
            consumer_input = (edge.to_child, edge.to_input)
            if consumer_input in consumer_inputs:
                raise ValueError(
                    "two artifact edges cannot bind the same consumer input"
                )
            consumer_inputs.add(consumer_input)
        return self

    @property
    def capability_names(self) -> list[str]:
        return [
            *(child.name for child in self.children),
            *(child.name for child in self.code_children),
            *(child.name for child in self.human_children),
        ]

    def capability(
        self, name: str
    ) -> AdmittedChildSpec | CodeChildSpec | HumanChildSpec:
        for flow_child in self.children:
            if flow_child.name == name:
                return flow_child
        for code_child in self.code_children:
            if code_child.name == name:
                return code_child
        for human_child in self.human_children:
            if human_child.name == name:
                return human_child
        raise ProcessContractError(f"process has no capability named {name!r}")

    def child(self, name: str) -> AdmittedChildSpec:
        for item in self.children:
            if item.name == name:
                return item
        raise ProcessContractError(f"process has no child named {name!r}")

    def save(self, parent_dir: Path | str) -> Path:
        parent = Path(parent_dir)
        parent.mkdir(parents=True, exist_ok=True)
        path = parent / PROCESS_CONTRACT_FILENAME
        payload = self.model_dump(mode="json")
        if self.schema_version == PROCESS_CONTRACT_SCHEMA:
            for field in ("code_children", "human_children", "artifact_edges"):
                payload.pop(field, None)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, parent_dir: Path | str) -> "ProcessContract":
        parent = Path(parent_dir)
        path = parent / PROCESS_CONTRACT_FILENAME
        if not path.is_file():
            raise ProcessContractError(
                f"{parent} is not a process-contract artifact "
                f"(no {PROCESS_CONTRACT_FILENAME})"
            )
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


class ProcessContractV1(ProcessContract):
    """Schema-specific projection for portable v1 consumers."""

    schema_version: Literal["openadapt.process-contract/v1"] = (
        PROCESS_CONTRACT_V1_SCHEMA
    )


def is_process_contract_artifact(path: Path | str) -> bool:
    """True when ``path`` is a process-contract parent directory."""

    return (Path(path) / PROCESS_CONTRACT_FILENAME).is_file()


def resolve_pointer(parent_dir: Path | str, stored: str) -> Path:
    """Resolve an envelope or bundle pointer against the parent directory."""

    parent = Path(parent_dir)
    rel = Path(stored)
    if rel.is_absolute():
        return rel
    return (parent / rel).resolve()


def live_bundle_content_digest(workflow: Workflow, bundle_dir: Path | str) -> str:
    """SHA-256 of the live admitted bundle, independent of the envelope."""

    from openadapt_flow.bundle_validation import (
        compute_content_digest,
        compute_file_hashes,
    )

    bundle = Path(bundle_dir)
    if workflow.manifest is not None and workflow.manifest.file_hashes:
        file_hashes = dict(workflow.manifest.file_hashes)
    else:
        file_hashes = compute_file_hashes(workflow, bundle)
    return compute_content_digest(workflow, file_hashes)


def load_child_envelope(
    parent_dir: Path | str, child: AdmittedChildSpec
) -> QualificationAdmissionEnvelope:
    """Load the child's v1 admission envelope from its pointer."""

    path = resolve_pointer(parent_dir, child.envelope)
    if not path.is_file():
        raise ProcessContractError(
            f"child {child.name!r} has no QualificationAdmissionEnvelope at "
            f"{path}; a compiled recording (including a compose child under "
            "composition.json) is not an independently admitted capability"
        )
    try:
        return QualificationAdmissionEnvelope.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise ProcessContractError(
            f"child {child.name!r} envelope at {path} is not a valid "
            "QualificationAdmissionEnvelope"
        ) from exc


def topological_order(contract: ProcessContract) -> list[str]:
    """Return child names in a deterministic topological order.

    Default edges: each child after the previous declared child, unless the
    parent listed explicit ``after`` predecessors. Cycles refuse.
    """

    names = contract.capability_names
    predecessors = predecessor_map(contract)
    edges: dict[str, set[str]] = {name: set() for name in names}
    incoming: dict[str, int] = {name: 0 for name in names}
    for name, preds in predecessors.items():
        for pred in preds:
            if name not in edges[pred]:
                edges[pred].add(name)
                incoming[name] += 1
    ready = [name for name in names if incoming[name] == 0]
    ordered: list[str] = []
    while ready:
        ready.sort(key=names.index)
        current = ready.pop(0)
        ordered.append(current)
        for nxt in sorted(edges[current], key=names.index):
            incoming[nxt] -= 1
            if incoming[nxt] == 0:
                ready.append(nxt)
    if len(ordered) != len(names):
        cyclic = [name for name in names if name not in ordered]
        raise ProcessContractError(f"process DAG has a cycle involving {cyclic}")
    return ordered


def predecessor_map(contract: ProcessContract) -> dict[str, list[str]]:
    """Return declared and artifact-derived predecessors for each child."""

    names = contract.capability_names
    explicit = {name: list(preds) for name, preds in contract.after.items()}
    use_linear_default = not any(explicit.values())
    preds: dict[str, list[str]] = {name: [] for name in names}
    for index, name in enumerate(names):
        listed = explicit.get(name, [])
        if not listed and use_linear_default and index > 0:
            listed = [names[index - 1]]
        preds[name] = listed
    if contract.schema_version == PROCESS_CONTRACT_V1_SCHEMA:
        for edge in contract.artifact_edges:
            if edge.from_child not in preds[edge.verifier_child]:
                preds[edge.verifier_child].append(edge.from_child)
            if (
                edge.to_child != edge.verifier_child
                and edge.verifier_child not in preds[edge.to_child]
            ):
                preds[edge.to_child].append(edge.verifier_child)
    return preds


def _pointer_for(src: Path, parent: Path) -> str:
    src = src.resolve()
    parent = parent.resolve()
    try:
        return src.relative_to(parent).as_posix()
    except ValueError:
        return str(src)


def _load_admitted_bundle(name: str, bundle: Path) -> Workflow:
    if (
        not (bundle / "workflow.json").is_file()
        and not (bundle / "workflow.json.enc").is_file()
    ):
        raise ProcessContractError(
            f"child {name!r} at {bundle} is not an admitted workflow bundle"
        )
    try:
        return Workflow.load(bundle)
    except Exception as exc:
        raise ProcessContractError(
            f"child {name!r} bundle could not be loaded: {type(exc).__name__}"
        ) from exc


def author_process_contract(
    children: Sequence[tuple[str, Path, Path]],
    *,
    handoffs: Sequence[HandoffBinding] = (),
    after: Optional[Mapping[str, Sequence[str]]] = None,
    allow_halt: Optional[Mapping[str, Sequence[str]]] = None,
    inputs: Optional[Sequence[str]] = None,
    name: Optional[str] = None,
    out: Path | str,
) -> ProcessContract:
    """Write ``process-contract.json`` that points at admitted children.

    ``children`` is an ordered list of ``(name, envelope_path, bundle_path)``.
    The parent does not copy recordings. Default sequencing is that order.
    """

    if len(children) < 2:
        raise ProcessContractError(
            "process needs at least two independently admitted children"
        )
    names = [item[0] for item in children]
    if len(names) != len(set(names)):
        raise ProcessContractError("process child names must be unique")

    out_dir = Path(out)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise ProcessContractError(
            f"process output {out_dir} already exists and is not empty"
        )
    out_dir.mkdir(parents=True, exist_ok=True)

    after_map = {key: list(value) for key, value in (after or {}).items()}
    halt_map = {key: list(value) for key, value in (allow_halt or {}).items()}
    errors: list[str] = []
    loaded: dict[str, Workflow] = {}
    envelopes: dict[str, QualificationAdmissionEnvelope] = {}
    specs: list[AdmittedChildSpec] = []

    for extra in set(after_map) | set(halt_map):
        if extra not in names:
            errors.append(f"after/allow-halt refers to unknown child {extra!r}")

    for child_name, envelope_src, bundle_src in children:
        envelope_path = Path(envelope_src)
        bundle_path = Path(bundle_src)
        if not envelope_path.is_file():
            errors.append(
                f"child {child_name!r} has no QualificationAdmissionEnvelope at "
                f"{envelope_path}; a compiled recording (including a compose "
                "child under composition.json) is not an independently "
                "admitted capability"
            )
            continue
        try:
            envelope = QualificationAdmissionEnvelope.model_validate_json(
                envelope_path.read_text(encoding="utf-8")
            )
        except Exception as exc:
            errors.append(
                f"child {child_name!r} envelope is not a valid "
                f"QualificationAdmissionEnvelope ({type(exc).__name__})"
            )
            continue
        try:
            workflow = _load_admitted_bundle(child_name, bundle_path)
        except ProcessContractError as exc:
            errors.append(str(exc))
            continue
        live_digest = live_bundle_content_digest(workflow, bundle_path)
        payload = envelope.payload
        if payload.bundle_content_digest != live_digest:
            errors.append(
                f"child {child_name!r} digest does not match the envelope "
                f"(live {live_digest}, envelope {payload.bundle_content_digest})"
            )
            continue
        halt_classes = halt_map.get(child_name, [])
        unknown_halts = [item for item in halt_classes if item not in TERMINAL_OUTCOMES]
        if unknown_halts:
            errors.append(
                f"child {child_name!r} allow_halt classes {unknown_halts} "
                "are not terminal outcomes"
            )
        valid_halts = [item for item in halt_classes if item in TERMINAL_OUTCOMES]
        halt_map[child_name] = valid_halts
        try:
            specs.append(
                AdmittedChildSpec(
                    name=child_name,
                    admission_id=payload.admission_id,
                    workflow_version_id=payload.workflow_version_id,
                    bundle_content_digest=payload.bundle_content_digest,
                    envelope=_pointer_for(envelope_path, out_dir),
                    bundle=_pointer_for(bundle_path, out_dir),
                    surface=workflow.surface,
                )
            )
        except Exception as exc:
            errors.append(f"child {child_name!r} spec is invalid: {exc}")
            continue
        loaded[child_name] = workflow
        envelopes[child_name] = envelope

    for handoff in handoffs:
        src_wf = loaded.get(handoff.from_child)
        dst_wf = loaded.get(handoff.to_child)
        if src_wf is None or dst_wf is None:
            if handoff.from_child not in names:
                errors.append(f"handoff from unknown child {handoff.from_child!r}")
            if handoff.to_child not in names:
                errors.append(f"handoff to unknown child {handoff.to_child!r}")
            continue
        bound = effect_bound_param_names(src_wf)
        if handoff.source not in bound:
            errors.append(
                f"handoff {handoff.from_child}.{handoff.source} is not a "
                f"parameter bound by a declared effect on {handoff.from_child} "
                f"(effect-bound params: {sorted(bound) or 'none'})"
            )
        dest_params = workflow_param_names(dst_wf)
        if handoff.target not in dest_params:
            errors.append(
                f"handoff target {handoff.to_child}.{handoff.target} is not a "
                f"parameter of {handoff.to_child} "
                f"(known: {sorted(dest_params) or 'none'})"
            )
        if handoff.target in set(dst_wf.secret_params or []):
            errors.append(
                f"handoff target {handoff.to_child}.{handoff.target} is a "
                "SECRET parameter; secrets are never copied between children"
            )

    if errors:
        _cleanup(out_dir)
        joined = "\n - ".join(errors)
        raise ProcessContractError(
            "cannot author process contract -- contract mismatch:\n - " + joined
        )

    valid_allow_halt: dict[str, list[AllowedHaltClass]] = {
        key: list(value)  # type: ignore[arg-type]
        for key, value in halt_map.items()
        if value
    }
    try:
        contract = ProcessContract(
            name=name or "process",
            children=specs,
            after=after_map,
            handoffs=list(handoffs),
            allow_halt=valid_allow_halt,
            inputs=list(inputs or []),
        )
        order = topological_order(contract)
    except (ProcessContractError, ValueError) as exc:
        _cleanup(out_dir)
        if isinstance(exc, ProcessContractError):
            raise
        raise ProcessContractError(str(exc)) from exc

    index = {child_name: i for i, child_name in enumerate(order)}
    backward: list[str] = []
    for handoff in contract.handoffs:
        if index[handoff.from_child] >= index[handoff.to_child]:
            backward.append(
                f"handoff {handoff.from_child}.{handoff.source} -> "
                f"{handoff.to_child}.{handoff.target} runs backwards: "
                f"{handoff.from_child!r} is not before {handoff.to_child!r} "
                "in process order"
            )
    if backward:
        _cleanup(out_dir)
        joined = "\n - ".join(backward)
        raise ProcessContractError(
            "cannot author process contract -- contract mismatch:\n - " + joined
        )
    contract.save(out_dir)
    return contract


def _cleanup(out_dir: Path) -> None:
    written = out_dir / PROCESS_CONTRACT_FILENAME
    if written.is_file():
        written.unlink()
    try:
        out_dir.rmdir()
    except OSError:
        pass
