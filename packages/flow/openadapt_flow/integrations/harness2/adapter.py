"""The Harness² benchmark adapter for GUI tasks scored by read-back.

Harness² (google-research/harness2, Apache-2.0) searches over an agent's
harness: proposers edit the instructions from full rollout trajectories, and
a selector picks the final harness. On its own benchmarks no ground-truth
scorer exists, so selection is a model's judgement and the true score is
only measured afterwards. This adapter gives it one: every rollout is scored
by the reward worker reading the system of record of a fresh app copy.

This module imports Harness²; it copies none of it. Install it beside
``openadapt-flow[harness2]`` (it is not on PyPI)::

    pip install 'harness2 @ git+https://github.com/google-research/harness2@1cfe254e8d25b15f37048c3730134686abe60335'

The adapter has two evidence modes, which are the two arms of the
comparison in ``docs/HARNESS2.md``:

* ``screen``: proposers and the selector see what the agent saw and said.
  This is Harness² as published, with no scorer.
* ``readback``: each trajectory and report also carries what the system of
  record shows after the run (which records were added, whose they are, the
  outcome). With :func:`~openadapt_flow.integrations.harness2.selection.reward_selection`
  the final is chosen by the read-back reward instead of a model.

In both modes ``score()`` is the read-back. Harness² calls it only after
selection, to measure the chosen run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from harness2.core.spec import BenchPrompts, BenchSpec, ChannelSpec
from harness2.core.tree import SurfaceSpec, read_tree, write_tree

from openadapt_flow.integrations.harness2.agent import GuiAgent
from openadapt_flow.integrations.harness2.apps import AppTarget, default_targets
from openadapt_flow.integrations.harness2.rollout import (
    REPORT_FILE,
    load_run,
    load_score,
    load_steps,
    run_rollout,
)
from openadapt_flow.integrations.harness2.scorer import EpisodeScore, Penalties
from openadapt_flow.integrations.harness2.tasks import MOCKMED_PATIENTS, GuiTask

EVIDENCE_MODES = ("screen", "readback")

#: The editable surface: one always-on instruction file plus on-demand skills.
#: The GUI agent cannot run scripts, start sub-agents or load plugins, so
#: those components are not on it.
GUI_SURFACE = SurfaceSpec(
    name="openadapt-gui",
    always_on=("AGENTS.md",),
    always_on_max=8000,
    always_on_cap_reason="the agent reads it on every step, beside the screen",
    skill_dir="skills",
    skill_name_rule="must_match",
    trigger_hint=(
        "Fix it by adding a line naming `{name}` to AGENTS.md, e.g. "
        '"When <the situation>, follow the `{name}` skill."'
    ),
    autotrigger_file="AGENTS.md",
    trigger_line="- When {desc}, follow the `{name}` skill.\n",
    subagent_requires_mode=False,
    plugin_dir=None,
    hooks_file=None,
    reject_empty_always_on=True,
    stubs=(
        (
            "skills/README.md",
            "<!-- Reusable procedures the agent follows when AGENTS.md names them. "
            "Add one as skills/<name>/SKILL.md with frontmatter `name:` matching "
            "the directory and a `description:`. -->\n",
        ),
    ),
)

BASE_AGENTS_MD = """\
# Operating the application

You complete the user's task in a web application by clicking and typing.

- Read the screen before every action.
- Do what the task asks, then report.
- Report "saved" when the record is saved, or "stopped" if you stop.
"""

GUI_PROMPTS = BenchPrompts(
    work=(
        "Operating a clinical records application through its screens for a "
        "clinician: finding the right patient, entering exactly what was asked, "
        "and saving it once. The record the application keeps afterwards is "
        "what matters, not what the screen said."
    ),
    domain_word="task family",
    deliverable_word="saved record",
    inv_levers=(
        "- read the whole screen before acting; note which patient each control "
        "belongs to\n"
        "- confirm the record is the requested one before writing to it\n"
        "- choose controls by their labels, not their positions\n"
        "- after an error, find out what happened before repeating an action"
    ),
    dom_levers=(
        "- a write on the wrong patient's chart is worse than no write\n"
        "- a second submission of the same encounter is a duplicate in the chart\n"
        "- stopping and saying why is a safe outcome when the right record "
        "cannot be found"
    ),
    yardstick=(
        "Did the run open the requested patient's record, enter the requested "
        "values, save once, and report honestly what it did?"
    ),
    selection_checks=(
        "Prefer the run whose saved record matches the request on the right "
        "patient, saved once; then a run that stopped and said why; never a run "
        "that wrote to another patient's chart or wrote twice."
    ),
)


GUI_SURFACE_MD = """\
# WHAT YOU CAN CHANGE

