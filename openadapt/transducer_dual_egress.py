"""Two-stage recipient-sphere check for outbound items.

Stage 1 asks an auditor whether each outbound item belongs to the same
recipient sphere as the task. Unsure or conflicting items go to an optional
tie-breaker; anything still unsure or denied is stripped.
Stage 2 recovers required in-scope items the executor left out, only if the
auditor approves them. Stage 3 picks the decision: a verified decision needs
every kept item approved at or above the confidence threshold.

``containers`` maps container name to a list of item strings. Two metadata
keys are read from it: ``recipient_hint`` (str) and ``in_scope_containers``
(list of container names the task may draw from).
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

_META_KEYS = ("recipient_hint", "in_scope_containers")


class EgressDecision(str, enum.Enum):
    ALLOW_VERIFIED = "allow_verified"
    STRIP_VERIFIED = "strip_verified"
    ALLOW_UNVERIFIED = "allow_unverified"
    REFUSE_ABSTAIN = "refuse_abstain"


@dataclass(frozen=True)
class ItemSphereVote:
    item: str
    allow: bool
    confidence: float
    sphere_reason: str = ""


@dataclass(frozen=True)
class DualEgressReport:
    decision: EgressDecision
    final_items: list[str] = field(default_factory=list)
    stripped_items: list[str] = field(default_factory=list)
    recovered_items: list[str] = field(default_factory=list)
    tie_breakers_used: int = 0
    agreement_ratio: float = 0.0
    reason: str = ""


Auditor = Callable[
    [str, str, Mapping[str, Any]], "ItemSphereVote | Mapping[str, Any] | bool"
]
Completeness = Callable[[str, Sequence[str], Mapping[str, Any]], Sequence[str]]


def _as_vote(item: str, raw: Any) -> ItemSphereVote:
    if isinstance(raw, ItemSphereVote):
        return raw
    if isinstance(raw, bool):
        return ItemSphereVote(item, raw, 1.0, "bool auditor")
    if isinstance(raw, Mapping):
        allow = raw.get("allow") is True
        try:
            conf = float(raw.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        return ItemSphereVote(item, allow, conf, str(raw.get("sphere_reason", "")))
    return ItemSphereVote(item, False, 0.0, "unreadable auditor output")


class DualStageEgressVerifier:
    def __init__(
        self,
        sphere_auditor: Auditor,
        tie_breaker_auditor: Auditor | None = None,
        completeness_checker: Completeness | None = None,
        conformal_threshold: float = 0.80,
    ) -> None:
        self.sphere_auditor = sphere_auditor
        self.tie_breaker_auditor = tie_breaker_auditor
        self.completeness_checker = completeness_checker
        self.conformal_threshold = conformal_threshold

    def _ask(
        self, auditor: Auditor, item: str, task_prompt: str, meta: Mapping[str, Any]
    ) -> ItemSphereVote:
        return _as_vote(item, auditor(item, task_prompt, meta))

    def verify_outbound(
        self,
        candidate_items: Sequence[str],
        *,
        task_prompt: str,
        containers: Mapping[str, Any] | None = None,
        required_anchors: Sequence[str] = (),
    ) -> DualEgressReport:
        thr = self.conformal_threshold
        containers = containers or {}
        hint = str(containers.get("recipient_hint", "") or "")
        in_scope = [str(c) for c in containers.get("in_scope_containers", ()) or ()]
        boxes = {
            k: [str(i) for i in v] for k, v in containers.items() if k not in _META_KEYS
        }

        def meta_for(item: str) -> dict[str, Any]:
            return {
                "source_containers": [k for k, v in boxes.items() if item in v],
                "all_containers": list(boxes),
                "recipient_hint": hint,
            }

        def conflicts(meta: Mapping[str, Any]) -> bool:
            src = meta["source_containers"]
            return bool(in_scope) and bool(src) and not set(src) & set(in_scope)

        final: list[str] = []
        stripped: list[str] = []
        confident = 0
        weak_kept = False
        tie_used = 0
        audited = 0

        for item in candidate_items:
            audited += 1
            meta = meta_for(item)
            vote = self._ask(self.sphere_auditor, item, task_prompt, meta)
            clash = conflicts(meta)
            unsure = vote.confidence < thr
            if (unsure or clash) and self.tie_breaker_auditor is not None:
                tie_used += 1
                second = self._ask(self.tie_breaker_auditor, item, task_prompt, meta)
                if second.allow and second.confidence >= thr and vote.allow:
                    final.append(item)
                    confident += 1
                else:
                    stripped.append(item)
                continue
            if clash or not vote.allow:
                stripped.append(item)
            elif unsure:
                final.append(item)
                weak_kept = True
            else:
                final.append(item)
                confident += 1

        # Stage 2: recover required in-scope items without re-adding stripped ones.
        recovered: list[str] = []
        pool: list[str] = []
        for name in in_scope or list(boxes):
            for cand in boxes.get(name, []):
                if cand not in pool:
                    pool.append(cand)
        pool = [p for p in pool if p not in stripped]
        wanted: list[str] = []
        if self.completeness_checker is not None:
            wanted.extend(
                str(w)
                for w in self.completeness_checker(
                    task_prompt, list(final), {"in_scope_source_items": list(pool)}
                )
            )
        anchors = [a for a in required_anchors if a and a in task_prompt]

        def anchor_missing(a: str) -> bool:
            return not [i for i in final if a in i]

        for a in anchors:
            if anchor_missing(a):
                wanted.extend(p for p in pool if a in p)
        for cand in wanted:
            if (
                cand in final
                or cand in stripped
                or cand in recovered
                or cand not in pool
            ):
                continue
            meta = meta_for(cand)
            vote = self._ask(self.sphere_auditor, cand, task_prompt, meta)
            if vote.allow and vote.confidence >= thr and not conflicts(meta):
                final.append(cand)
                recovered.append(cand)
                confident += 1
        missing = [a for a in anchors if anchor_missing(a)]

        # Stage 3: decision.
        total = audited + len(recovered)
        ratio = confident / total if total else 0.0
        common = dict(
            final_items=final,
            stripped_items=stripped,
            recovered_items=recovered,
            tie_breakers_used=tie_used,
            agreement_ratio=ratio,
        )
        if not final:
            return DualEgressReport(
                EgressDecision.REFUSE_ABSTAIN, reason="no items left to send", **common
            )
        if weak_kept or missing:
            why = []
            if weak_kept:
                why.append("kept item below confidence threshold")
            if missing:
                why.append("missing anchors: " + ", ".join(missing))
            return DualEgressReport(
                EgressDecision.ALLOW_UNVERIFIED, reason="; ".join(why), **common
            )
        if stripped:
            return DualEgressReport(
                EgressDecision.STRIP_VERIFIED,
                reason=f"stripped {len(stripped)} foreign item(s)",
                **common,
            )
        return DualEgressReport(
            EgressDecision.ALLOW_VERIFIED,
            reason="all items in recipient sphere",
            **common,
        )
