"""Composition artifact: a thin sequencer of compiled Flow bundles.

Compose is an operator product path. It is not a second process engine and
not a larger workflow-program graph. Each child bundle stays bound to the
surface it was recorded on. The parent runs children in declared order, or
in topological order of an explicit after DAG, and starts a child only after
every predecessor ended VERIFIED (or in an explicitly allowed halt class).

Handoffs are verified facts: parameter values that a predecessor's CONFIRMED
effect contract already bound. The parent does not guess window titles or
URLs. Missing evidence HALTs.

The on-disk form is a directory:

    composed/
      composition.json
      children/<name>/workflow.json   # copied child bundle

certify and run load this directory the same way they load a single bundle.
Subflows, worklists, and ProgramGraph stay inside each child.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

COMPOSITION_FILENAME = "composition.json"
COMPOSITION_SCHEMA: Literal["openadapt.composition/v1"] = "openadapt.composition/v1"
CHILDREN_DIR = "children"

TERMINAL_OUTCOMES = (
    "VERIFIED",
    "COMPLETED_UNVERIFIED",
    "HALTED",
    "FAILED",
    "ROLLED_BACK",
)

AllowedHaltClass = Literal[
    "VERIFIED",
    "COMPLETED_UNVERIFIED",
    "HALTED",
    "FAILED",
    "ROLLED_BACK",
]


class CompositionError(ValueError):
    """Authoring or runtime refusal for a composition artifact."""


class ChildSpec(BaseModel):
    """One compiled child bundle inside a composition."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    bundle: str = Field(
        min_length=1,
        description="Parent-relative path to the copied child bundle directory",
    )
    surface: Optional[str] = Field(
        default=None,
        description="Recorded surface of the child, copied from the child bundle",
    )
    after: list[str] = Field(
        default_factory=list,
        description="Predecessor child names. Empty means previous in order.",
    )
    allowed_halt_classes: list[AllowedHaltClass] = Field(
        default_factory=list,
        description=(
            "Extra predecessor terminal classes that may start THIS child. "
            "VERIFIED is always allowed. Anything else must be named here."
        ),
    )

    @field_validator("after")
    @classmethod
    def _unique_after(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("child after list must not repeat names")
        return value

    @field_validator("allowed_halt_classes")
    @classmethod
    def _unique_halts(cls, value: list[AllowedHaltClass]) -> list[AllowedHaltClass]:
        if len(value) != len(set(value)):
            raise ValueError("allowed halt classes must be unique")
        return value


class HandoffBinding(BaseModel):
    """One verified fact that a predecessor must supply to a successor.

    ``source`` names a parameter that ``from_child`` binds in a declared
    effect contract. After that child ends VERIFIED, the bound value becomes
    ``to_child``'s ``target`` parameter. The value is not read from a window
    title, a URL, or OCR.
    """

    model_config = ConfigDict(extra="forbid")

    from_child: str = Field(min_length=1, max_length=128)
    source: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
        description="Effect-bound parameter name on the predecessor",
    )
    to_child: str = Field(min_length=1, max_length=128)
    target: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
        description="Parameter name on the successor that receives the fact",
    )


class CompositionProvenance(BaseModel):
    """Certification stamp for a composition artifact (additive)."""

    model_config = ConfigDict(extra="forbid")

    certified: bool = False
    policy_name: Optional[str] = None
    certified_at: Optional[str] = None
    child_results: dict[str, bool] = Field(default_factory=dict)


