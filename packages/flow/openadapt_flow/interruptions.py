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
   "Accept" and any write word are outside it. The label is the live
   control's, not only the declared one: before the click, the backend must
   show that the element the selector or id resolved to is the one control
   with the declared role and accessible name (:func:`live_control_error`).
c. **No write.** The dismissal runs under the run's write watchers
   (:mod:`openadapt_flow.runtime.effect_watch`): the browser's write fence
   and the system-of-record read-back. A watcher that sees a write, or a run
   with none, halts.
   The whole interruption is classified, never its button word: the
   backend reads the full dialog (text, title, owning window), and one
   that mentions a patient, a record, or saving, applying or changing
   anything (:data:`BUSINESS_WORDS`) is never benign. A backend that
   can't read the whole dialog (a pixel surface) can't prove the class,
   so the run halts.
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


#: Words that make an interruption a business question, never a benign one:
#: it mentions record content, a patient, or saving, applying or changing
#: something ("Keep my changes?", "Apply template to patient?"). A dialog
#: whose whole text has one is never cleared automatically, whatever its
#: button says.
BUSINESS_WORDS = re.compile(
    r"\b(?:patients?|records?|charts?|encounters?|orders?|prescri\w*|"
    r"medications?|claims?|invoices?|payments?|transfers?|templates?|"
    r"sav(?:e|es|ed|ing)|appl(?:y|ies|ied|ying)|chang(?:e|es|ed|ing)|"
    r"discard\w*|delet\w*|remov\w*|submit\w*|approv\w*|confirm\w*|"
    r"overwrit\w*|replac\w*|unsaved|undo|revert\w*)\b",
    re.IGNORECASE,
)

#: Text a class's whole dialog may hold beyond its declared texts and its
#: controls' labels: a short system message, nothing more.
MAX_RESIDUAL_CHARS = 240


def business_words(text: str) -> list[str]:
    """The business words in ``text`` (see :data:`BUSINESS_WORDS`)."""

    return sorted({m.group(0).lower() for m in BUSINESS_WORDS.finditer(text)})


def class_texts(interstitial: "Interstitial") -> list[str]:
    """The texts a class is detected by (its ``TEXT_PRESENT`` operands)."""

    from openadapt_flow.ir import PredicateKind

    out: list[str] = []

    def walk(pred: Any) -> None:
        if pred.kind is PredicateKind.TEXT_PRESENT and pred.text:
            out.append(pred.text)
        elif pred.kind is PredicateKind.AND:
            for op in pred.operands:
                walk(op)

    walk(interstitial.detect)
    return out


def _squash(text: str) -> str:
    return " ".join(text.split()).casefold()


