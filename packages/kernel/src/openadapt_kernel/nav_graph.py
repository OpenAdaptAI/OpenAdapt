"""Zero-PHI Reusable App Navigation Graph, Stuck-Loop Detector, and Async Reconciliation Watcher."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional, Sequence, Tuple, Union
from urllib.parse import urlparse

from openadapt_kernel.egress_guard import EgressPolicy
from openadapt_kernel.macro import ControlSignature
from openadapt_kernel.prepare import PendingReconciliationRecord
from openadapt_kernel.zero_phi_repair import sanitize_ui_label_allowlist

_ID_SEGMENT_RE = re.compile(
    r"^(?:\d+|[0-9a-fA-F]{8,}|[0-9a-fA-F]{8}-[0-9a-fA-F-]{27}|[A-Za-z]{1,6}-\d+[A-Za-z0-9-]*)$"
)
_NAV_EXTRA_TOKENS = frozenset(
    "claims dashboard patients portal portals records settings users workflows".split()
)

ReconciliationStatus = Literal[
    "SUBMITTED_PENDING_RECONCILIATION",
    "RECONCILED_COMPLETE",
    "RECONCILIATION_ESCALATED",
]


def _default_nav_vocab(policy: Optional[EgressPolicy] = None) -> frozenset[str]:
    pol = policy or EgressPolicy()
    return pol.effective_tokens() | _NAV_EXTRA_TOKENS


def sanitize_url_to_view_pattern(
    raw_url: str,
    *,
    policy: Optional[EgressPolicy] = None,
) -> str:
    """Convert any live URL into a Zero-PHI path template by replacing IDs/non-allowlisted segments with {id}."""
    parsed = urlparse((raw_url or "/").strip())
    path = parsed.path or "/"
    vocab = _default_nav_vocab(policy)
    segments = [seg for seg in path.split("/") if seg]
    if not segments:
        return "/"
    normalized: List[str] = []
    for seg in segments:
        if seg == "{id}":
            normalized.append("{id}")
            continue
        low = seg.lower()
        if _ID_SEGMENT_RE.fullmatch(seg) or low not in vocab:
            normalized.append("{id}")
        else:
            normalized.append(low)
    return "/" + "/".join(normalized)


def _sanitize_control_signature(sig: ControlSignature, vocab: frozenset[str]) -> ControlSignature:
    safe_labels = tuple(
        sorted(t for t in sig.label_tokens if t.lower() in vocab)
    )
    safe_texts = tuple(
        sorted(t for t in sig.text_tokens if t.lower() in vocab)
    )
    safe_name = sig.name.lower() if sig.name.lower() in vocab else ""
    safe_ph = " ".join(
        t for t in sanitize_ui_label_allowlist(sig.placeholder).split() if t != "<OPAQUE>"
    )
    return ControlSignature(
        in_modal=bool(sig.in_modal),
        tag=sig.tag.lower() if sig.tag.lower() in vocab else "",
        input_type=sig.input_type.lower() if sig.input_type.lower() in vocab else "",
        role=sig.role.lower() if sig.role.lower() in vocab else "",
        name=safe_name,
        placeholder=safe_ph,
        label_tokens=safe_labels,
        text_tokens=safe_texts,
        ordinal=int(sig.ordinal),
    )


def _sig_to_dict(sig: ControlSignature) -> Dict[str, Any]:
    return {
        "in_modal": sig.in_modal,
        "tag": sig.tag,
        "input_type": sig.input_type,
        "role": sig.role,
        "name": sig.name,
        "placeholder": sig.placeholder,
        "label_tokens": list(sig.label_tokens),
        "text_tokens": list(sig.text_tokens),
        "ordinal": sig.ordinal,
    }


def _sig_from_dict(data: Dict[str, Any]) -> ControlSignature:
    return ControlSignature(
        in_modal=bool(data.get("in_modal", False)),
        tag=str(data.get("tag", "")),
        input_type=str(data.get("input_type", "")),
        role=str(data.get("role", "")),
        name=str(data.get("name", "")),
        placeholder=str(data.get("placeholder", "")),
        label_tokens=tuple(str(t) for t in (data.get("label_tokens") or [])),
        text_tokens=tuple(str(t) for t in (data.get("text_tokens") or [])),
        ordinal=int(data.get("ordinal", 0)),
    )


@dataclass(frozen=True)
class NavNode:
    """Zero-PHI view node in an application navigation graph."""

    view_id: str
    url_pattern: str
    role_landmarks: Tuple[str, ...] = ()


@dataclass
class NavEdge:
    """Directed transition between two Zero-PHI views via a semantic ControlSignature."""

    src_view_id: str
    dst_view_id: str
    action_signature: ControlSignature
    success_count: int = 1


class AppNavigationGraph:
    """Reusable Zero-PHI navigation graph supporting BFS shortest-path routing across app views."""

    def __init__(self, *, policy: Optional[EgressPolicy] = None) -> None:
        self._policy = policy or EgressPolicy()
        self.nodes: Dict[str, NavNode] = {}
        self.edges: List[NavEdge] = []

    def ensure_node(
        self,
        raw_url: str,
        *,
        role_landmarks: Sequence[str] = (),
    ) -> NavNode:
        pattern = sanitize_url_to_view_pattern(raw_url, policy=self._policy)
        clean_landmarks: List[str] = []
        for lm in role_landmarks:
            toks = [t for t in sanitize_ui_label_allowlist(lm).split() if t != "<OPAQUE>"]
            if toks:
                clean_landmarks.append(" ".join(toks))
        existing = self.nodes.get(pattern)
        merged = tuple(sorted(set(existing.role_landmarks if existing else ()) | set(clean_landmarks)))
        node = NavNode(view_id=pattern, url_pattern=pattern, role_landmarks=merged)
        self.nodes[pattern] = node
        return node

    def record_transition(
        self,
        src_url: str,
        dst_url: str,
        action_signature: ControlSignature,
        *,
        src_landmarks: Sequence[str] = (),
        dst_landmarks: Sequence[str] = (),
    ) -> NavEdge:
        """Record a Zero-PHI transition between two views, sanitizing URLs and ControlSignature."""
        vocab = _default_nav_vocab(self._policy)
        src_node = self.ensure_node(src_url, role_landmarks=src_landmarks)
        dst_node = self.ensure_node(dst_url, role_landmarks=dst_landmarks)
        safe_sig = _sanitize_control_signature(action_signature, vocab)

        for edge in self.edges:
            if (
                edge.src_view_id == src_node.view_id
                and edge.dst_view_id == dst_node.view_id
                and edge.action_signature == safe_sig
            ):
                edge.success_count += 1
                return edge

        new_edge = NavEdge(
            src_view_id=src_node.view_id,
            dst_view_id=dst_node.view_id,
            action_signature=safe_sig,
            success_count=1,
        )
        self.edges.append(new_edge)
        return new_edge

    def shortest_path(self, current_view: str, target_view: str) -> List[NavEdge]:
        """Compute the shortest BFS edge sequence from current_view to target_view."""
        src = sanitize_url_to_view_pattern(current_view, policy=self._policy)
        dst = sanitize_url_to_view_pattern(target_view, policy=self._policy)
        if src == dst:
            return []

        adj: Dict[str, List[NavEdge]] = {}
        for edge in self.edges:
            adj.setdefault(edge.src_view_id, []).append(edge)
        for edge_list in adj.values():
            edge_list.sort(key=lambda e: (-e.success_count, e.dst_view_id))

        visited: set[str] = {src}
        queue: deque[Tuple[str, List[NavEdge]]] = deque([(src, [])])
        while queue:
            cur, path = queue.popleft()
            for edge in adj.get(cur, []):
                nxt = edge.dst_view_id
                if nxt == dst:
                    return [*path, edge]
                if nxt not in visited:
                    visited.add(nxt)
                    queue.append((nxt, [*path, edge]))
        return []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "nodes": [
                {
                    "view_id": n.view_id,
                    "url_pattern": n.url_pattern,
                    "role_landmarks": list(n.role_landmarks),
                }
                for n in sorted(self.nodes.values(), key=lambda x: x.view_id)
            ],
            "edges": [
                {
                    "src_view_id": e.src_view_id,
                    "dst_view_id": e.dst_view_id,
                    "action_signature": _sig_to_dict(e.action_signature),
                    "success_count": e.success_count,
                }
                for e in self.edges
            ],
        }

    @classmethod
    def from_dict(
        cls,
        data: Dict[str, Any],
        *,
        policy: Optional[EgressPolicy] = None,
    ) -> "AppNavigationGraph":
        graph = cls(policy=policy)
        for n in data.get("nodes") or []:
            node = NavNode(
                view_id=str(n["view_id"]),
                url_pattern=str(n["url_pattern"]),
                role_landmarks=tuple(str(lm) for lm in (n.get("role_landmarks") or [])),
            )
            graph.nodes[node.view_id] = node
        for e in data.get("edges") or []:
            graph.edges.append(
                NavEdge(
                    src_view_id=str(e["src_view_id"]),
                    dst_view_id=str(e["dst_view_id"]),
                    action_signature=_sig_from_dict(dict(e.get("action_signature") or {})),
                    success_count=int(e.get("success_count", 1)),
                )
            )
        return graph

    def save_json(self, path: Union[str, Path]) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        return out

    @classmethod
    def load_json(
        cls,
        path: Union[str, Path],
        *,
        policy: Optional[EgressPolicy] = None,
    ) -> "AppNavigationGraph":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")), policy=policy)


@dataclass(frozen=True)
class StuckLoopStatus:
    """State hash and twin-fallback decision emitted by StuckLoopDetector."""

    state_hash: str
    unchanged_count: int
    should_fallback_to_twin: bool


class StuckLoopDetector:
    """Hashes normalized URL + DOM state and signals digital-twin fallback when progress stalls."""

    def __init__(self, max_unchanged_steps: int = 2) -> None:
        self.max_unchanged_steps = max(1, int(max_unchanged_steps))
        self._last_hash: Optional[str] = None
        self._unchanged_count: int = 0

    def reset(self) -> None:
        self._last_hash = None
        self._unchanged_count = 0

    def observe(self, url: str, dom_state: str) -> StuckLoopStatus:
        norm_url = sanitize_url_to_view_pattern(url)
        norm_dom = " ".join((dom_state or "").split())
        digest = hashlib.sha256(f"{norm_url}\n{norm_dom}".encode("utf-8")).hexdigest()
        if self._last_hash == digest:
            self._unchanged_count += 1
        else:
            self._last_hash = digest
            self._unchanged_count = 0
        return StuckLoopStatus(
            state_hash=digest,
            unchanged_count=self._unchanged_count,
            should_fallback_to_twin=self._unchanged_count >= self.max_unchanged_steps,
        )


@dataclass
class WatchedReconciliationItem:
    """Tracked async portal/clearinghouse submission awaiting post-commit reconciliation."""

    record: PendingReconciliationRecord
    status: ReconciliationStatus = "SUBMITTED_PENDING_RECONCILIATION"
    attempts: int = 0
    max_attempts: int = 3
    detail: str = ""
    history: List[str] = field(default_factory=list)


class ReconciliationWatcher:
    """Polls PendingReconciliationRecord submissions to verified completion or explicit escalation."""

    def __init__(self) -> None:
        self._items: Dict[str, WatchedReconciliationItem] = {}

    def enqueue(
        self,
        record: PendingReconciliationRecord,
        *,
        max_attempts: int = 3,
    ) -> WatchedReconciliationItem:
        item = WatchedReconciliationItem(
            record=record,
            status="SUBMITTED_PENDING_RECONCILIATION",
            attempts=0,
            max_attempts=max(1, int(max_attempts)),
        )
        self._items[record.obligation_id] = item
        return item

    def get(self, obligation_id: str) -> Optional[WatchedReconciliationItem]:
        return self._items.get(obligation_id)

    def poll_once(
        self,
        probe_fn: Callable[[PendingReconciliationRecord], Tuple[bool, str]],
    ) -> List[WatchedReconciliationItem]:
        """Run one poll pass over all pending items, updating status to complete or escalated."""
        checked: List[WatchedReconciliationItem] = []
        for item in self._items.values():
            if item.status != "SUBMITTED_PENDING_RECONCILIATION":
                continue
            item.attempts += 1
            verified, detail = probe_fn(item.record)
            item.detail = str(detail)
            item.history.append(f"attempt={item.attempts}:verified={verified}:{detail}")
            if verified:
                item.status = "RECONCILED_COMPLETE"
            elif item.attempts >= item.max_attempts:
                item.status = "RECONCILIATION_ESCALATED"
                if not item.detail:
                    item.detail = "reconciliation_timeout"
            checked.append(item)
        return checked

    def poll_until_settled(
        self,
        probe_fn: Callable[[PendingReconciliationRecord], Tuple[bool, str]],
        *,
        interval_s: float = 0.0,
        max_rounds: int = 10,
    ) -> Dict[str, WatchedReconciliationItem]:
        """Poll repeatedly until every item reaches RECONCILED_COMPLETE or RECONCILIATION_ESCALATED."""
        rounds = max(1, int(max_rounds))
        for _ in range(rounds):
            pending = [
                it for it in self._items.values() if it.status == "SUBMITTED_PENDING_RECONCILIATION"
            ]
            if not pending:
                break
            self.poll_once(probe_fn)
            still_pending = any(
                it.status == "SUBMITTED_PENDING_RECONCILIATION" for it in self._items.values()
            )
            if still_pending and interval_s > 0:
                time.sleep(interval_s)

        for item in self._items.values():
            if item.status == "SUBMITTED_PENDING_RECONCILIATION":
                item.status = "RECONCILIATION_ESCALATED"
                item.detail = item.detail or "reconciliation_max_rounds_exceeded"
        return dict(self._items)

    def summary(self) -> Dict[str, int]:
        counts = {"pending": 0, "reconciled": 0, "escalated": 0}
        for item in self._items.values():
            if item.status == "SUBMITTED_PENDING_RECONCILIATION":
                counts["pending"] += 1
            elif item.status == "RECONCILED_COMPLETE":
                counts["reconciled"] += 1
            else:
                counts["escalated"] += 1
        return counts
