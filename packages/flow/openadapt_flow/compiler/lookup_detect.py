"""Learn the record check from the recording: a look-up after the save.

People often check a save by looking the record up: they search for it,
open it, and read it. When a recording (``record``, the desktop recorder, or
an agent's JIT capture) shows that after the save, this module finds it and
proposes two checks, with no marking and no configuration:

- ``check.proposed.json``: the look-up as a check in the same session
  (:mod:`openadapt_flow.compiler.readback_authoring`, Flow tier 3). The search
  is its state reset. ``openadapt-flow check accept REC`` turns it into
  ``check.json``, and a person still admits the compiled check;
- ``readback.proposed.yaml``: the same look-up as a separate-session
  ``desktop_readback`` (tier 2 with its own sign-in), for desktop, RDP and
  Citrix recordings. It names the read-back session's address as
  ``UNRESOLVED`` and marks every effect ``needs_operator_confirmation``.

What it looks for, in order:

1. the save: the last click (or Enter) whose label has a save word;
2. after it, text typed into a box and submitted (Enter or a click): the
   search. Its text should be a value the run typed, or a recorded
   parameter's value;
3. optionally, a click that opens a result;
4. on the last frame, the parameter values the person typed, each read with
   OCR, with the label next to it (``Visit type: Follow-up``) or above it.

Nothing found means nothing proposed. Everything proposed is a draft a
person reviews. No model calls.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from openadapt_flow import sealed_set as _sealed
from openadapt_flow.runtime.effects.desktop_readback import save_like_words

CHECK_PROPOSAL_FILE = "check.proposed.json"
READBACK_PROPOSAL_FILE = "readback.proposed.yaml"

#: What to tell the person when the recording has no look-up after the save.
LOOKUP_PROMPT = "Look the record up once so OpenAdapt can check future saves."
LOOKUP_GUIDANCE = (
    LOOKUP_PROMPT
    + " After you save, search for the record, open it, and stop recording "
    "with the saved values on screen."
)

#: Labels that commit a record. Narrower than the read-back guard's list:
#: here a match names the save, there a match refuses a click.
_SAVE_WORDS = frozenset(
    {
        "save",
        "submit",
        "sign",
        "post",
        "book",
        "finalize",
        "finalise",
        "file",
        "send",
        "confirm",
        "approve",
        "commit",
        "complete",
        "schedule",
        "update",
        "apply",
    }
)

OcrFn = Callable[[bytes], list[Any]]


def _default_ocr(png: bytes) -> list[Any]:
    from openadapt_flow.vision.ocr import ocr

    return ocr(png)


def _norm(text: str) -> str:
    return " ".join(str(text).casefold().split())


def _squash(text: str) -> str:
    """Casefolded with every space removed: OCR often drops or adds spaces."""

    return "".join(str(text).casefold().split())


def _shows(line_text: str, value: str) -> bool:
    """Whether a line shows ``value`` on its own, ignoring spaces."""

    want = _squash(value)
    hay = _squash(line_text)
    start = hay.find(want)
    while start >= 0:
        end = start + len(want)
        before_ok = start == 0 or not hay[start - 1].isalnum()
        after_ok = end == len(hay) or not hay[end].isalnum()
        if before_ok and after_ok:
            return True
        start = hay.find(want, start + 1)
    return False


# -- what a look-up is -----------------------------------------------------------


@dataclass(frozen=True)
class SeenValue:
    """A recorded value found on the last frame."""

    param: str
    value: str
    #: The value's line on the frame, ``(x, y, w, h)``.
    region: tuple[int, int, int, int]
    #: The label next to it, and where the value sits relative to the label.
    label: Optional[str]
    at: str
    confidence: float


@dataclass
class LookupProposal:
    """A look-up after the save, as recorded."""

    save_event: int
    save_label: str
    #: The first event of the look-up (the search box click or the typing).
    start_event: int
    search_event: int
    query: str
    query_param: Optional[str]
    #: The search box: its structural locator and label when recorded, and
    #: the point the person clicked to focus it.
    box_point: Optional[tuple[int, int]]
    box_label: Optional[str]
    box_structural: Optional[dict[str, Any]]
    #: ``None`` when Enter submitted the search.
    submit_label: Optional[str]
    submit_structural: Optional[dict[str, Any]]
    open_event: Optional[int]
    open_point: Optional[tuple[int, int]]
    fields: list[SeenValue] = field(default_factory=list)
    frame_size: tuple[int, int] = (0, 0)

    @property
    def identity(self) -> Optional[SeenValue]:
        """The field showing the searched value, if the record view shows it."""

        if self.query_param is None:
            return None
        for seen in self.fields:
            if seen.param == self.query_param:
                return seen
        return None

    def summary(self) -> list[str]:
        # Parameter names only: typed values and screen text can be patient
        # data.
        return [
            f"Found a look-up after the save (event {self.save_event}): a search "
            f"for parameter {self.query_param}"
            + (", then a result opened" if self.open_event is not None else ""),
            "  it shows: " + ", ".join(seen.param for seen in self.fields),
        ]


# -- reading the recording ---------------------------------------------------------


def _events(recording: Path) -> list[dict[str, Any]]:
    path = recording / "events.jsonl"
    if not _sealed.is_file(path):
        return []
    events = []
    for line in _sealed.read_text(path).splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


def _meta(recording: Path) -> dict[str, Any]:
    path = recording / "meta.json"
    if not _sealed.is_file(path):
        return {}
    return json.loads(_sealed.read_text(path))


def _frame(recording: Path, index: int, which: str) -> Optional[bytes]:
    path = recording / "frames" / f"{index:04d}_{which}.png"
    return _sealed.read_bytes(path) if _sealed.is_file(path) else None


def _params(meta: Mapping[str, Any]) -> dict[str, str]:
    secret = set(meta.get("secret_params") or [])
    return {
        str(name): str(value)
        for name, value in (meta.get("params") or {}).items()
        if name not in secret and isinstance(value, (str, int, float)) and str(value)
    }


def _structural_label(event: Mapping[str, Any]) -> str:
    structural = event.get("structural") or {}
    for key in ("name", "automation_id"):
        value = structural.get(key) if isinstance(structural, dict) else None
        if value:
            return str(value)
    identity = event.get("structured_identity")
    return str(identity) if identity else ""


def _ocr_label_at(png: Optional[bytes], x: int, y: int, ocr: OcrFn) -> str:
    """The text of the line under a click, read from the frame before it."""

    if png is None:
        return ""
    best = ""
    best_area = None
    for line in ocr(png):
        lx, ly, lw, lh = line.region
        if lx - 6 <= x <= lx + lw + 6 and ly - 6 <= y <= ly + lh + 6:
            area = lw * lh
            if best_area is None or area < best_area:
                best, best_area = str(line.text), area
    return best


def _click_label(recording: Path, event: Mapping[str, Any], ocr: OcrFn) -> str:
    label = _structural_label(event)
    if label:
        return label
    if event.get("x") is None or event.get("y") is None:
        return ""
    return _ocr_label_at(
        _frame(recording, int(event["i"]), "before"),
        int(event["x"]),
        int(event["y"]),
        ocr,
    )


def _is_save_label(label: str) -> bool:
    words = save_like_words(label)
    return any(word in _SAVE_WORDS for word in words)


_CLICKS = ("click", "double_click")

#: How many clicks, from the end, are read to find the save.
_MAX_CLICKS_READ = 50

#: Shorter values ("1", "M") would match unrelated text.
_MIN_VALUE_CHARS = 3


def _is_enter(event: Mapping[str, Any]) -> bool:
    return event.get("kind") in ("key", "hotkey") and str(
        event.get("key") or ""
    ).lower() in ("enter", "return")


def _label_for(line: Any, lines: list[Any], value: str) -> tuple[Optional[str], str]:
    """The label of a value's line: a prefix, a line to its left, or above."""

    text = " ".join(str(line.text).split())
    if ":" in text and not _squash(text).startswith(_squash(value)):
        prefix = text.split(":", 1)[0].strip()
        if prefix and re.search(r"[A-Za-z]", prefix) and not _shows(prefix, value):
            return prefix, "right"
    x, y, w, h = line.region
    left = [
        other
        for other in lines
        if other is not line
        and other.region[0] + other.region[2] <= x + 2
        and abs((other.region[1] + other.region[3] / 2) - (y + h / 2)) <= h * 0.6
        and re.search(r"[A-Za-z]", str(other.text))
    ]
    if left:
        nearest = max(left, key=lambda o: o.region[0] + o.region[2])
        return " ".join(str(nearest.text).split()).rstrip(":").strip(), "right"
    above = [
        other
        for other in lines
        if other is not line
        and other.region[1] + other.region[3] <= y + 2
        and y - (other.region[1] + other.region[3]) <= h * 2.5
        and other.region[0] <= x + 20
        and other.region[0] + other.region[2] >= x - 20
        and re.search(r"[A-Za-z]", str(other.text))
    ]
    if above:
        nearest = max(above, key=lambda o: o.region[1])
        return " ".join(str(nearest.text).split()).rstrip(":").strip(), "below"
    return None, "self"


