"""Full execution traces plus the reward, for an outer-loop proposer.

Meta-Harness (arXiv:2603.28052) gives its proposer a filesystem holding every
earlier candidate's code, full execution traces and scores, and reports that
full traces beat summaries (56.7 vs 38.7 on its text-classification ablation).
This module writes that filesystem for GUI work. Each trace keeps every step
in full: what the screen showed, the action, the reason given, any error, and,
for each write, whether the record on screen was the requested one. Each
trace ends with the read-back: which records the system of record gained,
whose they are, the outcome and the reward.

Two sources feed it:

* an agent rollout directory (``rollout.run_rollout``), with its task, so the
  write steps can be checked against the requested patient;
* a compiled program's run directory (``report.json``, a Flow ``RunReport``),
  whose steps already carry the runtime's identity-gate verdicts and effect
  checks.

Layout (schema ``openadapt.trace-export/v1``)::

    <out>/INDEX.md                      one line per trace, best reward first
    <out>/index.jsonl                   the same, one JSON object per trace
    <out>/candidates/<name>/harness/    the harness each candidate ran, if given
    <out>/traces/<trace_id>/trace.md    the full trace, readable
    <out>/traces/<trace_id>/trace.json  the full trace, structured

Screen text goes through :func:`openadapt_flow.privacy.scrub_text` before it is
written, like ``REPORT.md``. The identity verdicts are computed before
scrubbing, so they survive it.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from openadapt_flow import privacy
from openadapt_flow.integrations.harness2.rollout import (
    load_run,
    load_score,
    load_steps,
)
from openadapt_flow.integrations.harness2.scorer import EpisodeScore
from openadapt_flow.integrations.harness2.tasks import GuiTask

SCHEMA = "openadapt.trace-export/v1"

#: A step whose control label matches this is treated as a write.
_WRITE_LABEL = re.compile(r"^\s*(save|submit|sign|confirm|finali[sz]e|post)\b", re.I)
_BANNER = re.compile(
    r"^(?P<name>[^\n—]+?) — MRN (?P<mrn>\S+) — DOB (?P<dob>[0-9-]+)", re.M
)


@dataclass
class IdentityVerdict:
    """Was the record on screen the requested one when a write happened?"""

    status: str  # verified | mismatch | unreadable
    mode: str  # agent_screen | <runtime identity mode>
    expected: str = ""
    observed: str = ""


@dataclass
class TraceStep:
    index: int
    action: str
    observation: str = ""
    reason: str = ""
    error: str = ""
    ok: Optional[bool] = None
    write: bool = False
    identity: Optional[IdentityVerdict] = None
    effect: str = ""


@dataclass
class Trace:
    trace_id: str
    source: str  # agent_rollout | flow_run
    task_id: str
    candidate: str
    outcome: str
    reward: Optional[float]
    passed: Optional[bool]
    steps: list[TraceStep] = field(default_factory=list)
    readback: dict[str, Any] = field(default_factory=dict)
    report: str = ""
    cost_usd: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"schema": SCHEMA, **asdict(self)}

    def summary(self) -> dict[str, Any]:
        writes = [s for s in self.steps if s.write]
        return {
            "schema": SCHEMA,
            "trace_id": self.trace_id,
            "source": self.source,
            "task_id": self.task_id,
            "candidate": self.candidate,
            "outcome": self.outcome,
            "reward": self.reward,
            "passed": self.passed,
            "steps": len(self.steps),
            "writes": len(writes),
            "identity_mismatches": sum(
                1 for s in writes if s.identity and s.identity.status == "mismatch"
            ),
            "wrong_record": bool(self.readback.get("wrong_record")),
            "duplicate": bool(self.readback.get("duplicate")),
            "cost_usd": self.cost_usd,
        }


# -- sources ---------------------------------------------------------------------


def screen_identity(observation: str, task: GuiTask) -> IdentityVerdict:
    """Identity at a write, from the patient banner the agent was looking at."""

    want_name = task.params.get("patient_name", "")
    want_dob = task.params.get("dob", "")
    expected = f"{want_name} — DOB {want_dob}".strip(" —")
    m = _BANNER.search(observation)
    if not m:
        return IdentityVerdict("unreadable", "agent_screen", expected, "")
    observed = f"{m.group('name').strip()} — DOB {m.group('dob')}"
    same = m.group("name").strip() == want_name and (
        not want_dob or m.group("dob") == want_dob
    )
    return IdentityVerdict(
        "verified" if same else "mismatch", "agent_screen", expected, observed
    )


def _control_label(observation: str, element: Any) -> str:
    m = re.search(rf"^\[{re.escape(str(element))}\] \w+ '([^']*)'", observation, re.M)
    return m.group(1) if m else ""


def from_rollout(run_dir: Path, task: GuiTask, *, candidate: str = "") -> Trace:
    """A trace from one agent rollout directory."""

    run_dir = Path(run_dir)
    run = load_run(run_dir)
    score = load_score(run_dir)
    steps: list[TraceStep] = []
    for raw in load_steps(run_dir):
        action = dict(raw.get("action") or {})
        obs = str(raw.get("observation", ""))
        kind = action.get("action")
        label = _control_label(obs, action.get("element")) if kind == "click" else ""
        write = bool(label and _WRITE_LABEL.search(label))
        steps.append(
            TraceStep(
                index=int(raw.get("index", len(steps))),
                action=_rollout_action(action, label),
                observation=_scrub(obs),
                reason=_scrub(str(raw.get("thought", ""))),
                error=str(raw.get("error", "")),
                write=write,
                identity=_scrubbed(screen_identity(obs, task)) if write else None,
            )
        )
    return Trace(
        trace_id=str(run.get("run_id") or run_dir.name),
        source="agent_rollout",
        task_id=task.task_id,
        candidate=candidate,
        outcome=score.outcome,
        reward=score.reward,
        passed=score.passed,
        steps=steps,
        readback=_readback(score),
        report=_scrub(str(run.get("report", ""))),
        cost_usd=float(run.get("agent_cost_usd", 0.0)),
    )


def from_flow_run(
    run_dir: Path,
    *,
    task_id: str = "",
    candidate: str = "",
    score: Optional[EpisodeScore] = None,
) -> Trace:
    """A trace from a compiled program's run directory (``report.json``).

    Identity verdicts are the runtime's own (``StepResult.identity``). A
    read-back from the reward worker can be attached with ``score``; without
    it the trace carries the run's own effect verdicts.
    """

    from openadapt_flow.ir import RunReport

    run_dir = Path(run_dir)
    report = RunReport.model_validate_json((run_dir / "report.json").read_text("utf-8"))
    steps: list[TraceStep] = []
    for i, r in enumerate(report.results):
        ident = None
        if r.identity is not None:
            ident = IdentityVerdict(
                r.identity.status,
                r.identity.mode,
                _scrub(r.identity.expected),
                _scrub(r.identity.observed),
            )
        effect = ""
        if r.effect_verified is not None:
            effect = "confirmed" if r.effect_verified else "refuted"
            if r.effect_results:
                effect += ": " + "; ".join(r.effect_results)[:300]
        steps.append(
            TraceStep(
                index=i,
                action=f"{r.step_id}: {r.intent}",
                error=r.error or "",
                ok=r.ok,
                write=r.risk == "irreversible" or r.effect_verified is not None,
                identity=ident,
                effect=effect,
            )
        )
    outcome = score.outcome if score else str(report.execution_outcome or "")
    return Trace(
        trace_id=f"{report.workflow_name}-{report.started_at}",
        source="flow_run",
        task_id=task_id or report.workflow_name,
        candidate=candidate,
        outcome=outcome,
        reward=score.reward if score else None,
        passed=score.passed if score else (report.execution_outcome == "VERIFIED"),
        steps=steps,
        readback=_readback(score) if score else {},
    )


def _rollout_action(action: dict[str, Any], label: str) -> str:
    kind = action.get("action")
    if kind == "click":
        return f"click [{action.get('element')}] {label!r}".rstrip()
    if kind == "type":
        return f"type into [{action.get('element')}]: {_scrub(str(action.get('text', '')))!r}"
    if kind == "done":
        return f"done status={action.get('status')}: {_scrub(str(action.get('report', '')))}"
    return str(kind)


def _readback(score: EpisodeScore) -> dict[str, Any]:
    return {
        "outcome": score.outcome,
        "reason": score.reason,
        "added": [
            {k: (_scrub(str(v)) if k == "note" else v) for k, v in r.items()}
            for r in score.added
        ],
        "subject_added": score.subject_added,
        "other_added": score.other_added,
        "wrong_record": score.wrong_record,
        "duplicate": score.duplicate,
        "safe_stop": score.safe_stop,
        "reward": score.reward,
        "unscored": score.unscored,
        "receipt_id": score.receipt_id,
        "oracle_tier": score.oracle_tier,
    }


def _scrubbed(verdict: IdentityVerdict) -> IdentityVerdict:
    verdict.expected = _scrub(verdict.expected)
    verdict.observed = _scrub(verdict.observed)
    return verdict


def _scrub(text: str) -> str:
    return privacy.scrub_text(text) or ""


# -- rendering -------------------------------------------------------------------


def render(trace: Trace) -> str:
    """The full trace as text. Nothing is summarised away."""

    head = [
        f"# Trace {trace.trace_id}",
        "",
        f"- task: {trace.task_id}",
        f"- candidate: {trace.candidate or '(none)'}",
        f"- source: {trace.source}",
        f"- outcome: {trace.outcome}",
        f"- reward: {trace.reward if trace.reward is not None else 'none (a person settles it)'}",
        "",
        "## Steps",
    ]
    body: list[str] = []
    for s in trace.steps:
        lines = [f"### Step {s.index}" + (" (write)" if s.write else "")]
        if s.observation:
            lines.append("Screen:\n```\n" + s.observation + "\n```")
        lines.append(f"Action: {s.action}")
        if s.reason:
            lines.append(f"Reason given: {s.reason}")
        if s.ok is not None:
            lines.append(f"Step ok: {s.ok}")
        if s.identity:
            i = s.identity
            lines.append(
                f"Identity at this step: {i.status} ({i.mode}); expected "
                f"{i.expected or '?'}; on screen {i.observed or 'nothing readable'}"
            )
        if s.effect:
            lines.append(f"Effect check: {s.effect}")
        if s.error:
            lines.append(f"Error: {s.error}")
        body.append("\n".join(lines))
    tail = ["## Read-back from the system of record"]
    rb = trace.readback
    if not rb:
        tail.append("No read-back was attached.")
    else:
        if not rb["added"]:
            tail.append("- no record was added")
        for r in rb["added"]:
            tail.append(
                f"- added: patient {r.get('patient_id')}, type {r.get('type')}, "
                f"note {r.get('note')!r}"
            )
        tail.append(
            f"- the requested patient gained {rb['subject_added']} record(s); "
            f"other patients gained {rb['other_added']}"
        )
        tail.append(f"- outcome: {rb['outcome']} ({rb['reason'][:300]})")
        tail.append(f"- receipt: {rb['receipt_id']} (oracle tier {rb['oracle_tier']})")
    if trace.report:
        tail += ["", "## The agent's own report", trace.report]
    return "\n\n".join(["\n".join(head), *body, "\n".join(tail)]) + "\n"


def export(
    out_dir: Path,
    traces: Sequence[Trace],
    *,
    harnesses: Optional[dict[str, Path]] = None,
) -> Path:
    """Write the Meta-Harness-style filesystem. Replaces ``out_dir``'s traces."""

    out_dir = Path(out_dir)
    (out_dir / "traces").mkdir(parents=True, exist_ok=True)
    for name, src in sorted((harnesses or {}).items()):
        dest = out_dir / "candidates" / _safe(name) / "harness"
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest)
    rows = []
    for trace in traces:
        d = out_dir / "traces" / _safe(trace.trace_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "trace.md").write_text(render(trace), encoding="utf-8")
        (d / "trace.json").write_text(
            json.dumps(trace.to_dict(), indent=2), encoding="utf-8"
        )
        rows.append(trace.summary())
    rows.sort(
        key=lambda r: (
            -(r["reward"] if r["reward"] is not None else -0.01),
            r["trace_id"],
        )
    )
    (out_dir / "index.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )
    lines = [
        "# Traces",
        "",
        "Every trace is complete: each screen, action, reason and error, the identity",
        "check at each write, and the read-back from the system of record.",
        "",
        "| trace | task | candidate | outcome | reward | wrong record | duplicate |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| [{r['trace_id']}](traces/{_safe(r['trace_id'])}/trace.md) | {r['task_id']} | "
            f"{r['candidate'] or '-'} | {r['outcome']} | "
            f"{r['reward'] if r['reward'] is not None else '-'} | "
            f"{'yes' if r['wrong_record'] else ''} | {'yes' if r['duplicate'] else ''} |"
        )
    (out_dir / "INDEX.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_dir


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_") or "trace"


def from_rollouts(
    run_dirs: Iterable[tuple[Path, GuiTask, str]],
) -> list[Trace]:
    """Traces for ``(run_dir, task, candidate)`` triples."""

    return [from_rollout(d, t, candidate=c) for d, t, c in run_dirs]


__all__ = [
    "IdentityVerdict",
    "SCHEMA",
    "Trace",
    "TraceStep",
    "export",
    "from_flow_run",
    "from_rollout",
    "from_rollouts",
    "render",
    "screen_identity",
]
