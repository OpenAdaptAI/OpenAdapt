"""The reading step: read one field off the screen into a typed value.

By default a field is read through one channel and the value is returned
with the grade ``unchecked`` and no Seal. Proof is opt-in through
:class:`~openadapt_flow.records.schema.ReadProof`. With ``double_read`` a
field is read through two channels:

- the **clipboard**: focus the field, select it, copy it, and read the
  application's own text. This is deterministic when the application allows
  it, so it is preferred as the value;
- **OCR** of the field's screen region, which checks the clipboard and is the
  only channel when copying is not possible.

:func:`combine_reads` turns the channel reads into a
:class:`~openadapt_flow.records.schema.FieldRead`: agreement, a value, a
confidence, the read grade and whether it needs review. :func:`read_field`
does the live read on a backend and then calls it. :func:`add_read` adds a
later read (the reopened record, or an export, database or API read) and
upgrades the grade only when that read agrees. An export read states its
source: a ``live_session`` export counts as a screen read, a
``committed_store`` export as an independent one.

Comparison reuses :mod:`openadapt_flow.runtime.effects.readback_values`, so
look-alike characters (O and 0, l and 1) in OCR make a read *ambiguous*,
never a silent match. No model calls.

Confidence is a fixed rule, not a learned score:

========================  ===========================================
Reads                     Confidence
========================  ===========================================
two or more agree         0.95, plus 0.05 x the OCR engine confidence
clipboard only            0.7
OCR only                  0.6 x the OCR engine confidence
look-alike ambiguity      0.4
disagree                  0.1
nothing parsed            0.0
========================  ===========================================

A field that sets ``min_confidence`` sends reads below it to review. Without
it, only disagreement, look-alike ambiguity, an invalid value or a missing
second channel (when ``double_read`` is on) send a field to review.
"""

from __future__ import annotations

import hashlib
import io
import re
from typing import Any, Callable, Optional, Protocol, Sequence

from openadapt_flow.records.schema import (
    Agreement,
    ChannelRead,
    FieldRead,
    FieldSpec,
    ReadChannel,
    ReadProof,
    SealRef,
    SourceKind,
    SourceRegion,
    grade_for,
)
from openadapt_flow.runtime.effects.readback_values import (
    compare_value,
    normalize_value,
)

__all__ = [
    "ClipboardReader",
    "SystemClipboard",
    "add_read",
    "canonical_value",
    "channel_read",
    "combine_reads",
    "normalize_for",
    "read_field",
]

#: Written to the clipboard before copying, so a copy that did nothing is
#: seen as no read instead of as the previous field's value.
_SENTINEL = "\u2063openadapt-records-empty\u2063"

_TRUE = {"true", "yes", "y", "1", "checked", "on"}
_FALSE = {"false", "no", "n", "0", "unchecked", "off"}
_INTEGER = re.compile(r"^-?\d+$")
_DECIMAL = re.compile(r"^-?\d+(\.\d+)?$")


class ClipboardReader(Protocol):
    """Reads and writes the clipboard of the session being driven."""

    def read_text(self) -> Optional[str]: ...

    def write_text(self, text: str) -> None: ...


def normalize_for(spec: FieldSpec, raw: Optional[str]) -> Optional[str]:
    """The canonical value of ``raw`` for ``spec``, or None if it does not parse."""

    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    kind = spec.kind
    if kind in ("text", "code", "date", "currency"):
        value = normalize_value(text, kind, date_order=spec.date_order)
    elif kind == "integer":
        cleaned = text.replace(",", "").replace(" ", "")
        value = str(int(cleaned)) if _INTEGER.match(cleaned) else None
    elif kind == "decimal":
        cleaned = text.replace(",", "").replace(" ", "")
        value = cleaned if _DECIMAL.match(cleaned) else None
    elif kind == "boolean":
        lowered = text.lower()
        value = "true" if lowered in _TRUE else "false" if lowered in _FALSE else None
    else:  # enum
        lowered = " ".join(text.lower().split())
        value = next((c for c in spec.choices if c.lower() == lowered), None)
    if value is not None and spec.pattern is not None:
        if not re.fullmatch(spec.pattern, value):
            return None
    return value


