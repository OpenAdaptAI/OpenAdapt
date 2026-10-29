"""Default compiler passes that turn demonstrated gestures into checked steps.

A demonstration keeps every literal gesture. A few of them leave a step that
asserts nothing, and a strict certification policy (``clinical-write``) then
refuses the bundle. These passes replace such a gesture with an equivalent one
that carries its own check, using only the recording's own frames. They used
to live in one script per workflow (``scripts/openemr_qualification.py``);
they now run in every compile.

Each pass is evidence-gated and records every change it makes, so the result
stays auditable: :func:`apply_fixup_passes` returns the notes, and the
compiler writes them to ``compile_notes.json`` in the bundle.

The two passes that drop a key (``drop_select_commit_enter`` and
``drop_select_all_on_empty_field``) are off unless the caller passes
``drop_redundant_keys=True``. Their evidence is about the demonstration, and
the TYPE read-back at replay can't make up for a wrong drop: a field that
holds text at replay gets the typed value appended, and the read-back
(the old value plus the typed text) still passes; a ``<select>`` the typed
label did not commit is not caught on every backend. Without the drop, a
strict policy may refuse the bundle, which is the safe direction.

=================================  ==========================================
Pass                               What it does, and the evidence it needs
=================================  ==========================================
``drop_select_commit_enter``       Opt-in (``drop_redundant_keys``). Drops
                                   the Enter that followed typing an option
                                   label into a browser ``<select>``
                                   (structural role ``combobox``), on the
                                   assumption that the typed label selects
                                   the option. Needs structural info;
                                   skipped without it (RDP and Citrix
                                   already collapse a pixel selection into
                                   one SELECT_OPTION step).
``drop_select_all_on_empty_field`` Opt-in (``drop_redundant_keys``). Drops
                                   Ctrl+A between a click and a TYPE when
                                   the Ctrl+A changed no pixel in the
                                   clicked field's box: the field was empty
                                   in the demonstration. It may not be at
                                   replay, and the TYPE read-back does not
                                   catch an appended value.
``focused_field_region_stable``    Gives a field click that has no
                                   postcondition a ``REGION_STABLE`` check of
                                   the clicked box, from the recorded
                                   after-frame (the focused field before
                                   anything is typed, so no parameter value
                                   is in it). A field is a click whose
                                   structural role is ``textbox`` or
                                   ``combobox``, or, with no structural info,
                                   a click followed directly by a TYPE.
``static_control_identifier_crop`` Gives a click on a static button that has
                                   no identity evidence at all a pixel
                                   identifier crop of the control itself,
                                   from the recorded before-frame, checked by
                                   the pixel tier of the identity ladder.
                                   Needs structural role ``button``, and only
                                   runs where the step's surface can VERIFY a
                                   crop (the target runtime has
                                   ``pixel_verify_enabled``; see
                                   :func:`identity.verifiable_identity_evidence`).
                                   On a pixel surface the compiler already
                                   emits identifier crops for identity-armed
                                   steps.
=================================  ==========================================

A pass that lacks its evidence (no frame, no region, no structure) leaves the
step exactly as compiled. So does a pass whose evidence the surface that runs
the step could not verify: an identity check that can only abstain halts an
irreversible step on every run, which is worse than no new evidence.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from openadapt_flow import sealed_set as _sealed
from openadapt_flow.ir import ActionKind, Postcondition, PostconditionKind, Step

#: Frames per step id: ``(before_png, after_png)``; either may be ``None``.
FramePairs = Mapping[str, tuple[Optional[bytes], Optional[bytes]]]
CropFn = Callable[[bytes, tuple[int, int, int, int]], bytes]

COMPILE_NOTES_FILE = "compile_notes.json"
COMPILE_NOTES_SCHEMA = "openadapt.compile-notes/v1"

#: Ctrl+A that left the field box within this pixel change is a no-op.
_SELECT_ALL_MAX_CHANGED_FRACTION = 0.005
_SELECT_ALL_CHANGED_DELTA = 40
_FIELD_ROLES = frozenset({"textbox", "combobox", "searchbox", "spinbutton"})
_COMMIT_KEYS = frozenset({"enter", "return"})


def _structural(step: Step) -> Any:
    return step.anchor.structural if step.anchor is not None else None


def is_select_all(step: Step) -> bool:
    """True for a Ctrl+A / Cmd+A gesture in either recorded form."""

    if step.action is ActionKind.HOTKEY:
        mods = sorted(m.lower() for m in step.modifiers)
        return (step.key or "").lower() == "a" and mods in (
            ["control"],
            ["ctrl"],
            ["meta"],
            ["cmd"],
        )
    return step.action is ActionKind.KEY and (step.key or "").lower() in {
        "control+a",
        "ctrl+a",
        "meta+a",
        "cmd+a",
    }


def region_unchanged(
    before: Optional[bytes],
    after: Optional[bytes],
    region: Optional[tuple[int, int, int, int]],
) -> bool:
    """True when ``region`` shows no meaningful pixel change between frames.

    A caret blink is a few pixels; a text selection highlight is most of the
    text. ``False`` whenever the evidence is missing.
    """

    if before is None or after is None or region is None:
        return False
    import numpy as np
    from PIL import Image

    a = np.asarray(Image.open(io.BytesIO(before)).convert("RGB"), dtype=np.int16)
    b = np.asarray(Image.open(io.BytesIO(after)).convert("RGB"), dtype=np.int16)
    if a.shape != b.shape:
        return False
    x, y, w, h = (int(v) for v in region)
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(a.shape[1], x + w), min(a.shape[0], y + h)
    if x1 <= x0 or y1 <= y0:
        return False
    delta = np.abs(a[y0:y1, x0:x1] - b[y0:y1, x0:x1]).max(axis=2)
    changed = float((delta > _SELECT_ALL_CHANGED_DELTA).mean())
    return changed <= _SELECT_ALL_MAX_CHANGED_FRACTION


def _is_field_click(steps: list[Step], index: int) -> bool:
    step = steps[index]
    if step.action is not ActionKind.CLICK or step.anchor is None:
        return False
    structural = _structural(step)
    if structural is not None and structural.role:
        return structural.role in _FIELD_ROLES
    # No structure (a pixel surface): a click that the demonstration followed
    # directly with typing focused a text field.
    return index + 1 < len(steps) and steps[index + 1].action is ActionKind.TYPE


def _has_identity_evidence(step: Step) -> bool:
    anchor = step.anchor
    return bool(
        anchor is not None
        and (
            anchor.context_text
            or anchor.structured_identity
            or anchor.identity_template
            or anchor.identifier_crop
        )
    )


def apply_fixup_passes(
    steps: list[Step],
    frames: FramePairs,
    bundle_dir: Path,
    *,
    crop_png: CropFn,
    phash_png: Callable[..., str],
    surface: Optional[str] = None,
    pixel_verify_enabled: bool = False,
    surface_kind_for: Optional[Callable[[Step], Optional[str]]] = None,
    drop_redundant_keys: bool = False,
) -> tuple[list[Step], list[dict[str, Any]]]:
    """Run the passes; return the new step list and one note per change.

    ``steps`` is not modified; changed steps are deep copies. Templates are
    written under ``bundle_dir/templates``.

    ``pixel_verify_enabled`` is the target runtime's
    ``runtime.pixel_verify_enabled``. ``surface_kind_for`` names the surface
    kind one step runs on, for a bundle whose steps span several surfaces;
    without it every step runs on ``surface``.

    ``drop_redundant_keys`` turns on the two passes that drop a demonstrated
    key (see the module docstring for why they are off by default).
    """
    from openadapt_flow.runtime.identity import verifiable_identity_evidence

    def checkable(step: Step) -> frozenset[str]:
        kind = surface_kind_for(step) if surface_kind_for is not None else None
        return verifiable_identity_evidence(
            kind or surface, pixel_verify_enabled=pixel_verify_enabled
        )

    notes: list[dict[str, Any]] = []
    dropped: set[str] = set()
    web_structure = surface not in ("rdp", "citrix")

    # Pass 1: Enter after typing an option label into a <select>.
    for i, step in enumerate(steps):
        structural = _structural(step)
        if (
            drop_redundant_keys
            and web_structure
            and step.action is ActionKind.CLICK
            and structural is not None
            and structural.role == "combobox"
            and i + 2 < len(steps)
            and steps[i + 1].action is ActionKind.TYPE
            and steps[i + 1].text
            and steps[i + 2].action is ActionKind.KEY
            and (steps[i + 2].key or "").lower() in _COMMIT_KEYS
        ):
            dropped.add(steps[i + 2].id)
            notes.append(
                {
                    "change": "drop_select_commit_enter",
                    "step": steps[i + 2].id,
                    "evidence": "structural role combobox, then TYPE, then Enter",
                    "selector": structural.selector,
                }
            )

    # Pass 2: Ctrl+A on a freshly clicked, empty field before TYPE.
    for i, step in enumerate(steps):
        if (
            not drop_redundant_keys
            or step.id in dropped
            or not is_select_all(step)
            or i == 0
            or i + 1 >= len(steps)
            or steps[i - 1].action is not ActionKind.CLICK
            or steps[i + 1].action is not ActionKind.TYPE
            or steps[i - 1].anchor is None
        ):
            continue
        before, after = frames.get(step.id, (None, None))
        region = tuple(steps[i - 1].anchor.region)  # type: ignore[union-attr]
        if region_unchanged(before, after, region):  # type: ignore[arg-type]
            dropped.add(step.id)
            notes.append(
                {
                    "change": "drop_select_all_on_empty_field",
                    "step": step.id,
                    "evidence": "Ctrl+A changed no pixel in the clicked field box",
                    "region": list(region),
                }
            )

    kept = [step for step in steps if step.id not in dropped]
    out: list[Step] = []
    for i, step in enumerate(kept):
        changed = False
        new = step
        # Pass 3: REGION_STABLE for a postcondition-less field click.
        if not step.expect and _is_field_click(kept, i):
            _before, after = frames.get(step.id, (None, None))
            if after is not None:
                region = tuple(int(v) for v in step.anchor.region)  # type: ignore[union-attr]
                new = new.model_copy(deep=True)
                changed = True
                rel = f"templates/{step.id}_expect.png"
                (bundle_dir / rel).parent.mkdir(parents=True, exist_ok=True)
                _sealed.write_bytes(bundle_dir / rel, crop_png(after, region))  # type: ignore[arg-type]
                new.expect = [
                    Postcondition(
                        kind=PostconditionKind.REGION_STABLE,
                        region=region,  # type: ignore[arg-type]
                        phash=phash_png(after, region=region),
                        phash_tolerance=8,
                        template=rel,
                    )
                ]
                notes.append(
                    {
                        "change": "focused_field_region_stable",
                        "step": step.id,
                        "evidence": (
                            "structural field role"
                            if _structural(step) is not None
                            else "click followed by TYPE (no structural info)"
                        ),
                        "region": list(region),
                    }
                )
        # Pass 4: identifier crop for an unlabeled static button.
        structural = _structural(step)
        if (
            step.action is ActionKind.CLICK
            and step.anchor is not None
            and structural is not None
            and structural.role == "button"
            and not _has_identity_evidence(step)
            and "identifier_crop" in checkable(step)
        ):
            before, _after = frames.get(step.id, (None, None))
            if before is not None:
                if not changed:
                    new = new.model_copy(deep=True)
                    changed = True
                region = tuple(int(v) for v in step.anchor.region)
                rel = f"templates/identifiers/{step.id}.png"
                (bundle_dir / rel).parent.mkdir(parents=True, exist_ok=True)
                _sealed.write_bytes(bundle_dir / rel, crop_png(before, region))  # type: ignore[arg-type]
                assert new.anchor is not None
                new.anchor.identifier_crop = rel
                new.anchor.identifier_region = region  # type: ignore[assignment]
                new.identity_armed = True
                new.identity_unarmed_reason = None
                new.identifier_crop_missing_reason = None
                notes.append(
                    {
                        "change": "static_control_identifier_crop",
                        "step": step.id,
                        "evidence": "structural role button with no identity evidence",
                        "region": list(region),
                    }
                )
        out.append(new)
    return out, notes


def write_compile_notes(bundle_dir: Path, notes: list[dict[str, Any]]) -> None:
    """Write ``compile_notes.json``; remove a stale one when nothing changed."""

    import json

    from openadapt_flow import sealed_set

    path = Path(bundle_dir) / COMPILE_NOTES_FILE
    if not notes:
        sealed_set.unlink(path, missing_ok=True)
        return
    sealed_set.write_text(
        path,
        json.dumps(
            {
                "schema": COMPILE_NOTES_SCHEMA,
                "note": (
                    "Changes the default compiler passes made to demonstrated "
                    "gestures. Each replaces a gesture that asserted nothing "
                    "with an equivalent one that carries its own check, or "
                    "adds a check the demonstration showed (empty_result_guard). "
                    "An entry with a warning is a check the compiler could not "
                    "add."
                ),
                "changes": notes,
            },
            indent=2,
        )
        + "\n",
    )


__all__ = [
    "COMPILE_NOTES_FILE",
    "apply_fixup_passes",
    "is_select_all",
    "region_unchanged",
    "write_compile_notes",
]
