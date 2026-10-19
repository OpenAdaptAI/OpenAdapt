"""Detect font and layout drift on a screen and halt with a named reason.

A recorded workflow assumes the screen looks the way it did when it was
recorded. A zoom change, a display-scaling change, a new default font or a
moved panel breaks that assumption. Without a check, the run fails later as
an unexplained "target not found" or, worse, acts on the wrong element.

:func:`fingerprint` summarizes a frame from its OCR lines: frame size,
typical text height, typical character width, and where a few stable labels
sit. :func:`compare` checks a live frame against the recorded fingerprint and
returns a :class:`DriftVerdict` listing every difference outside tolerance.
:func:`check_layout` raises :class:`LayoutDriftHalt` so the run stops before
acting and a person sees why.

The compiler stores one fingerprint per click step of the recording in
:data:`FINGERPRINT_ASSET`. The replayer compares the frame it is about to act
on against it, by default on remote-desktop replays (``rdp``/``citrix``
targets and the FreeRDP backend), and halts the step with the named reasons
on drift. The stored fingerprints hold frame size and font metrics. Next to
them the compiler stores a scale landmark set per step: salted hashes of
unique label text with each label's centre, which
:mod:`openadapt_flow.runtime.render_scale` uses to estimate a display-scale
change. No label text is stored.

An optional advisor (for example a small local layout model) may be given.
Its output is recorded as advice for the person reviewing the halt. It never
changes the verdict, never authorizes a step and is never called on a frame
that matches. Healthy runs make no model calls.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping, Optional, Sequence

from openadapt_flow.runtime.identity import squash

DriftReason = Literal[
    "resolution_changed",
    "session_profile_changed",
    "font_size_changed",
    "font_changed",
    "layout_changed",
    "landmark_missing",
    "too_little_text",
]


@dataclass(frozen=True)
class Tolerances:
    """How much a live frame may differ before it counts as drift."""

    #: Allowed ratio of live to recorded median text height.
    text_height_ratio: float = 0.08
    #: Allowed ratio of live to recorded median character width, after
    #: correcting for any text-height change.
    char_width_ratio: float = 0.10
    #: Allowed landmark displacement in pixels, after removing the shared
    #: offset of all landmarks (a scrolled or moved window is not drift).
    landmark_px: int = 12
    #: Fewest OCR lines needed to judge font metrics.
    min_lines: int = 3


@dataclass(frozen=True)
class LayoutFingerprint:
    """What a screen looked like, without any of its values.

    Attributes:
        viewport: Frame width and height.
        text_height: Median OCR line height in pixels.
        char_width: Median width per character in pixels.
        lines: OCR lines used.
        landmarks: Centre of each stable label, by label text.
        profile_sha256: Session profile digest, when the frame came from a
            pinned remote session.
    """

    viewport: tuple[int, int]
    text_height: Optional[float]
    char_width: Optional[float]
    lines: int
    landmarks: Mapping[str, tuple[int, int]]
    profile_sha256: Optional[str] = None


def _find_landmark(lines: Sequence[Any], label: str) -> Optional[tuple[int, int]]:
    want = squash(label)
    best = None
    for line in lines:
        text = squash(line.text)
        if want and want in text:
            x, y, w, h = line.region
            # Locate the label inside the line by character offset.
            start = text.index(want)
            per = w / max(1, len(text))
            cx = int(x + per * (start + len(want) / 2))
            cand = (cx, int(y + h / 2))
            if best is None or len(text) < best[0]:
                best = (len(text), cand)
    return best[1] if best else None


def fingerprint(
    lines: Sequence[Any],
    viewport: tuple[int, int],
    *,
    landmarks: Sequence[str] = (),
    profile_sha256: Optional[str] = None,
) -> LayoutFingerprint:
    """Summarize a frame from its OCR lines (objects with text and region)."""
    usable = [ln for ln in lines if len(squash(ln.text)) >= 3]
    heights = [ln.region[3] for ln in usable]
    widths = [ln.region[2] / len(squash(ln.text)) for ln in usable]
    found = {}
    for label in landmarks:
        pos = _find_landmark(usable, label)
        if pos is not None:
            found[label] = pos
    return LayoutFingerprint(
        viewport=(int(viewport[0]), int(viewport[1])),
        text_height=statistics.median(heights) if heights else None,
        char_width=statistics.median(widths) if widths else None,
        lines=len(usable),
        landmarks=found,
        profile_sha256=profile_sha256,
    )


@dataclass
class DriftVerdict:
    """Result of :func:`compare`."""

    reasons: list[DriftReason] = field(default_factory=list)
    details: list[str] = field(default_factory=list)
    advice: Optional[str] = None

    @property
    def drifted(self) -> bool:
        return bool(self.reasons)

    def add(self, reason: DriftReason, detail: str) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)
        self.details.append(detail)


class LayoutDriftHalt(Exception):
    """The screen no longer matches the recording; a person must look."""

    def __init__(self, verdict: DriftVerdict) -> None:
        super().__init__("; ".join(verdict.details))
        self.verdict = verdict


def compare(
    recorded: LayoutFingerprint,
    live: LayoutFingerprint,
    tolerances: Tolerances = Tolerances(),
) -> DriftVerdict:
    """List every way ``live`` differs from ``recorded`` beyond tolerance."""
    v = DriftVerdict()
    if recorded.viewport != live.viewport:
        v.add(
            "resolution_changed",
            f"frame is {live.viewport[0]}x{live.viewport[1]}, "
            f"recorded {recorded.viewport[0]}x{recorded.viewport[1]}",
        )
    if (
        recorded.profile_sha256
        and live.profile_sha256
        and recorded.profile_sha256 != live.profile_sha256
    ):
        v.add("session_profile_changed", "remote session profile differs")

    if recorded.lines >= tolerances.min_lines and live.lines < tolerances.min_lines:
        v.add(
            "too_little_text",
            f"read {live.lines} text lines, recorded {recorded.lines}",
        )

    height_ratio = None
    if recorded.text_height and live.text_height and live.lines >= tolerances.min_lines:
        height_ratio = live.text_height / recorded.text_height
        if abs(height_ratio - 1) > tolerances.text_height_ratio:
            v.add(
                "font_size_changed",
                f"text is {height_ratio:.2f}x the recorded height",
            )
    if recorded.char_width and live.char_width and live.lines >= tolerances.min_lines:
        width_ratio = live.char_width / recorded.char_width
        # A pure zoom scales width and height together; a different font
        # changes their proportion.
        relative = width_ratio / height_ratio if height_ratio else width_ratio
        if abs(relative - 1) > tolerances.char_width_ratio:
            v.add(
                "font_changed",
                f"character width is {relative:.2f}x the recorded proportion",
            )

    shared = [k for k in recorded.landmarks if k in live.landmarks]
    for k in recorded.landmarks:
        if k not in live.landmarks:
            v.add("landmark_missing", f"label {k!r} not found")
    if shared:
        dxs = [live.landmarks[k][0] - recorded.landmarks[k][0] for k in shared]
        dys = [live.landmarks[k][1] - recorded.landmarks[k][1] for k in shared]
        ox, oy = statistics.median(dxs), statistics.median(dys)
        scale = height_ratio or 1.0
        for k, dx, dy in zip(shared, dxs, dys):
            # A uniform zoom moves labels proportionally; it is reported as a
            # font-size change above, so judge position at the recorded scale.
            ex = abs(dx - ox) / scale
            ey = abs(dy - oy) / scale
            if max(ex, ey) > tolerances.landmark_px and len(shared) > 1:
                v.add("layout_changed", f"label {k!r} moved {int(ex)},{int(ey)} px")
    return v


def check_layout(
    recorded: LayoutFingerprint,
    live: LayoutFingerprint,
    *,
    tolerances: Tolerances = Tolerances(),
    advisor: Optional[Callable[[DriftVerdict], Optional[str]]] = None,
) -> DriftVerdict:
    """Return the verdict for a matching frame; raise on drift.

    Args:
        recorded: Fingerprint stored with the recording.
        live: Fingerprint of the current frame.
        tolerances: Drift tolerances.
        advisor: Optional recovery aid called only after drift is found.
            Its text is attached as ``advice``; the run halts regardless.

    Raises:
        LayoutDriftHalt: The live frame drifted.
    """
    verdict = compare(recorded, live, tolerances)
    if not verdict.drifted:
        return verdict
    if advisor is not None:
        try:
            verdict.advice = advisor(verdict)
        except Exception as exc:  # noqa: BLE001 - advice is optional
            verdict.advice = f"advisor failed: {type(exc).__name__}"
    raise LayoutDriftHalt(verdict)


# ------------------------------------------------------- bundle storage

#: Where the compiler stores the recorded fingerprints. Under ``templates/``
#: so the bundle manifest hashes it (and an encrypted bundle seals it) like
#: every other recorded asset.
FINGERPRINT_ASSET = "templates/layout_fingerprint.json"
FINGERPRINT_SCHEMA = "openadapt.layout-fingerprints/v1"


def fingerprint_to_dict(fp: LayoutFingerprint) -> dict[str, Any]:
    return {
        "viewport": list(fp.viewport),
        "text_height": fp.text_height,
        "char_width": fp.char_width,
        "lines": fp.lines,
        "landmarks": {k: list(v) for k, v in fp.landmarks.items()},
        "profile_sha256": fp.profile_sha256,
    }


def fingerprint_from_dict(data: Mapping[str, Any]) -> LayoutFingerprint:
    vw, vh = data["viewport"]
    return LayoutFingerprint(
        viewport=(int(vw), int(vh)),
        text_height=data.get("text_height"),
        char_width=data.get("char_width"),
        lines=int(data.get("lines") or 0),
        landmarks={
            str(k): (int(v[0]), int(v[1]))
            for k, v in (data.get("landmarks") or {}).items()
        },
        profile_sha256=data.get("profile_sha256"),
    )


def dump_bundle_fingerprints(
    steps: Mapping[str, LayoutFingerprint],
    *,
    scale_landmarks: Optional[Mapping[str, Any]] = None,
) -> bytes:
    """Serialize the per-step fingerprints.

    ``scale_landmarks`` is ``{"salt": hex, "steps": {step_id: [[digest, cx,
    cy, w, h], ...]}}`` from :func:`openadapt_flow.runtime.render_scale.scale_landmark_set`:
    salted hashes of label text with their centres, no text.
    """
    body: dict[str, Any] = {
        "schema": FINGERPRINT_SCHEMA,
        "steps": {k: fingerprint_to_dict(v) for k, v in sorted(steps.items())},
    }
    if scale_landmarks:
        body["scale_landmarks"] = {
            "salt": scale_landmarks["salt"],
            "steps": {
                k: v for k, v in sorted(scale_landmarks["steps"].items()) if k in steps
            },
        }
    return (json.dumps(body, indent=2, sort_keys=True) + "\n").encode("utf-8")


def load_bundle_fingerprints(raw: Optional[bytes]) -> dict[str, LayoutFingerprint]:
    """Recorded fingerprints by step id. Empty when absent or unreadable."""
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
        if data.get("schema") != FINGERPRINT_SCHEMA:
            return {}
        return {
            str(k): fingerprint_from_dict(v) for k, v in data.get("steps", {}).items()
        }
    except (ValueError, KeyError, TypeError, AttributeError):
        return {}


def load_scale_landmarks(
    raw: Optional[bytes],
) -> tuple[Optional[str], dict[str, list[list[Any]]]]:
    """``(salt, {step_id: [[digest, cx, cy, w, h], ...]})``; empty when absent."""
    if not raw:
        return None, {}
    try:
        data = json.loads(raw.decode("utf-8"))
        if data.get("schema") != FINGERPRINT_SCHEMA:
            return None, {}
        block = data.get("scale_landmarks") or {}
        steps = {
            str(k): [[str(e[0]), *(float(x) for x in e[1:5])] for e in v]
            for k, v in (block.get("steps") or {}).items()
        }
        return str(block.get("salt", "")), steps
    except (ValueError, KeyError, TypeError, AttributeError, IndexError):
        return None, {}


def describe(verdict: DriftVerdict) -> str:
    """``reason[, reason]: detail; detail`` for a halt message."""
    return f"{', '.join(verdict.reasons)}: {'; '.join(verdict.details)}"
