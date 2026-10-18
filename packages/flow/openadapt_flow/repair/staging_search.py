"""Search for a multi-step bridge on a staging copy, scored by read-back.

Self-repair (:mod:`openadapt_flow.repair.self_repair`) probes each proposed
fix up to the last point before the save and stops there. That proves the
bridge reaches the save on the right record. It cannot show what the save
writes. Two bridges can look the same up to the save and still write
different records: one menu path files the encounter as Triage, another as
Consult.

:class:`StagingBridgeSearch` closes that gap without touching production.
For each candidate bridge, on a resettable staging copy of the app, it:

1. runs self-repair itself with that one bridge (halt, patch, probe,
   campaigns), stopping at the approval request, which nobody answers;
2. runs the patched bundle end to end on a fresh copy, through the save;
3. reads the copy's system of record with the reward worker and scores it.

The best candidate that the read-back verifies becomes the one proposal
:class:`StagingSearchAgent` hands to the production self-repair. From there
nothing changes: production probes it before the save, runs the campaigns,
and a person approves it before anything is saved
(candidate -> replay_passed -> fault_passed -> approved -> staged -> canary
-> active). The search never opens a production session.

Candidates come from a :class:`BridgeProposer`: :class:`ScriptedBridges` for
tests, :class:`ModelBridges` for a model that reads the halted trace and the
last verified traces (:mod:`openadapt_flow.integrations.harness2.traces`).
"""

from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

from openadapt_flow.integrations.harness2 import traces
from openadapt_flow.integrations.harness2.rollout import drop_worker_secrets
from openadapt_flow.integrations.harness2.scorer import (
    EpisodeScore,
    Penalties,
    ReadBackScorer,
)
from openadapt_flow.integrations.harness2.tasks import GuiTask
from openadapt_flow.ir import RunReport, Workflow
from openadapt_flow.repair.self_repair import (
    RepairProposal,
    RepairRequest,
    ScreenAccess,
    SelfRepair,
    SelfRepairTarget,
)

#: Runtime outcome -> the runtime signal the reward worker takes.
_SIGNAL = {
    "VERIFIED": "completed",
    "COMPLETED_UNVERIFIED": "completed",
    "HALTED": "halted_before_effect",
    "ROLLED_BACK": "halted_before_effect",
    "FAILED": "failed_platform",
}


@dataclass(frozen=True)
class StagingCopy:
    """A resettable copy of the app that the search may write to.

    ``target`` opens sessions on the copy only. ``reset`` empties the copy's
    store. ``oracle_recipe`` is how the reward worker reads that store.
    ``identity`` is the item's subject (for MockMed, ``{"patient_id": "p1"}``)
    and ``record`` the values the item's save must write.
    """

    target: SelfRepairTarget
    reset: Callable[[], None]
    oracle_recipe: Mapping[str, Any]
    identity: Mapping[str, str]
    record: Mapping[str, str]
    fault_probes: Sequence[str] = ()


class BridgeProposer(Protocol):
    """Proposes candidate bridges for one halted step, best guess first."""

    dialect: str
    model_calls: int
    cost_usd: float

    def bridges(self, request: RepairRequest, history: str) -> list[RepairProposal]: ...


class ScriptedBridges:
    """Fixed candidates, for tests. No model calls."""

    model_calls = 0
    cost_usd = 0.0

    def __init__(self, *bridges: Sequence[Mapping[str, Any]], dialect: str = "generic"):
        self._bridges = [list(b) for b in bridges]
        self.dialect = dialect
        self.seen_history = ""

    def bridges(self, request: RepairRequest, history: str) -> list[RepairProposal]:
        self.seen_history = history
        return [RepairProposal(actions=b) for b in self._bridges]


_BRIDGES_PROMPT = """\
A compiled GUI program halted at one step because the screen changed. Propose
up to {n} different bridges: each is 1 to {max_actions} clicks, by the visible
label of the control, that would reach the step's goal on the current screen.
The last click must do what the halted step did.

Halted step: {intent}
Recorded control label: {label}
Why it halted: {reason}
Mode: {mode} ({mode_help})

Visible text on the current screen, one control per line:
{screen}

Earlier runs of the same item, the halted one first:
{history}

Reply with JSON only: {{"bridges": [["label", "label", ...], ...]}}
"""


