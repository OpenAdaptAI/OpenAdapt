"""Verified commit macros and no-progress (stall) detection.

Two app-agnostic components:

* ``distill_verified_trace`` turns one verified step trace plus the concrete
  item payload into a parameterized ``CompiledCommitMacro``. ``instantiate_macro``
  binds the next item's payload into executable steps. ``MacroStore`` keeps the
  macros on disk, scoped by ``isolation_mode``. A macro that drifts is demoted
  so the caller falls back to the base model.
* ``NoProgressDetector`` hashes the page state after each step and returns a
  ``StallRecoveryPlan`` when consecutive steps change nothing or two states
  alternate.

Nothing here names an application, site or selector.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = "1"
ISOLATION_EPHEMERAL = "iid_ephemeral"
ISOLATION_PERSISTENT = "continual_persistent"

_PLACEHOLDER_RE = re.compile(r"\{\{|\}\}|\{(\w+)\}")
_SLOT_NAME_RE = re.compile(r"\W")


@dataclass
class CompiledMacroStep:
    action: str
    selector: str
    value_template: str
    slot_name: str | None = None
    is_commit: bool = False


@dataclass
class CompiledCommitMacro:
    macro_id: str
    route_pattern: str
    steps: list[CompiledMacroStep]
    slot_names: list[str]
    verified_count: int = 1
    schema_version: str = SCHEMA_VERSION


def _escape(text: str) -> str:
    return text.replace("{", "{{").replace("}", "}}")


def _step_failed(step: Mapping[str, Any]) -> bool:
    if step.get("ok") is False or step.get("succeeded") is False:
        return True
    if step.get("failed") or step.get("noop") or step.get("no_op"):
        return True
    return bool(step.get("error"))


def _templatize(
    value: str, payload: Mapping[str, str]
) -> tuple[str, str | None, list[str]]:
    """Replace payload field values inside ``value`` with ``{slot}`` markers.

    Returns (template, primary_slot, slots_used). An exact match wins; otherwise
    longer field values are substituted first so short values cannot split
    longer ones.
    """
    fields = [
        (name, str(text))
        for name, text in payload.items()
        if isinstance(text, str) and text.strip()
    ]
    for name, text in fields:
        if value == text:
            return "{" + name + "}", name, [name]
    fields.sort(key=lambda item: len(item[1]), reverse=True)
    pieces = [value]
    used: list[str] = []
    for name, text in fields:
        if text not in value:
            continue
        next_pieces: list[str] = []
        hit = False
        for piece in pieces:
            # Odd indexes are already-substituted markers; leave them alone.
            if piece.startswith("\0"):
                next_pieces.append(piece)
                continue
            parts = piece.split(text)
            if len(parts) > 1:
                hit = True
            for index, part in enumerate(parts):
                if index:
                    next_pieces.append("\0" + name)
                if part:
                    next_pieces.append(part)
        pieces = next_pieces
        if hit:
            used.append(name)
    if not used:
        return _escape(value), None, []
    template = "".join(
        "{" + p[1:] + "}" if p.startswith("\0") else _escape(p) for p in pieces
    )
    return template, used[0], used


def distill_verified_trace(
    step_trace: Sequence[Mapping[str, Any]],
    item_payload: Mapping[str, str],
    route_pattern: str = "",
) -> CompiledCommitMacro | None:
    """Build a parameterized macro from a verified trace, or None if unusable.

    Failed, no-op and immediately repeated identical steps are dropped. The
    trace must end up with at least one step and a commit step.
    """
    steps: list[CompiledMacroStep] = []
    slots: list[str] = []
    last_key: tuple[str, str, str] | None = None
    for raw in step_trace:
        if _step_failed(raw):
            continue
        action = str(raw.get("action") or "").strip()
        selector = str(raw.get("selector") or "")
        if not action:
            continue
        value = raw.get("value")
        value = "" if value is None else str(value)
        key = (action, selector, value)
        if key == last_key and not raw.get("is_commit"):
            continue
        last_key = key
        template, slot, used = _templatize(value, item_payload)
        for name in used:
            if name not in slots:
                slots.append(name)
        steps.append(
            CompiledMacroStep(
                action=action,
                selector=selector,
                value_template=template,
                slot_name=slot,
                is_commit=bool(raw.get("is_commit")),
            )
        )
    if not steps or not any(step.is_commit for step in steps) or not slots:
        return None
    digest = hashlib.sha256(
        json.dumps(
            [route_pattern, [(s.action, s.selector, s.value_template) for s in steps]]
        ).encode()
    ).hexdigest()[:16]
    return CompiledCommitMacro(
        macro_id=f"macro-{digest}",
        route_pattern=route_pattern,
        steps=steps,
        slot_names=slots,
    )


def instantiate_macro(
    macro: CompiledCommitMacro, next_payload: Mapping[str, str]
) -> list[dict[str, Any]]:
    """Bind ``next_payload`` into the macro. ``[]`` when any slot is missing."""
    for name in macro.slot_names:
        value = next_payload.get(name)
        if not isinstance(value, str) or not value.strip():
            return []

    def _bind(match: re.Match[str]) -> str:
        token = match.group(0)
        if token == "{{":
            return "{"
        if token == "}}":
            return "}"
        return str(next_payload[match.group(1)])

    out: list[dict[str, Any]] = []
    for index, step in enumerate(macro.steps):
        out.append(
            {
                "action": step.action,
                "selector": step.selector,
                "value": _PLACEHOLDER_RE.sub(_bind, step.value_template),
                "is_commit": step.is_commit,
                "source": "compiled_macro",
                "macro_id": macro.macro_id,
                "macro_step_index": index,
            }
        )
    return out


class MacroStore:
    """Cold-disk JSON macro store.

    ``iid_ephemeral``: macros live under ``(run_id, task_id)`` and are wiped on
    ``reset()`` or when a different task starts.
    ``continual_persistent``: macros live under ``(origin_path_pattern,
    task_kind)`` and survive across tasks and runs.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        isolation_mode: str = ISOLATION_EPHEMERAL,
        run_id: str = "",
    ) -> None:
        if isolation_mode not in (ISOLATION_EPHEMERAL, ISOLATION_PERSISTENT):
            raise ValueError(f"unknown isolation_mode: {isolation_mode!r}")
        self.path = Path(path)
        self.isolation_mode = isolation_mode
        self.run_id = run_id

    # -- storage ---------------------------------------------------------
    @contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(self.path.name + ".lock")
        with open(lock_path, "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                data = self._read()
                yield data
                self._write(data)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {"entries": {}}
        if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
            return {"entries": {}}
        return data

    def _write(self, data: dict[str, Any]) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(data, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _key(self, task_id: str, origin_path_pattern: str, task_kind: str) -> str:
        if self.isolation_mode == ISOLATION_PERSISTENT:
            return f"persistent|{origin_path_pattern}|{task_kind}"
        return f"ephemeral|{self.run_id}|{task_id}"

    # -- public API ------------------------------------------------------
    def put(
        self,
        macro: CompiledCommitMacro,
        *,
        task_id: str = "",
        origin_path_pattern: str = "",
        task_kind: str = "",
    ) -> None:
        key = self._key(task_id, origin_path_pattern, task_kind)
        with self._locked() as data:
            if self.isolation_mode == ISOLATION_EPHEMERAL:
                self._wipe_other_tasks(data, key)
            entries = data["entries"]
            old = entries.get(key)
            if (
                old
                and old["status"] == "active"
                and old["macro"]["macro_id"] == macro.macro_id
            ):
                old["macro"]["verified_count"] += macro.verified_count
                return
            entries[key] = {
                "macro": asdict(macro),
                "status": "active",
                "failures": 0,
                "mode": self.isolation_mode,
            }

    def get(
        self,
        *,
        task_id: str = "",
        origin_path_pattern: str = "",
        task_kind: str = "",
    ) -> CompiledCommitMacro | None:
        key = self._key(task_id, origin_path_pattern, task_kind)
        with self._locked() as data:
            if self.isolation_mode == ISOLATION_EPHEMERAL:
                self._wipe_other_tasks(data, key)
            entry = data["entries"].get(key)
        if not entry or entry["status"] != "active":
            return None
        raw = entry["macro"]
        if raw.get("schema_version") != SCHEMA_VERSION:
            return None
        return CompiledCommitMacro(
            macro_id=raw["macro_id"],
            route_pattern=raw["route_pattern"],
            steps=[CompiledMacroStep(**s) for s in raw["steps"]],
            slot_names=list(raw["slot_names"]),
            verified_count=raw["verified_count"],
            schema_version=raw["schema_version"],
        )

    def record_macro_outcome(self, macro_id: str, succeeded: bool) -> bool:
        """Record a replay result. Returns True while the macro stays usable.

        A failure (selector gone, read-back mismatch) invalidates the macro at
        once, so the next ``get`` returns None and the caller uses the base
        model. Unknown ids return False.
        """
        with self._locked() as data:
            for entry in data["entries"].values():
                if entry["macro"]["macro_id"] != macro_id:
                    continue
                if succeeded:
                    if entry["status"] == "active":
                        entry["macro"]["verified_count"] += 1
                else:
                    entry["failures"] += 1
                    entry["status"] = "invalidated"
                return entry["status"] == "active"
        return False

    def reset(self) -> None:
        """End of episode: drop ephemeral entries. Persistent ones stay."""
        with self._locked() as data:
            data["entries"] = {
                k: v
                for k, v in data["entries"].items()
                if v.get("mode") != ISOLATION_EPHEMERAL
            }

    @staticmethod
    def _wipe_other_tasks(data: dict[str, Any], keep_key: str) -> None:
        data["entries"] = {
            k: v
            for k, v in data["entries"].items()
            if v.get("mode") != ISOLATION_EPHEMERAL or k == keep_key
        }


# ---------------------------------------------------------------------------
# Stall detection
# ---------------------------------------------------------------------------

_INPUT_TAGS = {"input", "textarea", "select"}
_INTERACTIVE_TAGS = _INPUT_TAGS | {"button", "a"}
_REGION_FLAGS = ("in_modal", "in_main", "below_fold", "below_the_fold")


@dataclass
class StallRecoveryPlan:
    is_stalled: bool
    stall_count: int
    blacklisted_selectors: list[str] = field(default_factory=list)
    surfaced_form_candidates: list[dict[str, Any]] = field(default_factory=list)
    escalate_effort: bool = False
    switch_to_vision: bool = False
    hint: str = ""


def _is_interactive(el: Mapping[str, Any]) -> bool:
    tag = str(el.get("tag") or "").lower()
    return tag in _INTERACTIVE_TAGS or bool(el.get("role"))


def _dom_hash(dom_elements: Sequence[Mapping[str, Any]] | None) -> str:
    if not dom_elements:
        return ""
    rows = sorted(
        json.dumps(
            [
                str(el.get("tag") or ""),
                str(el.get("type") or ""),
                str(el.get("selector") or ""),
                str(el.get("text") or ""),
                str(el.get("value") or ""),
                bool(el.get("disabled")),
            ]
        )
        for el in dom_elements
        if _is_interactive(el)
    )
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()


def _action_key(action: Mapping[str, Any] | None) -> tuple[str, str, str]:
    if not action:
        return ("", "", "")
    return (
        str(action.get("action") or ""),
        str(action.get("selector") or ""),
        str(action.get("value") or ""),
    )


def _candidate_rank(el: Mapping[str, Any]) -> int:
    tag = str(el.get("tag") or "").lower()
    kind = str(el.get("type") or "").lower()
    if tag == "button" and kind == "submit":
        return 0
    if tag in _INPUT_TAGS:
        return 1
    return 2


class NoProgressDetector:
    """Flags runs of steps that change nothing, or two states that alternate."""

    def __init__(self, stall_threshold: int = 2, max_escalations: int = 2) -> None:
        self.stall_threshold = max(1, stall_threshold)
        self.max_escalations = max(0, max_escalations)
        self.reset()

    def reset(self) -> None:
        self._prev_digest: tuple[str, str, str] | None = None
        self._history: list[tuple[str, tuple[str, str, str]]] = []
        self._stall_count = 0
        self._escalations = 0
        self._attempted: set[str] = set()
        self._blacklist: list[str] = []

    @staticmethod
    def state_digest(
        url: str,
        dom_elements: Sequence[Mapping[str, Any]] | None,
        screenshot: bytes | None,
    ) -> tuple[str, str, str]:
        shot = hashlib.sha256(screenshot).hexdigest() if screenshot else ""
        return (url, _dom_hash(dom_elements), shot)

    def observe_step(
        self,
        url: str,
        dom_elements: Sequence[Mapping[str, Any]] | None,
        screenshot: bytes | None,
        last_action: Mapping[str, Any] | None,
    ) -> StallRecoveryPlan | None:
        digest = self.state_digest(url, dom_elements, screenshot)
        action = _action_key(last_action)
        selector = action[1]
        if selector:
            self._attempted.add(selector)
        first = self._prev_digest is None
        unchanged = (not first) and digest == self._prev_digest
        self._prev_digest = digest
        self._history.append((url, action))
        self._history = self._history[-4:]

        if unchanged:
            self._stall_count += 1
            if selector and selector not in self._blacklist:
                self._blacklist.append(selector)
        else:
            self._stall_count = 0

        cycle = self._is_cycle()
        if self._stall_count < self.stall_threshold and not cycle:
            return None

        count = max(self._stall_count, self.stall_threshold if cycle else 0)
        if cycle and selector and selector not in self._blacklist:
            self._blacklist.append(selector)
        candidates = self._candidates(dom_elements)
        escalate = self._escalations < self.max_escalations
        if escalate:
            self._escalations += 1
        vision = not candidates and (screenshot is not None or not dom_elements)
        reason = (
            "actions alternate between two states"
            if cycle
            else (f"{count} steps changed nothing")
        )
        hint = f"No progress: {reason}."
        if self._blacklist:
            hint += f" Do not retry: {', '.join(self._blacklist)}."
        if candidates:
            hint += f" Untried controls: {', '.join(c['selector'] for c in candidates[:5])}."
        elif vision:
            hint += " No untried DOM controls; inspect the screenshot."
        return StallRecoveryPlan(
            is_stalled=True,
            stall_count=count,
            blacklisted_selectors=list(self._blacklist),
            surfaced_form_candidates=candidates,
            escalate_effort=escalate,
            switch_to_vision=vision,
            hint=hint,
        )

    def _is_cycle(self) -> bool:
        if len(self._history) < 4:
            return False
        a, b, c, d = self._history
        return a == c and b == d and a != b

    def _candidates(
        self, dom_elements: Sequence[Mapping[str, Any]] | None
    ) -> list[dict[str, Any]]:
        found: list[tuple[int, int, dict[str, Any]]] = []
        for index, el in enumerate(dom_elements or []):
            selector = str(el.get("selector") or "")
            if not selector or selector in self._attempted:
                continue
            if el.get("disabled"):
                continue
            tag = str(el.get("tag") or "").lower()
            kind = str(el.get("type") or "").lower()
            is_control = tag in _INPUT_TAGS or (tag == "button" and kind == "submit")
            in_region = any(el.get(flag) for flag in _REGION_FLAGS)
            if not is_control and not (in_region and _is_interactive(el)):
                continue
            found.append((_candidate_rank(el), index, dict(el)))
        found.sort(key=lambda item: (item[0], item[1]))
        return [item[2] for item in found]


__all__ = [
    "CompiledCommitMacro",
    "CompiledMacroStep",
    "MacroStore",
    "NoProgressDetector",
    "StallRecoveryPlan",
    "distill_verified_trace",
    "instantiate_macro",
]