def canonical_value(spec: FieldSpec, raw: Optional[str]) -> Optional[str]:
    """The value to report and export for ``raw``, or None if it does not parse.

    Text and code keep the application's characters, with runs of whitespace
    collapsed, so ``AC-1042`` stays ``AC-1042``. Other kinds use their
    normalized form (an ISO date, a plain decimal amount).
    """

    normalized = normalize_for(spec, raw)
    if normalized is None or raw is None:
        return None
    if spec.kind in ("text", "code"):
        return " ".join(raw.split())
    return normalized


def channel_read(
    spec: FieldSpec,
    channel: ReadChannel,
    raw: Optional[str],
    *,
    engine_confidence: Optional[float] = None,
    session: Optional[str] = None,
    source_kind: Optional[SourceKind] = None,
    record_id: Optional[str] = None,
    record_count: Optional[int] = None,
) -> ChannelRead:
    """One channel's read of ``spec``, normalized for its kind.

    An ``export`` read needs ``source_kind``. ``session`` defaults from the
    channel: ``independent`` for database, API and committed-store export
    reads, ``acting`` for everything else.
    """

    if session is None:
        independent = channel in ("database", "api") or (
            channel == "export" and source_kind == "committed_store"
        )
        session = "independent" if independent else "acting"
    return ChannelRead(
        channel=channel,
        raw=raw,
        normalized=normalize_for(spec, raw),
        engine_confidence=engine_confidence,
        session=session,  # type: ignore[arg-type]
        source_kind=source_kind,
        record_id=record_id,
        record_count=record_count,
    )


def _compare(spec: FieldSpec, a: ChannelRead, b: ChannelRead) -> str:
    """match, mismatch or ambiguous for two parsed reads of one field.

    Only equal normalized values match. Reads that differ but could be the
    same value under OCR look-alikes are ambiguous, never a match.
    """

    assert a.normalized is not None and b.normalized is not None
    if a.normalized == b.normalized:
        if spec.kind == "code" and not _same_code_layout(a.raw, b.raw):
            return "mismatch"
        return "match"
    verdict = _compare_different(spec, a, b)
    return "ambiguous" if verdict == "match" else verdict


def _compare_different(spec: FieldSpec, a: ChannelRead, b: ChannelRead) -> str:
    if spec.kind not in ("text", "code"):
        # Typed values compared in canonical form; OCR look-alikes in the raw
        # text still count as ambiguous rather than wrong.
        ocr = a.channel == "ocr" or b.channel == "ocr"
        if ocr and spec.kind in ("date", "currency"):
            raw_a, raw_b = a.raw or "", b.raw or ""
            observed, expected = (
                (raw_a, raw_b) if a.channel == "ocr" else (raw_b, raw_a)
            )
            return compare_value(
                observed,
                expected,
                kind=spec.kind,
                source="ocr",
                date_order=spec.date_order,
            )
        return "mismatch"
    ocr_side = a if a.channel == "ocr" else b if b.channel == "ocr" else None
    if ocr_side is None:
        return "mismatch"
    other = b if ocr_side is a else a
    return compare_value(
        ocr_side.normalized or "", other.normalized or "", kind=spec.kind, source="ocr"
    )


_CODE_PARTS = re.compile(r"[^\s\-./]+")


def _same_code_layout(a: Optional[str], b: Optional[str]) -> bool:
    """False when both codes are split by separators, in different places.

    Code values are compared without separators, so ``AC1042`` from a
    database matches ``AC-1042`` on screen. ``12-345`` and ``123-45`` are
    different codes, though: when both sides have separators, the parts
    between them must line up.
    """

    parts_a = _CODE_PARTS.findall((a or "").lower())
    parts_b = _CODE_PARTS.findall((b or "").lower())
    if len(parts_a) < 2 or len(parts_b) < 2:
        return True
    return parts_a == parts_b


def _agreement(spec: FieldSpec, parsed: Sequence[ChannelRead]) -> Agreement:
    if len(parsed) < 2:
        return "single"
    anchor = parsed[0]
    verdicts = [_compare(spec, anchor, other) for other in parsed[1:]]
    if "mismatch" in verdicts:
        return "disagree"
    if "ambiguous" in verdicts:
        return "ambiguous"
    return "agree"


def _confidence(agreement: Agreement, parsed: Sequence[ChannelRead]) -> float:
    ocr_conf = [r.engine_confidence for r in parsed if r.channel == "ocr"]
    engine = min((c for c in ocr_conf if c is not None), default=1.0)
    if not parsed:
        return 0.0
    if agreement == "agree":
        return round(0.95 + 0.05 * engine, 4)
    if agreement == "ambiguous":
        return 0.4
    if agreement == "disagree":
        return 0.1
    only = parsed[0]
    if only.channel == "ocr":
        return round(0.6 * (only.engine_confidence or 0.0), 4)
    return 0.7