class ModelBridges:
    """A model proposes bridges as control labels; OCR maps labels to points.

    The model reads the halted step, the controls OCR finds on the live
    screen, and the full traces of the halted run and the last verified runs.
    It answers with labels, which keeps it from inventing coordinates.
    """

    dialect = "generic"

    def __init__(self, model: Any, *, n: int = 3, max_actions: int = 3) -> None:
        self.model = model
        self.n = n
        self.max_actions = max_actions
        self.model_calls = 0
        self.cost_usd = 0.0

    def bridges(self, request: RepairRequest, history: str) -> list[RepairProposal]:
        from openadapt_flow.vision.ocr import ocr

        lines = ocr(request.live_png)
        points = {
            _squash(ln.text): (
                int(ln.region[0] + ln.region[2] / 2),
                int(ln.region[1] + ln.region[3] / 2),
            )
            for ln in lines
            if ln.text.strip()
        }
        prompt = _BRIDGES_PROMPT.format(
            n=self.n,
            max_actions=min(self.max_actions, request.max_actions),
            intent=request.intent,
            label=request.recorded_label or "(none)",
            reason=request.halt_reason[:500],
            mode=request.mode,
            mode_help=(
                "the control is gone; the last click replaces it"
                if request.mode == "replace"
                else "the click acted but a new screen appeared; the bridge continues after it"
            ),
            screen="\n".join(ln.text for ln in lines if ln.text.strip()),
            history=history[:20_000] or "(none)",
        )
        reply = self.model.complete(
            "You repair GUI automations.", prompt, max_tokens=600
        )
        self.model_calls += 1
        self.cost_usd += reply.usd
        try:
            raw = json.loads(
                reply.text[reply.text.find("{") : reply.text.rfind("}") + 1]
            )
            labels = [list(map(str, b)) for b in raw.get("bridges", [])][: self.n]
        except (ValueError, TypeError, AttributeError):
            return []
        out = []
        for bridge in labels:
            actions = []
            for label in bridge[: request.max_actions]:
                point = points.get(_squash(label))
                if point is None:
                    break
                actions.append({"kind": "click", "x": point[0], "y": point[1]})
            else:
                if actions:
                    out.append(RepairProposal(actions=actions, note=" > ".join(bridge)))
        return out


@dataclass
class CandidateOutcome:
    """What one candidate bridge did on the staging copy."""

    index: int
    actions: list[dict[str, Any]]
    note: str = ""
    #: Self-repair's status on staging (``awaiting_approval`` = probe and
    #: campaigns passed and it reached the approval request).
    repair_status: str = ""
    reason: str = ""
    runtime_outcome: str = ""
    outcome: str = ""
    reward: Optional[float] = None
    wrong_record: bool = False
    duplicate: bool = False
    wrong_value: bool = False
    receipt_id: str = ""


@dataclass
class SearchResult:
    winner: Optional[RepairProposal]
    winner_index: Optional[int]
    candidates: list[CandidateOutcome] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "openadapt.staging-bridge-search/v1",
            "winner_index": self.winner_index,
            "candidates": [asdict(c) for c in self.candidates],
        }