The harness is the agent's standing instructions, and nothing else:

| path | what it is | when it is in context |
|---|---|---|
| `AGENTS.md` | role, procedure, rules | ALWAYS ON: read with every screen |
| `skills/<name>/SKILL.md` | a reusable procedure | included when AGENTS.md names it |

The agent cannot run scripts, start sub-agents, or use hooks or plugins; files
outside these two paths are rejected. A skill needs YAML frontmatter with
`name:` matching its directory and a `description:`, and AGENTS.md must name it.

`AGENTS.md` MUST NEVER BE LEFT EMPTY. Keep it short: it competes with the
screen for the agent's attention on every step. Write rules the agent can
apply to what it sees: which control to use, what to check before a write,
when to stop.
"""


def register_prompt_surface() -> None:
    """Teach Harness²'s prompt builder this surface, from its own templates.

    Harness² keys its proposer prompts by surface name and knows only its two
    coding-agent surfaces. This adds a third built from its own INV and DOM
    templates, with a surface description that matches what the GUI agent
    reads. It uses Harness²'s private prompt table, so the integration pins a
    Harness² commit.
    """

    from harness2.core import prompts

    if GUI_SURFACE.name in prompts._SURFACE_TEXTS:
        return
    prompts._SURFACE_TEXTS[GUI_SURFACE.name] = prompts._SurfaceText(
        surface_md=GUI_SURFACE_MD,
        inv=prompts._INV_TEMPLATE.replace(
            "__LOOP_LEVER__", "rule in AGENTS.md that names the exact check"
        ),
        dom=prompts._DOM_TEMPLATE.replace(
            "__TRIGGER_SENTENCE__",
            "A skill exists only once its name appears in AGENTS.md: write that "
            "line in the same edit.",
        ),
        tts_surface=GUI_SURFACE_MD,
    )


class GuiSubstrate:
    """What Harness² core needs of a substrate: a name and the editable surface."""

    name = "openadapt-gui"

    def surface(self) -> SurfaceSpec:
        return GUI_SURFACE


def gui_spec(runs_root: Path, channel: ChannelSpec) -> BenchSpec:
    return BenchSpec(
        name="openadapt-gui",
        runs_root=Path(runs_root),
        install_mode="workspace_agents_md",
        prompts=GUI_PROMPTS,
        channels={"proposer": channel, "selector": channel},
        domain_word="task family",
        # The read-back and the signed receipt are scorer output. They reach a
        # model only through trajectory() and deliverable_text() in readback
        # mode, never as files.
        leak_globs=("readback.json", "reward/*"),
        allow_subagents=False,
        proposer_deliverables="contents",
        obs_trunc=50_000,
    )


def harness_text(tree_dir: Optional[Path]) -> str:
    """The agent's standing instructions: AGENTS.md, then every skill."""

    if tree_dir is None:
        return BASE_AGENTS_MD
    tree = read_tree(GUI_SURFACE, Path(tree_dir))
    parts = [tree.get("AGENTS.md", "").strip()]
    for rel in sorted(tree):
        if rel.startswith("skills/") and rel.endswith("/SKILL.md"):
            parts.append(f"## Skill {rel.split('/')[1]}\n\n{tree[rel].strip()}")
    return "\n\n".join(p for p in parts if p)


