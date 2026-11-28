"""Bijective Synthetic-Twin masking for screenshots and text.

Replaces known dossier strings and detected PHI/PII patterns (dates, phone
numbers, SSNs, emails, currency amounts, member IDs) with format-preserving
realistic fake values before a screenshot or prompt is sent to an external VLM,
then translates any returned synthetic tokens back to the local values.

Pillow is imported lazily inside :func:`mask_screenshot_png` so the core
package stays importable without the ``vision`` extra.
"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from openadapt_flow.ir import Region

# Ordered PHI/PII patterns for automatic detection in OCR text and prompts.
# More specific patterns come first so overlaps resolve to the tightest match.
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_ISO_DATE_RE = re.compile(
    r"\b(?:19|20)\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])\b"
)
_US_DATE_RE = re.compile(
    r"\b(?:0?[1-9]|1[0-2])/(?:0?[1-9]|[12]\d|3[01])/(?:19|20)\d{2}\b"
)
_PHONE_RE = re.compile(
    r"(?<!\w)(?:\+1[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]\d{3}[\s.-]\d{4}(?!\w)"
)
_CURRENCY_RE = re.compile(r"\$\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\$\d+(?:\.\d{2})?")
_MEMBER_ID_RE = re.compile(
    r"\b(?:MRN|MEM|MBR|PAT|ID|POL|CLM|ACC)[-_ ]?[A-Z0-9]{4,12}\b"
)

_PATTERN_SPECS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ssn", _SSN_RE),
    ("email", _EMAIL_RE),
    ("iso_date", _ISO_DATE_RE),
    ("us_date", _US_DATE_RE),
    ("phone", _PHONE_RE),
    ("currency", _CURRENCY_RE),
    ("member_id", _MEMBER_ID_RE),
)

_FIRST_NAMES: tuple[str, ...] = (
    "Claire",
    "David",
    "Nadia",
    "Owen",
    "Priya",
    "Samuel",
    "Tessa",
    "Victor",
    "Hannah",
    "Julian",
    "Kendra",
    "Marcus",
    "Elena",
    "Arthur",
    "Miriam",
    "Caleb",
)

_LAST_NAMES: tuple[str, ...] = (
    "Bennett",
    "Mercer",
    "Morales",
    "Keller",
    "Sato",
    "Holloway",
    "Vance",
    "Patel",
    "Nolan",
    "Brooks",
    "Castillo",
    "Okafor",
    "Lindholm",
    "Frazier",
    "Chen",
    "Gallagher",
)


@dataclass(frozen=True)
class OcrTextBox:
    """One OCR text box in full-frame pixel coordinates.

    Attributes:
        text: Recognized text inside the box.
        region: Bounding box ``(x, y, w, h)`` in screen pixels.
        confidence: Recognition confidence in ``[0, 1]``.
    """

    text: str
    region: Region
    confidence: float = 1.0

    @classmethod
    def from_ocr_line(cls, line: Any) -> "OcrTextBox":
        """Convert an ``OcrLine`` or duck-typed object with ``text`` and ``region``."""
        x, y, w, h = line.region
        return cls(
            text=str(line.text),
            region=(int(x), int(y), int(w), int(h)),
            confidence=float(getattr(line, "confidence", 1.0)),
        )


@dataclass(frozen=True)
class MaskReport:
    """Audit summary returned by :func:`mask_screenshot_png`.

    Attributes:
        total_boxes: Total OCR boxes inspected.
        masked_box_count: Number of OCR boxes repainted with synthetic twin text.
        unmasked_box_count: Number of static OCR boxes left untouched.
        masked_token_count: Total token replacements applied across all boxes.
        raw_dossier_leaks: Count of raw dossier values still found in the
            resulting OCR box texts (0 when masking succeeded completely).
        leaked_keys: Dossier keys whose raw values were still present, if any.
    """

    total_boxes: int
    masked_box_count: int
    unmasked_box_count: int
    masked_token_count: int
    raw_dossier_leaks: int
    leaked_keys: tuple[str, ...] = ()


def _hash_int(seed: int, raw: str, nonce: int) -> int:
    digest = hashlib.sha256(f"{seed}:{nonce}:{raw}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _infer_category(value: str, key_hint: Optional[str] = None) -> str:
    """Classify a raw string into a format-preserving generator category."""
    if _SSN_RE.fullmatch(value):
        return "ssn"
    if _EMAIL_RE.fullmatch(value):
        return "email"
    if _ISO_DATE_RE.fullmatch(value):
        return "iso_date"
    if _US_DATE_RE.fullmatch(value):
        return "us_date"
    if _PHONE_RE.fullmatch(value):
        return "phone"
    if _CURRENCY_RE.fullmatch(value):
        return "currency"
    if _MEMBER_ID_RE.fullmatch(value):
        return "member_id"

    hint = (key_hint or "").lower()
    if any(tok in hint for tok in ("name", "patient", "provider", "doctor")):
        return "name"
    if any(
        tok in hint for tok in ("mrn", "member", "policy", "claim", "account", "id")
    ):
        return "member_id"
    if "ssn" in hint:
        return "ssn"
    if "email" in hint:
        return "email"
    if "phone" in hint or "tel" in hint:
        return "phone"
    if any(tok in hint for tok in ("dob", "date", "birth")):
        return "iso_date"
    if any(tok in hint for tok in ("amount", "copay", "balance", "price", "cost")):
        return "currency"

    # Multi-word alphabetic strings (e.g. "Ada Lovelace") default to name twins.
    parts = value.split()
    if 1 <= len(parts) <= 3 and all(
        p.replace("-", "").replace("'", "").isalpha() for p in parts
    ):
        if len(parts) >= 2 and all(p[0].isupper() for p in parts):
            return "name"
    return "generic"


def _gen_iso_date(seed: int, raw: str, nonce: int) -> str:
    h = _hash_int(seed, raw, nonce)
    year = 1950 + (h % 50)
    month = 1 + ((h >> 8) % 12)
    day = 1 + ((h >> 16) % 28)
    return f"{year:04d}-{month:02d}-{day:02d}"


def _gen_us_date(seed: int, raw: str, nonce: int) -> str:
    h = _hash_int(seed, raw, nonce)
    year = 1950 + (h % 50)
    month = 1 + ((h >> 8) % 12)
    day = 1 + ((h >> 16) % 28)
    parts = raw.split("/")
    pad_m = len(parts) >= 1 and len(parts[0]) == 2
    pad_d = len(parts) >= 2 and len(parts[1]) == 2
    m_str = f"{month:02d}" if pad_m else str(month)
    d_str = f"{day:02d}" if pad_d else str(day)
    return f"{m_str}/{d_str}/{year:04d}"


def _gen_ssn(seed: int, raw: str, nonce: int) -> str:
    h = _hash_int(seed, raw, nonce)
    # Valid-shaped non-real area code outside 000, 666, and 900-999.
    area = 200 + (h % 450)
    group = 10 + ((h >> 12) % 89)
    serial = 1000 + ((h >> 24) % 8999)
    return f"{area:03d}-{group:02d}-{serial:04d}"


def _gen_phone(seed: int, raw: str, nonce: int) -> str:
    h = _hash_int(seed, raw, nonce)
    area = 201 + (h % 780)
    if area == 555:
        area = 556
    exchange = 200 + ((h >> 12) % 799)
    subscriber = 1000 + ((h >> 24) % 8999)
    digits = f"{area:03d}{exchange:03d}{subscriber:04d}"

    # Preserve exact punctuation and optional leading "+1".
    raw_digits = [i for i, ch in enumerate(raw) if ch.isdigit()]
    chars = list(raw)
    if len(raw_digits) == 11 and raw[raw_digits[0]] == "1":
        target_indices = raw_digits[1:]
    else:
        target_indices = raw_digits[-10:]
    for idx, digit_char in zip(target_indices, digits):
        chars[idx] = digit_char
    return "".join(chars)


def _gen_email(seed: int, raw: str, nonce: int) -> str:
    h = _hash_int(seed, raw, nonce)
    first = _FIRST_NAMES[h % len(_FIRST_NAMES)].lower()
    last = _LAST_NAMES[(h >> 8) % len(_LAST_NAMES)].lower()
    num = 10 + ((h >> 16) % 89)
    return f"{first}.{last}{num}@example.test"


def _gen_currency(seed: int, raw: str, nonce: int) -> str:
    h = _hash_int(seed, raw, nonce)
    has_cents = "." in raw
    has_commas = "," in raw
    raw_num = raw.lstrip("$").replace(",", "")
    int_part = raw_num.split(".")[0]
    width = max(1, len(int_part))
    low = 10 ** (width - 1) if width > 1 else 1
    high = (10**width) - 1
    span = max(1, high - low + 1)
    dollars = low + (h % span)
    formatted_dollars = f"{dollars:,}" if has_commas else str(dollars)
    if has_cents:
        cents = (h >> 16) % 100
        return f"${formatted_dollars}.{cents:02d}"
    return f"${formatted_dollars}"


def _gen_member_id(seed: int, raw: str, nonce: int) -> str:
    """Preserve prefix and separator layout while replacing digits deterministically."""
    h = _hash_int(seed, raw, nonce)
    out: list[str] = []
    # Keep leading alphabetic prefix (e.g. "MRN", "MEM", "ID") so visual layout
    # and semantics stay recognizable to a VLM.
    prefix_end = 0
    while prefix_end < len(raw) and raw[prefix_end].isalpha():
        prefix_end += 1
    out.append(raw[:prefix_end])
    state = h
    for i, ch in enumerate(raw[prefix_end:], start=prefix_end):
        state = _hash_int(seed, f"{raw}:{i}", nonce + i)
        if ch.isdigit():
            out.append(str(state % 10))
        elif ch.isupper():
            out.append(chr(ord("A") + (state % 26)))
        elif ch.islower():
            out.append(chr(ord("a") + (state % 26)))
        else:
            out.append(ch)
    return "".join(out)


def _gen_name(seed: int, raw: str, nonce: int) -> str:
    h = _hash_int(seed, raw, nonce)
    words = raw.split()
    first = _FIRST_NAMES[h % len(_FIRST_NAMES)]
    last = _LAST_NAMES[(h >> 8) % len(_LAST_NAMES)]
    if len(words) == 1:
        return first if nonce == 0 else f"{first}{nonce}"
    if len(words) == 2:
        return f"{first} {last}"
    mid = chr(ord("A") + ((h >> 16) % 26))
    return f"{first} {mid}. {last}"


def _gen_generic(seed: int, raw: str, nonce: int) -> str:
    h = _hash_int(seed, raw, nonce)
    out: list[str] = []
    for i, ch in enumerate(raw):
        v = _hash_int(seed, f"{raw}:{i}", nonce + i)
        if ch.isdigit():
            out.append(str(v % 10))
        elif ch.isupper():
            out.append(chr(ord("A") + (v % 26)))
        elif ch.islower():
            out.append(chr(ord("a") + (v % 26)))
        else:
            out.append(ch)
    if "".join(out) == raw:
        return f"TWIN-{h % 100000:05d}"
    return "".join(out)


_GENERATORS = {
    "iso_date": _gen_iso_date,
    "us_date": _gen_us_date,
    "ssn": _gen_ssn,
    "phone": _gen_phone,
    "email": _gen_email,
    "currency": _gen_currency,
    "member_id": _gen_member_id,
    "name": _gen_name,
    "generic": _gen_generic,
}


class SyntheticTwinProjector:
    """Bijective mapper between local dossier/PHI values and realistic fake twins.

    Every raw value maps to a unique format-preserving synthetic twin, and
    every synthetic twin maps back to its exact original raw value.
    """

    def __init__(
        self,
        dossier: Optional[Mapping[str, str]] = None,
        *,
        seed: int = 0,
    ) -> None:
        self._seed = int(seed)
        self._dossier: dict[str, str] = {}
        self._raw_to_twin: dict[str, str] = {}
        self._twin_to_raw: dict[str, str] = {}
        if dossier:
            for key, value in dossier.items():
                val = str(value).strip()
                if not val:
                    continue
                self._dossier[str(key)] = val
                cat = _infer_category(val, key_hint=str(key))
                self.project(val, category=cat)

    @property
    def dossier(self) -> Mapping[str, str]:
        """Read-only view of registered dossier entries."""
        return dict(self._dossier)

    @property
    def forward_map(self) -> Mapping[str, str]:
        """Copy of the raw -> synthetic twin mapping."""
        return dict(self._raw_to_twin)

    @property
    def reverse_map(self) -> Mapping[str, str]:
        """Copy of the synthetic twin -> raw mapping."""
        return dict(self._twin_to_raw)

    def project(self, raw_value: str, *, category: Optional[str] = None) -> str:
        """Return the bijective synthetic twin for ``raw_value``."""
        if not raw_value:
            return raw_value
        existing = self._raw_to_twin.get(raw_value)
        if existing is not None:
            return existing
        cat = category or _infer_category(raw_value)
        gen = _GENERATORS.get(cat, _gen_generic)
        for nonce in range(1024):
            candidate = gen(self._seed, raw_value, nonce)
            if (
                candidate != raw_value
                and candidate not in self._twin_to_raw
                and candidate not in self._raw_to_twin
            ):
                self._raw_to_twin[raw_value] = candidate
                self._twin_to_raw[candidate] = raw_value
                return candidate
        raise RuntimeError(
            f"Could not allocate unique synthetic twin for category {cat!r}"
        )

    def unproject(self, twin_value: str) -> str:
        """Return the original raw value for ``twin_value``, or ``twin_value`` if unknown."""
        return self._twin_to_raw.get(twin_value, twin_value)

    def mask_text(self, text: str) -> tuple[str, int]:
        """Replace known dossier values and detected PHI patterns in one pass.

        Returns:
            A tuple ``(masked_text, replacement_count)``.
        """
        if not text:
            return text, 0

        # Collect non-overlapping spans: (start, end, raw_substring, category).
        spans: list[tuple[int, int, str, Optional[str]]] = []

        def _overlaps(start: int, end: int) -> bool:
            return any(not (end <= s or start >= e) for s, e, _, _ in spans)

        # 1. Known registered raw values (longest first to prefer full names/IDs).
        known_raws = sorted(self._raw_to_twin.keys(), key=len, reverse=True)
        for raw in known_raws:
            if not raw or raw not in text:
                continue
            search_from = 0
            while True:
                idx = text.find(raw, search_from)
                if idx == -1:
                    break
                end = idx + len(raw)
                if not _overlaps(idx, end):
                    spans.append((idx, end, raw, None))
                search_from = end

        # 2. Regex-detected PHI patterns in uncovered regions.
        for cat, pattern in _PATTERN_SPECS:
            for match in pattern.finditer(text):
                start, end = match.span()
                if not _overlaps(start, end):
                    spans.append((start, end, match.group(0), cat))

        if not spans:
            return text, 0

        spans.sort(key=lambda item: item[0])
        pieces: list[str] = []
        cursor = 0
        for start, end, raw_sub, span_cat in spans:
            if start > cursor:
                pieces.append(text[cursor:start])
            pieces.append(self.project(raw_sub, category=span_cat))
            cursor = end
        if cursor < len(text):
            pieces.append(text[cursor:])
        return "".join(pieces), len(spans)

    def unmask_text(self, text: str) -> str:
        """Translate synthetic twin tokens in ``text`` back to their real values."""
        if not text or not self._twin_to_raw:
            return text

        spans: list[tuple[int, int, str]] = []

        def _overlaps(start: int, end: int) -> bool:
            return any(not (end <= s or start >= e) for s, e, _ in spans)

        twins = sorted(self._twin_to_raw.keys(), key=len, reverse=True)
        for twin in twins:
            if not twin or twin not in text:
                continue
            search_from = 0
            while True:
                idx = text.find(twin, search_from)
                if idx == -1:
                    break
                end = idx + len(twin)
                if not _overlaps(idx, end):
                    spans.append((idx, end, self._twin_to_raw[twin]))
                search_from = end

        if not spans:
            return text

        spans.sort(key=lambda item: item[0])
        pieces: list[str] = []
        cursor = 0
        for start, end, original in spans:
            if start > cursor:
                pieces.append(text[cursor:start])
            pieces.append(original)
            cursor = end
        if cursor < len(text):
            pieces.append(text[cursor:])
        return "".join(pieces)


def unmask_text(text: str, projector: SyntheticTwinProjector) -> str:
    """Translate synthetic twin tokens returned by a VLM back to local dossier values."""
    return projector.unmask_text(text)


def _sample_background_rgb(
    img: Any, x0: int, y0: int, x1: int, y1: int
) -> tuple[int, int, int]:
    """Estimate the solid background color around a bounding box using border pixels."""
    w, h = img.size
    samples: list[tuple[int, int, int]] = []
    top_y = max(0, y0 - 1)
    bot_y = min(h - 1, y1)
    left_x = max(0, x0 - 1)
    right_x = min(w - 1, x1)

    step_x = max(1, (x1 - x0) // 8)
    for px in range(max(0, x0), min(w, x1), step_x):
        samples.append(img.getpixel((px, top_y)))
        samples.append(img.getpixel((px, bot_y)))

    step_y = max(1, (y1 - y0) // 4)
    for py in range(max(0, y0), min(h, y1), step_y):
        samples.append(img.getpixel((left_x, py)))
        samples.append(img.getpixel((right_x, py)))

    if not samples:
        return (255, 255, 255)
    rs = sorted(p[0] for p in samples)
    gs = sorted(p[1] for p in samples)
    bs = sorted(p[2] for p in samples)
    mid = len(samples) // 2
    return (int(rs[mid]), int(gs[mid]), int(bs[mid]))


def _contrast_text_rgb(bg: tuple[int, int, int]) -> tuple[int, int, int]:
    """Pick dark text on light backgrounds and light text on dark backgrounds."""
    luminance = 0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2]
    return (24, 24, 24) if luminance >= 128.0 else (240, 240, 240)


def _load_fitted_font(box_h: int) -> Any:
    """Load a Pillow font sized to fit inside ``box_h`` pixels."""
    from PIL import ImageFont

    target_size = max(10, int(round(box_h * 0.72)))
    try:
        return ImageFont.load_default(size=target_size)
    except TypeError:
        return ImageFont.load_default()


def mask_screenshot_png(
    png_bytes: bytes,
    ocr_boxes: Sequence[OcrTextBox],
    projector: SyntheticTwinProjector,
) -> tuple[bytes, MaskReport]:
    """Paint format-preserving synthetic twin text over PHI OCR boxes in a PNG.

    Boxes whose text contains no dossier entries or detected PHI patterns are
    left completely untouched so static UI labels keep their exact pixels.

    Args:
        png_bytes: Original screenshot PNG bytes.
        ocr_boxes: OCR text boxes in screenshot pixel coordinates.
        projector: Bijective :class:`SyntheticTwinProjector` instance.

    Returns:
        A tuple ``(masked_png_bytes, MaskReport)``.
    """
    from PIL import Image, ImageDraw

    img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    w, h = img.size
    draw = ImageDraw.Draw(img)

    masked_box_count = 0
    masked_token_count = 0
    resulting_texts: list[str] = []

    for box in ocr_boxes:
        masked_text, count = projector.mask_text(box.text)
        resulting_texts.append(masked_text)
        if count == 0:
            continue

        rx, ry, rw, rh = box.region
        if rw <= 0 or rh <= 0:
            continue
        x0 = max(0, min(w - 1, int(rx)))
        y0 = max(0, min(h - 1, int(ry)))
        x1 = max(x0 + 1, min(w, int(rx + rw)))
        y1 = max(y0 + 1, min(h, int(ry + rh)))

        bg = _sample_background_rgb(img, x0, y0, x1, y1)
        fg = _contrast_text_rgb(bg)
        draw.rectangle((x0, y0, x1, y1), fill=bg)

        font = _load_fitted_font(y1 - y0)
        text_y = y0 + max(0, ((y1 - y0) - max(10, int((y1 - y0) * 0.72))) // 2)
        draw.text((x0 + 2, text_y), masked_text, fill=fg, font=font)

        masked_box_count += 1
        masked_token_count += count

    # Audit that zero raw dossier values remain in any resulting OCR box text.
    combined_masked = "\n".join(resulting_texts)
    leaked: list[str] = []
    for key, raw_val in projector.dossier.items():
        if raw_val and raw_val in combined_masked:
            leaked.append(key)

    out = io.BytesIO()
    img.save(out, format="PNG")
    report = MaskReport(
        total_boxes=len(ocr_boxes),
        masked_box_count=masked_box_count,
        unmasked_box_count=len(ocr_boxes) - masked_box_count,
        masked_token_count=masked_token_count,
        raw_dossier_leaks=len(leaked),
        leaked_keys=tuple(leaked),
    )
    return out.getvalue(), report
