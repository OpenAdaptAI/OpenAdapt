"""Extra looks before stopping on a look-alike identifier.

On a pixel-only surface (RDP, Citrix, canvas) the OCR identity tier abstains
when the record's identifier carries a look-alike glyph (O/0, l/1/I): OCR can
read ``A1001`` and a different record's ``Al001`` as the same string. Before
the run stops on such an abstain, the replayer takes extra looks that change
nothing on the screen:

- ``frame``: the same OCR read on :data:`FRAME_LOOKS` fresh screenshots. Each
  must abstain on the same look-alike identifier classes as the first read.
  A frame that names another record ends the check with a mismatch.
- ``magnified``: the replayer's existing 2x re-read of the first frame. It
  must not name another record, and when it reads the identifier it must read
  it in the same look-alike classes.
- ``known_ids``: the complete known-ID list (:mod:`.known_ids`) when the
  deployment configured one.
- ``clipboard``: where the surface allows it, a drag across the recorded
  identifier cell, a copy, and a read of the clipboard (:func:`copy_identifier`).
  The clipboard holds the application's own characters, so ``0`` and ``O``
  differ.

OCR looks never settle the question on their own: a look-alike twin can read
the same on every frame and at every magnification. Only two looks can
resolve: the known-ID list, and a clipboard read that equals an EXACT
expected identifier. An exact expected identifier comes only from a run
parameter embedded in the band (the operator's typed or supplied value). A
recorded OCR band is not exact (a recording can read ``A1001`` as ``Alo01``),
so in recorded-record mode the clipboard can contradict but never accept.

:func:`decide` accepts only when every frame agrees, no look contradicts, and
at least one look resolves to the expected record. Any contradiction is
final. Otherwise the original abstain stands, with the looks recorded on it.
"""

from __future__ import annotations

import hashlib
import io
import secrets
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Literal, Mapping, Optional, Sequence

from openadapt_flow.ir import IdentityCheck, IdentityLook, Region
from openadapt_flow.runtime import identity as identity_mod
from openadapt_flow.runtime.known_ids import (
    ABSTAIN_REASON_GLYPH,
    KnownIdList,
    id_core,
    lookalike_class,
    resolve_lookalike_abstain,
)

FRAME_LOOKS = 2
"""Fresh screenshots read after the first one."""

SELECTION_PAD_PX = 6
"""How far outside the identifier cell a selection highlight may paint."""

COPY_GESTURES = ("drag", "double_click")


def identifier_tokens(text: str) -> list[str]:
    """Identifier-shaped tokens of a read, in :func:`known_ids.id_core` form."""
    out = []
    for raw in (text or "").split():
        tok = identity_mod.squash(raw)
        if tok and identity_mod._is_identifier_shaped(tok):
            out.append(id_core(tok))
    return out


def lookalike_classes(text: str) -> frozenset[str]:
    """Look-alike classes of the identifier-shaped tokens in ``text``."""
    return frozenset(lookalike_class(t) for t in identifier_tokens(text))


def vulnerable_classes(text: str) -> frozenset[str]:
    """Classes of the identifiers OCR could have collapsed (O/0, l/1/I)."""
    return frozenset(
        lookalike_class(t)
        for t in identifier_tokens(text)
        if identity_mod._is_glyph_vulnerable_identifier(t)
    )


def is_glyph_abstain(check: Optional[IdentityCheck]) -> bool:
    return (
        check is not None
        and check.status == "abstain"
        and check.abstain_reason == ABSTAIN_REASON_GLYPH
    )


def exact_expected_ids(
    run_values: Mapping[str, Any], embedded: Iterable[str]
) -> tuple[str, ...]:
    """Exact identifiers the operator gave for parameters in the band.

    Only parameters embedded in the recorded band count: a run value that is
    not part of the identity band says nothing about which record this is.
    """
    out: list[str] = []
    for name in embedded:
        value = run_values.get(name)
        if isinstance(value, str):
            out.extend(identifier_tokens(value))
    return tuple(dict.fromkeys(out))