def _same_line(a: Any, b: Any) -> bool:
    ax, ay, aw, ah = a.region
    bx, by, bw, bh = b.region
    return (
        _norm(a.text) == _norm(b.text)
        and abs((ax + aw / 2) - (bx + bw / 2)) <= 12
        and abs((ay + ah / 2) - (by + bh / 2)) <= 12
    )


def _same_place(a: Any, b: Any) -> bool:
    """Two OCR lines overlap on screen (the search box, before and after)."""

    ax, ay, aw, ah = a.region
    bx, by, bw, bh = b.region
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def _covers(line: Any, point: Optional[tuple[int, int]]) -> bool:
    if point is None:
        return False
    x, y, w, h = line.region
    px, py = point
    return x - 12 <= px <= x + w + 12 and y - 12 <= py <= y + h + 12


def _values_on_frame(
    png: bytes,
    params: Mapping[str, str],
    ocr: OcrFn,
    *,
    before: Optional[bytes] = None,
    search_box: Optional[tuple[int, int]] = None,
    echo: Optional[list[Any]] = None,
) -> tuple[list[SeenValue], tuple[int, int]]:
    """Recorded values the look-up brought onto the screen.

    A line that was already on screen before the look-up started (the form
    the person filled in and saved) is not the record view, and neither is
    the search box echoing the query.
    """

    from io import BytesIO

    from PIL import Image

    with Image.open(BytesIO(png)) as image:
        size = image.size
    lines = [line for line in ocr(png) if str(line.text).strip()]
    earlier = [line for line in ocr(before) if str(line.text).strip()] if before else []
    fresh = [
        line
        for line in lines
        if not any(_same_line(line, old) for old in earlier)
        and not _covers(line, search_box)
        and not any(_same_place(line, shown) for shown in echo or [])
    ]
    seen: list[SeenValue] = []
    for name, value in params.items():
        if len(_squash(value)) < _MIN_VALUE_CHARS:
            continue
        hits = [line for line in fresh if _shows(line.text, value)]
        if len(hits) != 1:
            # Not shown, or shown twice (a typed form still on screen):
            # nothing a check can read without guessing.
            continue
        line = hits[0]
        label, at = _label_for(line, lines, value)
        region = tuple(int(v) for v in line.region)
        text = " ".join(str(line.text).split())
        if label and at == "right" and _squash(text).startswith(_squash(label)):
            # "Slot: 2026-11-02 09:30" is one OCR line: keep the value's part
            # of it, estimated from where the value starts in the text.
            cut = text.find(":") + 1 if ":" in text else len(label)
            share = cut / max(1, len(text))
            x, y, w, h = region
            skip = int(w * share)
            region = (x + skip, y, max(1, w - skip), h)
        seen.append(
            SeenValue(
                param=name,
                value=value,
                region=region,  # type: ignore[arg-type]
                label=label,
                at=at,
                confidence=float(line.confidence),
            )
        )
    return seen, size