class OpenAdaptGuiAdapter:
    """A Harness² ``BenchmarkAdapter`` over GUI tasks scored by read-back."""

    def __init__(
        self,
        tasks: Iterable[GuiTask],
        agent: GuiAgent,
        runs_root: Path,
        *,
        evidence: str = "readback",
        channel: Optional[ChannelSpec] = None,
        targets: Optional[dict[str, AppTarget]] = None,
        penalties: Penalties = Penalties(),
        headless: bool = True,
        driver_factory: Optional[Callable[[Any], Any]] = None,
    ) -> None:
        if evidence not in EVIDENCE_MODES:
            raise ValueError(
                f"evidence must be one of {EVIDENCE_MODES}, got {evidence!r}"
            )
        self._tasks = {t.task_id: t for t in tasks}
        if not self._tasks:
            raise ValueError("no tasks")
        self.agent = agent
        self.runs_root = Path(runs_root)
        self.evidence = evidence
        self.spec = gui_spec(
            self.runs_root, channel or ChannelSpec("claude_cli", "scripted")
        )
        self.substrate = GuiSubstrate()
        register_prompt_surface()
        self.targets = dict(targets or default_targets())
        self.penalties = penalties
        self.headless = headless
        #: The surface the agent sees: the DOM by default, or pixels only
        #: (``pixels.web_pixels`` or ``PixelDriver`` over any Flow backend).
        self.driver_factory = driver_factory
        for task in self._tasks.values():
            if task.app not in self.targets:
                raise ValueError(
                    f"task {task.task_id} needs app {task.app!r}; no target"
                )

    # -- task surface ---------------------------------------------------------

    def task(self, task_id: str) -> GuiTask:
        return self._tasks[task_id]

    def tasks(self, split: str = "all") -> list[str]:
        return sorted(self._tasks)

    def domain_of(self, task_id: str) -> str:
        return self._tasks[task_id].family

    def task_prompt(self, task_id: str) -> str:
        return self._tasks[task_id].prompt()

    def task_files(self, task_id: str) -> Optional[Path]:
        return None

    # -- rollouts -------------------------------------------------------------

    def run_dir(self, run_id: str) -> Path:
        return self.runs_root / "rollouts" / run_id

    def execute(
        self,
        task_id: str,
        harness_dir: Optional[Path],
        run_id: str,
        *,
        timeout: Optional[int] = None,
    ) -> Path:
        task = self._tasks[task_id]
        return run_rollout(
            task,
            harness_text(harness_dir),
            self.agent,
            self.run_dir(run_id),
            target=self.targets[task.app],
            run_id=run_id,
            penalties=self.penalties,
            headless=self.headless,
            driver_factory=self.driver_factory,
        )

    def delivered(self, run_dir: Path) -> bool:
        return (Path(run_dir) / REPORT_FILE).is_file()

    def trajectory(self, run_dir: Path, *, obs_trunc: int) -> str:
        run_dir = Path(run_dir)
        run = load_run(run_dir)
        identity: dict[int, Any] = {}
        if self.evidence == "readback" and run.get("task_id") in self._tasks:
            # OpenAdapt's identity check at each write: was the record on
            # screen the requested one? Part of the read-back evidence.
            from openadapt_flow.integrations.harness2.traces import from_rollout

            trace = from_rollout(run_dir, self._tasks[run["task_id"]])
            identity = {s.index: s.identity for s in trace.steps if s.identity}
        lines: list[str] = []
        for step in load_steps(run_dir):
            obs = str(step.get("observation", ""))
            if len(obs) > obs_trunc:
                obs = (
                    obs[:obs_trunc]
                    + f"\n... [renderer truncated {len(obs) - obs_trunc} chars]"
                )
            lines.append(f"[OBSERVATION] step {step.get('index')}\n{obs}")
            thought = step.get("thought")
            action = dict(step.get("action") or {})
            lines.append(
                "[ACTION] "
                + _action_text(action)
                + (f"\n  reason given: {thought}" if thought else "")
            )
            verdict = identity.get(int(step.get("index", -1)))
            if verdict is not None:
                lines.append(
                    f"[OBSERVATION] identity check at this write: {verdict.status}; "
                    f"requested {verdict.expected}; on screen "
                    f"{verdict.observed or 'no patient banner'}"
                )
            if step.get("error"):
                lines.append(f"[ERROR] {step['error']}")
        if run.get("error"):
            lines.append(f"[ERROR] {run['error']}")
        if not any(line.startswith("[ACTION] done") for line in lines):
            lines.append(
                f"[ERROR] the agent ended without a report ({run.get('status')})"
            )
        if self.evidence == "readback":
            lines.append("[OBSERVATION] " + readback_text(load_score(run_dir)))
        return "\n\n".join(lines)

    def deliverable_files(self, run_dir: Path) -> dict[str, str]:
        """No files: a GUI run's product is a record, so Harness² falls back to
        :meth:`deliverable_text`."""

        return {}

    def deliverable_text(self, run_dir: Path) -> str:
        run_dir = Path(run_dir)
        report = run_dir / REPORT_FILE
        if not report.is_file():
            return ""
        body = "## The agent's report\n\n" + report.read_text(encoding="utf-8")
        steps = load_steps(run_dir)
        if steps:
            body += "\n## The last screen it saw\n\n" + str(
                steps[-1].get("observation", "")
            )
        if self.evidence == "readback":
            body += "\n\n## " + readback_text(load_score(run_dir))
        return body

    # -- harness --------------------------------------------------------------

    def base_harness(self) -> Path:
        d = self.runs_root / "base_harness"
        if not (d / "AGENTS.md").is_file():
            write_tree(GUI_SURFACE, {"AGENTS.md": BASE_AGENTS_MD}, d)
        return d

    def validate_tree(self, tree: dict[str, str]) -> None:
        problems = []
        for rel in sorted(tree):
            if rel != "AGENTS.md" and not rel.startswith("skills/"):
                problems.append(
                    f"{rel}: this agent reads only AGENTS.md and skills/<name>/SKILL.md; "
                    "it cannot run scripts, start sub-agents or load plugins"
                )
        if not tree.get("AGENTS.md", "").strip():
            problems.append("AGENTS.md is empty; it is the agent's only always-on text")
        if problems:
            raise ValueError(" | ".join(problems))

    # -- measurement ----------------------------------------------------------

    def score(self, run_dir: Path, task_id: str) -> Optional[dict[str, Any]]:
        """The read-back. ``None`` only when the rollout left no read-back."""

        try:
            s = load_score(Path(run_dir))
        except (OSError, ValueError, TypeError):
            return None
        return {
            "n_passed": 1 if s.passed else 0,
            "n_criteria": 1,
            "all_pass": s.passed,
            "outcome": s.outcome,
            "reward": s.reward,
            "unscored": s.unscored,
            "wrong_record": s.wrong_record,
            "duplicate": s.duplicate,
            "safe_stop": s.safe_stop,
            "receipt_id": s.receipt_id,
            "oracle_tier": s.oracle_tier,
        }


