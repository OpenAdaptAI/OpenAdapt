"""Learn an informational screen's dismissal during a repair, from evidence.

A What's New screen or a notice can replace the screen a step expects. Its
dismiss control is usually labelled "Continue", "Next", "OK" or "Close",
and "continue" and "next" read as writes: a wizard's Continue often
submits. Self-repair therefore classifies such a control from what it did,
never from its label. It learns the dismissal only when all of this holds:

1. **A person's recording shows it.** A recording made with network capture
   (``record --capture-network``) has a click on the same control (same
   selector, same label) with no request other than ``GET``, ``HEAD`` or
   ``OPTIONS`` in its window, and URL, title and page count unchanged, as
   the compile-side rule requires (``docs/design/LOCAL_EDIT_CLASSIFICATION.md``).
2. **The click on the halted screen wrote nothing.** Self-repair clicks it
   behind the browser's write fence (:mod:`openadapt_flow.backends._write_fence`),
   which blocks and reports every write request and every new WebSocket and
   refuses to go up when a socket is already open. No attempt, URL, title and
   page count unchanged, and the control's label gone afterwards.

The result is a workflow :class:`~openadapt_flow.ir.Interstitial`, not a new
step: it is dismissed only when it is on screen, and always behind the same
fence (``write_fence=True``), so a dismissal that tries to write at run time
is blocked and halts the run. The probe, the campaigns and a person's
approval follow as for any repair.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from openadapt_flow.ir import (
    Anchor,
    Interstitial,
    Predicate,
    PredicateKind,
)

#: The dismiss labels the compile-side rule accepts (``compiler.local_edit``).
_DISMISS_LABEL = re.compile(
    r"^\s*(?:continue(?:\s+to\s+\w+(?:\s+\w+)?)?|next|close|ok(?:ay)?|got\s+it|"
    r"dismiss|not\s+now|maybe\s+later|skip\s+for\s+now)\s*$",
    re.IGNORECASE,
)
_DISMISS_ROLES = frozenset({"button", "link"})


class DismissalRefused(RuntimeError):
    """The control can't be learned as a dismissal; the reason says why."""


def is_dismiss_label(label: Optional[str]) -> bool:
    """A label a dismiss control commonly has, with no other write word."""

    from openadapt_flow.risk import only_weak_write_words

    return bool(label and _DISMISS_LABEL.match(label) and only_weak_write_words(label))


def _squash(text: Optional[str]) -> str:
    return " ".join(str(text or "").split()).casefold()


def _jsonl(path: Path) -> list[dict[str, Any]]:
    from openadapt_flow import sealed_set

    text = sealed_set.read_text(path)
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def recorded_no_write(
    recordings: Sequence[Path], selector: str, label: str
) -> Optional[str]:
    """Where a person's captured recording shows this control writing nothing.

    Returns ``"<recording>#<event>"`` for the first click on the control
    (same selector and label, a button or link) whose window has no write
    request and whose URL, title and page count stayed the same, else None.
    A recording without network capture proves nothing.
    """

    from openadapt_flow import sealed_set
    from openadapt_flow.compiler.local_edit import (
        _no_write,
        _store_unchanged,
        _unchanged,
    )
    from openadapt_flow.network_capture import NETWORK_FILE
    from openadapt_flow.network_capture import load as load_network

    for recording in recordings:
        recording = Path(recording)
        events_path = recording / "events.jsonl"
        if not sealed_set.is_file(events_path) or not sealed_set.is_file(
            recording / NETWORK_FILE
        ):
            continue
        try:
            events = _jsonl(events_path)
            network = load_network(recording)
        except Exception:  # noqa: BLE001 - unreadable evidence proves nothing
            continue
        for i, event in enumerate(events):
            target = event.get("structural") or {}
            if (
                event.get("kind") != "click"
                or target.get("selector") != selector
                or _squash(target.get("name")) != _squash(label)
                or str(target.get("role") or "").lower() not in _DISMISS_ROLES
            ):
                continue
            if (
                _unchanged(event)
                and _no_write(events, i, network) is True
                and _store_unchanged(event) is not False
            ):
                return f"{recording.name}#{event.get('i', i)}"
    return None