def detect_lookup(
    recording: str | Path,
    *,
    ocr: Optional[OcrFn] = None,
    values: Optional[Mapping[str, str]] = None,
) -> Optional[LookupProposal]:
    """Find a look-up after the save in ``recording``, or None.

    ``values`` names the run's inputs (``{name: typed value}``) when the
    recording's ``meta.json`` doesn't, as for an agent's JIT capture.
    """

    recording = Path(recording)
    ocr = ocr or _default_ocr
    events = _events(recording)
    if not events:
        return None
    params = _params(_meta(recording))
    for name, value in (values or {}).items():
        if str(value).strip() and str(value) not in params.values():
            params[str(name)] = str(value)
    by_value = {_norm(v): k for k, v in params.items()}

    # 1. The save: the last click whose label names a commit. Searched from
    # the end, and only over the last clicks, so a long recording isn't read
    # with OCR click by click.
    save_index: Optional[int] = None
    save_label = ""
    clicks_read = 0
    for position in range(len(events) - 1, -1, -1):
        event = events[position]
        if event.get("kind") not in _CLICKS:
            continue
        clicks_read += 1
        if clicks_read > _MAX_CLICKS_READ:
            break
        label = _click_label(recording, event, ocr)
        if _is_save_label(label):
            save_index, save_label = position, label
            break
    if save_index is None:
        return None

    # 2. Everything after the save must be a look-up: clicks, one typed
    # search, Enter, Tab, scrolling. Any other key, a second typed text, or a
    # click whose label could write means it isn't one.
    tail = events[save_index + 1 :]
    typed = [p for p, e in enumerate(tail) if e.get("kind") == "type"]
    if len(typed) != 1:
        return None
    for event in tail:
        kind = event.get("kind")
        if kind in _CLICKS:
            if save_like_words(_click_label(recording, event, ocr)):
                return None
        elif kind in ("key", "hotkey"):
            if event.get("modifiers") or str(event.get("key") or "").lower() not in (
                "enter",
                "return",
                "tab",
            ):
                return None
        elif kind not in ("type", "scroll"):
            return None
    search_offset = typed[0]
    search = tail[search_offset]
    query = str(search.get("text") or "")
    if search.get("secret") or not query.strip():
        return None
    query_param = search.get("param") or by_value.get(_norm(query))
    if not query_param:
        # The search must be a value the run knows, so each run searches for
        # its own record.
        return None
    submit_offset = next(
        (
            p
            for p in range(search_offset + 1, min(len(tail), search_offset + 4))
            if _is_enter(tail[p]) or tail[p].get("kind") in _CLICKS
        ),
        None,
    )
    if submit_offset is None:
        return None
    submit = tail[submit_offset]
    focus = tail[search_offset - 1] if search_offset > 0 else None
    if focus is not None and focus.get("kind") not in _CLICKS:
        focus = None

    submit_label: Optional[str] = None
    submit_structural: Optional[dict[str, Any]] = None
    if submit.get("kind") in _CLICKS:
        submit_label = _click_label(recording, submit, ocr) or None
        submit_structural = submit.get("structural") or None

    # 3. A click after the search that opens a result.
    open_event = next(
        (e for e in tail[submit_offset + 1 :] if e.get("kind") in _CLICKS), None
    )

    # 4. The record view: recorded values the look-up brought onto the last
    # frame. Not what was on screen right after the save (the saved form), and
    # not the search box echoing the query.
    box_point = None
    if focus is not None and focus.get("x") is not None:
        box_point = (int(focus["x"]), int(focus["y"]))
    last = events[-1]
    png = _frame(recording, int(last.get("i", len(events) - 1)), "after")
    if png is None:
        return None
    save_i = int(events[save_index].get("i", save_index))
    search_i = int(search.get("i", save_index + 1 + search_offset))
    echo_frame = _frame(recording, search_i, "after")
    echo = (
        [line for line in ocr(echo_frame) if _shows(line.text, query)]
        if echo_frame is not None
        else []
    )
    seen, size = _values_on_frame(
        png,
        params,
        ocr,
        before=_frame(recording, save_i, "after"),
        search_box=box_point,
        echo=echo,
    )
    if not seen:
        return None
    box_structural = (focus or {}).get("structural") or search.get("structural")
    return LookupProposal(
        save_event=save_i,
        save_label=save_label,
        start_event=int(tail[0].get("i", save_index + 1)),
        search_event=search_i,
        query=query,
        query_param=str(query_param),
        box_point=box_point,
        box_label=search.get("field_label") or None,
        box_structural=box_structural or None,
        submit_label=submit_label,
        submit_structural=submit_structural,
        open_event=int(open_event["i"]) if open_event is not None else None,
        open_point=(
            (int(open_event["x"]), int(open_event["y"]))
            if open_event is not None and open_event.get("x") is not None
            else None
        ),
        fields=seen,
        frame_size=size,
    )


