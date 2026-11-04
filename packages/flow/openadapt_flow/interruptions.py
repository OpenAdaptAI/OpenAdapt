"""Interruption classes: benign interruptions a run may clear by itself.

Production apps interrupt: "Your session expires in 5 minutes", an "Update
available" toast, a feature tour, a banner, a modal from background polling.
Each would halt a compiled run for a person. An *interruption class* lets the
run clear one itself, but only when all four of these hold
(``docs/INTERRUPTION_RECOVERY.md``):

a. **Identity.** It matches the class by what it says and what its control
   is: every one of the class's texts is on screen, and the dismiss control
   resolves by its role, name, selector or window, never by position.
b. **A non-committing action.** The dismissal is Escape or a control whose
   label is in a closed set (:data:`NON_COMMITTING_LABEL`): close, dismiss,
   not now, continue session, and the like. "OK", "Continue", "Yes",
   "Accept" and any write word are outside it.
c. **No write.** The dismissal runs under the run's write watchers
   (:mod:`openadapt_flow.runtime.effect_watch`): the browser's write fence
   and the system-of-record read-back. A watcher that sees a write, or a run
   with none, halts.
d. **The expected screen comes back.** The class's text is gone, and the
   step then goes through its normal identity check; a screen that isn't
   the one the step expects halts there.

Every dismissal is written to the step's report and to the write receipt.

Classes are data, never engine code: a per-app YAML file (see
:func:`load_interruption_classes`) or a class a person taught from a halt
(:func:`learn_interruption`). Each becomes an :class:`~openadapt_flow.ir.Interstitial`
with ``effect_watch=True``, so the replayer's existing interstitial path
(one audited action, re-settle, clearance check, no retry) applies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional, Sequence

if TYPE_CHECKING:  # pragma: no cover
    from openadapt_flow.ir import Interstitial, Predicate

#: The labels a dismiss control may carry. Closed on purpose: a label outside
#: it ("OK", "Continue", "Yes", "Accept", "Submit") may commit something, so
#: it is never cleared automatically, whatever a watcher would have seen.
NON_COMMITTING_LABEL = re.compile(
    r"^\s*(?:close(?:\s+(?:dialog|banner|notification|message|tour|this))?|"
    r"dismiss(?:\s+(?:all|notification|message|banner))?|"
    r"not\s+now|maybe\s+later|later|remind\s+me\s+later|no,?\s+thanks|"
    r"skip(?:\s+(?:for\s+now|tour|this))?|got\s+it|hide|"
    r"continue\s+(?:session|working)|stay\s+(?:signed|logged)\s+in|"
    r"keep\s+me\s+(?:signed|logged)\s+in|extend\s+(?:my\s+)?session|"
    r"i'?m\s+still\s+here)\s*[.!]?\s*$",
    re.IGNORECASE,
)


def is_non_committing_label(label: Optional[str]) -> bool:
    """Whether ``label`` is one of the closed non-committing dismiss labels."""

    return bool(label and NON_COMMITTING_LABEL.match(label))


def dismiss_label(interstitial: "Interstitial") -> Optional[str]:
    """The dismiss control's label: its accessible name, else its OCR text."""

    anchor = interstitial.dismiss_anchor
    if anchor is None:
        return None
    structural = anchor.structural
    if structural is not None and structural.name and structural.name.strip():
        return structural.name.strip()
    if anchor.ocr_text and anchor.ocr_text.strip():
        return anchor.ocr_text.strip()
    return None


def _has_text_identity(pred: "Predicate") -> bool:
    from openadapt_flow.ir import PredicateKind

    if pred.kind is PredicateKind.TEXT_PRESENT:
        return bool(pred.text and pred.text.strip())
    if pred.kind is PredicateKind.AND:
        return any(_has_text_identity(op) for op in pred.operands)
    if pred.kind is PredicateKind.OR:
        return bool(pred.operands) and all(
            _has_text_identity(op) for op in pred.operands
        )
    return False