class StagingBridgeSearch:
    """Try each candidate bridge on the staging copy; keep the verified best."""

    def __init__(
        self,
        proposer: BridgeProposer,
        staging: StagingCopy,
        bundle_dir: Path,
        work_dir: Path,
        *,
        halted_run_dir: Optional[Path] = None,
        verified_run_dirs: Sequence[Path] = (),
        last_verified: int = 3,
        max_candidates: int = 4,
        penalties: Penalties = Penalties(),
    ) -> None:
        self.proposer = proposer
        self.staging = staging
        self.bundle_dir = Path(bundle_dir)
        self.work_dir = Path(work_dir)
        self.halted_run_dir = halted_run_dir
        self.verified_run_dirs = (
            list(verified_run_dirs)[-last_verified:] if last_verified else []
        )
        self.max_candidates = max_candidates
        self.penalties = penalties
        #: The directory of the latest search (``<work_dir>/search-<time>``).
        self.search_dir = self.work_dir

    def history(self) -> str:
        """The halted trace, then the last verified ones, rendered in full."""

        parts = []
        dirs = (
            [self.halted_run_dir] if self.halted_run_dir else []
        ) + self.verified_run_dirs
        for d in dirs:
            if d is not None and (Path(d) / "report.json").is_file():
                parts.append(traces.render(traces.from_flow_run(Path(d))))
        return "\n\n".join(parts)

    def search(self, request: RepairRequest) -> SearchResult:
        # Every search gets its own directory: run paths, stores and episodes
        # are single-use (the runtime refuses a run path it has run before).
        stamp = time.strftime("%Y%m%dT%H%M%S")
        self.search_dir = self.work_dir / f"search-{stamp}-{time.time_ns() % 10**6:06d}"
        proposals = self.proposer.bridges(request, self.history())[
            : self.max_candidates
        ]
        result = SearchResult(winner=None, winner_index=None)
        for i, proposal in enumerate(proposals):
            result.candidates.append(self._try(i, proposal))
        verified = [
            c
            for c in result.candidates
            if c.outcome == "verified" and c.reward is not None
        ]
        if verified:
            best = max(verified, key=lambda c: (c.reward, -c.index))
            result.winner_index = best.index
            result.winner = proposals[best.index]
        self.search_dir.mkdir(parents=True, exist_ok=True)
        (self.search_dir / "search.json").write_text(
            json.dumps(result.to_dict(), indent=2, default=str), encoding="utf-8"
        )
        return result

    def _try(self, index: int, proposal: RepairProposal) -> CandidateOutcome:
        out = CandidateOutcome(
            index=index,
            actions=[dict(a) for a in proposal.bridge()],
            note=proposal.note,
        )
        cdir = self.search_dir / f"candidate-{index}"
        self.staging.reset()
        repair = SelfRepair(
            _OneProposal(proposal, self.proposer.dialect),
            self.staging.target,
            store=cdir / "store",
            screen_access=ScreenAccess(
                enabled_by="staging-search", mode="allow_unscrubbed"
            ),
            max_attempts=1,
            fault_probes=self.staging.fault_probes,
            approval_wait_s=0,
        )
        repaired = repair.run(self.bundle_dir, cdir / "halt")
        out.repair_status = repaired.status
        out.reason = repaired.evidence.reason or ""
        try:
            if (
                repaired.status != "awaiting_approval"
                or repaired.patched_bundle is None
            ):
                return out
            self._full_run(out, repaired, cdir, index)
            return out
        finally:
            _withdraw(repaired, cdir)

    def _full_run(
        self, out: CandidateOutcome, repaired: Any, cdir: Path, index: int
    ) -> None:
        # The probe stopped before the save. Run the patched bundle through
        # the save on a fresh copy, and read what it wrote.
        self.staging.reset()
        task = GuiTask(
            task_id=f"repair/{request_step(repaired)}",
            family="staging-repair",
            app="staging",
            instruction="",
            params={**dict(self.staging.record), **dict(self.staging.identity)},
            identity=dict(self.staging.identity),
            record_fields=tuple(self.staging.record),
        )
        scorer = ReadBackScorer(
            task,
            dict(self.staging.oracle_recipe),
            cdir / "reward",
            penalties=self.penalties,
        )
        episode = f"episode_staging_{index:04d}_{cdir.parent.name[-16:] or 'search'}"
        episode = re.sub(r"[^A-Za-z0-9._:-]", "_", episode)
        scorer.begin(episode)
        patched = Path(repaired.patched_bundle)
        session = self.staging.target.open()
        report: Optional[RunReport] = None
        try:
            report = self.staging.target.execute(
                Workflow.load(patched), patched, session, cdir / "full"
            )
        except Exception as exc:  # noqa: BLE001 - recorded, scored as a platform failure
            out.reason = f"full run failed: {type(exc).__name__}: {exc}"[:500]
        finally:
            session.close()
        out.runtime_outcome = (
            str(report.execution_outcome or "") if report else "FAILED"
        )
        signal = _SIGNAL.get(out.runtime_outcome, "halted_before_effect")
        if report is not None and report.success and not report.execution_outcome:
            signal = "completed"
        try:
            score: EpisodeScore = scorer.score(episode, signal)
        finally:
            drop_worker_secrets(cdir / "reward" / "worker")
        out.outcome = score.outcome
        out.reward = score.reward
        out.wrong_record = score.wrong_record
        out.duplicate = score.duplicate
        out.wrong_value = score.wrong_value
        out.receipt_id = score.receipt_id