def _preferred(parsed: Sequence[ChannelRead]) -> ChannelRead:
    """The read whose value is reported: independent, then clipboard, then structure."""

    rank = {
        "export": 1,
        "database": 0,
        "api": 0,
        "clipboard": 1,
        "structure": 2,
        "accessibility": 2,
        "ocr": 3,
    }
    return min(parsed, key=lambda r: 0 if r.independent else rank[r.channel])


def combine_reads(
    spec: FieldSpec,
    reads: Sequence[ChannelRead],
    *,
    region: Optional[SourceRegion] = None,
    seal: Optional[SealRef] = None,
    proof: Optional[ReadProof] = None,
) -> FieldRead:
    """Judge the channel reads of one field and build its :class:`FieldRead`.

    A single read is not sent to review on its own: it is simply
    ``unchecked``. It is sent to review when ``proof.double_read`` asked for
    two channels, or when the field sets ``min_confidence`` and the read is
    below it.
    """

    if not reads:
        raise ValueError("combine_reads needs at least one channel read")
    # Which record, and only one: a value read from two records, or from a
    # read that matched several, is not one record's value.
    record_problems = []
    for r in reads:
        if r.record_count is not None and r.record_count != 1:
            record_problems.append(
                f"the {r.channel} read matched {r.record_count} records"
            )
        elif r.session == "independent" and r.record_count is None and r.normalized:
            record_problems.append(
                f"the {r.channel} read did not state that it read exactly one record"
            )
    ids = {r.record_id for r in reads if r.record_id is not None}
    if len(ids) > 1:
        record_problems.append("the reads are of different records")
    parsed = [r for r in reads if r.normalized is not None]
    agreement = _agreement(spec, parsed)
    if record_problems and agreement in ("agree", "single"):
        agreement = "disagree" if len(ids) > 1 else "ambiguous"
    confidence = _confidence(agreement, parsed)
    grade = grade_for(tuple(parsed), agreement)
    value = None
    if agreement in ("agree", "single") and parsed:
        value = canonical_value(spec, _preferred(parsed).raw)
    reasons = list(record_problems)
    if not parsed:
        reasons.append("no channel read a valid value")
        if not spec.required and not any(r.raw and r.raw.strip() for r in reads):
            # An optional field that is empty on every channel is fine; one
            # that shows text no channel could parse is not.
            reasons.clear()
    elif record_problems:
        pass
    elif agreement == "disagree":
        reasons.append("channels disagree")
    elif agreement == "ambiguous":
        reasons.append("reads differ only in look-alike characters")
    elif proof is not None and proof.double_read and len(parsed) < 2:
        reasons.append("double read requested; only one channel read a value")
    elif spec.min_confidence is not None and confidence < spec.min_confidence:
        reasons.append(f"confidence {confidence:.2f} below {spec.min_confidence:.2f}")
    unparsed = [r.channel for r in reads if r.raw and r.normalized is None]
    if unparsed and parsed:
        reasons.append(f"not a valid {spec.kind}: {', '.join(unparsed)}")
    return FieldRead(
        field=spec.name,
        value=value,
        reads=tuple(reads),
        agreement=agreement,
        confidence=confidence,
        grade=grade,
        region=region,
        seal=seal,
        needs_review=bool(reasons),
        review_reason="; ".join(reasons) or None,
    )


def add_read(spec: FieldSpec, prior: FieldRead, later: ChannelRead) -> FieldRead:
    """Add a later read (reopened record or independent channel) and re-judge.

    The grade rises only if every parsed read, old and new, agrees. A later
    read that disagrees sends the field to review.
    """

    return combine_reads(
        spec, (*prior.reads, later), region=prior.region, seal=prior.seal
    ).model_copy(update={"review_decision": prior.review_decision})


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _crop_sha256(png: bytes, bbox: tuple[int, int, int, int]) -> Optional[str]:
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is a core dependency
        return None
    x, y, w, h = bbox
    with Image.open(io.BytesIO(png)) as image:
        crop = image.crop((x, y, x + w, y + h))
        out = io.BytesIO()
        crop.save(out, format="PNG")
    return _sha256(out.getvalue())


