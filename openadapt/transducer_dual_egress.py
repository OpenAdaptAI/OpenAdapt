"""Two-stage recipient-sphere check for outbound items.

Stage 1 audits outbound items against the task's recipient sphere. It splits
rejected composite items into clauses so approved clauses can be retained.
Stage 2 recovers required in-scope items the executor left out, only if the
auditor approves them. Stage 3 lets a second auditor veto kept items after
stripping or recovery, or on every call when ``always_cross_check`` is enabled.

``containers`` maps container names to scalars, sequences, or nested mappings.
Leaf items carry provenance from their container and nested paths. Two metadata
keys are read from it: ``recipient_hint`` (str) and ``in_scope_containers``
(list of container names or paths the task may draw from).
"""

from __future__ import annotations

import enum
import re
import unicodedata
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

_META_KEYS = ("recipient_hint", "in_scope_containers")
_LINE_BREAK = re.compile(r"(\r?\n(?:[ \t]*\r?\n)*)")
_BULLET = re.compile(r"^([ \t]*(?:[-*+•]|\d+[.)])[ \t]+)")
_CLAUSE_BREAK = re.compile(r"([ \t]*(?:→|->)[ \t]*|(?<=[.!?])[ \t]+)")
_INLINE_HEADING = re.compile(r"^([^:.!?]+:)[ \t]+(?=\S)")
_MARKDOWN_HEADING = re.compile(r"^#{1,6}[ \t]+")


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


@dataclass(frozen=True)
class _KeptItem:
    text: str
    confident: bool
    cross_checked: bool = False


@dataclass(frozen=True)
class _OutboundLine:
    separator: str
    marker: str
    heading: str
    clauses: list[tuple[str, str]]


Auditor = Callable[
    [str, str, Mapping[str, Any]], "ItemSphereVote | Mapping[str, Any] | bool"
]
Completeness = Callable[[str, Sequence[str], Mapping[str, Any]], Sequence[str]]


def _flatten_containers(containers: Mapping[str, Any]) -> dict[str, list[str]]:
    boxes: dict[str, list[str]] = {}

    def visit(value: Any, paths: tuple[str, ...]) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                child_path = f"{paths[-1]}.{key}"
                boxes.setdefault(child_path, [])
                visit(child, (*paths, child_path))
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child, paths)
        else:
            leaf = str(value)
            for path in paths:
                boxes[path].append(leaf)

    for name, value in containers.items():
        if name not in _META_KEYS:
            boxes.setdefault(name, [])
            visit(value, (name,))
    return boxes


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"[\W_]+", " ", text).strip()


def _overlaps(left: str, right: str) -> bool:
    if len(left) > len(right):
        left, right = right, left
    return len(left) >= 12 and f" {left} " in f" {right} "


def _split_with_separators(
    text: str, separator: re.Pattern[str]
) -> Iterator[tuple[str, str]]:
    parts = separator.split(text)
    return zip(["", *parts[1::2]], parts[::2])