@dataclass(frozen=True)
class LearnedDismissal:
    interstitial: Interstitial
    template: str
    crop: bytes
    after: bytes
    evidence: str


def learn_dismissal(
    *,
    name: str,
    template: str,
    label: str,
    structural: Any,
    point: tuple[int, int],
    region: tuple[int, int, int, int],
    crop: bytes,
    ocr_text: Optional[str],
    backend: Any,
    recordings: Sequence[Path],
    settle: Any,
    shows: Any,
) -> LearnedDismissal:
    """Click the control behind the write fence and learn it, or refuse.

    ``settle()`` returns the settled screen; ``shows(screen, text)`` says
    whether ``text`` is on it. Refusals before the click leave the page as it
    was; a refusal after it (a blocked write, a changed page) means the page
    changed and the caller must start again from a fresh halt.
    """

    if structural is None or not getattr(structural, "selector", None):
        raise DismissalRefused(
            f"{label!r} has no structural selector, so no recording can be "
            "matched to it"
        )
    role = str(getattr(structural, "role", "") or "").lower()
    if role not in _DISMISS_ROLES:
        raise DismissalRefused(f"{label!r} is a {role or 'control'}, not a button")
    evidence = recorded_no_write(recordings, structural.selector, label)
    if evidence is None:
        raise DismissalRefused(
            f"{label!r} reads as a write, and no recording made with network "
            "capture shows a person clicking it without a write. Record it once "
            "(`openadapt-flow record --capture-network`) and pass the recording "
            "as repair evidence (`--repair-evidence-recording`)"
        )
    opener = getattr(backend, "write_fence", None)
    if not callable(opener):
        raise DismissalRefused(
            "this backend cannot block writes, so the click can't be proven "
            "write-free"
        )

    def read(attr: str) -> Any:
        try:
            return getattr(backend, attr)
        except Exception:  # noqa: BLE001 - unreadable is refused below
            return None

    before = {k: read(k) for k in ("url", "page_title", "page_count")}
    if any(v is None for v in before.values()):
        raise DismissalRefused("URL, title or page count could not be read")
    with opener() as report:
        if report.refusal is not None:
            raise DismissalRefused(f"the write fence could not go up: {report.refusal}")
        backend.click(int(point[0]), int(point[1]))
        after = settle()
    if report.attempts:
        raise DismissalRefused(
            f"clicking {label!r} tried to write "
            f"({', '.join(sorted(set(report.attempts)))}); the fence blocked it, "
            "and it is not learned"
        )
    now = {k: read(k) for k in ("url", "page_title", "page_count")}
    if now != before:
        changed = ", ".join(k for k in before if before[k] != now[k])
        raise DismissalRefused(f"clicking {label!r} changed the page ({changed})")
    if shows(after, label):
        raise DismissalRefused(f"{label!r} is still on screen after the click")
    anchor = Anchor(
        template=template,
        structural=structural,
        region=region,
        click_point=point,
        ocr_text=ocr_text,
    )
    interstitial = Interstitial(
        name=name,
        detect=Predicate(
            kind=PredicateKind.AND,
            operands=[
                Predicate(kind=PredicateKind.TEXT_PRESENT, text=label),
                Predicate(
                    kind=PredicateKind.ANCHOR_RESOLVES, anchor=anchor, intent=label
                ),
            ],
        ),
        dismiss_anchor=anchor,
        risk="reversible",
        consequential=False,
        clearance=Predicate(kind=PredicateKind.TEXT_ABSENT, text=label),
        write_fence=True,
    )
    return LearnedDismissal(
        interstitial=interstitial,
        template=template,
        crop=crop,
        after=after,
        evidence=evidence,
    )
