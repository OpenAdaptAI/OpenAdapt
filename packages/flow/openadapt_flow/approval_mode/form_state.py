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
  change but a changed value does. When the backend names the app window the
  item's verified steps were in (``approval_form_region``), only that window
  is read, in reading order and with its frame in the digest, so text
  elsewhere on the screen that changes by itself (a clock in a taskbar, a
  notification) is not mistaken for an edited form.
- When the backend can also read that window's text fields through the
  platform's accessibility API (``approval_form_fields``: UI Automation on
  Windows), each field and the title bar is read by its value and painted
  out of the OCR read, and the rest of the window is still read by OCR. A
  caret blinking in a text box or a hovered title-bar button then reads the
  same every time, while a changed value changes the digest through either
  channel.

Only a digest leaves this module. The digest stays in the run directory; it
never enters the report or anything sent to Cloud.
"""

from __future__ import annotations

import hashlib
import io
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


def screen_text_state(
    vision: Any, frame_png: bytes, regions: Optional[Sequence[Region]] = None
) -> Optional[FormState]:
    """Sorted words on the screen, or ``None`` when OCR found nothing.

    With ``regions`` (the app's window), only those parts of the frame are
    read, and the words keep their reading order, so swapped values change
    the digest. The regions are part of the digest.
    """

    try:
        if not regions:
            words = _words(vision, frame_png)
            if not words:
                return None
            return _digest("screen_text", sorted(words))
        rows = [
            [list(region), _words(vision, _crop(frame_png, region))]
            for region in regions
        ]
    except Exception:  # noqa: BLE001 - unreadable is not "unchanged"
        return None
    if not any(words for _region, words in rows):
        return None
    return _digest("screen_text", {"rows": rows})


def _clamp(region: Region, width: int, height: int) -> Optional[Region]:
    x, y, w, h = region
    left, top = max(0, x), max(0, y)
    right, bottom = min(width, x + w), min(height, y + h)
    if right <= left or bottom <= top:
        return None
    return (left, top, right - left, bottom - top)


def window_fields_state(
    vision: Any, frame_png: bytes, window_fields: dict[str, Any]
) -> Optional[FormState]:
    """The window's fields by value, and the rest of it by OCR, or ``None``.

    ``window_fields`` is a backend's ``approval_form_fields`` answer: the
    window's frame and its title bar and text fields, each with its screen
    rectangle and text. Every field rectangle is painted out of the window
    crop before OCR, so what OCR reads there (a caret, a hovered button)
    cannot vary between reads; the field's own value stands in for it. The
    frame, each field's kind, rectangle and value, and the remaining
    characters in reading order are all in the digest. ``None`` when nothing was read.
    """

    from PIL import Image, ImageDraw

    try:
        bounds = tuple(int(v) for v in window_fields["bounds"])
        fields = list(window_fields["fields"])
        with Image.open(io.BytesIO(frame_png)) as image:
            window = _clamp(bounds, *image.size)  # type: ignore[arg-type]
            if window is None:
                return None
            x0, y0, w, h = window
            crop = image.convert("RGB").crop((x0, y0, x0 + w, y0 + h))
        draw = ImageDraw.Draw(crop)
        rows: list[Any] = []
        for field in fields:
            kind, text = str(field["kind"]), str(field["text"])
            fx, fy, fw, fh = (int(v) for v in field["rect"])
            rows.append([kind, [fx, fy, fw, fh], text])
            inside = _clamp((fx - x0, fy - y0, fw, fh), w, h)
            if inside is not None:
                ix, iy, iw, ih = inside
                draw.rectangle((ix, iy, ix + iw - 1, iy + ih - 1), fill="white")
        out = io.BytesIO()
        crop.save(out, format="PNG")
        words = _words(vision, out.getvalue())
    except Exception:  # noqa: BLE001 - unreadable is not "unchanged"
        return None
    if not words and not any(row[2] for row in rows):
        return None
    # OCR's word boundaries move with what else is on the screen ("Benchmark
    # Harness" reads as one word or two), so the words are joined: the
    # characters and their reading order are what the person saw.
    return _digest(
        "window_fields",
        {"window": list(window), "fields": rows, "text": "".join(words)},
    )


def capture_form_state(
    backend: Any,
    vision: Any,
    frame_png: Optional[bytes] = None,
    regions: Optional[Sequence[Region]] = None,
    window_fields: Optional[dict[str, Any]] = None,
) -> Optional[FormState]:
    """The best available field state for this backend, or ``None``.

    A backend may provide ``form_state()`` returning JSON-serializable data;
    a browser backend is read through its page; any other backend falls back
    to the screen: the app window's fields by value and the rest by OCR when
    ``window_fields`` is given, else the words in ``regions`` (or on the
    whole screen).
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
    if window_fields is not None:
        return window_fields_state(vision, frame_png, window_fields)
    return screen_text_state(vision, frame_png, regions)


def approval_window_regions(
    backend: Any,
    frame_png: Optional[bytes],
    save_step: Optional[str],
    results: Sequence[Any] = (),
) -> Optional[list[Region]]:
    """The app window's part of the frame, for a screen-text form state.

    A backend that knows the window this item's verified steps were in
    (``approval_form_region``) bounds the read to that window, so the whole
    form the person saw is read, and the clock or a notification outside it
    is not. The save step is left out, so the approval request (taken before
    the save resolves) and the recheck after the answer ask about the same
    window; its frame is part of the digest, so a moved or resized window
    halts the save. The earlier steps' points (``results``, the run's step
    results so far) let a resumed run, which verified nothing in this process
    yet, name the same window. None, and the whole screen is read, when the
    backend cannot say. Never raises.
    """

    region_of = getattr(backend, "approval_form_region", None)
    if not callable(region_of) or frame_png is None:
        return None
    try:
        points = [
            (int(prior.resolution.point[0]), int(prior.resolution.point[1]))
            for prior in results
            if prior.step_id != save_step and prior.resolution is not None
        ]
        region = region_of(save_step, points)
        if region is None:
            return None
        from PIL import Image

        with Image.open(io.BytesIO(frame_png)) as image:
            width, height = image.size
        x, y, w, h = (int(v) for v in region)
    except Exception:  # noqa: BLE001 - no window means the whole screen
        return None
    left, top = max(0, x), max(0, y)
    right, bottom = min(width, x + w), min(height, y + h)
    if right <= left or bottom <= top:
        return None
    return [(left, top, right - left, bottom - top)]


def approval_window_fields(
    backend: Any, save_step: Optional[str], results: Sequence[Any] = ()
) -> Optional[dict[str, Any]]:
    """The backend's ``approval_form_fields`` answer for the approval's
    window, or ``None`` (and the caller reads the window by OCR alone).

    The same window :func:`approval_window_regions` names; the save step is
    left out and the earlier steps' points name the window on a resumed run.
    Never raises.
    """

    fields_of = getattr(backend, "approval_form_fields", None)
    if not callable(fields_of):
        return None
    try:
        points = [
            (int(prior.resolution.point[0]), int(prior.resolution.point[1]))
            for prior in results
            if prior.step_id != save_step and prior.resolution is not None
        ]
        answer = fields_of(save_step, points)
    except Exception:  # noqa: BLE001 - no answer is OCR alone
        return None
    if not isinstance(answer, dict) or set(answer) != {"bounds", "fields"}:
        return None
    return answer


def approval_form_state(
    backend: Any,
    vision: Any,
    frame_png: Optional[bytes],
    save_step: Optional[str],
    results: Sequence[Any] = (),
) -> Optional[FormState]:
    """The approval's form state for ``save_step``, or ``None``.

    The app window's fields by value where the backend can read them,
    otherwise the window (or the whole screen) by OCR. A request and its
    recheck that read through different channels get different kinds, so
    the save halts.
    """

    if frame_png is not None:
        fields = approval_window_fields(backend, save_step, results)
        if fields is not None:
            return capture_form_state(backend, vision, frame_png, window_fields=fields)
    regions = approval_window_regions(backend, frame_png, save_step, results)
    return capture_form_state(backend, vision, frame_png, regions)


__all__ = [
    "FormState",
    "approval_form_state",
    "approval_window_fields",
    "approval_window_regions",
    "capture_form_state",
    "window_fields_state",
    "dom_form_state",
    "screen_text_state",
]