def read_field(
    spec: FieldSpec,
    backend: Any,
    bbox: tuple[int, int, int, int],
    *,
    proof: Optional[ReadProof] = None,
    clipboard: Optional[ClipboardReader] = None,
    copy_keys: Sequence[str] = ("Meta+a", "Meta+c"),
    ocr: Optional[Callable[..., list[Any]]] = None,
    session: str = "acting",
    step_id: Optional[str] = None,
    seal: Optional[SealRef] = None,
    frame_png: Optional[bytes] = None,
) -> FieldRead:
    """Read ``spec`` at ``bbox`` on ``backend``.

    By default (``proof`` unset or all off) the field is read through one
    channel: the clipboard when one is given, otherwise OCR. The result is
    ``unchecked`` and carries no Seal. With ``proof.double_read`` both
    channels are read and compared. With ``proof.seal`` the given ``seal``
    pointer is attached; it is required then and ignored otherwise.

    ``backend`` is a Flow :class:`~openadapt_flow.backend.Backend`. The
    screenshot is taken before the field is focused, so the region and its
    hashes show the screen that was read. For the clipboard read, the
    clipboard is first set to a marker, the field centre is clicked, and
    ``copy_keys`` are pressed; a clipboard still holding the marker counts as
    no read. Pass ``session="reopened"`` when reading a record that was
    closed and reopened by its identity fields. ``frame_png`` reuses a frame
    the caller already captured (the runtime's pre-step frame) instead of
    taking a new screenshot.
    """

    proof = proof or ReadProof()
    if proof.double_read and clipboard is None:
        raise ValueError("double_read needs a clipboard as the second channel")
    if proof.seal and seal is None:
        raise ValueError("seal is on for this field but no SealRef was given")
    use_clipboard = clipboard is not None
    use_ocr = proof.double_read or not use_clipboard

    png = frame_png if frame_png is not None else backend.screenshot()
    width_height = None
    try:
        from PIL import Image

        with Image.open(io.BytesIO(png)) as image:
            width_height = image.size
    except Exception:  # noqa: BLE001 - frame size is optional evidence
        width_height = None
    region = SourceRegion(
        frame_sha256=_sha256(png),
        bbox=bbox,
        crop_sha256=_crop_sha256(png, bbox),
        frame_size=width_height,
        step_id=step_id,
    )
    reads: list[ChannelRead] = []
    if use_clipboard:
        assert clipboard is not None
        clipboard.write_text(_SENTINEL)
        x, y, w, h = bbox
        backend.click(x + w // 2, y + h // 2)
        for key in copy_keys:
            backend.press(key)
        copied = clipboard.read_text()
        if copied == _SENTINEL:
            copied = None
        reads.append(channel_read(spec, "clipboard", copied, session=session))
    if use_ocr:
        if ocr is None:
            from openadapt_flow.vision.ocr import ocr as _ocr

            ocr = _ocr
        lines = ocr(png, region=bbox) or []
        ocr_text = " ".join(line.text for line in lines).strip()
        ocr_conf = min((float(line.confidence) for line in lines), default=0.0)
        reads.append(
            channel_read(
                spec,
                "ocr",
                ocr_text or None,
                engine_confidence=ocr_conf if lines else None,
                session=session,
            )
        )
    return combine_reads(
        spec, reads, region=region, seal=seal if proof.seal else None, proof=proof
    )


class SystemClipboard:
    """The clipboard of the machine Flow runs on (macOS ``pbcopy``/``pbpaste``,
    Linux ``xclip``). A redirected remote-desktop clipboard shows up here too."""

    def __init__(self) -> None:
        import shutil
        import sys

        if sys.platform == "darwin":
            self._read = ["pbpaste"]
            self._write = ["pbcopy"]
        elif shutil.which("xclip"):
            self._read = ["xclip", "-selection", "clipboard", "-o"]
            self._write = ["xclip", "-selection", "clipboard"]
        else:
            raise RuntimeError("no clipboard tool found (pbpaste or xclip)")

    def read_text(self) -> Optional[str]:
        import subprocess

        done = subprocess.run(self._read, capture_output=True, timeout=5, check=False)
        if done.returncode != 0:
            return None
        return done.stdout.decode("utf-8", errors="replace")

    def write_text(self, text: str) -> None:
        import subprocess

        subprocess.run(self._write, input=text.encode("utf-8"), timeout=5, check=True)