# -- what it proposes ----------------------------------------------------------------


def to_check_marks(proposal: LookupProposal) -> Any:
    """The look-up as ``check.json`` marks (a same-session check, tier 3)."""

    from openadapt_flow.compiler.readback_authoring import CheckMarks, MarkedField

    fields = []
    for seen in proposal.fields:
        x, y, w, h = seen.region
        fields.append(
            # OCR reads a tight crop badly; leave room above and below.
            MarkedField(
                param=seen.param,
                region=(max(0, x - 4), max(0, y - 8), w + 8, h + 16),
            )
        )
    return CheckMarks(
        start_event=proposal.start_event,
        fields=fields,
        prompt=LOOKUP_PROMPT + " (found in the recording)",
    )


def _ref_for(seen: SeenValue, panel: tuple[int, int, int, int]) -> dict[str, Any]:
    if seen.label:
        return {"text": seen.label, "at": seen.at, "region": list(panel)}
    x, y, w, h = seen.region
    return {"region": [max(0, x - 4), max(0, y - 4), w + 8, h + 8]}


def _panel(proposal: LookupProposal) -> tuple[int, int, int, int]:
    """A box around the record view's values and their labels."""

    xs, ys, xe, ye = [], [], [], []
    for seen in proposal.fields:
        x, y, w, h = seen.region
        xs.append(x)
        ys.append(y)
        xe.append(x + w)
        ye.append(y + h)
    pad = 60
    width, height = proposal.frame_size
    x0 = max(0, min(xs) - 300)
    y0 = max(0, min(ys) - pad)
    x1 = min(width or max(xe) + pad, max(xe) + pad)
    y1 = min(height or max(ye) + pad, max(ye) + pad)
    return (x0, y0, x1 - x0, y1 - y0)


