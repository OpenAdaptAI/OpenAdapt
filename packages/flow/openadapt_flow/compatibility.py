"""Which features work under which run profiles: the compatibility matrix.

Each cell either works or is refused with a named reason. A refused feature
never falls back silently: the run either says why it used the slower or
narrower path (an API step's ``api_fallback``, a pipelined read-back's
``inline_reason``), or the command refuses before anything runs.

``tests/test_compatibility_matrix.py`` checks every cell against the runtime:
a cell that works must engage the feature, and a refused cell must produce
exactly the reason written here. ``docs/COMPATIBILITY.md`` is generated from
this module by ``scripts/build_compatibility_doc.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

Status = Literal["works", "refused"]


@dataclass(frozen=True)
class Profile:
    key: str
    title: str
    description: str


@dataclass(frozen=True)
class Feature:
    key: str
    title: str
    description: str
    doc: str


@dataclass(frozen=True)
class Cell:
    feature: str
    profile: str
    status: Status
    #: For a refused cell: the reason the run or the command gives, verbatim.
    reason: Optional[str] = None
    #: A short note for the table, such as a condition the cell needs.
    note: Optional[str] = None
    #: The tests that show the cell's behavior (``file::test``).
    evidence: tuple[str, ...] = ()


PROFILES: tuple[Profile, ...] = (
    Profile(
        "default",
        "Default (gated)",
        "`openadapt-flow run` with no `--profile`: a run authorization from the "
        "run gate, and per-item approval (`each_item`), the default approval mode.",
    ),
    Profile(
        "durable",
        "Durable",
        "`--durable`: a checkpoint after every verified step, and pause and "
        "resume from the last one.",
    ),
    Profile(
        "governed",
        "Governed",
        "The Standard or Regulated profile contract with a run authorization "
        "from the run gate: durable, an identity check on every save, and a "
        "record check that reads the system of record. This is how the Python "
        "API and the tutorial run it.",
    ),
    Profile(
        "pinned",
        "Pinned",
        "`--pin-digest`: the run refuses any bundle but the one with that "
        "sealed content digest.",
    ),
    Profile(
        "qualified",
        "Qualified",
        "A signed production qualification admission "
        "(`--qualification-authority-file`), which `openadapt-flow run` "
        "requires for Standard and Regulated actuation.",
    ),
    Profile(
        "managed",
        "Managed",
        "A Cloud-dispatched run (`--managed-dispatch-file`), whose run "
        "authorization comes from Cloud.",
    ),
)

FEATURES: tuple[Feature, ...] = (
    Feature(
        "api_step",
        "API step",
        "An admitted save compiled from network capture, run as one request in "
        "place of its GUI steps.",
        "API_TIER.md",
    ),
    Feature(
        "pipelined_readback",
        "Pipelined read-back",
        "`--readback-window`: an item's record check runs while the next item starts.",
        "THROUGHPUT.md",
    ),
    Feature(
        "self_repair",
        "Self-repair",
        "`--self-repair`: an agent proposes a fix for a halt on a changed "
        "screen; probes and a person's approval decide it.",
        "SELF_REPAIR.md",
    ),
    Feature(
        "standard_changes",
        "Standard changes",
        "Self-repair fixes of a class a person approved in advance, in a "
        "signed repair policy.",
        "SELF_REPAIR.md#standard-changes-repairs-without-a-person",
    ),
    Feature(
        "parallel_workers",
        "Parallel workers",
        "`--workers N`: worklist items on several signed-in sessions at once.",
        "THROUGHPUT.md",
    ),
    Feature(
        "multi_surface",
        "Multi-surface",
        "One program across several windows, tabs or apps.",
        "MULTI_SURFACE.md",
    ),
)

API_QUALIFIED = (
    "a production qualification admission doesn't cover API steps yet, so the "
    "qualified GUI path runs"
)
API_MANAGED = "a managed dispatch doesn't bind API steps yet"
DURABLE_INLINE = "durable runs verify inline"
POOL_LOCAL_ONLY = (
    "--workers and --readback-window apply to local runs, not a managed dispatch "
    "or a qualification case"
)
REPAIR_PINNED = "--pin-digest pins the original bundle"
REPAIR_QUALIFIED = (
    "a signed qualification admission binds the original bundle; the approved "
    "bundle needs its own admission"
)
REPAIR_MANAGED = "a managed dispatch binds the original bundle"

_REPAIR_EVIDENCE = (
    "tests/test_self_repair.py::test_renamed_button_is_repaired_approved_on_the_phone_and_verified",
)
_STANDARD_EVIDENCE = (
    "tests/test_standard_change.py::test_relabelled_link_is_repaired_without_a_person",
)

CELLS: tuple[Cell, ...] = (
    # -- API step ---------------------------------------------------------------
    Cell(
        "api_step",
        "default",
        "works",
        note="the person approves the exact request",
        evidence=(
            "tests/test_api_step_approval.py::test_the_person_approves_the_exact_request_and_it_is_sent_once",
            "tests/test_compatibility_matrix.py::test_api_step_runs_where_the_matrix_says",
        ),
    ),
    Cell(
        "api_step",
        "durable",
        "works",
        note="a save that may have landed is never resent",
        evidence=(
            "tests/test_api_step_durable.py::test_a_durable_run_uses_the_api_step_and_checkpoints_after_it",
            "tests/test_api_step_durable.py::test_an_uncertain_send_is_never_resent_by_resume",
        ),
    ),
    Cell(
        "api_step",
        "governed",
        "works",
        note="needs `api admit --identity`",
        evidence=(
            "tests/test_api_step_governed.py::test_the_default_profile_runs_the_admitted_api_step",
        ),
    ),
    Cell(
        "api_step",
        "pinned",
        "works",
        note="the pin is checked before the run",
        evidence=(
            "tests/test_compatibility_matrix.py::test_api_step_runs_where_the_matrix_says",
        ),
    ),
    Cell(
        "api_step",
        "qualified",
        "refused",
        reason=API_QUALIFIED,
        evidence=(
            "tests/test_compatibility_matrix.py::test_api_step_refuses_where_the_matrix_says",
        ),
    ),
    Cell(
        "api_step",
        "managed",
        "refused",
        reason=API_MANAGED,
        evidence=(
            "tests/test_compatibility_matrix.py::test_api_step_refuses_where_the_matrix_says",
        ),
    ),
    # -- pipelined read-back ------------------------------------------------------
    Cell(
        "pipelined_readback",
        "default",
        "works",
        note="GUI and API saves",
        evidence=(
            "tests/test_readback_pipeline.py::test_confirmed_readback_settles_the_saved_report",
            "tests/test_api_step_readback_repair.py::test_an_api_save_reads_back_while_the_next_item_runs",
            "tests/test_compatibility_matrix.py::test_pipelined_readback_where_the_matrix_says",
        ),
    ),
    Cell(
        "pipelined_readback",
        "durable",
        "refused",
        reason=DURABLE_INLINE,
        note="the record check runs before the next item",
        evidence=(
            "tests/test_compatibility_matrix.py::test_pipelined_readback_where_the_matrix_says",
        ),
    ),
    Cell(
        "pipelined_readback",
        "governed",
        "refused",
        reason=DURABLE_INLINE,
        note="Standard and Regulated are durable",
        evidence=(
            "tests/test_compatibility_matrix.py::test_pipelined_readback_where_the_matrix_says",
        ),
    ),
    Cell(
        "pipelined_readback",
        "pinned",
        "works",
        note="when the run isn't durable",
        evidence=(
            "tests/test_compatibility_matrix.py::test_pipelined_readback_where_the_matrix_says",
        ),
    ),
    Cell(
        "pipelined_readback",
        "qualified",
        "refused",
        reason=DURABLE_INLINE,
        note="Standard and Regulated are durable",
        evidence=(
            "tests/test_compatibility_matrix.py::test_pipelined_readback_where_the_matrix_says",
        ),
    ),
    Cell(
        "pipelined_readback",
        "managed",
        "refused",
        reason=POOL_LOCAL_ONLY,
        evidence=(
            "tests/test_compatibility_matrix.py::test_the_worklist_pool_where_the_matrix_says",
        ),
    ),
    # -- self-repair ---------------------------------------------------------------
    Cell(
        "self_repair",
        "default",
        "works",
        evidence=(
            "tests/test_self_repair_cli.py::test_run_self_repair_cli_resumes_after_one_phone_tap",
            "tests/test_compatibility_matrix.py::test_self_repair_where_the_matrix_says",
        ),
    ),
    Cell(
        "self_repair",
        "durable",
        "works",
        note="self-repair always runs durable",
        evidence=_REPAIR_EVIDENCE,
    ),
    Cell(
        "self_repair",
        "governed",
        "works",
        note="Standard contract through the Python API",
        evidence=_REPAIR_EVIDENCE,
    ),
    Cell(
        "self_repair",
        "pinned",
        "refused",
        reason=REPAIR_PINNED,
        evidence=(
            "tests/test_self_repair_cli.py::test_run_self_repair_refuses_a_pinned_bundle",
            "tests/test_compatibility_matrix.py::test_self_repair_where_the_matrix_says",
        ),
    ),
    Cell(
        "self_repair",
        "qualified",
        "refused",
        reason=REPAIR_QUALIFIED,
        evidence=(
            "tests/test_compatibility_matrix.py::test_self_repair_where_the_matrix_says",
        ),
    ),
    Cell(
        "self_repair",
        "managed",
        "refused",
        reason=REPAIR_MANAGED,
        evidence=(
            "tests/test_compatibility_matrix.py::test_self_repair_where_the_matrix_says",
        ),
    ),
    # -- standard changes -----------------------------------------------------------
    Cell(
        "standard_changes",
        "default",
        "works",
        note="needs a signed repair policy",
        evidence=_STANDARD_EVIDENCE,
    ),
    Cell(
        "standard_changes",
        "durable",
        "works",
        note="needs a signed repair policy",
        evidence=_STANDARD_EVIDENCE,
    ),
    Cell(
        "standard_changes",
        "governed",
        "works",
        note="needs a signed repair policy",
        evidence=_STANDARD_EVIDENCE,
    ),
    Cell(
        "standard_changes",
        "pinned",
        "refused",
        reason=REPAIR_PINNED,
        evidence=(
            "tests/test_compatibility_matrix.py::test_self_repair_where_the_matrix_says",
        ),
    ),
    Cell(
        "standard_changes",
        "qualified",
        "refused",
        reason=REPAIR_QUALIFIED,
        evidence=(
            "tests/test_compatibility_matrix.py::test_self_repair_where_the_matrix_says",
        ),
    ),
    Cell(
        "standard_changes",
        "managed",
        "refused",
        reason=REPAIR_MANAGED,
        evidence=(
            "tests/test_compatibility_matrix.py::test_self_repair_where_the_matrix_says",
        ),
    ),
    # -- parallel workers -----------------------------------------------------------
    Cell(
        "parallel_workers",
        "default",
        "works",
        evidence=(
            "tests/test_cli_worklist_pool.py::test_two_workers_split_the_items_one_row_each",
            "tests/test_compatibility_matrix.py::test_the_worklist_pool_where_the_matrix_says",
        ),
    ),
    Cell(
        "parallel_workers",
        "durable",
        "works",
        note="each item has its own durable run",
        evidence=(
            "tests/test_compatibility_matrix.py::test_workers_run_governed_durable_api_items",
        ),
    ),
    Cell(
        "parallel_workers",
        "governed",
        "works",
        note="each item gets its own single-use authorization",
        evidence=(
            "tests/test_compatibility_matrix.py::test_workers_run_governed_durable_api_items",
        ),
    ),
    Cell(
        "parallel_workers",
        "pinned",
        "works",
        evidence=(
            "tests/test_compatibility_matrix.py::test_the_worklist_pool_where_the_matrix_says",
        ),
    ),
    Cell(
        "parallel_workers",
        "qualified",
        "works",
        note="the admission is checked for every item",
        evidence=(
            "tests/test_compatibility_matrix.py::test_the_worklist_pool_where_the_matrix_says",
        ),
    ),
    Cell(
        "parallel_workers",
        "managed",
        "refused",
        reason=POOL_LOCAL_ONLY,
        evidence=(
            "tests/test_compatibility_matrix.py::test_the_worklist_pool_where_the_matrix_says",
        ),
    ),
    # -- multi-surface ----------------------------------------------------------------
    Cell(
        "multi_surface",
        "default",
        "works",
        note="an API step inside it runs on the GUI (below)",
        evidence=(
            "tests/test_multi_surface_governed.py::test_governed_durable_multi_surface_run_reaches_verified",
        ),
    ),
    Cell(
        "multi_surface",
        "durable",
        "works",
        note="resume re-binds each surface by identity",
        evidence=(
            "tests/test_multi_surface_identity.py::test_resume_rebinds_surfaces_by_identity",
        ),
    ),
    Cell(
        "multi_surface",
        "governed",
        "works",
        evidence=(
            "tests/test_multi_surface_governed.py::test_governed_durable_multi_surface_run_reaches_verified",
        ),
    ),
    Cell(
        "multi_surface",
        "pinned",
        "works",
        evidence=(
            "tests/test_multi_surface_governed.py::test_governed_durable_multi_surface_run_reaches_verified",
        ),
    ),
    Cell(
        "multi_surface",
        "qualified",
        "works",
        note="one environment boundary per surface",
        evidence=(
            "tests/test_multi_surface_governed.py::test_case_evidence_needs_a_matching_observation_per_used_surface",
        ),
    ),
    Cell(
        "multi_surface",
        "managed",
        "works",
        note="every backend surface must be bound",
        evidence=(
            "tests/test_multi_surface_contract.py::test_managed_dispatch_needs_every_backend_surface_bound",
        ),
    ),
)

#: The API step combined with the other features, in one run.
API_MULTI_SURFACE = "a run across several windows or tabs uses the GUI steps"
API_REPAIR_PROBE = (
    "self-repair's probes and resumed run exercise the repaired GUI step, and "
    "the person approved that step, so its API step doesn't run"
)

COMBINATIONS: tuple[Cell, ...] = (
    Cell(
        "api_step",
        "parallel_workers",
        "works",
        note="each item runs its own API step",
        evidence=(
            "tests/test_compatibility_matrix.py::test_workers_run_governed_durable_api_items",
        ),
    ),
    Cell(
        "api_step",
        "pipelined_readback",
        "works",
        note="when the run isn't durable",
        evidence=(
            "tests/test_api_step_readback_repair.py::test_an_api_save_reads_back_while_the_next_item_runs",
        ),
    ),
    Cell(
        "api_step",
        "self_repair",
        "refused",
        reason=API_REPAIR_PROBE,
        note="the first, ordinary run can use the API step",
        evidence=(
            "tests/test_api_step_readback_repair.py::test_a_probe_or_resumed_run_never_sends_the_api_step",
        ),
    ),
    Cell(
        "api_step",
        "standard_changes",
        "refused",
        reason=API_REPAIR_PROBE,
        note="the repaired bundle's test-record run is GUI-only too",
        evidence=(
            "tests/test_api_step_readback_repair.py::test_a_standard_change_test_record_run_is_gui_only",
        ),
    ),
    Cell(
        "api_step",
        "multi_surface",
        "refused",
        reason=API_MULTI_SURFACE,
        evidence=(
            "tests/test_compatibility_matrix.py::test_api_step_refuses_in_a_multi_surface_run",
        ),
    ),
)


def cell(feature: str, profile: str) -> Cell:
    """The cell for ``feature`` under ``profile``. Raises ``KeyError``."""

    for item in CELLS:
        if item.feature == feature and item.profile == profile:
            return item
    raise KeyError((feature, profile))


def _mark(item: Cell) -> str:
    if item.status == "works":
        text = "Works"
        if item.note:
            text += f": {item.note}"
        return text
    text = f"Refused: {item.reason}"
    if item.note:
        text += f" ({item.note})"
    return text


def render_markdown() -> str:
    """The body of ``docs/COMPATIBILITY.md``, generated from this module."""

    feature_titles = {f.key: f.title for f in FEATURES}
    lines = [
        "# Compatibility: features and run profiles",
        "",
        "<!-- Generated by scripts/build_compatibility_doc.py from",
        "     openadapt_flow/compatibility.py. Edit those, not this file. -->",
        "",
        "Each feature either works under a run profile or is refused with a",
        "named reason. Nothing falls back silently. When a run takes the slower or",
        "narrower path, it records why: an API step's `api_fallback`, a",
        "pipelined read-back's `inline_reason`, or the command's refusal before",
        "anything runs. `tests/test_compatibility_matrix.py` checks every cell",
        "against the runtime, and fails when a cell that should work falls back",
        "or a refusal gives another reason.",
        "",
        "## The widest combination today",
        "",
        "A Regulated, durable, governed run with per-item approval, on parallel",
        "workers, using the API step for an admitted save, is the combination",
        "where the most work together: each worker's item sends one request a",
        "person approved, checks it with the system-of-record read-back, and",
        "reads `VERIFIED`. In that run the read-back runs inline (durable), and",
        "self-repair runs through the Python API under the Standard contract.",
        "Under a signed qualification admission (what `openadapt-flow run`",
        "requires for Standard and Regulated actuation) the API step and",
        "self-repair are still refused, each with its reason below.",
        "",
        "## Profiles",
        "",
    ]
    for profile in PROFILES:
        lines.append(f"- **{profile.title}.** {profile.description}")
    lines += ["", "## Features", ""]
    for feature in FEATURES:
        lines.append(
            f"- **{feature.title}** ([{feature.doc.split('#')[0]}]({feature.doc})). "
            f"{feature.description}"
        )
    lines += [
        "",
        "## At a glance",
        "",
        "| Feature | " + " | ".join(p.title for p in PROFILES) + " |",
        "| --- | " + " | ".join("---" for _ in PROFILES) + " |",
    ]
    for feature in FEATURES:
        marks = [
            "Works" if cell(feature.key, p.key).status == "works" else "Refused"
            for p in PROFILES
        ]
        lines.append(f"| {feature.title} | " + " | ".join(marks) + " |")
    lines += [
        "",
        "Every refusal names its reason (below), and a run or command that",
        "refuses says it in those words.",
    ]
    lines += ["", "## Features by profile", ""]
    for feature in FEATURES:
        lines += [f"### {feature.title}", "", "| Profile | Result |", "| --- | --- |"]
        for profile in PROFILES:
            lines.append(
                f"| {profile.title} | {_mark(cell(feature.key, profile.key))} |"
            )
        lines.append("")
    lines += [
        "## The API step with the other features",
        "",
        "| With | Result |",
        "| --- | --- |",
    ]
    for item in COMBINATIONS:
        lines.append(f"| {feature_titles[item.profile]} | {_mark(item)} |")
    lines += [
        "",
        "## Evidence",
        "",
        "Each cell names the tests that show it. They run in CI.",
        "",
    ]
    for item in (*CELLS, *COMBINATIONS):
        against = next(
            (p.title for p in PROFILES if p.key == item.profile),
            feature_titles.get(item.profile, item.profile),
        )
        tests = ", ".join(f"`{t}`" for t in item.evidence)
        lines.append(f"- {feature_titles[item.feature]} / {against}: {tests}")
    lines.append("")
    return "\n".join(lines)