@dataclass(frozen=True)
class IdentityCopyConfig:
    """Turns on the select-and-copy look for a deployment.

    Only for surfaces where selecting the identifier cell and copying it
    changes nothing: a read-only text cell, with the remote session's
    clipboard redirected to the machine Flow runs on. Any screen change
    outside the cell during the look halts the run.
    """

    gesture: str = "drag"
    copy_keys: tuple[str, ...] = ("Control+c",)

    def __post_init__(self) -> None:
        if self.gesture not in COPY_GESTURES:
            raise ValueError(
                f"identity copy gesture must be one of {COPY_GESTURES}, "
                f"not {self.gesture!r}"
            )
        if not self.copy_keys:
            raise ValueError("identity copy needs at least one copy key")


@dataclass(frozen=True)
class LookBinding:
    """A clipboard read taken at step entry, reusable at actuation.

    The actuation-time identity check takes no input. It reuses this read only
    when the identifier cell on its frame shows the same pixels as one of the
    crops seen at step entry.
    """

    step_id: str
    classes: frozenset[str]
    read: "ClipboardRead"
    crops: tuple[bytes, ...]

    def matches(
        self, step_id: str, classes: frozenset[str], crop: Optional[bytes]
    ) -> bool:
        return (
            step_id == self.step_id
            and classes == self.classes
            and any(crops_match(crop, c) for c in self.crops)
        )


@dataclass(frozen=True)
class ClipboardRead:
    """What one select-and-copy look returned.

    Attributes:
        text: The copied text, or None when nothing was copied.
        changed_outside: The screen changed outside the identifier cell
            while the look ran. The step halts.
        pre_crop: The identifier cell's pixels just before the gesture.
        post_crop: The identifier cell's pixels just after the copy.
        frame_sha256: SHA-256 of the frame taken just after the copy.
        reason: Why nothing was copied, when nothing was.
    """

    text: Optional[str]
    changed_outside: bool = False
    pre_crop: Optional[bytes] = None
    post_crop: Optional[bytes] = None
    frame_sha256: Optional[str] = None
    reason: Optional[str] = None


def _look(
    kind: Literal["frame", "magnified", "known_ids", "clipboard"],
    outcome: Literal["agrees", "resolves", "contradicts", "unavailable"],
    reason: Optional[str] = None,
    frame_sha256: Optional[str] = None,
) -> IdentityLook:
    return IdentityLook(
        kind=kind, outcome=outcome, reason=reason, frame_sha256=frame_sha256
    )


def _frame_look(
    first_classes: frozenset[str],
    read: IdentityCheck,
    frame_sha256: Optional[str],
    contradicts: Callable[[IdentityCheck], bool],
) -> IdentityLook:
    if read.status == "mismatch":
        reason = "names_another_record" if contradicts(read) else "read_incomplete"
        return _look("frame", "contradicts", reason, frame_sha256)
    if not is_glyph_abstain(read):
        return _look("frame", "unavailable", f"read_{read.status}", frame_sha256)
    if lookalike_classes(read.observed) != first_classes:
        return _look("frame", "unavailable", "identifier_read_differs", frame_sha256)
    return _look("frame", "agrees", None, frame_sha256)


def _magnified_look(
    first_classes: frozenset[str],
    read: Optional[IdentityCheck],
    contradicts: Callable[[IdentityCheck], bool],
) -> IdentityLook:
    if read is None:
        return _look("magnified", "unavailable", "not_taken")
    if read.status == "mismatch" and contradicts(read):
        return _look("magnified", "contradicts", "names_another_record")
    if read.status in ("unreadable", "mismatch"):
        # Nothing readable, or only part of the band: says nothing either way.
        return _look("magnified", "unavailable", f"read_{read.status}")
    classes = lookalike_classes(read.observed)
    if not classes:
        return _look("magnified", "unavailable", "no_identifier_read")
    if classes != first_classes:
        return _look("magnified", "unavailable", "identifier_read_differs")
    return _look("magnified", "agrees")


