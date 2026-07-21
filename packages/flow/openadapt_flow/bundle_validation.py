"""Bundle schema v2: manifest/provenance, content digest, and load-time
structural + integrity validation.

A compiled bundle is the trust boundary between the demonstration compiler and
the deterministic replayer: everything the replayer will do to a real system of
record is frozen in ``workflow.json`` (+ its template PNGs). Historically that
artifact was raw JSON with a ``schema_version`` stuck at 1, no migration path,
no manifest, no content hashes, and -- most importantly -- NO structural
validation on load. A malformed or tampered graph would only surface as a
confusing runtime failure (or, worse, a silent wrong action).

This module closes those gaps additively (it never rewrites an existing
bundle's semantics):

* **Manifest / provenance** (:class:`~openadapt_flow.ir.BundleManifest`):
  per-file SHA-256 hashes of the template/image assets, a whole-bundle
  ``content_digest``, the compiler version that produced the bundle, and -- for
  a certified bundle -- the policy it was certified against, its certification
  status, and an optional expiry.
* **Migration** (:func:`migrate_bundle_dict`): a v1 bundle (or one with no
  ``schema_version``) migrates cleanly to v2 in memory on read. v2 is a strict
  superset of v1, so migration only stamps the new version and back-fills the
  manifest if it is absent -- an existing bundle keeps replaying byte-for-byte.
* **Load-time structural validation** (:func:`validate_workflow`): reject a
  malformed or uncertifiable graph with clear, per-issue errors BEFORE the
  replayer touches it. Rules span graph well-formedness (entry exists, every
  transition target resolves, each state's kind matches its payload, referenced
  subflows exist, ids are unique, terminals are reachable, no unsafe
  unconditional cycle) and the ONE safety rule that motivates all the rest:
  every path that reaches a consequential/irreversible action also reaches its
  effect verification.
* **Integrity** (:func:`verify_integrity`): recompute the digest on load and
  refuse a bundle whose ``workflow.json`` or assets were tampered with after
  the manifest was sealed.

Everything here is import-light (stdlib + pydantic + the IR); it brings in no
OCR / cv2 / model dependencies, so it is safe to call on every load.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Iterator, Literal, Optional

from pydantic import BaseModel, Field

from openadapt_flow.ir import (
    BundleManifest,
    BundleProvenance,
    ProgramGraph,
    State,
    StateKind,
    Step,
    Workflow,
)
from openadapt_flow.traversal import iter_workflow_steps

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass


# ---------------------------------------------------------------------------
# Hashing / digest / manifest
# ---------------------------------------------------------------------------


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _referenced_asset_paths(workflow: "Workflow") -> set[str]:
    """Every bundle-relative image path the IR references (templates, identifier
    crops, postcondition template crops), across the linear steps AND every
    program / subflow action state."""
    refs: set[str] = set()
    for step in iter_workflow_steps(workflow):
        anchor = step.anchor
        if anchor is not None:
            if anchor.template:
                refs.add(anchor.template)
            if anchor.identifier_crop:
                refs.add(anchor.identifier_crop)
            for lm_anchor in _predicate_anchor_templates(step):
                refs.add(lm_anchor)
        for pc in step.expect:
            if pc.template:
                refs.add(pc.template)
    return refs


def _predicate_anchor_templates(step: "Step") -> Iterator[str]:
    """Template paths carried by a step's ``wait_until`` / ``guard`` predicate
    anchors (an ANCHOR_RESOLVES predicate embeds a full :class:`Anchor`)."""
    preds = []
    if step.wait_until is not None:
        preds.append(step.wait_until)
    if step.guard is not None:
        preds.append(step.guard.predicate)
    while preds:
        p = preds.pop()
        if p.anchor is not None and p.anchor.template:
            yield p.anchor.template
        preds.extend(p.operands)


def compute_file_hashes(workflow: "Workflow", bundle_dir: Path | str) -> dict[str, str]:
    """SHA-256 every template/image asset in the bundle.

    Hashes the union of (a) every file under ``templates/`` and (b) every
    bundle-relative image path the IR references, that actually exists on disk.
    Keyed by POSIX bundle-relative path, sorted for a stable, reproducible
    manifest. Missing referenced files are simply omitted (the structural
    validator reports dangling references separately; the digest reflects what
    is actually present)."""
    bundle = Path(bundle_dir)
    paths: set[str] = set()

    templates_dir = bundle / "templates"
    if templates_dir.is_dir():
        for f in templates_dir.rglob("*"):
            if f.is_file():
                paths.add(f.relative_to(bundle).as_posix())

    for rel in _referenced_asset_paths(workflow):
        candidate = bundle / rel
        if candidate.is_file():
            paths.add(Path(rel).as_posix())

    return {rel: _sha256_file(bundle / rel) for rel in sorted(paths)}


def _workflow_content(workflow: "Workflow") -> dict:
    """The canonical, manifest-free content of a workflow used for the digest.

    Excludes the ``manifest`` field itself (the digest lives INSIDE the
    manifest, so it must be computed over everything else) so the digest is a
    pure function of the semantic bundle content."""
    return workflow.model_dump(mode="json", exclude={"manifest"})


def compute_content_digest(workflow: "Workflow", file_hashes: dict[str, str]) -> str:
    """A whole-bundle SHA-256 over the manifest-free workflow content AND the
    asset hashes -- one digest that changes if ANY byte of the semantic bundle
    (the JSON or a template) changes."""
    payload = {
        "workflow": _workflow_content(workflow),
        "files": dict(sorted(file_hashes.items())),
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return _sha256_bytes(canonical.encode("utf-8"))


def build_runtime_parameter_schema(workflow: "Workflow") -> list[dict]:
    """Return the canonical hosted parameter contract without runtime values.

    The shape deliberately includes legacy defaults and secret-only parameters,
    but never their example/default values. This is the single cross-language
    representation signed by Flow and consumed by Cloud.
    """
    names = sorted(
        set(workflow.params) | set(workflow.param_specs) | set(workflow.secret_params)
    )
    payload: list[dict] = []
    for name in names:
        spec = workflow.param_specs.get(name)
        payload.append(
            {
                "name": name,
                "type": spec.type.value if spec is not None else "string",
                "required": (
                    spec.required
                    if spec is not None
                    else name in workflow.secret_params
                ),
                "secret": name in workflow.secret_params,
                "choices": list(spec.choices) if spec is not None else [],
            }
        )
    return payload


def compute_parameter_schema_digest(workflow: "Workflow") -> str:
    """Digest the canonical runtime parameter contract without values."""
    payload = build_runtime_parameter_schema(workflow)
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return _sha256_bytes(canonical.encode("utf-8"))


def build_manifest(workflow: "Workflow", bundle_dir: Path | str) -> BundleManifest:
    """Compute a fresh :class:`BundleManifest` for ``workflow`` in ``bundle_dir``.

    Recomputes the asset hashes and content digest, stamps the current compiler
    version, and CARRIES OVER any certification/provenance already recorded on
    the workflow's existing manifest (so re-saving a certified bundle keeps its
    certification unless it is re-certified). Idempotent for unchanged content:
    two calls over the same bundle produce the same digest."""
    from openadapt_flow import __version__ as _compiler_version

    prior = workflow.manifest
    prior_prov = prior.provenance if prior is not None else None

    file_hashes = compute_file_hashes(workflow, bundle_dir)
    digest = compute_content_digest(workflow, file_hashes)

    provenance = BundleProvenance(
        compiler_version=_compiler_version,
        source_recording_sha256=(
            prior_prov.source_recording_sha256 if prior_prov else None
        ),
        compiler_config_sha256=(
            prior_prov.compiler_config_sha256 if prior_prov else None
        ),
        created_at=(prior_prov.created_at if prior_prov else workflow.created_at),
        policy_name=prior_prov.policy_name if prior_prov else None,
        certified=prior_prov.certified if prior_prov else False,
        certification_status=(prior_prov.certification_status if prior_prov else None),
        certified_at=prior_prov.certified_at if prior_prov else None,
        expires_at=prior_prov.expires_at if prior_prov else None,
    )
    return BundleManifest(
        content_digest=digest,
        file_hashes=file_hashes,
        provenance=provenance,
        encrypted=workflow.encrypted,
    )


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

#: The current bundle schema version. Kept in sync with
#: :data:`openadapt_flow.ir.SCHEMA_VERSION`.
from openadapt_flow.ir import SCHEMA_VERSION  # noqa: E402


def migrate_bundle_dict(data: dict) -> dict:
    """Migrate a raw ``workflow.json`` dict to the current schema version.

    v2 is a strict, ADDITIVE superset of v1 (typed params, program graph,
    manifest, ... all default-empty), so migrating a v1 bundle needs no field
    transforms -- it only stamps the current ``schema_version`` so downstream
    code can rely on it. A bundle with no ``schema_version`` key is treated as
    the original v1 shape. Returns the SAME dict, mutated in place, for
    convenience. Forward-compatible: a bundle already at (or above) the current
    version is left untouched."""
    version = data.get("schema_version", 1)
    if not isinstance(version, int):
        version = 1
    if version < SCHEMA_VERSION:
        # --- future field transforms would live here, gated on `version` ---
        data["schema_version"] = SCHEMA_VERSION
    return data


# ---------------------------------------------------------------------------
# Integrity
# ---------------------------------------------------------------------------


class BundleIntegrityError(Exception):
    """A bundle's recomputed digest does not match its sealed manifest -- the
    ``workflow.json`` or a template asset was altered after the manifest was
    written."""


def verify_integrity(
    workflow: "Workflow",
    bundle_dir: Path | str,
    stored: BundleManifest,
    *,
    decrypted_assets: Optional[dict[str, bytes]] = None,
) -> None:
    """Recompute the bundle's asset hashes + content digest and compare them to
    ``stored``. Raises :class:`BundleIntegrityError` on any mismatch.

    Two independent checks, so a bundle whose templates are written AFTER the
    workflow.json was sealed (a legitimate compile ordering) is not falsely
    rejected:

    1. the ``workflow.json`` content (plus the SEALED asset-hash list) must
       still hash to the sealed ``content_digest`` -- catches any edit to the
       serialized workflow;
    2. every asset the manifest SEALED must still be present and hash to its
       recorded value -- from disk for plaintext bundles, or from the caller's
       authenticated in-memory plaintext map for encrypted bundles. Assets
       ADDED after the seal are outside the seal and are not checked here
       (re-saving reseals them).

    Only meaningful for a bundle that carries a persisted manifest with a
    non-empty digest; legacy (pre-v2) bundles have no sealed digest to compare
    against, and the caller skips this check for them."""
    if not stored.content_digest:
        return
    # 1. Recompute the digest over the loaded workflow content using the SEALED
    #    file-hash list (NOT re-hashed from disk), so post-seal template
    #    additions do not move it -- only a workflow.json edit (or a change to
    #    the sealed asset set) breaks this.
    recomputed = compute_content_digest(workflow, stored.file_hashes)
    if recomputed != stored.content_digest:
        raise BundleIntegrityError(
            "bundle content digest mismatch: expected "
            f"{stored.content_digest[:16]}..., recomputed {recomputed[:16]}... "
            "-- the workflow.json was modified after the manifest was sealed"
        )
    # 2. Each sealed asset must still hash to its recorded value.
    bundle = Path(bundle_dir)
    for rel, expected in stored.file_hashes.items():
        if decrypted_assets is not None:
            data = decrypted_assets.get(rel)
            if data is None:
                raise BundleIntegrityError(
                    f"manifest lists sealed asset {rel!r} but its authenticated "
                    "plaintext is unavailable"
                )
            if _sha256_bytes(data) != expected:
                raise BundleIntegrityError(
                    f"sealed asset {rel!r} plaintext hash mismatch "
                    "(tampered or corrupted)"
                )
            continue
        path = bundle / rel
        if not path.is_file():
            raise BundleIntegrityError(
                f"manifest lists asset {rel!r} but it is missing from the bundle"
            )
        if _sha256_file(path) != expected:
            raise BundleIntegrityError(
                f"asset {rel!r} hash mismatch (tampered or corrupted)"
            )


# ---------------------------------------------------------------------------
# Structural + safety validation
# ---------------------------------------------------------------------------

IssueCategory = Literal["structure", "safety"]


class ValidationIssue(BaseModel):
    """A single validation problem found in a bundle."""

    category: IssueCategory
    code: str
    message: str
    state_id: Optional[str] = None
    graph: Optional[str] = None

    def render(self) -> str:
        where = ""
        if self.graph:
            where += f"[{self.graph}] "
        if self.state_id:
            where += f"({self.state_id}) "
        return f"{self.category}:{self.code} {where}{self.message}"


class ValidationReport(BaseModel):
    """The outcome of validating a bundle: a list of issues, categorized."""

    workflow_name: str
    issues: list[ValidationIssue] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    @property
    def structural_ok(self) -> bool:
        return not any(i.category == "structure" for i in self.issues)

    def by_category(self, category: IssueCategory) -> list[ValidationIssue]:
        return [i for i in self.issues if i.category == category]

    def render(self) -> str:
        if not self.issues:
            return f"validate {self.workflow_name!r}: OK (no issues)"
        lines = [f"validate {self.workflow_name!r}: {len(self.issues)} issue(s)"]
        lines.extend(f"  - {i.render()}" for i in self.issues)
        return "\n".join(lines)

    def raise_if(
        self, categories: Iterable[IssueCategory] = ("structure", "safety")
    ) -> None:
        """Raise :class:`BundleValidationError` if any issue in ``categories``
        is present. Load uses ``categories=("structure",)`` so a genuinely
        malformed graph is rejected while the (advisory-at-load) safety findings
        are surfaced by lint/certify instead."""
        cats = set(categories)
        offending = [i for i in self.issues if i.category in cats]
        if offending:
            raise BundleValidationError(self, offending)


class BundleValidationError(Exception):
    """A bundle failed structural (or requested) validation on load."""

    def __init__(
        self, report: ValidationReport, offending: list[ValidationIssue]
    ) -> None:
        self.report = report
        self.offending = offending
        joined = "; ".join(i.render() for i in offending)
        super().__init__(f"bundle {report.workflow_name!r} failed validation: {joined}")


def _is_consequential(step: "Step") -> bool:
    """Whether a step performs a consequential / irreversible action -- the
    write that MUST be verified against the system of record. True when the step
    itself is classified irreversible OR any of its declared effects is."""
    if step.risk == "irreversible":
        return True
    return any(getattr(e, "risk", "reversible") == "irreversible" for e in step.effects)


def _has_effect_verification(step: "Step") -> bool:
    """Whether a step carries an effect-verification contract: a system-of-record
    :class:`Effect` on the step, or on its API binding. This is the verification
    that the replayer runs immediately after the action and HALTs on if not
    CONFIRMED, so a step that carries it verifies on EVERY path that reaches the
    action."""
    if step.effects:
        return True
    if step.api_binding is not None and step.api_binding.effects:
        return True
    return False


def _validate_graph(
    graph: "ProgramGraph",
    graph_name: str,
    known_subflows: set[str],
    issues: list[ValidationIssue],
    seen_state_ids: set[str],
    seen_step_ids: set[str],
) -> None:
    states = graph.states

    # 1. entry exists
    if graph.entry not in states:
        issues.append(
            ValidationIssue(
                category="structure",
                code="missing_entry",
                graph=graph_name,
                message=f"program entry {graph.entry!r} is not a defined state",
            )
        )

    for sid, state in states.items():
        # 2. dict key must equal the state's own id
        if state.id != sid:
            issues.append(
                ValidationIssue(
                    category="structure",
                    code="state_id_mismatch",
                    graph=graph_name,
                    state_id=sid,
                    message=f"state keyed {sid!r} declares id {state.id!r}",
                )
            )
        # 3. global state-id uniqueness (across program + all subflows)
        key = f"{graph_name}::{state.id}"
        if state.id in seen_state_ids:
            issues.append(
                ValidationIssue(
                    category="structure",
                    code="duplicate_state_id",
                    graph=graph_name,
                    state_id=state.id,
                    message=f"state id {state.id!r} is defined more than once",
                )
            )
        seen_state_ids.add(state.id)
        _ = key

        _validate_state_payload(
            state, graph_name, states, known_subflows, issues, seen_step_ids
        )

    # 8. reachability + unsafe unconditional cycle
    _validate_reachability_and_cycles(graph, graph_name, issues)


def _validate_state_payload(
    state: "State",
    graph_name: str,
    states: dict[str, "State"],
    known_subflows: set[str],
    issues: list[ValidationIssue],
    seen_step_ids: set[str],
) -> None:
    kind = state.kind

    # 4. kind <-> payload consistency
    if kind is StateKind.ACTION:
        if state.step is None:
            issues.append(
                ValidationIssue(
                    category="structure",
                    code="action_without_step",
                    graph=graph_name,
                    state_id=state.id,
                    message="ACTION state carries no step",
                )
            )
    elif kind is StateKind.BRANCH:
        if not any(t.guard is not None for t in state.transitions):
            issues.append(
                ValidationIssue(
                    category="structure",
                    code="branch_without_predicate",
                    graph=graph_name,
                    state_id=state.id,
                    message=(
                        "BRANCH state has no guarded (predicate) transition to "
                        "branch on"
                    ),
                )
            )
    elif kind is StateKind.LOOP:
        if state.loop is None:
            issues.append(
                ValidationIssue(
                    category="structure",
                    code="loop_without_spec",
                    graph=graph_name,
                    state_id=state.id,
                    message="LOOP state carries no LoopSpec",
                )
            )
        elif state.loop.body not in known_subflows:
            # 6. referenced subflow (loop body) must exist
            issues.append(
                ValidationIssue(
                    category="structure",
                    code="missing_loop_body",
                    graph=graph_name,
                    state_id=state.id,
                    message=(
                        f"LOOP body subflow {state.loop.body!r} is not a defined "
                        "subflow"
                    ),
                )
            )
    elif kind is StateKind.SUBFLOW_CALL:
        if state.subflow is None:
            issues.append(
                ValidationIssue(
                    category="structure",
                    code="subflow_call_without_target",
                    graph=graph_name,
                    state_id=state.id,
                    message="SUBFLOW_CALL state names no subflow",
                )
            )
        elif state.subflow not in known_subflows:
            # 6. referenced subflow must exist
            issues.append(
                ValidationIssue(
                    category="structure",
                    code="missing_subflow",
                    graph=graph_name,
                    state_id=state.id,
                    message=f"SUBFLOW_CALL names undefined subflow {state.subflow!r}",
                )
            )
    elif kind is StateKind.TERMINAL:
        if state.outcome is None:
            issues.append(
                ValidationIssue(
                    category="structure",
                    code="terminal_without_outcome",
                    graph=graph_name,
                    state_id=state.id,
                    message="TERMINAL state declares no outcome",
                )
            )
        if state.transitions:
            issues.append(
                ValidationIssue(
                    category="structure",
                    code="terminal_with_transitions",
                    graph=graph_name,
                    state_id=state.id,
                    message="TERMINAL state must have no outgoing transitions",
                )
            )

    # 5. every transition target resolves
    for t in state.transitions:
        if t.target not in states:
            issues.append(
                ValidationIssue(
                    category="structure",
                    code="dangling_transition",
                    graph=graph_name,
                    state_id=state.id,
                    message=f"transition targets undefined state {t.target!r}",
                )
            )
    # on_exception handler (if any) must resolve within the graph
    if state.on_exception is not None and state.on_exception not in states:
        issues.append(
            ValidationIssue(
                category="structure",
                code="dangling_exception_handler",
                graph=graph_name,
                state_id=state.id,
                message=(
                    f"on_exception targets undefined state {state.on_exception!r}"
                ),
            )
        )

    # 7. step-id uniqueness across the whole program
    if state.step is not None:
        if state.step.id in seen_step_ids:
            issues.append(
                ValidationIssue(
                    category="structure",
                    code="duplicate_step_id",
                    graph=graph_name,
                    state_id=state.id,
                    message=f"step id {state.step.id!r} is defined more than once",
                )
            )
        seen_step_ids.add(state.step.id)


def _validate_reachability_and_cycles(
    graph: "ProgramGraph", graph_name: str, issues: list[ValidationIssue]
) -> None:
    states = graph.states
    if graph.entry not in states:
        return  # already reported; reachability is undefined

    # Reachability over ALL transitions from the entry.
    reachable: set[str] = set()
    stack = [graph.entry]
    while stack:
        sid = stack.pop()
        if sid in reachable or sid not in states:
            continue
        reachable.add(sid)
        for t in states[sid].transitions:
            if t.target in states and t.target not in reachable:
                stack.append(t.target)
        st = states[sid]
        if st.on_exception in states and st.on_exception not in reachable:
            stack.append(st.on_exception)  # type: ignore[arg-type]

    # Every terminal must be reachable from the entry.
    for sid, state in states.items():
        if state.kind is StateKind.TERMINAL and sid not in reachable:
            issues.append(
                ValidationIssue(
                    category="structure",
                    code="unreachable_terminal",
                    graph=graph_name,
                    state_id=sid,
                    message="terminal state is not reachable from the program entry",
                )
            )

    # Unsafe UNCONDITIONAL cycle: a cycle formed purely by guard-less (TRUE)
    # transitions loops forever deterministically with nothing to break it (a
    # LOOP state's bounded iteration is internal to its body subflow, not a
    # transition cycle, so it is not flagged here). DFS with a recursion stack
    # over the unconditional-edge subgraph restricted to reachable states.
    WHITE, GREY, BLACK = 0, 1, 2
    color: dict[str, int] = {sid: WHITE for sid in states if sid in reachable}

    def uncond_targets(sid: str) -> list[str]:
        return [
            t.target
            for t in states[sid].transitions
            if t.guard is None and t.target in states and t.target in reachable
        ]

    cycle_reported = False

    def dfs(sid: str) -> bool:
        nonlocal cycle_reported
        color[sid] = GREY
        for tgt in uncond_targets(sid):
            if color.get(tgt) == GREY:
                if not cycle_reported:
                    issues.append(
                        ValidationIssue(
                            category="structure",
                            code="unsafe_cycle",
                            graph=graph_name,
                            state_id=sid,
                            message=(
                                "unconditional transition cycle through "
                                f"{tgt!r} loops forever with no guard to exit"
                            ),
                        )
                    )
                    cycle_reported = True
                return True
            if color.get(tgt) == WHITE and dfs(tgt):
                return True
        color[sid] = BLACK
        return False

    for sid in list(color):
        if color[sid] == WHITE:
            dfs(sid)


def validate_workflow(workflow: "Workflow") -> ValidationReport:
    """Validate a compiled bundle's structure and safety.

    Runs every rule and returns a :class:`ValidationReport`; it never raises
    (call :meth:`ValidationReport.raise_if` to convert findings to an error).

    A LINEAR bundle (``program is None``) has no graph to walk, so only the
    step-level rules apply -- id uniqueness and the effect-verification safety
    rule -- and a well-formed linear bundle validates trivially.

    A PROGRAM bundle is validated graph by graph (the top-level ``program`` and
    every ``subflow``): entry exists, transition/handler targets resolve, each
    state's kind matches its payload, referenced subflows exist, state/step ids
    are unique, terminals are reachable, and no unconditional cycle can spin
    forever. The safety rule spans BOTH shapes via the canonical step traversal.
    """
    issues: list[ValidationIssue] = []
    seen_state_ids: set[str] = set()
    seen_step_ids: set[str] = set()

    if workflow.program is not None:
        known_subflows = set(workflow.subflows.keys())
        _validate_graph(
            workflow.program,
            "program",
            known_subflows,
            issues,
            seen_state_ids,
            seen_step_ids,
        )
        for name, sub in workflow.subflows.items():
            _validate_graph(
                sub,
                f"subflow:{name}",
                known_subflows,
                issues,
                seen_state_ids,
                seen_step_ids,
            )
    else:
        # Linear bundle: only step-id uniqueness among the linear steps.
        for step in workflow.steps:
            if step.id in seen_step_ids:
                issues.append(
                    ValidationIssue(
                        category="structure",
                        code="duplicate_step_id",
                        state_id=step.id,
                        message=f"step id {step.id!r} is defined more than once",
                    )
                )
            seen_step_ids.add(step.id)

    # Safety rule (spans linear + program via the canonical traversal): every
    # consequential/irreversible action must carry effect verification, so every
    # path that reaches the write also reaches its system-of-record check.
    for step in iter_workflow_steps(workflow):
        if _is_consequential(step) and not _has_effect_verification(step):
            issues.append(
                ValidationIssue(
                    category="safety",
                    code="unverified_consequential_write",
                    state_id=step.id,
                    message=(
                        "irreversible/consequential action declares no "
                        "system-of-record effect -- a path reaches this write "
                        "with no effect verification (silent-wrong-write risk)"
                    ),
                )
            )

    return ValidationReport(workflow_name=workflow.name, issues=issues)