_SESSION_PLACEHOLDERS = {
    "rdp": {"kind": "rdp", "rdp_host": "UNRESOLVED"},
    "citrix": {"kind": "citrix", "rdp_window_title": "UNRESOLVED"},
    "windows": {"kind": "windows", "agent_url": "UNRESOLVED"},
    "macos": {
        "kind": "macos",
        "macos_app": "UNRESOLVED",
        "macos_window_title": "UNRESOLVED",
    },
    "linux": {
        "kind": "linux",
        "linux_app": "UNRESOLVED",
        "linux_window_title": "UNRESOLVED",
    },
}


def to_desktop_readback(
    proposal: LookupProposal, session_kind: str
) -> Optional[dict[str, Any]]:
    """The look-up as a separate-session ``desktop_readback`` section.

    None when the record view doesn't show the searched value (the check
    needs it to confirm the record), or for a web recording.
    """

    kind = (session_kind or "").strip().lower()
    identity = proposal.identity
    if (
        kind not in _SESSION_PLACEHOLDERS
        or identity is None
        or identity.label is None
        or not proposal.query_param
    ):
        # The check confirms the record by the identifier next to its label;
        # without one there is nothing it can read without guessing.
        return None
    panel = _panel(proposal)
    structural = proposal.box_structural or {}
    if structural.get("automation_id"):
        box: dict[str, Any] = {"automation_id": structural["automation_id"]}
    elif proposal.box_point is not None:
        x, y = proposal.box_point
        box = {"region": [max(0, x - 20), max(0, y - 20), 320, 40]}
    else:
        return None
    search: dict[str, Any] = {"box": box, "query": {"param": proposal.query_param}}
    if proposal.submit_label is not None:
        sub = proposal.submit_structural or {}
        if sub.get("automation_id"):
            search["submit"] = {"automation_id": sub["automation_id"]}
        else:
            search["submit"] = {"text": proposal.submit_label}
    identity_ref = _ref_for(identity, panel)
    section: dict[str, Any] = {
        "session": dict(_SESSION_PLACEHOLDERS[kind]),
        "search": search,
        "ready": identity_ref,
        "identity": {
            "reader": identity_ref,
            "value": {"param": proposal.query_param},
            "field": proposal.query_param,
        },
        "fields": {
            seen.param: {"control": _ref_for(seen, panel)}
            for seen in proposal.fields
            if seen is not identity
        },
    }
    if kind == "rdp":
        section["credentials"] = {
            "username_env": "OPENADAPT_READBACK_USERNAME",
            "password_env": "OPENADAPT_READBACK_PASSWORD",
        }
    if proposal.open_point is not None:
        _x, y = proposal.open_point
        width = proposal.frame_size[0] or 1280
        section["open"] = {"results": {"region": [0, max(0, y - 120), width, 400]}}
    if not section["fields"]:
        return None
    return section