def _clipboard_look(
    first_classes: frozenset[str],
    vulnerable: frozenset[str],
    copied: Optional[ClipboardRead],
    exact: Sequence[str],
    known_ids: Optional[KnownIdList],
) -> IdentityLook:
    if copied is None:
        return _look("clipboard", "unavailable", "not_configured")
    sha = copied.frame_sha256
    if copied.text is None:
        return _look("clipboard", "unavailable", copied.reason or "nothing_copied", sha)
    tokens = identifier_tokens(copied.text)
    by_class: dict[str, set[str]] = {}
    for tok in tokens:
        by_class.setdefault(lookalike_class(tok), set()).add(tok)
    if set(by_class) != set(first_classes):
        # The copy did not cover the identifier OCR read (or covered more).
        return _look("clipboard", "unavailable", "copy_differs_from_read", sha)
    if any(len(v) != 1 for v in by_class.values()):
        return _look("clipboard", "unavailable", "copy_ambiguous", sha)
    copied_ids = {k: next(iter(v)) for k, v in by_class.items()}
    if known_ids is not None and known_ids.complete:
        listed = {m for k in copied_ids for m in known_ids.members_of_class(k)}
        if any(core not in listed for core in copied_ids.values()):
            return _look("clipboard", "contradicts", "copied_id_not_listed", sha)
    exact_by_class: dict[str, set[str]] = {}
    for e in exact:
        exact_by_class.setdefault(lookalike_class(e), set()).add(e)
    for cls, core in copied_ids.items():
        expected = exact_by_class.get(cls)
        if expected and core not in expected:
            return _look("clipboard", "contradicts", "copied_id_differs", sha)
    if vulnerable and all(
        cls in exact_by_class and copied_ids[cls] in exact_by_class[cls]
        for cls in vulnerable
    ):
        return _look("clipboard", "resolves", "copied_id_equals_run_value", sha)
    return _look("clipboard", "agrees", "no_exact_expected_id", sha)


def decide(
    first: IdentityCheck,
    *,
    frames: Sequence[tuple[IdentityCheck, Optional[str]]],
    magnified: Optional[IdentityCheck],
    contradicts: Callable[[IdentityCheck], bool],
    known_ids: Optional[KnownIdList] = None,
    known_ids_lift: Optional[IdentityCheck] = None,
    clipboard: Optional[ClipboardRead] = None,
    exact_expected: Sequence[str] = (),
) -> IdentityCheck:
    """Combine the looks into one verdict.

    Args:
        first: The first read: a glyph-only abstain.
        frames: ``(read, frame_sha256)`` per fresh screenshot.
        magnified: The 2x re-read of the first frame, if taken.
        contradicts: Whether a ``mismatch`` read names another record
            (``replayer.identity_read_contradicts``).
        known_ids: The complete known-ID list, for the clipboard cross-check.
        known_ids_lift: The list's verdict on the frame reads
            (:func:`known_ids.resolve_lookalike_abstain`), or None.
        clipboard: The select-and-copy read, or None when not taken.
        exact_expected: Exact identifiers from run parameters in the band.

    Returns:
        ``verified`` (with every look recorded) when every frame agrees, no
        look contradicts and one resolves; the contradicting read (or the
        first, as ``mismatch``) when any look contradicts; otherwise
        ``first`` with the looks recorded.
    """
    if not is_glyph_abstain(first):
        return first
    first_classes = lookalike_classes(first.observed)
    vulnerable = vulnerable_classes(first.observed)
    looks: list[IdentityLook] = []
    for read, sha in frames:
        look = _frame_look(first_classes, read, sha, contradicts)
        looks.append(look)
        if look.outcome == "contradicts":
            # A fresh frame that names another record is final.
            return read.model_copy(update={"looks": looks})
    mag = _magnified_look(first_classes, magnified, contradicts)
    looks.append(mag)
    if mag.outcome == "contradicts":
        assert magnified is not None
        return magnified.model_copy(update={"looks": looks})
    if known_ids is not None or known_ids_lift is not None:
        looks.append(
            _look("known_ids", "resolves", "single_listed_id")
            if known_ids_lift is not None
            else _look("known_ids", "unavailable", "list_does_not_settle_it")
        )
    if clipboard is not None:
        clip = _clipboard_look(
            first_classes, vulnerable, clipboard, exact_expected, known_ids
        )
        looks.append(clip)
        if clip.outcome == "contradicts":
            return first.model_copy(
                update={
                    "status": "mismatch",
                    "abstain_reason": None,
                    "looks": looks,
                }
            )
    frames_agree = bool(frames) and all(
        look.outcome == "agrees" for look in looks if look.kind == "frame"
    )
    magnified_ok = mag.outcome in ("agrees", "unavailable")
    resolved = any(look.outcome == "resolves" for look in looks)
    if frames_agree and magnified_ok and resolved:
        base = known_ids_lift if known_ids_lift is not None else first
        return base.model_copy(
            update={"status": "verified", "abstain_reason": None, "looks": looks}
        )
    return first.model_copy(update={"looks": looks})