def whole_interruption_error(
    interstitial: "Interstitial", found: Optional[Mapping[str, str]]
) -> Optional[str]:
    """Why the interruption on screen is not this class, judged on the whole
    dialog (``found``: its ``text``, ``title`` and ``window`` from the
    backend's ``interruption_text``), or None when it is.

    The button word never decides. The dialog must show every class text,
    carry the class's title and window when the class declares them, hold
    no business word anywhere, and say little beyond the class texts.
    """

    if found is None:
        return (
            "the whole interruption could not be read (its dialog, banner or "
            "toast, with its title and window), so its class can't be proven"
        )
    text = _squash(found.get("text") or "")
    title = _squash(found.get("title") or "")
    window = (found.get("window") or "").strip()
    texts = class_texts(interstitial)
    missing = [t for t in texts if _squash(t) not in text]
    if missing:
        return f"the interruption does not show the class text {missing}"
    words = business_words(" ".join((text, title)))
    if words:
        return (
            f"the interruption mentions {', '.join(words)}: it is a business "
            "question, not a benign interruption"
        )
    if interstitial.dialog_title and _squash(interstitial.dialog_title) != title:
        return f"the interruption's title is {title!r}, not the class's"
    anchor = interstitial.dismiss_anchor
    want_window = (
        anchor.structural.window_name
        if anchor is not None and anchor.structural is not None
        else None
    )
    if want_window and want_window.strip() != window:
        return "the interruption belongs to a different window than the class's"
    residual = text
    labels = [*texts, dismiss_label(interstitial) or ""]
    for part in sorted({_squash(p) for p in labels if p}, key=len, reverse=True):
        residual = residual.replace(part, " ")
    residual = " ".join(residual.split())
    if len(residual) > MAX_RESIDUAL_CHARS:
        return (
            f"the interruption says {len(residual)} characters beyond the "
            "class's texts, more than a system message"
        )
    return None


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
    words = business_words(" ".join(class_texts(interstitial)))
    words += business_words(interstitial.dialog_title or "")
    if words:
        return (
            f"an interruption class can't mention {', '.join(sorted(set(words)))}: "
            "that is a business question, not a benign interruption"
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
    if (
        structural is None
        or not (structural.role or "").strip()
        or not (structural.name or "").strip()
    ):
        # The label that is checked must be the label of the control that is
        # clicked. Only a declared role and accessible name can be compared
        # with the live control (:func:`live_control_error`); an OCR text or
        # a template can't.
        return (
            "the dismiss control must declare its structural role and "
            "accessible name, so its identity can be checked on the live "
            "control before the click"
        )
    return None


def live_control_error(
    interstitial: "Interstitial", backend: Any, handle: Any
) -> Optional[str]:
    """Why the control ``handle`` resolved to is not the declared dismiss
    control, or None when it is.

    The closed-set label check (:func:`interruption_class_error`) is made on
    the *declared* name. A selector or automation id resolves first and may
    point at any control, so the declared name proves nothing until the
    live control is shown to carry it: the backend's
    ``structural_control_is(locator, handle, role=, name=)`` must confirm that
    the very element bound by ``handle`` is the one control in its scope with
    the declared role and exact accessible name. A backend that can't say,
    a missing handle, or any error halts before the click.
    """

    anchor = interstitial.dismiss_anchor
    if anchor is None:
        return None  # a key dismissal (Escape only) has no control
    locator = anchor.structural
    role = (locator.role or "").strip() if locator is not None else ""
    name = (locator.name or "").strip() if locator is not None else ""
    if locator is None or not role or not name:
        return "the dismiss control declares no role and name to check"
    if not is_non_committing_label(name):
        return f"the dismiss control {name!r} is not a non-committing label"
    if handle is None:
        return (
            "the dismiss control was not bound to one live element, so its "
            "label can't be checked"
        )
    check = getattr(backend, "structural_control_is", None)
    if not callable(check):
        return (
            "this backend can't read the live control's role and name, so "
            "the control that would be clicked can't be shown to be "
            f"{name!r}"
        )
    try:
        same = check(locator, handle, role=role, name=name) is True
    except Exception:  # noqa: BLE001 - unreadable means unproven
        same = False
    if not same:
        return (
            f"the control that would be clicked is not the {role} {name!r} the "
            "class declares (its selector or id resolves to a different "
            "control)"
        )
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
    title = entry.get("title")
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
            dialog_title=str(title) if title else None,
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
    if interstitial.dialog_title:
        entry["title"] = interstitial.dialog_title
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


class LabelChangedDuringClick(RuntimeError):
    """The dismiss control, or what labels it, changed while it was being
    clicked, and the click guard blocked the input. Delivery was attempted;
    the outcome must be reconciled, never retried."""

    def __init__(self, violations: Sequence[str]) -> None:
        self.violations = [str(v) for v in violations]
        super().__init__(
            "the control's label changed during the click; the guard blocked "
            "the input (" + "; ".join(self.violations) + ")"
        )


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
    reader = getattr(backend, "interruption_text", None)
    whole = reader(locator, list(texts)) if callable(reader) else None
    error = whole_interruption_error(interstitial, whole)
    if error is not None:
        raise LearnRefused(error)
    handle = locate(locator)
    if handle is None:
        raise LearnRefused(f"{dismiss_label(interstitial)!r} did not resolve uniquely")
    control_error = live_control_error(interstitial, backend, handle)
    if control_error is not None:
        raise LearnRefused(control_error)
    opener = getattr(backend, "structural_label_guard", None)
    if not callable(opener):
        raise LearnRefused(
            "this backend can't keep the control's label for the whole click, "
            "so the class can't be learned here"
        )
    role = (locator.role or "").strip()
    label = (locator.name or "").strip()
    with watch_effects(backend, effect_verifier) as report:
        if report.refusal is not None:
            raise LearnRefused(f"the dismissal can't be watched: {report.refusal}")
        guarded: Any = None
        try:
            with opener(locator, handle, role=role, name=label) as guarded:
                if guarded.refusal is not None:
                    raise LearnRefused(f"the click can't be guarded: {guarded.refusal}")
                act(locator, handle)
        except LearnRefused:
            raise
        except Exception as exc:
            if guarded is not None and guarded.violations:
                raise LearnRefused(
                    str(LabelChangedDuringClick(guarded.violations))
                ) from exc
            raise
        if guarded.violations:
            raise LearnRefused(str(LabelChangedDuringClick(guarded.violations)))
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