def proposed_effects(proposal: LookupProposal) -> list[dict[str, Any]]:
    """Draft effects: each value the record view showed equals what was typed.

    Field checks, not a count: one record view can't show a duplicate.
    """

    identity = proposal.identity
    if identity is None or not proposal.query_param:
        return []
    return [
        {
            "kind": "field_equals",
            "match": {proposal.query_param: {"param": proposal.query_param}},
            "field": seen.param,
            "value": {"param": seen.param},
            "needs_operator_confirmation": True,
        }
        for seen in proposal.fields
        if seen is not identity
    ]


def write_proposals(
    recording: str | Path,
    proposal: LookupProposal,
    *,
    session_kind: Optional[str] = None,
) -> list[Path]:
    """Write ``check.proposed.json`` and, for desktop recordings,
    ``readback.proposed.yaml``. Returns the paths written."""

    import yaml

    from openadapt_flow.record_sor import _write_private

    recording = Path(recording)
    written: list[Path] = []
    marks = to_check_marks(proposal)
    check_path = recording / CHECK_PROPOSAL_FILE
    # Both files hold screen text and regions of a patient's record: 0600.
    _write_private(check_path, marks.model_dump_json(indent=2) + "\n")
    written.append(check_path)
    section = to_desktop_readback(proposal, session_kind or "")
    if section is not None:
        document = {
            "verifier": {"kind": "desktop_readback", "desktop_readback": section},
            "effects": proposed_effects(proposal),
        }
        path = recording / READBACK_PROPOSAL_FILE
        header = (
            "# Proposed from the look-up you recorded after the save.\n"
            "# Fill in every UNRESOLVED value, check the regions, and remove\n"
            "# needs_operator_confirmation from each effect you accept.\n"
        )
        _write_private(path, header + yaml.safe_dump(document, sort_keys=False))
        written.append(path)
    return written


def propose_from_recording(
    recording: str | Path,
    *,
    session_kind: Optional[str] = None,
    values: Optional[Mapping[str, str]] = None,
    ocr: Optional[OcrFn] = None,
) -> tuple[Optional[LookupProposal], list[Path], list[str]]:
    """Detect a look-up and write its proposals. Never raises: a detector
    failure is reported, and the recording is left as it was.

    Returns the proposal (or None), the paths written, and lines to print.
    """

    from openadapt_flow.compiler.readback_authoring import CHECK_FILE

    recording = Path(recording)
    try:
        proposal = detect_lookup(recording, ocr=ocr, values=values)
        if proposal is None:
            if _sealed.is_file(recording / CHECK_FILE):
                return None, [], []
            return None, [], [LOOKUP_GUIDANCE]
        written = write_proposals(recording, proposal, session_kind=session_kind)
    except Exception as exc:  # noqa: BLE001 - a proposal is never required
        return (
            None,
            [],
            [f"Could not propose a check from the recording ({type(exc).__name__})."],
        )
    lines = proposal.summary()
    for path in written:
        lines.append(f"Proposed: {path}")
    return proposal, written, lines


def accept_check_proposal(recording: str | Path) -> Path:
    """Turn ``check.proposed.json`` into ``check.json`` (a person still
    admits the compiled check). Refuses to overwrite a ``check.json``."""

    from openadapt_flow.compiler.readback_authoring import CHECK_FILE, CheckMarks

    recording = Path(recording)
    source = recording / CHECK_PROPOSAL_FILE
    target = recording / CHECK_FILE
    if not _sealed.is_file(source):
        raise FileNotFoundError(f"no {CHECK_PROPOSAL_FILE} in {recording}")
    if _sealed.exists(target):
        raise FileExistsError(f"{target} exists; remove it first to replace it")
    marks = CheckMarks.model_validate_json(_sealed.read_text(source))
    _sealed.write_text(target, marks.model_dump_json(indent=2) + "\n")
    return target


__all__ = [
    "CHECK_PROPOSAL_FILE",
    "LOOKUP_GUIDANCE",
    "LOOKUP_PROMPT",
    "LookupProposal",
    "READBACK_PROPOSAL_FILE",
    "SeenValue",
    "accept_check_proposal",
    "detect_lookup",
    "proposed_effects",
    "to_check_marks",
    "to_desktop_readback",
    "propose_from_recording",
    "write_proposals",
]