# ----------------------------------------------------------- copy look


def copy_region(lines: Iterable[Any], within: Region) -> Optional[Region]:
    """The text box to select: the one line inside ``within`` that holds a
    look-alike identifier.

    Selecting only that line keeps the gesture off every other control in the
    row. None when no line, or more than one, holds such an identifier: the
    copy look is then not taken.
    """
    hits = []
    for line in lines:
        region = getattr(line, "region", None)
        if region is None or not identity_mod.regions_intersect(region, within):
            continue
        if vulnerable_classes(str(getattr(line, "text", ""))):
            hits.append(region)
    if len(hits) != 1:
        return None
    x, y, w, h = (int(v) for v in hits[0])
    return (x - 2, y - 2, w + 4, h + 4)


def crop_png(png: bytes, region: Region) -> Optional[bytes]:
    return identity_mod.crop_region(png, region)


def _diff_outside(pre: bytes, post: bytes, allowed: Region) -> Optional[bool]:
    """Whether ``post`` differs from ``pre`` outside ``allowed``.

    None when the frames cannot be compared (a different size counts as a
    change).
    """
    try:
        import numpy as np
        from PIL import Image

        a = np.asarray(Image.open(io.BytesIO(pre)).convert("RGB"), dtype=np.int16)
        b = np.asarray(Image.open(io.BytesIO(post)).convert("RGB"), dtype=np.int16)
    except Exception:  # noqa: BLE001 - an unreadable frame cannot be judged
        return None
    if a.shape != b.shape:
        return True
    changed = np.abs(a - b).max(axis=2) > 0
    x, y, w, h = allowed
    x0 = max(0, x - SELECTION_PAD_PX)
    y0 = max(0, y - SELECTION_PAD_PX)
    x1 = min(changed.shape[1], x + w + SELECTION_PAD_PX)
    y1 = min(changed.shape[0], y + h + SELECTION_PAD_PX)
    changed[y0:y1, x0:x1] = False
    return bool(changed.any())