def interruption_class_error(interstitial: "Interstitial") -> Optional[str]:
    """Why ``interstitial`` can't be an automatically cleared interruption
    class, or None when it can. Checked on load and again at run time."""

    if interstitial.dismiss_key is None and interstitial.dismiss_anchor is None:
        return "an interruption class must declare its dismissal"
    if not _has_text_identity(interstitial.detect):
        return (
            "an interruption class must be detected by its text (TEXT_PRESENT), "
            "not only by how it looks or where it is"
        )
    if interstitial.dismiss_key is not None:
        return None  # Escape only (Interstitial's own validation)
    label = dismiss_label(interstitial)
    if not is_non_committing_label(label):
        return (
            f"the dismiss control {label!r} is not a non-committing label "
            "(close, dismiss, not now, continue session, ...); it may commit "
            "something, so it is never cleared automatically"
        )
    anchor = interstitial.dismiss_anchor
    assert anchor is not None
    structural = anchor.structural
    has_identity = bool(anchor.template.strip()) or (
        structural is not None
        and any(
            (v or "").strip()
            for v in (structural.selector, structural.automation_id, structural.name)
        )
    )
    if not has_identity:
        return "the dismiss control must be found by its identity, not its position"
    return None


# -- per-app data files ---------------------------------------------------------


class InterruptionClassError(ValueError):
    """An interruption class file or entry that can't be used."""


def interruption_from_data(entry: Mapping[str, Any]) -> "Interstitial":
    """One class from its data-file entry.

    ``{name, texts: [..], dismiss: {key: Escape} | {control: {role, name,
    selector, automation_id, window}}, gone: <text>}``. ``gone`` defaults to
    the first text: the clearance is that text no longer on screen.
    """

    from openadapt_flow.ir import (
        Anchor,
        Interstitial,
        Predicate,
        PredicateKind,
        StructuralLocator,
    )

    name = str(entry.get("name") or "").strip()
    texts = [str(t) for t in entry.get("texts") or () if str(t).strip()]
    if not name or not texts:
        raise InterruptionClassError("each interruption needs a name and texts")
    dismiss = entry.get("dismiss") or {}
    if not isinstance(dismiss, Mapping):
        raise InterruptionClassError(f"{name}: dismiss must be a mapping")
    key = dismiss.get("key")
    control = dismiss.get("control")
    anchor = None
    if control is not None:
        if not isinstance(control, Mapping):
            raise InterruptionClassError(f"{name}: dismiss.control must be a mapping")
        anchor = Anchor(
            template="",
            structural=StructuralLocator(
                selector=control.get("selector"),
                role=control.get("role"),
                name=control.get("name"),
                automation_id=control.get("automation_id"),
                window_name=control.get("window"),
            ),
            region=(0, 0, 0, 0),
            click_point=(0, 0),
            ocr_text=control.get("name"),
        )
    present = [Predicate(kind=PredicateKind.TEXT_PRESENT, text=t) for t in texts]
    detect = (
        present[0]
        if len(present) == 1
        else Predicate(kind=PredicateKind.AND, operands=present)
    )
    gone = str(entry.get("gone") or texts[0])
    try:
        return Interstitial(
            name=name,
            detect=detect,
            dismiss_key=key,
            dismiss_anchor=anchor,
            risk="reversible",
            consequential=False,
            clearance=Predicate(kind=PredicateKind.TEXT_ABSENT, text=gone),
            effect_watch=True,
        )
    except Exception as exc:
        raise InterruptionClassError(f"{name}: {exc}") from exc


def interruption_to_data(interstitial: "Interstitial") -> dict[str, Any]:
    """The data-file entry for a class, the inverse of
    :func:`interruption_from_data` for classes it can express."""

    from openadapt_flow.ir import PredicateKind

    detect = interstitial.detect
    operands = detect.operands if detect.kind is PredicateKind.AND else [detect]
    texts = [
        op.text for op in operands if op.kind is PredicateKind.TEXT_PRESENT and op.text
    ]
    entry: dict[str, Any] = {"name": interstitial.name, "texts": texts}
    if interstitial.dismiss_key is not None:
        entry["dismiss"] = {"key": interstitial.dismiss_key}
    else:
        anchor = interstitial.dismiss_anchor
        assert anchor is not None and anchor.structural is not None
        s = anchor.structural
        control = {
            "role": s.role,
            "name": s.name,
            "selector": s.selector,
            "automation_id": s.automation_id,
            "window": s.window_name,
        }
        entry["dismiss"] = {"control": {k: v for k, v in control.items() if v}}
    clearance = interstitial.clearance
    if (
        clearance is not None
        and clearance.text
        and texts
        and clearance.text != texts[0]
    ):
        entry["gone"] = clearance.text
    return entry