def _withdraw(repaired: Any, cdir: Path) -> None:
    """Close what the staging self-repair left open for a person.

    Each staging candidate stops at an approval request. Nobody may answer
    it: the request is removed and the staging candidate is rejected, so a
    portal pointed at the search's directory has nothing to approve.
    """

    from openadapt_flow.repair.candidate import Actor
    from openadapt_flow.repair.lifecycle import RepairLifecycleError, RepairStore

    shutil.rmtree(cdir / "halt" / "item-approval", ignore_errors=True)
    candidate_id = getattr(repaired.evidence, "candidate_id", None)
    if not candidate_id:
        return
    try:
        RepairStore(cdir / "store").reject(
            candidate_id,
            Actor(kind="automation", id="openadapt-staging-search"),
            "staging-only candidate: scored on a staging copy, never for approval",
        )
    except (RepairLifecycleError, OSError, ValueError):
        pass


def request_step(repaired: Any) -> str:
    return str(getattr(repaired.evidence, "halted_step_id", "") or "step")


class _OneProposal:
    """Hands self-repair exactly one fixed proposal, once."""

    agent_id = "agent:staging-search-candidate"
    requires_model_key = False

    def __init__(self, proposal: RepairProposal, dialect: str) -> None:
        self.proposal: Optional[RepairProposal] = proposal
        self.dialect = dialect

    def propose(self, request: RepairRequest) -> Optional[RepairProposal]:
        proposal, self.proposal = self.proposal, None
        return proposal


class StagingSearchAgent:
    """A self-repair agent whose one proposal is the staging search's winner.

    Give it to the production :class:`~openadapt_flow.repair.self_repair.SelfRepair`.
    On the first request it runs the search on staging and returns the best
    verified bridge, or ``None`` (give up) when no candidate verified. It
    proposes once; a refused winner is not retried with a worse candidate.
    """

    def __init__(
        self,
        search: StagingBridgeSearch,
        *,
        agent_id: str = "agent:staging-search",
    ) -> None:
        self.search = search
        self.agent_id = agent_id
        self.dialect = search.proposer.dialect
        # A model-backed proposer runs on the customer's key, like any agent.
        self.requires_model_key = not isinstance(search.proposer, ScriptedBridges)
        self.result: Optional[SearchResult] = None

    def propose(self, request: RepairRequest) -> Optional[RepairProposal]:
        if self.result is not None:
            return None
        self.result = self.search.search(request)
        winner = self.result.winner
        if winner is None:
            return None
        return RepairProposal(
            actions=list(winner.bridge()),
            model_calls=int(getattr(self.search.proposer, "model_calls", 0)),
            cost_usd=float(getattr(self.search.proposer, "cost_usd", 0.0)),
            note=(
                f"verified on a staging copy as candidate {self.result.winner_index} "
                f"of {len(self.result.candidates)}"
            ),
        )


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


__all__ = [
    "BridgeProposer",
    "CandidateOutcome",
    "ModelBridges",
    "ScriptedBridges",
    "SearchResult",
    "StagingBridgeSearch",
    "StagingCopy",
    "StagingSearchAgent",
]