def copy_identifier(
    backend: Any,
    clipboard: Any,
    region: Region,
    *,
    gesture: str = "drag",
    copy_keys: Sequence[str] = ("Control+c",),
    settle: Optional[Callable[[], bytes]] = None,
    crop_region: Optional[Region] = None,
) -> ClipboardRead:
    """Select the identifier cell, copy it, and read the clipboard.

    The clipboard's previous text is restored afterwards. The clipboard is
    first set to a random marker; a clipboard still holding the marker counts
    as nothing copied. The frames just before the gesture and just after the
    copy must be identical outside the identifier cell (a selection highlight
    may paint inside it); otherwise ``changed_outside`` is set and the caller
    halts.

    Args:
        backend: The run's backend. ``drag`` needs a ``drag(x, y, ex, ey)``
            method; ``double_click`` uses ``click(x, y, double=True)``.
        clipboard: A :class:`~openadapt_flow.records.read.ClipboardReader`.
        region: The identifier cell on the live frame.
        gesture: ``drag`` (across the cell, left to right, at mid height) or
            ``double_click`` (at the cell's centre).
        copy_keys: Keys that copy the selection on the driven session.
        settle: Returns the next settled frame; defaults to a screenshot.
        crop_region: Where to cut ``pre_crop`` / ``post_crop``; defaults to
            ``region``.
    """
    x, y, w, h = (int(v) for v in region)
    if w < 4 or h < 4:
        return ClipboardRead(text=None, reason="cell_too_small")
    if gesture == "drag" and not callable(getattr(backend, "drag", None)):
        return ClipboardRead(text=None, reason="backend_cannot_drag")
    if gesture not in COPY_GESTURES:
        return ClipboardRead(text=None, reason="unknown_gesture")
    capture = settle or backend.screenshot
    try:
        previous = clipboard.read_text()
    except Exception:  # noqa: BLE001 - no clipboard channel, no look
        return ClipboardRead(text=None, reason="clipboard_unreadable")
    marker = f"openadapt-identity-look-{secrets.token_hex(8)}"
    copied: Optional[str] = None
    pre = post = None
    try:
        clipboard.write_text(marker)
        pre = capture()
        mid_y = y + h // 2
        inset = max(2, min(6, w // 10))
        if gesture == "drag":
            backend.drag(x + inset, mid_y, x + w - inset, mid_y)
        else:
            backend.click(x + w // 2, mid_y, double=True)
        for key in copy_keys:
            backend.press(key)
        post = capture()
        copied = clipboard.read_text()
    except Exception as exc:  # noqa: BLE001 - a failed look is no look
        return ClipboardRead(
            text=None,
            changed_outside=bool(
                pre is not None
                and post is not None
                and _diff_outside(pre, post, region) is not False
            ),
            reason=f"look_failed:{type(exc).__name__}"[:80],
        )
    finally:
        if previous is not None:
            try:
                clipboard.write_text(previous)
            except Exception:  # noqa: BLE001 - restoring is best effort
                pass
    outside = _diff_outside(pre, post, region) if pre and post else None
    read = ClipboardRead(
        text=None if copied in (None, marker) else copied,
        changed_outside=outside is not False,
        pre_crop=crop_png(pre, crop_region or region) if pre else None,
        post_crop=crop_png(post, crop_region or region) if post else None,
        frame_sha256=hashlib.sha256(post).hexdigest() if post else None,
        reason="nothing_copied" if copied in (None, marker) else None,
    )
    return read


def crops_match(a: Optional[bytes], b: Optional[bytes]) -> bool:
    """Whether two identifier-cell crops show the same pixels.

    Tolerates encoder noise (every channel within 8 levels, mean under 1),
    never a changed glyph: a different character moves whole strokes.
    """
    if a is None or b is None:
        return False
    if a == b:
        return True
    try:
        import numpy as np
        from PIL import Image

        x = np.asarray(Image.open(io.BytesIO(a)).convert("RGB"), dtype=np.int16)
        y = np.asarray(Image.open(io.BytesIO(b)).convert("RGB"), dtype=np.int16)
    except Exception:  # noqa: BLE001 - unreadable crops never match
        return False
    if x.shape != y.shape:
        return False
    d = np.abs(x - y)
    return bool(d.max() <= 8 and d.mean() < 1.0)


__all__ = [
    "COPY_GESTURES",
    "ClipboardRead",
    "FRAME_LOOKS",
    "IdentityCopyConfig",
    "LookBinding",
    "copy_identifier",
    "copy_region",
    "crops_match",
    "decide",
    "exact_expected_ids",
    "identifier_tokens",
    "is_glyph_abstain",
    "lookalike_classes",
    "resolve_lookalike_abstain",
    "vulnerable_classes",
]
