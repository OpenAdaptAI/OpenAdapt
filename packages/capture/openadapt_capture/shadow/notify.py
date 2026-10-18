"""The prompt a person sees when a motif fires, and what they can answer.

This module is the hook a tray or notification surface implements. Capture
has no UI; OpenAdapt Desktop implements it in its tray and window (see
``docs/SHADOW_MINING.md``).

The prompt text is fixed. It carries a count and nothing else: no application
name, no control label, no value. A notification can show on a lock screen or
a shared display, so it must not say what the person was doing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

PROMPT_TITLE = "OpenAdapt noticed a repeated task"
PROMPT_FIELDS = frozenset({"motif_id", "count", "title", "body", "actions"})
MAX_PROMPT_COUNT = 9999


class PromptAction(str, Enum):
    """The two answers a person can give."""

    REVIEW_DRAFT = "review_draft"
    IGNORE = "ignore"


PROMPT_ACTIONS: tuple[PromptAction, ...] = (PromptAction.REVIEW_DRAFT, PromptAction.IGNORE)
ACTION_LABELS = {PromptAction.REVIEW_DRAFT: "Review draft", PromptAction.IGNORE: "Ignore"}


class PromptLeak(AssertionError):
    """A prompt carried content it must never carry."""


@dataclass(frozen=True)
class MotifPrompt:
    """One offer to turn a repeated task into a draft."""

    motif_id: str
    count: int

    @property
    def title(self) -> str:
        return PROMPT_TITLE

    @property
    def body(self) -> str:
        n = max(0, min(int(self.count), MAX_PROMPT_COUNT))
        return f"You did this {n} times today."

    @property
    def actions(self) -> tuple[PromptAction, ...]:
        return PROMPT_ACTIONS

    def payload(self) -> dict[str, object]:
        """The complete notification payload."""

        payload: dict[str, object] = {
            "motif_id": self.motif_id,
            "count": max(0, min(int(self.count), MAX_PROMPT_COUNT)),
            "title": self.title,
            "body": self.body,
            "actions": [{"id": a.value, "label": ACTION_LABELS[a]} for a in self.actions],
        }
        assert_generic_prompt(payload)
        return payload


def assert_generic_prompt(payload: dict[str, object]) -> None:
    """Refuse a payload with extra fields or non-template text."""

    extra = set(payload) - PROMPT_FIELDS
    if extra:
        raise PromptLeak(f"prompt carries unexpected fields: {sorted(extra)}")
    if payload.get("title") != PROMPT_TITLE:
        raise PromptLeak("prompt title is not the fixed template")
    count = payload.get("count")
    if not isinstance(count, int) or isinstance(count, bool):
        raise PromptLeak("prompt count must be an integer")
    if payload.get("body") != f"You did this {count} times today.":
        raise PromptLeak("prompt body is not the fixed template")
    motif_id = payload.get("motif_id")
    if not isinstance(motif_id, str) or len(motif_id) != 16:
        raise PromptLeak("prompt motif_id must be a 16-character digest")
    int(motif_id, 16)


@runtime_checkable
class MotifNotifier(Protocol):
    """Implemented by a tray or notification surface."""

    def offer(self, prompt: MotifPrompt) -> None:
        """Show the prompt. The answer comes back through
        :meth:`~openadapt_capture.shadow.observer.ShadowObserver.decide`."""
        ...


class CollectingNotifier:
    """A stub notifier that keeps prompts in a list. Used by tests and as the
    reference for a real tray integration."""

    def __init__(self) -> None:
        self.prompts: list[MotifPrompt] = []

    def offer(self, prompt: MotifPrompt) -> None:
        prompt.payload()  # validates
        self.prompts.append(prompt)


_HEX8 = re.compile(r"^[0-9a-f]{8}$")


def _coarse(token: str) -> str:
    """A token without its keyed label part.

    Keys are destroyed every 24 hours, so a keyed part would stop matching the
    next day and an ignored task would come back. The coarse form keeps the
    kind, app, and role, which is enough to recognize the task again.
    """

    return "|".join("#" if _HEX8.match(part) else part for part in token.split("|"))


def _bigram_marks(tokens: tuple[str, ...]) -> frozenset[str]:
    coarse = [_coarse(t) for t in tokens]
    return frozenset(
        hashlib.sha256(f"{a}\x1f{b}".encode()).hexdigest()[:12] for a, b in zip(coarse, coarse[1:])
    )


class SuppressionList:
    """Motifs a person chose to ignore.

    A motif is suppressed by its id and also by structural resemblance: a later
    motif whose symbol pairs mostly match an ignored one (Jaccard similarity of
    hashed bigrams at or above ``resemblance``) stays quiet too, so ignoring a
    task isn't undone by one extra click. Only ids and hashed bigrams are kept,
    never symbols. Bigrams are taken over coarse tokens (no keyed part), so an
    ignore still holds after the daily key rotation. Pass ``path`` to remember
    ignores across restarts.
    """

    def __init__(self, path: Path | None = None, *, resemblance: float = 0.6) -> None:
        self._path = Path(path) if path is not None else None
        self._resemblance = resemblance
        self._ids: set[str] = set()
        self._marks: list[frozenset[str]] = []
        if self._path is not None and self._path.exists():
            try:
                data = json.loads(self._path.read_text())
                self._ids = {str(i) for i in data.get("ids", [])}
                self._marks = [frozenset(map(str, m)) for m in data.get("marks", [])]
            except (OSError, ValueError, TypeError, AttributeError):
                # A damaged list must not stop shadow mode from starting; the
                # person can ignore the task again.
                self._ids, self._marks = set(), []

    def add(self, motif_id: str, tokens: tuple[str, ...]) -> None:
        self._ids.add(motif_id)
        self._marks.append(_bigram_marks(tokens))
        self._save()

    def is_suppressed(self, motif_id: str, tokens: tuple[str, ...]) -> bool:
        if motif_id in self._ids:
            return True
        marks = _bigram_marks(tokens)
        for ignored in self._marks:
            union = marks | ignored
            if union and len(marks & ignored) / len(union) >= self._resemblance:
                return True
        return False

    def clear(self) -> None:
        self._ids.clear()
        self._marks.clear()
        self._save()

    def _save(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {"ids": sorted(self._ids), "marks": [sorted(m) for m in self._marks]}
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle)
        os.replace(tmp, self._path)