def _split_outbound(item: str) -> list[_OutboundLine]:
    if not (_LINE_BREAK.search(item) or _CLAUSE_BREAK.search(item)):
        return []
    lines: list[_OutboundLine] = []
    for line_sep, line in _split_with_separators(item, _LINE_BREAK):
        bullet = _BULLET.match(line)
        marker = bullet[0] if bullet else ""
        body = line[len(marker) :].strip()
        if not body:
            continue
        if not marker and (
            body.rstrip("*_").endswith(":") or _MARKDOWN_HEADING.match(body)
        ):
            lines.append(_OutboundLine(line_sep, marker, body, []))
            continue
        clauses = list(_split_with_separators(body, _CLAUSE_BREAK))
        inline = _INLINE_HEADING.match(clauses[0][1]) if len(clauses) > 1 else None
        heading = ""
        if inline:
            heading = inline[1]
            clauses[0] = ("", clauses[0][1][inline.end() :])
        clauses = [(sep, text.strip()) for sep, text in clauses if text.strip()]
        lines.append(_OutboundLine(line_sep, marker, heading, clauses))
    return lines


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
        always_cross_check: bool = False,
    ) -> None:
        self.sphere_auditor = sphere_auditor
        self.tie_breaker_auditor = tie_breaker_auditor
        self.completeness_checker = completeness_checker
        self.conformal_threshold = conformal_threshold
        self.always_cross_check = always_cross_check

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
        boxes = _flatten_containers(containers)
        pool = dict.fromkeys(
            leaf for name in in_scope or boxes for leaf in boxes.get(name, ())
        )
        in_scope_items = set(pool)
        sources: dict[str, set[str]] = {}
        for path, leaves in boxes.items():
            for leaf in leaves:
                sources.setdefault(leaf, set()).add(path)
        normalized = {leaf: _normalize(leaf) for leaf in sources}

        def meta_for(
            item: str, lines: Sequence[_OutboundLine] = ()
        ) -> tuple[dict[str, Any], bool]:
            item_norm = _normalize(item)
            exact_texts = {item}
            for line in lines:
                if line.heading:
                    exact_texts.add(line.heading)
                exact_texts.update(text for _, text in line.clauses)
            matched_paths: set[str] = set()
            clash = False
            for leaf, paths in sources.items():
                if leaf in exact_texts or _overlaps(item_norm, normalized[leaf]):
                    matched_paths.update(paths)
                    # An in-scope copy of a leaf suffices. An unrelated in-scope
                    # leaf cannot clear the conflict for a different leaf.
                    if leaf not in in_scope_items:
                        clash = True
            meta = {
                "source_containers": [path for path in boxes if path in matched_paths],
                "all_containers": list(boxes),
                "recipient_hint": hint,
            }
            return meta, clash

        tie_breaker = self.tie_breaker_auditor
        tie_used = 0

        def audit(item: str, lines: Sequence[_OutboundLine] = ()) -> _KeptItem | None:
            nonlocal tie_used
            meta, clash = meta_for(item, lines)
            vote = self._ask(self.sphere_auditor, item, task_prompt, meta)
            sure = vote.confidence >= thr
            if (not sure or clash) and tie_breaker is not None:
                tie_used += 1
                second = self._ask(tie_breaker, item, task_prompt, meta)
                if (
                    not clash
                    and vote.allow
                    and second.allow
                    and second.confidence >= thr
                ):
                    return _KeptItem(item, confident=True, cross_checked=True)
                return None
            if clash or not vote.allow:
                return None
            return _KeptItem(item, confident=sure)

        def redact(
            lines: Sequence[_OutboundLine],
        ) -> tuple[_KeptItem | None, list[str]]:
            dropped: list[str] = []
            kept: list[_KeptItem] = []
            rebuilt: list[str] = []
            pending_heading: tuple[str, _KeptItem] | None = None

            def audit_unit(unit: str) -> _KeptItem | None:
                approved = audit(unit)
                if approved is None:
                    dropped.append(unit)
                return approved

            for line in lines:
                heading = audit_unit(line.heading) if line.heading else None
                if not line.clauses:
                    pending_heading = (line.separator, heading) if heading else None
                    continue

                approved_clauses: list[str] = []
                approved_units: list[_KeptItem] = []
                for sep, clause in line.clauses:
                    if unit := audit_unit(clause):
                        approved_clauses.append(
                            (sep if approved_clauses else "") + unit.text
                        )
                        approved_units.append(unit)
                if not approved_units:
                    continue
                if pending_heading:
                    heading_sep, standalone = pending_heading
                    rebuilt.append((heading_sep if rebuilt else "") + standalone.text)
                    kept.append(standalone)
                    pending_heading = None
                prefix = heading.text + " " if heading else ""
                rebuilt.append(
                    (line.separator if rebuilt else "")
                    + line.marker
                    + prefix
                    + "".join(approved_clauses)
                )
                if heading:
                    kept.append(heading)
                kept.extend(approved_units)

            if not kept or not dropped:
                # When no denied clause was found, the whole-item rejection stands.
                return None, dropped
            rewritten = "".join(rebuilt)
            _, clash = meta_for(rewritten, _split_outbound(rewritten))
            if clash:
                return None, [*dropped, rewritten]
            if len(kept) == 1 and rewritten == kept[0].text:
                return kept[0], dropped
            return _KeptItem(
                rewritten, confident=all(unit.confident for unit in kept)
            ), dropped

        final: list[_KeptItem] = []
        stripped: list[str] = []
        for item in candidate_items:
            lines = _split_outbound(item)
            approved = audit(item, lines)
            if approved is not None:
                final.append(approved)
            elif lines:
                approved, dropped = redact(lines)
                stripped.extend(dropped or [item])
                if approved is not None:
                    final.append(approved)
            else:
                stripped.append(item)

        # Stage 2: recover required in-scope items without re-adding stripped ones.
        recovered: list[str] = []
        for item in stripped:
            pool.pop(item, None)
        final_texts = [item.text for item in final]
        wanted: list[str] = []
        if self.completeness_checker is not None:
            wanted.extend(
                str(w)
                for w in self.completeness_checker(
                    task_prompt,
                    list(final_texts),
                    {"in_scope_source_items": list(pool)},
                )
            )
        anchors = [a for a in required_anchors if a and a in task_prompt]

        def anchor_missing(a: str) -> bool:
            return not any(a in item.text for item in final)

        for a in anchors:
            if anchor_missing(a):
                wanted.extend(p for p in pool if a in p)
        for cand in wanted:
            if cand in final_texts or cand not in pool:
                continue
            meta, clash = meta_for(cand, _split_outbound(cand))
            vote = self._ask(self.sphere_auditor, cand, task_prompt, meta)
            if vote.allow and vote.confidence >= thr and not clash:
                final.append(_KeptItem(cand, confident=True))
                final_texts.append(cand)
                recovered.append(cand)

        # Stage 3: a second judge can veto even a high-confidence approval.
        total = len(candidate_items) + len(recovered)
        cross_check_failed = False
        if tie_breaker is not None and (
            self.always_cross_check or stripped or recovered
        ):
            veto_threshold = min(0.50, thr)
            confirmed: list[_KeptItem] = []
            for item in final:
                if not item.cross_checked:
                    tie_used += 1
                    meta, _ = meta_for(item.text, _split_outbound(item.text))
                    second = self._ask(tie_breaker, item.text, task_prompt, meta)
                    if second.allow and second.confidence >= veto_threshold:
                        confirmed.append(item)
                    else:
                        stripped.append(item.text)
                        cross_check_failed = True
                else:
                    confirmed.append(item)
            final = confirmed
            final_texts = [item.text for item in final]
            recovered = [item for item in recovered if item in final_texts]
        missing = [a for a in anchors if anchor_missing(a)]

        ratio = sum(item.confident for item in final) / total if total else 0.0
        common = dict(
            final_items=final_texts,
            stripped_items=stripped,
            recovered_items=recovered,
            tie_breakers_used=tie_used,
            agreement_ratio=ratio,
        )
        if not final and not cross_check_failed:
            return DualEgressReport(
                EgressDecision.REFUSE_ABSTAIN, reason="no items left to send", **common
            )
        weak_kept = any(not item.confident for item in final)
        if weak_kept or missing or cross_check_failed:
            why = []
            if cross_check_failed:
                why.append("kept items lacked independent approval")
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