def load_interruption_classes(path: Path | str) -> list["Interstitial"]:
    """The classes in a per-app interruption file (YAML)::

        interruptions:
          - name: session expiry warning
            texts: ["Your session will expire"]
            dismiss: {control: {role: button, name: Stay signed in}}
          - name: product tour
            texts: ["Take a quick tour"]
            dismiss: {key: Escape}

    Raises :class:`InterruptionClassError` on any entry that fails the
    read-only policy, so a bad file stops the run before it starts.
    """

    import yaml

    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    entries = data.get("interruptions") if isinstance(data, Mapping) else None
    if not isinstance(entries, list):
        raise InterruptionClassError(f"{path}: expected an 'interruptions' list")
    classes = [interruption_from_data(entry) for entry in entries]
    names = [c.name for c in classes]
    if len(set(names)) != len(names):
        raise InterruptionClassError(f"{path}: interruption names must be unique")
    return classes


def save_interruption_class(path: Path | str, interstitial: "Interstitial") -> None:
    """Add (or replace by name) one class in a per-app interruption file."""

    import yaml

    path = Path(path)
    data: dict[str, Any] = {"interruptions": []}
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if isinstance(loaded, Mapping):
            data = dict(loaded)
    entries = [
        e
        for e in data.get("interruptions") or []
        if not (isinstance(e, Mapping) and e.get("name") == interstitial.name)
    ]
    entries.append(interruption_to_data(interstitial))
    data["interruptions"] = entries
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


# -- learning a class from a halt -----------------------------------------------


class LearnRefused(RuntimeError):
    """The interruption can't be learned as a class; the reason says why."""


@dataclass(frozen=True)
class LearnedInterruption:
    interstitial: "Interstitial"
    #: What the watchers saw: the proof the dismissal wrote nothing.
    watchers: tuple[str, ...]


def learn_interruption(
    *,
    name: str,
    texts: Sequence[str],
    control: Mapping[str, Any],
    backend: Any,
    effect_verifier: Any = None,
    shows: Callable[[str], bool],
    settle: Callable[[], Any] = lambda: None,
) -> LearnedInterruption:
    """Offer to learn the interruption a person is clearing during a halt.

    ``control`` is the control the person pointed at (``role``, ``name``,
    ``selector``, ...). The class is built and checked against the read-only
    policy first (identity, non-committing label); then this function clicks
    the control under the run's write watchers on the live screen and
    learns the class only when no watcher saw a write and the interruption's
    text is gone (``shows(text)`` is False). A refusal after the click
    means the screen changed, and the person continues by hand.
    """

    from openadapt_flow.ir import StructuralLocator
    from openadapt_flow.runtime.effect_watch import watch_effects

    interstitial = interruption_from_data(
        {"name": name, "texts": list(texts), "dismiss": {"control": dict(control)}}
    )
    missing = [t for t in texts if not shows(t)]
    if missing:
        raise LearnRefused(f"the interruption's text is not on screen: {missing}")
    locate = getattr(backend, "locate_structural", None)
    act = getattr(backend, "act_structural", None)
    if not callable(locate) or not callable(act):
        raise LearnRefused(
            "this backend can't find the control by its identity, so the "
            "class can't be learned here"
        )
    anchor = interstitial.dismiss_anchor
    assert anchor is not None and anchor.structural is not None
    locator: StructuralLocator = anchor.structural
    handle = locate(locator)
    if handle is None:
        raise LearnRefused(f"{dismiss_label(interstitial)!r} did not resolve uniquely")
    with watch_effects(backend, effect_verifier) as report:
        if report.refusal is not None:
            raise LearnRefused(f"the dismissal can't be watched: {report.refusal}")
        act(locator, handle)
        settle()
    if not report.held:
        raise LearnRefused(
            "the dismissal tried to write ("
            + "; ".join(report.attempts)
            + "), so it is not learned"
        )
    gone = interstitial.clearance.text if interstitial.clearance else texts[0]
    if gone and shows(gone):
        raise LearnRefused(f"{gone!r} is still on screen after the dismissal")
    return LearnedInterruption(
        interstitial=interstitial, watchers=tuple(report.watchers)
    )