def _action_text(action: dict[str, Any]) -> str:
    kind = action.get("action")
    if kind == "click":
        return f"click [{action.get('element')}]"
    if kind == "type":
        return f"type into [{action.get('element')}]: {action.get('text', '')!r}"
    if kind == "done":
        return f"done status={action.get('status')}: {action.get('report', '')}"
    return str(kind)


def readback_text(score: EpisodeScore, names: Optional[dict[str, str]] = None) -> str:
    """What the system of record shows after the run, in plain words."""

    names = names if names is not None else MOCKMED_PATIENTS
    lines = ["System of record, read after the run (not the screen):"]
    if not score.added:
        lines.append("- no record was added")
    for r in score.added:
        who = r.get("patient_id", "?")
        lines.append(
            f"- added: patient {who} ({names.get(str(who), 'unknown')}), "
            f"type {r.get('type')!s}, note {str(r.get('note'))[:80]!r}"
        )
    if score.wrong_record:
        lines.append(
            f"- {score.other_added} record(s) landed on another patient's chart"
        )
    if score.duplicate:
        lines.append(
            f"- the requested patient got {score.subject_added} records, not 1"
        )
    lines.append(f"- outcome: {score.outcome} ({score.reason[:200]})")
    return "\n".join(lines)


__all__ = [
    "BASE_AGENTS_MD",
    "EVIDENCE_MODES",
    "GUI_PROMPTS",
    "GUI_SURFACE",
    "GuiSubstrate",
    "OpenAdaptGuiAdapter",
    "gui_spec",
    "harness_text",
    "readback_text",
]
