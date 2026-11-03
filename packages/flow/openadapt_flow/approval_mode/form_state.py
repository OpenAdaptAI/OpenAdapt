"""Read the filled form's field state, to prove it didn't change while waiting.

A person approves the item they see. Between the approval request and the
save, the engine re-reads the form and refuses the save if anything changed
(design 13, phase 0: "re-reads every input field. A field that changed during
the pause halts the item before the save").

- In a browser the engine reads the page's own form controls: every
  ``input`` (except hidden ones), ``textarea``, ``select`` and editable
  region, in every frame, with its value, checked state and selection.
  Password fields contribute only whether they're empty.
- Elsewhere it reads the text on the screen with the engine's local OCR,
  as a sorted list of words, so a cursor blink or hover doesn't count as a
  change but a changed value does. When the run knows where the item's
  fields are (the rows of the steps it has already resolved), only those
  rows are read, so text elsewhere on the screen that changes by itself (a
  clock in a taskbar, a notification) is not mistaken for an edited form.

Only a digest leaves this module. The digest stays in the run directory; it
never enters the report or anything sent to Cloud.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Optional, Sequence

#: (kind, "sha256:<hex>")
FormState = tuple[str, str]
#: x, y, width, height in frame pixels.
Region = tuple[int, int, int, int]

_DOM_FORM_STATE_JS = """() => {
  const rows = [];
  const editable = (el) => el.isContentEditable === true;
  const nodes = document.querySelectorAll(
    'input, textarea, select, [contenteditable]'
  );
  nodes.forEach((el, index) => {
    const tag = el.tagName.toLowerCase();
    if (tag !== 'input' && tag !== 'textarea' && tag !== 'select'
        && !editable(el)) {
      return;
    }
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (tag === 'input' && type === 'hidden') {
      return;
    }
    const key = [index, tag, type, el.getAttribute('name') || '',
                 el.getAttribute('id') || ''];
    if (tag === 'select') {
      const chosen = Array.from(el.selectedOptions || []).map(
        (opt) => [opt.index, opt.value]
      );
      rows.push([key, 'select', chosen, !!el.disabled]);
    } else if (tag === 'input' && (type === 'checkbox' || type === 'radio')) {
      rows.push([key, 'check', !!el.checked, el.value, !!el.disabled]);
    } else if (tag === 'input' && type === 'password') {
      rows.push([key, 'password_filled', (el.value || '').length > 0]);
    } else if (tag === 'input' || tag === 'textarea') {
      rows.push([key, 'value', el.value, !!el.disabled, !!el.readOnly]);
    } else {
      rows.push([key, 'editable', el.innerText]);
    }
  });
  return rows;
}"""

_WORD = re.compile(r"[0-9A-Za-zÀ-ɏ]+")


def _digest(kind: str, payload: Any) -> FormState:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    domain = f"openadapt.item-approval-form-state/{kind}/v1\n".encode("ascii")
    return kind, "sha256:" + hashlib.sha256(domain + encoded).hexdigest()


def dom_form_state(backend: Any) -> Optional[FormState]:
    """The browser page's form-control state, or ``None`` when unreadable."""

    page = getattr(backend, "page", None)
    if page is None:
        return None
    frames = list(getattr(page, "frames", None) or [])
    if not frames:
        main = getattr(page, "main_frame", None)
        frames = [main] if main is not None else []
    if not frames:
        return None
    payload: list[Any] = []
    for index, frame in enumerate(frames):
        try:
            rows = frame.evaluate(_DOM_FORM_STATE_JS)
        except Exception:  # noqa: BLE001 - an unreadable frame is not "unchanged"
            return None
        if not isinstance(rows, list):
            return None
        name = getattr(frame, "name", "") or ""
        payload.append([index, name, rows])
    return _digest("dom", payload)


def _words(vision: Any, png: bytes) -> list[str]:
    lines = vision.ocr(png)
    words: list[str] = []
    for line in lines or []:
        text = str(getattr(line, "text", "") or "")
        words.extend(word.casefold() for word in _WORD.findall(text))
    return words


def _crop(png: bytes, region: Region) -> bytes:
    import io

    from PIL import Image

    x, y, w, h = region
    with Image.open(io.BytesIO(png)) as image:
        out = io.BytesIO()
        image.crop((x, y, x + w, y + h)).save(out, format="PNG")
    return out.getvalue()


def field_rows(
    ys: Sequence[int], frame_size: tuple[int, int], row_px: int
) -> Optional[list[Region]]:
    """Full-width rows of ``row_px`` centred on each ``y``, merged; None if none."""

    width, height = frame_size
    half = row_px // 2
    bands = sorted(
        (max(0, y - half), min(height, y + half))
        for y in ys
        if min(height, y + half) > max(0, y - half)
    )
    if not bands:
        return None
    merged = [bands[0]]
    for top, bottom in bands[1:]:
        if top <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], bottom))
        else:
            merged.append((top, bottom))
    return [(0, top, width, bottom - top) for top, bottom in merged]


def screen_text_state(
    vision: Any, frame_png: bytes, regions: Optional[Sequence[Region]] = None
) -> Optional[FormState]:
    """Sorted words on the screen, or ``None`` when OCR found nothing.

    With ``regions``, only those parts of the frame are read, each kept
    apart so a value cannot move from one field's row to another unseen.
    """

    try:
        if not regions:
            words = _words(vision, frame_png)
            if not words:
                return None
            return _digest("screen_text", sorted(words))
        rows = [
            [list(region), sorted(_words(vision, _crop(frame_png, region)))]
            for region in regions
        ]
    except Exception:  # noqa: BLE001 - unreadable is not "unchanged"
        return None
    if not any(words for _region, words in rows):
        return None
    return _digest("screen_text", {"rows": rows})


def capture_form_state(
    backend: Any,
    vision: Any,
    frame_png: Optional[bytes] = None,
    regions: Optional[Sequence[Region]] = None,
) -> Optional[FormState]:
    """The best available field state for this backend, or ``None``.

    A backend may provide ``form_state()`` returning JSON-serializable data;
    a browser backend is read through its page; any other backend falls back
    to the words on the screen, in ``regions`` when they are given.
    """

    custom = getattr(backend, "form_state", None)
    if callable(custom):
        try:
            return _digest("dom", custom())
        except Exception:  # noqa: BLE001
            return None
    if getattr(backend, "page", None) is not None:
        return dom_form_state(backend)
    if frame_png is None:
        try:
            frame_png = backend.screenshot()
        except Exception:  # noqa: BLE001
            return None
    return screen_text_state(vision, frame_png, regions)


__all__ = [
    "FormState",
    "capture_form_state",
    "dom_form_state",
    "field_rows",
    "screen_text_state",
]