class Composition(BaseModel):
    """Parent sequencer spec. Not a Workflow and not a ProgramGraph."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["openadapt.composition/v1"] = COMPOSITION_SCHEMA
    name: str = Field(min_length=1, max_length=256)
    children: list[ChildSpec] = Field(min_length=2)
    handoffs: list[HandoffBinding] = Field(default_factory=list)
    provenance: CompositionProvenance = Field(default_factory=CompositionProvenance)

    @model_validator(mode="after")
    def _closed_graph(self) -> "Composition":
        names = [child.name for child in self.children]
        if len(names) != len(set(names)):
            raise ValueError("composition child names must be unique")
        known = set(names)
        for child in self.children:
            unknown = [name for name in child.after if name not in known]
            if unknown:
                raise ValueError(
                    f"child {child.name!r} after names unknown children {unknown}"
                )
            if child.name in child.after:
                raise ValueError(f"child {child.name!r} cannot follow itself")
        for handoff in self.handoffs:
            if handoff.from_child not in known:
                raise ValueError(f"handoff from unknown child {handoff.from_child!r}")
            if handoff.to_child not in known:
                raise ValueError(f"handoff to unknown child {handoff.to_child!r}")
            if handoff.from_child == handoff.to_child:
                raise ValueError(
                    f"handoff {handoff.source} cannot target the same child "
                    f"{handoff.from_child!r}"
                )
        return self

    def child(self, name: str) -> ChildSpec:
        for item in self.children:
            if item.name == name:
                return item
        raise CompositionError(f"composition has no child named {name!r}")

    def save(self, parent_dir: Path | str) -> Path:
        parent = Path(parent_dir)
        parent.mkdir(parents=True, exist_ok=True)
        path = parent / COMPOSITION_FILENAME
        path.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, parent_dir: Path | str) -> "Composition":
        parent = Path(parent_dir)
        path = parent / COMPOSITION_FILENAME
        if not path.is_file():
            raise CompositionError(
                f"{parent} is not a composition artifact (no {COMPOSITION_FILENAME})"
            )
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


def is_composition_artifact(path: Path | str) -> bool:
    """True when ``path`` is a composition parent directory."""

    return (Path(path) / COMPOSITION_FILENAME).is_file()


def child_bundle_path(parent_dir: Path | str, child: ChildSpec) -> Path:
    """Resolve a child bundle path against the parent directory."""

    parent = Path(parent_dir)
    rel = Path(child.bundle)
    if rel.is_absolute() or ".." in rel.parts:
        raise CompositionError(
            f"child {child.name!r} bundle path must be parent-relative "
            f"without '..' (got {child.bundle!r})"
        )
    return parent / rel


def topological_order(composition: Composition) -> list[str]:
    """Return child names in a deterministic topological order.

    Default edges: each child after the previous declared child, unless the
    child listed explicit after predecessors. Cycles refuse.
    """

    names = [child.name for child in composition.children]
    explicit = {child.name: list(child.after) for child in composition.children}
    use_linear_default = not any(explicit.values())
    edges: dict[str, set[str]] = {name: set() for name in names}
    incoming: dict[str, int] = {name: 0 for name in names}
    for index, name in enumerate(names):
        preds = explicit[name]
        if not preds and use_linear_default and index > 0:
            preds = [names[index - 1]]
        for pred in preds:
            if name not in edges[pred]:
                edges[pred].add(name)
                incoming[name] += 1
    ready = [name for name in names if incoming[name] == 0]
    ordered: list[str] = []
    while ready:
        # Stable: earliest declared name among ready nodes.
        ready.sort(key=names.index)
        current = ready.pop(0)
        ordered.append(current)
        for nxt in sorted(edges[current], key=names.index):
            incoming[nxt] -= 1
            if incoming[nxt] == 0:
                ready.append(nxt)
    if len(ordered) != len(names):
        cyclic = [name for name in names if name not in ordered]
        raise CompositionError(f"composition DAG has a cycle involving {cyclic}")
    return ordered


def predecessor_map(composition: Composition) -> dict[str, list[str]]:
    """Return declared-order predecessors for each child.

    Same edges ``topological_order`` uses: linear previous-in-order when no
    child named ``after``, otherwise only the explicit after lists.
    """

    names = [child.name for child in composition.children]
    explicit = {child.name: list(child.after) for child in composition.children}
    use_linear_default = not any(explicit.values())
    preds: dict[str, list[str]] = {name: [] for name in names}
    for index, name in enumerate(names):
        listed = explicit[name]
        if not listed and use_linear_default and index > 0:
            listed = [names[index - 1]]
        preds[name] = listed
    return preds
