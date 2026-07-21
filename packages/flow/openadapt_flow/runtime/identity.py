"""Target-identity evidence: capture at compile time, verify before acting.

Template/geometry evidence answers "does the recorded *position* still look
right?", not "is this the recorded *target*?" — for a button inside a table
row the discriminative text (the row's name column) sits outside the
template crop, so a pixel-identical sibling row scores ~1.0 while being the
wrong entity (see docs/validation/VALIDATION.md, Track A).

This module closes that gap with a horizontal *context band*: the full-width
strip of OCR text at the target's row, minus the target's own label (labels
are mutable evidence the ladder heals through — rename drift must not fail
identity). The compiler stores the band text on the anchor
(``Anchor.context_text``); the replayer re-reads the band around the
*resolved* point before clicking and requires either:

- **context mode** — an order-insensitive token match against the recorded
  band text (see :func:`band_match`): matched tokens must cover >=
  ``COVERAGE_THRESHOLD`` of the recorded band, no contiguous run of
  uncovered recorded characters may exceed ``UNCOVERED_RUN_CAP`` (a wrong
  entity is a contiguous mismatch — a replaced name — even when long
  shared row text keeps raw coverage high), AND no more than
  ``CONTRADICTED_CHARS_CAP`` characters may be NEAR-MISSES: tokens only
  match when OCR-equivalent under the engine's character-confusion
  classes, and an unmatched token whose observed counterpart is a
  near-name ('Phil'/'Philip', 'John'/'Joan', an off-by-one DOB) is
  affirmative wrong-sibling evidence with its own, stricter budget.
  Since the 2026-07-10 out-of-corpus review, four further budgets close
  the classes corpus v1 excluded by construction: a name-plausible token
  matched ONLY by letter-letter confusion equivalence is SUSPECT (a
  Neil/Nell collision is indistinguishable from a misread — abort, see
  ``SUSPECT_CHARS_CAP``); a raw-unequal, non-confusion-equivalent 1-2
  char token is contradiction (middle initials, the SEX column); an
  observed name-shaped token the recorded band cannot explain is
  contradiction (appended names, two-row merges, rows that merely
  MENTION the recorded patient); and an ABSENT name-like alphabetic
  token >= 4 chars refuses even inside the generic run cap (identity
  must not verify with its identity token never read).
  Order-insensitivity matters because OCR re-reads the same band in a
  different segmentation order between visits (e.g. page chrome around a
  modal), or
- **param mode** — when a workflow parameter's demonstrated value is
  embedded in the recorded band (a parameterized *target*, e.g. the patient
  row), the run's value is substituted into the recorded band and the WHOLE
  substituted band is verified: the run's value must appear in the live
  band AND the band's non-param residue must still match. A band that
  merely mentions the run's value somewhere is not identity.

Both checks compare squashed (lowercased, whitespace-free) text, the same
OCR-tolerant form used by ``benchmark.verify``. Timestamp-bearing lines are
excluded from the recorded context (volatile by construction, same
rationale as the compiler's postcondition filter).

NOTE (commit 8421d51): parameterized values are never asserted as compiled
POSTCONDITIONS — that rule stands. Identity verification is a *pre-action*
check against runtime values, which is exactly what a parameterized target
needs.

Everything here is import-light (no cv2/OCR at module import); image work is
lazy so unit tests can fake the vision namespace.
"""

from __future__ import annotations

import difflib
import re
from collections import Counter
from datetime import date
from typing import (
    Any,
    Iterable,
    Literal,
    NamedTuple,
    Optional,
    Protocol,
    runtime_checkable,
)

from openadapt_flow.ir import IdentityCheck, Point, Region
from openadapt_flow.volatility import (  # noqa: F401 - TIMESTAMP_RE re-exported
    TIMESTAMP_RE,
    is_volatile_line,
)

# Recorded band text shorter than this (squashed) is too weak to
# discriminate anything and is not stored — generic fragments like
# "Active High 3" (11 squashed chars) otherwise arm false confidence: any
# sibling row sharing the generic columns would verify. Bands this short
# also yield "unreadable" (proceed flagged, never verified) when they
# reach verification via an older bundle.
MIN_CONTEXT_CHARS = 12

# A workflow parameter's demonstrated value must be at least this long
# (squashed) to switch the check into param mode; 1-3 char examples match
# everywhere by accident. Kept at 4 (not higher) so real-world first-name
# parameters ("Phil") still get param-mode substitution — an over-trigger
# is harmless now that param mode verifies the WHOLE substituted band
# (the run's value alone can no longer disarm the residue check).
MIN_PARAM_CHARS = 4

# Fraction of the recorded band's squashed characters that must be covered
# by matched tokens. Measured with the token matcher on MockMed rows: the
# true row re-reads at 1.0 (0.97+ under injected OCR jitter); a look-alike
# row sharing every column except the name covers ~0.67 — 0.8 splits the
# populations with margin on both sides.
COVERAGE_THRESHOLD = 0.8

# Coverage alone is defeated when shared text dominates the band (a short
# wrong name next to a long shared procedure string covers 0.89): a wrong
# entity is a CONTIGUOUS mismatch, so no contiguous run of unmatched
# recorded characters (adjacent unmatched tokens merge) may exceed this
# cap. 4 tolerates a single genuinely mutable short cell ("with" garbled
# beyond recognition, a 1-char counter) while a replaced name —
# "Jane Li" -> "Ann Wu" leaves 6 contiguous uncovered chars — fails.
# Operating point picked from the held-out adversarial ROC
# (docs/validation/IDENTITY_EVIDENCE.md).
UNCOVERED_RUN_CAP = 4

# Token matching (see band_match): a recorded token is MATCHED only when
# some observed token — or a concatenation of consecutive observed tokens
# (OCR split the token), or when a concatenation of consecutive recorded
# tokens (OCR joined them) — is OCR-EQUIVALENT to it: identical after
# canonicalizing the character classes real OCR engines confuse
# (l/1/i/|, O/0, 5/s, 2/z, 8/b, 9/g, rn/m, cl/d, vv/w). There is no raw
# similarity-ratio tier and no partial-containment tier anymore: both
# accepted semantic *extensions* of name tokens ('Phil' inside 'Philip'
# at containment 1.0; 'John' vs 'Joan' at ratio 0.75) — the third
# reopening of the wrong-patient P0 (near-name siblings; see
# docs/validation/VALIDATION.md). A substitution of a/o mid-token is a
# different word, not OCR noise; OCR noise has characteristic char-class
# patterns, and only those are accepted.
MIN_BLOCK = 3

# Whole-token similarity used ONLY by substitute_param's token-ownership
# test (which band tokens belong to a parameter's demo value), not by
# band matching.
TOKEN_SIM_RATIO = 0.7

# A recorded token that is NOT matched is CONTRADICTED — affirmative
# evidence of a different entity, not mere absence — when a >=3-char
# observed token is a near-miss for it: canonically similar at >=
# CONTRADICTION_SIM (DOB/MRN single-field edits ~0.9, John/Joan 0.75,
# 3-char names Ted/Tad 0.67), or one canonically contains the other with
# alphabetic residue ('Phil' inside 'Phillipa', sim 0.67 — a semantic
# extension). An unmatched recorded ALPHABETIC token paired with an
# unexplained observed alphabetic token is likewise contradiction (a
# replaced word: 'Amy' -> 'Kim' at sim 0.0), and a generational suffix
# (Jr/Sr/II/III/IV) present on exactly one side always contradicts.
# Contradicted characters are capped separately from mere-absence runs:
# absence is often OCR dropout (occlusion, dropped tokens — the $-cost
# fallback direction), while contradiction is the wrong-sibling-row
# signature. Both caps come from the held-out ROC.
CONTRADICTION_SIM = 0.62
CONTRADICTED_CHARS_CAP = 0

# SUSPECT tokens (2026-07-10 out-of-corpus review): a DISCRIMINATOR token
# whose ONLY match is confusion-equivalence — canonical-equal but
# raw-unequal — is charged to this zero budget, because a confusion-only
# match on a discriminator is indistinguishable at band level from a
# wrong-row read. Two discriminator classes qualify:
#
#   - NAMES (Blocker 1): a name-plausible token (>= MIN_BLOCK chars, no
#     digits on either side) — 'Neil'/'Nell' (i/l), 'Clay'/'Day' (cl/d),
#     'Marnie'/'Mamie' (rn/m) are DIFFERENT REAL NAMES that canonicalize
#     identically. Digit-class noise on a NAME ('Belford'/'Be1ford',
#     '5ample') stays a clean match — a human name contains no digit, so
#     the recorded token being all-alpha proves it is a name and the
#     digit came from OCR, not a rival name.
#
#   - IDENTIFIERS (5th reopening, second review): a RECORDED token that
#     CONTAINS A DIGIT (MRN, account number, chart ref, DOB) whose match
#     needed a letter/digit confusion (l/1, O/0, S/5, Z/2, B/8, g/9) is
#     a different identifier, 'A01234'/'AO1234' — and the identifier is
#     precisely what disambiguates same-name patients. The round-3 rule
#     missed this because it keyed on name-plausibility (false for any
#     token with a digit), so the suspect budget was OFF for exactly the
#     tokens whose confusion equivalence is most dangerous. Scoping on
#     the RECORDED token (not the observed one) is what keeps
#     name-with-digit-noise verifying while identifier-with-digit
#     aborting: the recording carries the ground truth of the token's
#     type. All-digit differences (748291 vs 748292) are NOT
#     confusion-equivalent (two digits are never in one class), so they
#     mismatch via coverage/contradiction, not here.
#
# The budget is zero for both. For identifiers this is option A of the
# review (no corroboration escape): a confusion-differing identifier
# aborts even when name and DOB raw-match, so two same-name patients
# distinguished only by an OCR-confusable identifier char never verify —
# the availability cost (true-row identifier OCR noise now halts) is the
# cheap direction and is disclosed in docs/LIMITS.md.
SUSPECT_CHARS_CAP = 0

# Unexplained observed NAME-SHAPED tokens (review Blocker 3): an
# observed-side superset used to verify unconditionally — context mode
# had no unexplained-observed-token budget (param mode always had one via
# its raw run requirement). An observed token that is name-shaped
# (leading uppercase in the raw band, alphabetic-dominated,
# name-plausible, >= MIN_BLOCK chars) and that the recorded band cannot
# explain is affirmative evidence of a different or merged row: an
# appended middle name, a second row OCR-merged into the band, or a
# message/cc row that MENTIONS the recorded patient. Budget zero, from
# the v1+v2 ROC. Lowercase adjacent-row bleed (mid-procedure words) is
# deliberately exempt — the legitimate spurious-token class.
UNEXPLAINED_NAME_TOKENS_CAP = 0

# Absent name-like tokens (review Major 4): the generic uncovered-run
# cap tolerates a 4-char absence, which let a band verify with its
# 4-char first name never read at all ('Belford, Phil' vs 'Belford,').
# Absence of a name-like ALPHABETIC token is worse than absence of
# trailing numerics (DOB/MRN dropout is the common $-cost OCR direction;
# a missing name is the identity token itself), so unmatched
# alphabetic-dominated, name-plausible tokens carry their own cap: 3
# chars — a dropped 'MRN' label or generic 3-char word is tolerated, a
# >= 4-char name-like token must be read (raw or confusion) to verify.
ABSENT_NAME_TOKEN_CAP = 3

# Glyph-vulnerable identifiers — name+DOB-primary identity (7th wrong-patient
# reopening; benchmark/dense_surface/DENSE_SURFACE.md and the digit-flanked
# review). The 6th-reopening fix (#26) flagged an identifier that carried a
# homoglyph LETTER (O/l/I) and charged it to a zero budget whenever it
# matched, halting identity WHENEVER such an identifier was present — even
# when a discriminative name+DOB independently carried the identity. That
# had two failures, both proven on the real render->OCR->match pipeline:
#
#   1. DIGIT-SIDE MISS (false accept, ~87%). A real MRN is
#      <alpha prefix><numeric body> (MG480312, AC50061). When the confusable
#      glyph is DIGIT-FLANKED, RapidOCR reads the DIGIT form on BOTH a
#      patient (AC50061) and a DIFFERENT same-name/DOB patient (AC5OO61,
#      letter O) — both collapse to 'AC50061', NO homoglyph LETTER survives,
#      #26's letter-only flag misses it, and the sibling verifies. No
#      string-level flag on the identifier can recover a distinction OCR
#      destroyed at the pixel level, and flagging the digit side (any 0/1 in
#      an MRN) would halt ~3 of every 4 real MRNs — catastrophic over-halt.
#   2. OVER-HALT (false abort). #26 halted a TRUE row whose own MRN merely
#      contained an O/l even when the patient's name+DOB clearly and
#      discriminatively identified them (measured 18.89% dense / 33% on a
#      realistic different-name/DOB corpus).
#
# The fix changes WHAT identity trusts, not the glyph rule. Identity is
# verified on the OCR-RELIABLE, linguistically-redundant signal — the
# patient NAME and DOB together — and a confusable-glyph identifier is
# CORROBORATION only, never the sole basis to verify:
#
#   - If a DISCRIMINATIVE NAME is present and matched (a name-like token
#     >= ID_CARRY_NAME_MIN chars that is not a generic column word — the
#     primary human identifier; a matched DOB corroborates it, name+DOB
#     together), identity is CARRIED by name/DOB. A confusable-glyph MRN in
#     the band does NOT block verification — most real patients differ by
#     name/DOB, so a wrong (sibling) row differs there and is caught by
#     coverage / contradiction; the MRN never has to be trusted. This is
#     the common case and must NOT over-halt.
#   - If identity rests SOLELY on a glyph-vulnerable identifier — no
#     discriminative name carries it (the clicked NAME cell is excluded and
#     only DOB + MRN + generic columns remain) and the identity turns on an
#     identifier OCR cannot be trusted to have read glyph-for-glyph —
#     identity is UNVERIFIABLE and HALTS. This is a safe false-ABORT (a
#     hybrid-fallback escalation or a human retry), never a false-accept.
#
# Charged to a zero budget so the operating point stays a hard gate.
GLYPH_AMBIGUOUS_ID_CHARS_CAP = 0

# Minimum length of a name-like alphabetic token for it to count as a
# DISCRIMINATIVE identity carrier (a real surname/first name). Shorter alpha
# tokens (a 3-char label, a status word) do not carry identity on their own.
ID_CARRY_NAME_MIN = 4

# Low-entropy record-list column words that are NOT patient names: the Sex,
# Status, and action-button vocabulary of an EMR list. A same-name sibling
# SHARES these, so they never discriminate identity and must not be mistaken
# for a name carrier (or a glyph-vulnerable MRN would wrongly verify because
# 'Active'/'Open' looked like a name). Compared on the OCR-canonical form of
# the squashed token (see :func:`_is_discriminative_name`).
_GENERIC_IDENTITY_WORDS = frozenset(
    (
        "active",
        "inactive",
        "pending",
        "open",
        "closed",
        "male",
        "female",
        "unknown",
        "discharged",
        "admitted",
        "scheduled",
        "cancelled",
        "canceled",
        "completed",
        "review",
        "results",
        "records",
        "patient",
        "status",
        "search",
        "demo",
        "chart",
        "charts",
        "appointment",
    )
)

# The letter/digit NEAR-HOMOGLYPHS of the O/0 and l/1/I classes — the glyphs
# RapidOCR collapses onto one another. BOTH sides now carry the SAME evidence
# and are flagged identically (the 9th wrong-patient reopening): a confusable
# glyph in an identifier-position token means OCR cannot be trusted to have read
# that token glyph-for-glyph, whether the surviving glyph is a LETTER (O/l/I —
# OCR read a letter where a digit likely belongs) or a DIGIT (0/1 — a
# same-identity re-read AND a homonym whose distinguishing letter-O collapsed to
# a digit-0 both produce this exact digit form). Earlier fixes (#26/#27) tried
# to treat the digit side more leniently — flag only when a homoglyph LETTER
# survived, or let a matched name+DOB "carry" a digit-body MRN — and each left a
# live wrong-patient VERIFY (the 8th reopening on alphanumeric MRNs, the 9th on
# PURELY NUMERIC ones: 100512 vs 1OO512 OCR byte-identically). There is no safe
# asymmetry: any identifier-position token bearing one of these glyphs forces
# the OCR tier to ABSTAIN. (| and ! are the shapes OCR emits for l/I.)
_ID_HOMOGLYPH_LETTERS = frozenset("oli|!")
_ID_HOMOGLYPH_DIGITS = frozenset("01")
_ID_HOMOGLYPH_CHARS = _ID_HOMOGLYPH_LETTERS | _ID_HOMOGLYPH_DIGITS

# Minimum length of a bare alphanumeric run for it to occupy an IDENTIFIER
# position (an MRN / account / chart ref). Below this a run is too short to be a
# discriminating identifier (a 1-2 char code, a sex-column letter).
_ID_MIN_LEN = 3

# Characters that cannot appear in a real person's name but do appear in
# OCR confusion classes: a raw-unequal token pair involving one of these
# is genuine OCR noise, not a possible different name.
_NON_NAME_CHARS = frozenset("0123456789|!")

# Generational suffixes: presence on one side only is identity-bearing
# ('Belford, Phil' vs 'Belford, Phil Jr' is a different patient).
# Membership is tested on the OCR-canonical form (a live 'II' commonly
# reads as 'lI'/'Il'; 'Sr' as '5r') — see _is_generational_suffix.
GENERATIONAL_SUFFIXES = frozenset({"jr", "jr.", "sr", "sr.", "ii", "iii", "iv"})

# OCR character-confusion classes (squashed/lowercased space). A pair of
# tokens is OCR-equivalent iff equal after canonicalization: multi-char
# shapes first (rn->m, cl->d, vv->w), then per-char class representatives.
_CONFUSION_GROUPS = ("l1i|!", "o0", "s5", "z2", "b8", "g9")
_CONFUSION_MULTI = (("rn", "m"), ("cl", "d"), ("vv", "w"))
_CONFUSION_CANON = {ch: group[0] for group in _CONFUSION_GROUPS for ch in group}

# Param mode: required contiguous run for the run's parameter value, scaled
# for short values (a full 16-char run cannot exist inside a 5-char name).
MAX_RUN_REQUIRED = 16

# Band-line refinement: a line belongs to the resolved point's row when its
# vertical center is within this fraction of its own height from the point
# (minimum slack in px below). The anchor's 64px template height spans 2-3
# rows of a dense table (~25px/row); matching per-row prevents a
# one-row-off resolution from verifying on text bleed from the true row.
ROW_PROXIMITY_FACTOR = 0.75
ROW_PROXIMITY_MIN_PX = 4

# Volatile-line policy lives in openadapt_flow.volatility (shared with the
# compiler's postcondition mining): clock-bearing lines and lines dated near
# the recording are dropped from identity context, but a line whose only
# date is FAR from the recording date — a DOB in an identity banner — is
# kept intact, dates included: that date is discriminative identity data,
# not chronology. TIMESTAMP_RE is re-exported from there for backwards
# compatibility.


def squash(text: str) -> str:
    """Lowercase and remove ALL whitespace (OCR-tolerant comparison form)."""
    return "".join(text.lower().split())


def band_region(point: Point, band_height: int, viewport: tuple[int, int]) -> Region:
    """Full-width horizontal band centered on ``point``'s row.

    Args:
        point: The (recorded or resolved) click point.
        band_height: Band height in pixels — the anchor's template crop
            height, so record- and replay-time bands cover the same strip.
        viewport: (width, height) of the frame.

    Returns:
        (x, y, w, h) clamped to the viewport.
    """
    vw, vh = viewport
    h = max(1, min(band_height, vh))
    y = min(max(0, point[1] - h // 2), max(0, vh - h))
    return (0, y, vw, min(h, vh - y))


def regions_intersect(a: Region, b: Region) -> bool:
    """Whether two (x, y, w, h) regions overlap. Public because the
    replayer uses it to exclude the resolved target's own crop from the
    live band (mirroring the compiler's record-time exclusion — the
    label is mutable evidence and must not participate in identity, and
    an ASYMMETRIC observed band would trip the unexplained-name budget
    on the target's own label)."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


_intersects = regions_intersect


def lines_near_point(lines: Iterable[Any], point_y: int) -> list[Any]:
    """Filter OCR lines to the single text row containing ``point_y``.

    A line belongs to the point's row when its vertical center is within
    ``ROW_PROXIMITY_FACTOR`` of its own height from ``point_y`` (with a
    small minimum slack). The coarse band (the anchor's template height,
    64px) spans 2-3 rows of a dense table; identity must be judged against
    the resolved point's OWN row, or a one-row-off resolution verifies on
    text bleed from the adjacent true row.

    Args:
        lines: OCR line objects (``text``/``region``).
        point_y: The click point's y in the same coordinate space as the
            lines' regions.

    Returns:
        The lines belonging to the point's row.
    """
    kept = []
    for line in lines:
        _, ly, _, lh = line.region
        center_y = ly + lh // 2
        slack = max(ROW_PROXIMITY_MIN_PX, int(ROW_PROXIMITY_FACTOR * lh))
        if abs(center_y - point_y) <= slack:
            kept.append(line)
    return kept


def context_from_lines(
    lines: Iterable[Any],
    *,
    exclude_region: Region,
    band: Region,
    point: Optional[Point] = None,
    min_confidence: float = 0.5,
    reference_date: Optional[date] = None,
) -> Optional[str]:
    """Extract the context-band text from full-frame OCR lines.

    Keeps confident lines whose vertical center lies inside ``band`` and
    which do NOT intersect ``exclude_region`` (the target's own crop: its
    label is mutable evidence, healed through on rename drift, so it must
    not participate in identity). Volatile lines — clock times, dates near
    the recording date — are dropped; a line whose only date is FAR from
    ``reference_date`` (a DOB in an identity banner) is kept whole, date
    included. When ``point`` is given, lines are further restricted to the
    point's own text row (see :func:`lines_near_point`) so the recorded
    band matches what replay-time verification reads — one row, not the
    2-3 rows a 64px band spans in a dense table. Kept lines are joined
    left-to-right.

    Args:
        lines: OCR line objects (``text``/``region``/``confidence``).
        exclude_region: The anchor's template crop region.
        band: The context band (see :func:`band_region`).
        point: The click point (row refinement); None keeps the whole band.
        min_confidence: Minimum OCR confidence for a line to count.
        reference_date: The recording date, enabling the near/far date
            split; when None every date-bearing line is dropped (heal-time
            re-extraction has no recording date).

    Returns:
        The joined band text, or None when the surviving text is too short
        (< ``MIN_CONTEXT_CHARS`` squashed chars) to discriminate anything.
    """
    kept = _kept_context_lines(
        lines,
        exclude_region=exclude_region,
        band=band,
        point=point,
        min_confidence=min_confidence,
        reference_date=reference_date,
    )
    if not kept:
        return None
    kept.sort(key=lambda item: item[0])
    joined = " ".join(text for _, _, text in kept)
    if len(squash(joined)) < MIN_CONTEXT_CHARS:
        return None
    return joined


def _kept_context_lines(
    lines: Iterable[Any],
    *,
    exclude_region: Region,
    band: Region,
    point: Optional[Point],
    min_confidence: float,
    reference_date: Optional[date],
) -> list[tuple[int, Any, str]]:
    """The confident, in-band, non-excluded, non-volatile identity lines.

    Shared by :func:`context_from_lines` (which joins the text) and
    :func:`context_region_from_lines` (which bounds them), so the identity
    band filter has a single definition. Each item is ``(left_x, line, text)``.
    """
    _, band_y, _, band_h = band
    kept: list[tuple[int, Any, str]] = []
    for line in lines:
        text = (getattr(line, "text", "") or "").strip()
        if not text or getattr(line, "confidence", 0.0) < min_confidence:
            continue
        lx, ly, lw, lh = line.region
        center_y = ly + lh // 2
        if not (band_y <= center_y < band_y + band_h):
            continue
        if _intersects(line.region, exclude_region):
            continue
        if is_volatile_line(text, reference_date=reference_date):
            continue
        kept.append((lx, line, text))
    if point is not None:
        near = lines_near_point([line for _, line, _ in kept], point[1])
        kept = [item for item in kept if item[1] in near]
    return kept


def context_region_from_lines(
    lines: Iterable[Any],
    *,
    exclude_region: Region,
    band: Region,
    point: Optional[Point] = None,
    min_confidence: float = 0.5,
    reference_date: Optional[date] = None,
) -> Optional[Region]:
    """Bounding box of the identity lines :func:`context_from_lines` keeps.

    Same filter, same arguments; returns the tight ``(x, y, w, h)`` box (in the
    recorded frame's coordinates) enclosing the surviving identity-band lines,
    or None when none survive. The compiler stores this as
    ``anchor.identifier_region`` on a PIXEL-ONLY substrate so the pixel-compare
    identity tier (:func:`verify_pixel_identity`) can re-cut the SAME box at the
    resolved point on replay. Unlike :func:`context_from_lines` it applies no
    minimum-length floor: any surviving identity pixels are worth arming the
    MISMATCH-only pixel tier with (it can never false-accept).
    """
    kept = _kept_context_lines(
        lines,
        exclude_region=exclude_region,
        band=band,
        point=point,
        min_confidence=min_confidence,
        reference_date=reference_date,
    )
    if not kept:
        return None
    x0 = min(line.region[0] for _, line, _ in kept)
    y0 = min(line.region[1] for _, line, _ in kept)
    x1 = max(line.region[0] + line.region[2] for _, line, _ in kept)
    y1 = max(line.region[1] + line.region[3] for _, line, _ in kept)
    return (int(x0), int(y0), int(x1 - x0), int(y1 - y0))


def required_run(length: int) -> int:
    """Contiguous-run requirement for a needle of ``length`` squashed chars.

    ``verify_note_saved``-style 16-char runs for long values, scaled down to
    ~80% of the needle for short ones (a 16-char run cannot exist inside a
    5-char name), never below 3.
    """
    return min(MAX_RUN_REQUIRED, max(3, -(-length * 4 // 5)))


def _matching_blocks(needle: str, hay: str) -> list:
    # autojunk=False: the default heuristic marks frequent characters of a
    # long OCR haystack as junk, silently collapsing real matches (same
    # pitfall benchmark.verify documents).
    return difflib.SequenceMatcher(
        None, needle, hay, autojunk=False
    ).get_matching_blocks()


def longest_run(needle: str, hay: str) -> int:
    """Longest contiguous common run between two squashed strings."""
    if not needle or not hay:
        return 0
    if needle in hay:
        return len(needle)
    return max((b.size for b in _matching_blocks(needle, hay)), default=0)


def coverage(needle: str, hay: str) -> float:
    """Fraction of ``needle`` covered by contiguous runs >= ``MIN_BLOCK``.

    Scattered 1-2 char coincidences accumulate on any text-dense screen, so
    only blocks of at least ``MIN_BLOCK`` characters count. Used for
    embedded-parameter detection; band verification uses the stricter
    :func:`band_match` (which also penalizes uncovered residue).
    """
    if not needle or not hay:
        return 0.0
    if needle in hay:
        return 1.0
    matched = sum(b.size for b in _matching_blocks(needle, hay) if b.size >= MIN_BLOCK)
    return matched / len(needle)


def tokenize(text: str) -> list[str]:
    """Split on whitespace and squash each token (lowercase, no spaces)."""
    return [squash(tok) for tok in text.split() if squash(tok)]


def ocr_canonical(token: str) -> str:
    """Canonical form under the OCR character-confusion classes.

    Two tokens are OCR-equivalent iff their canonical forms are equal —
    'paln' == 'pain' (l/i), '5ample' == 'sample' (5/s), 'cornpre' ==
    'compre' (rn/m) — while 'john' != 'joan' (a/o is not an OCR
    confusion) and 'phil' != 'philip' (extension is not noise).
    """
    t = token.lower()
    for a, b in _CONFUSION_MULTI:
        t = t.replace(a, b)
    return "".join(_CONFUSION_CANON.get(ch, ch) for ch in t)


def _alpha_dominated(token: str) -> bool:
    return sum(ch.isalpha() for ch in token) * 2 >= len(token)


def _name_plausible(token: str) -> bool:
    """Whether every character of a squashed token could appear in a real
    name (no digits, no confusion-class symbols): only such tokens can
    collide with a DIFFERENT real name under letter-letter confusions."""
    return not any(ch in _NON_NAME_CHARS for ch in token)


def _has_digit(token: str) -> bool:
    return any(ch.isdigit() for ch in token)


# Intra-identifier separators. Real MRNs / account refs are sometimes formatted
# with these (``MG-4408``, ``123-45-6789``); we strip them before judging an
# identifier so a separator does not smuggle a collapsible MRN past the
# glyph-abstain gate (the P0 separator-bypass reopening). DATES also carry these
# separators, so ``_is_identifier_shaped`` excludes date-shaped tokens FIRST.
_ID_SEP_RE = re.compile(r"[-/.]")

# A 3-segment date (``01/15/1980``, ``1980-01-15``, ``15.01.1980``), matched on
# the OCR-homoglyph-canonical form so an OCR'd DOB (``0l/l5/l980``) is still
# caught. Range-validated across the common segment orders so a genuinely
# date-SHAPED value is excluded but a non-date separator token (``123-45-6789``,
# ``MG-4408``) is NOT — it stays an identifier and remains glyph-gated.
_DATE_SEG_RE = re.compile(r"^(\d{1,4})[-/.](\d{1,2})[-/.](\d{1,4})$")


def _is_date_like(token: str) -> bool:
    """True iff ``token`` plausibly parses as a 3-segment calendar date.

    Dates are deliberately NOT treated as glyph-collapsible identifiers: a DOB
    sits in every patient band, so gating on it would abstain on every band
    (the DOB's identity role is chronology, handled elsewhere). Matched on the
    homoglyph-canonical form (O->0, l/I/|/!->1) so an OCR-degraded DOB is caught
    too, and range-validated so a non-date separator token (an MRN like
    ``MG-4408`` or a ``123-45-6789``) is not mistaken for a date and therefore
    stays glyph-gated."""
    canon = token
    for ch in "oO":
        canon = canon.replace(ch, "0")
    for ch in "lI|!":
        canon = canon.replace(ch, "1")
    m = _DATE_SEG_RE.match(canon)
    if m is None:
        return False
    a, b, c = (int(g) for g in m.groups())

    def _plausible(year: int, month: int, day: int) -> bool:
        return (
            1 <= month <= 12
            and 1 <= day <= 31
            and (1900 <= year <= 2100 or 0 <= year <= 99)
        )

    # Accept if it reads as a real date under any common segment order
    # (Y/M/D, M/D/Y, or D/M/Y). A separator token that fits none is an
    # identifier, not a date.
    return (
        _plausible(a, b, c)  # Y/M/D
        or _plausible(c, a, b)  # M/D/Y
        or _plausible(c, b, a)  # D/M/Y
    )


def _is_identifier_shaped(token: str) -> bool:
    """Whether a squashed token occupies an IDENTIFIER position (an MRN /
    account / chart ref) rather than a name, date, or column word.

    Conservative by construction (the 9th wrong-patient reopening). An
    identifier is a contiguous ALPHANUMERIC run of at least ``_ID_MIN_LEN``
    chars that CARRIES A DIGIT:

    - the alphanumeric-RUN requirement excludes dates/DOBs, which carry a `/`
      or `-` separator (``01/15/1980`` is not a bare run) — a date is judged as
      chronology/identity elsewhere, never as a collapsible identifier;
    - the DIGIT requirement is what distinguishes an identifier from a NAME: a
      real person's name carries no digit, so a purely-alphabetic run is a name
      or a low-entropy column word (``Active``), handled by the
      name/coverage/contradiction budgets and the letter-letter suspect rule —
      NOT by the glyph-collapse gate. A run WITH a digit is an identifier:
      purely numeric (``100512``), alphanumeric (``AC50061``), any casing.

    Separators are STRIPPED before the run test, so a hyphen/slash-formatted
    MRN (``MG-4408``, ``123-45-6789``) is still an identifier — the P0
    separator-bypass reopening, where ``token.isalnum()`` silently exempted any
    separator-bearing MRN from the glyph gate and a same-name/same-DOB homonym
    with a dashed collapsible MRN VERIFIED. DATES (which also carry separators)
    are excluded FIRST via :func:`_is_date_like`, so stripping separators cannot
    turn a DOB into a gated identifier and over-halt every band.

    This is deliberately over-inclusive on the identifier side: a bare numeric
    run that is really an un-separated date is treated AS an identifier (→ the
    glyph gate can force ABSTAIN), the SAFE over-halting direction. Only clearly
    non-identifier shapes (too short, date-shaped, purely alphabetic) are
    excluded."""
    if _is_date_like(token):
        return False
    core = _ID_SEP_RE.sub("", token)
    return len(core) >= _ID_MIN_LEN and core.isalnum() and _has_digit(core)


def _is_glyph_vulnerable_identifier(token: str) -> bool:
    """Whether a squashed token is an IDENTIFIER whose glyphs OCR cannot be
    trusted to have read glyph-for-glyph (see GLYPH_AMBIGUOUS_ID_CHARS_CAP).

    True iff the token is IDENTIFIER-SHAPED (:func:`_is_identifier_shaped`) AND
    carries at least one character in the O/0 or l/1/I near-homoglyph classes
    (:data:`_ID_HOMOGLYPH_CHARS`), on EITHER side — a letter O/l/I OR a digit
    0/1.

    The 9th wrong-patient reopening DROPPED the earlier `letter AND digit`
    (alphanumeric-mix) requirement. A real MRN can be PURELY NUMERIC, and a
    numeric MRN is exactly as glyph-collapsible as an alphanumeric one:
    ``100512`` (recorded) and a DIFFERENT patient's ``1OO512`` (letter O's) OCR
    to the byte-identical string ``100512``, so a matcher keyed on a
    letter+digit mix never flagged ``100512`` and the homonym VERIFIED. The
    rule is now structural and symmetric: ANY identifier-position token bearing
    a confusable glyph — numeric, alphanumeric, or lowercase — makes the OCR
    tier ABSTAIN. A name or a separator-bearing date is not identifier-shaped
    and is never flagged here; a clean identifier bearing NONE of {0,1,O,l,I}
    (e.g. ``RC79284``) is identifier-shaped but not glyph-vulnerable, so it
    still verifies."""
    return _is_identifier_shaped(token) and any(
        ch in _ID_HOMOGLYPH_CHARS for ch in token
    )


# Back-compat alias: #26 named this predicate for the letter-only class.
_is_glyph_ambiguous_identifier = _is_glyph_vulnerable_identifier


def _is_discriminative_name(token: str) -> bool:
    """Whether a squashed token is a discriminative name carrier: a
    name-plausible, alphabetic-dominated token of at least
    ID_CARRY_NAME_MIN chars (a real surname / first name) that is NOT a
    low-entropy record-list column word ('Active'/'Pending'/'Open'). Short
    alpha tokens and shared status/column words do not carry identity
    alone."""
    return (
        len(token) >= ID_CARRY_NAME_MIN
        and _alpha_dominated(token)
        and _name_plausible(token)
        and ocr_canonical(token) not in _GENERIC_IDENTITY_CANON
    )


def _suspicious_pair(expected: str, observed: str) -> bool:
    """A canonical-equal, raw-unequal token pair whose match is a
    confusion-only match on a DISCRIMINATOR — indistinguishable at band
    level from a wrong-row read. Two qualifying shapes:

    - NAME collision (Neil/Nell): both sides name-plausible and long
      enough to be a name;
    - IDENTIFIER collision (A01234/AO1234): the RECORDED token contains a
      digit (an MRN/account/DOB), so a letter/digit confusion turned it
      into a DIFFERENT identifier. Scoped on the RECORDED token: a name
      OCR'd WITH a digit ('Belford' -> 'Be1ford') has an all-alpha
      recorded token and is NOT suspect (clean OCR noise), while an
      identifier is suspect regardless of the observed side.
    """
    if _has_digit(expected):
        # Recorded identifier matched only via confusion: a different ID.
        return True
    return (
        len(expected) >= MIN_BLOCK
        and _name_plausible(expected)
        and _name_plausible(observed)
    )


def _name_shaped(raw_token: str, squashed_token: str) -> bool:
    """Whether an observed band token looks like part of a person's name:
    leading uppercase in the RAW band text, alphabetic-dominated,
    name-plausible, at least MIN_BLOCK squashed chars. Used by the
    unexplained-observed-token budget — lowercase adjacent-row bleed
    (mid-procedure words) is deliberately not name-shaped."""
    return (
        len(squashed_token) >= MIN_BLOCK
        and raw_token[:1].isupper()
        and _alpha_dominated(squashed_token)
        and _name_plausible(squashed_token)
    )


_GEN_SUFFIX_CANON = frozenset(ocr_canonical(s) for s in GENERATIONAL_SUFFIXES)

# OCR-canonical forms of the generic record-list column words (see
# _GENERIC_IDENTITY_WORDS): compared against the canonical form of a band
# token so 'Act1ve'/'0pen'-style OCR noise still folds to the stop word.
_GENERIC_IDENTITY_CANON = frozenset(ocr_canonical(w) for w in _GENERIC_IDENTITY_WORDS)


def _is_generational_suffix(token: str) -> bool:
    """Whether a squashed token is a Jr/Sr/II/III/IV generational suffix,
    tolerating OCR confusions ('lI' for 'II', '5r' for 'Sr')."""
    return ocr_canonical(token) in _GEN_SUFFIX_CANON


class BandMatch(NamedTuple):
    """Result of matching a recorded band against a live band.

    Attributes:
        coverage: Fraction of recorded squashed characters in matched
            tokens.
        max_uncovered_run: Longest contiguous run of unmatched recorded
            characters (adjacent unmatched tokens merge) — absence
            evidence (OCR dropout or a replaced stretch).
        contradicted_chars: Total squashed characters of recorded tokens
            with a NEAR-MISS in the observed band — affirmative evidence
            of a different entity (sibling names, edited DOB/MRN fields,
            generational suffixes, replaced words, changed short tokens
            like a middle initial or the sex column).
        suspect_chars: Total squashed characters of recorded
            name-plausible tokens matched ONLY by letter-letter
            confusion equivalence — a Neil/Nell-class collision,
            indistinguishable from a misread of the true row (abort is
            the correct outcome for both readings).
        unexplained_name_tokens: Observed name-shaped tokens the
            recorded band cannot explain — appended names, merged rows,
            rows that mention the recorded patient.
        max_absent_alpha_token: Longest unmatched name-like alphabetic
            recorded token — absence of the identity token itself, worse
            than trailing-numerics dropout.
        glyph_ambiguous_id_chars: Squashed characters of MATCHED recorded
            glyph-vulnerable identifier tokens (an identifier-position
            token -- numeric or alphanumeric -- carrying an O/0 or l/1/I
            near-homoglyph; see
            GLYPH_AMBIGUOUS_ID_CHARS_CAP). Charged in FULL on EITHER side and
            REGARDLESS of a matched name/DOB (the 8th wrong-patient
            reopening): a RAW match here may be an OCR glyph-collapse of a
            DIFFERENT patient's identifier, and a matched name+DOB cannot
            rule out a same-name/same-DOB homonym whose distinguishing MRN
            glyph collapsed. A positive value makes verify_target_identity
            ABSTAIN (OCR cannot certify) when every OTHER budget passes.
    """

    coverage: float
    max_uncovered_run: int
    contradicted_chars: int
    suspect_chars: int = 0
    unexplained_name_tokens: int = 0
    max_absent_alpha_token: int = 0
    glyph_ambiguous_id_chars: int = 0


def _match_tokens(
    exp: list[str], obs: list[str]
) -> tuple[list[bool], list[bool], list[bool], list[bool], list[bool]]:
    """Mark matched recorded tokens and explained observed tokens.

    Order-insensitive at token granularity (OCR re-reads the same band in
    a different segmentation order between visits), with explicit
    merge/split handling — splits and joins preserve LOCAL adjacency even
    when segments permute:

    - single: an observed token is OCR-equivalent to the recorded token;
    - split: consecutive recorded tokens concatenate (OCR-equivalently)
      to one observed token (recorded 'Show' 'Active' vs observed
      'ShowActive');
    - join: consecutive observed tokens concatenate to one recorded
      token (recorded 'ShowActive' vs observed 'Show Active').

    OCR-equivalence is canonical-form equality (:func:`ocr_canonical`):
    there is deliberately NO similarity-ratio or partial-containment
    acceptance — those tiers verified near-name siblings (Phil/Philip,
    John/Joan), the third wrong-patient reopening.

    For each matched recorded token the match QUALITY is tracked too:
    ``raw_matched`` marks tokens with a raw-equal (squashed-identical)
    counterpart, and ``suspect_evidence`` marks tokens whose only
    evidence is a letter-letter confusion equivalence against a
    name-plausible observed counterpart (the Neil/Nell collision class —
    see :data:`SUSPECT_CHARS_CAP`).

    A further quality flag, ``glyph_ambiguous_id``, marks glyph-vulnerable
    identifier tokens (an identifier-position token -- numeric or
    alphanumeric -- carrying an O/0 or l/1/I char, on
    either side) that matched RAW — the raw equality may be an OCR
    glyph-collapse of a DIFFERENT identifier. Whether such a token halts
    depends on the name+DOB-primary gate in :func:`band_match` (see
    :data:`GLYPH_AMBIGUOUS_ID_CHARS_CAP`).

    Returns:
        ``(matched, explained, raw_matched, suspect_evidence,
        glyph_ambiguous_id)``.
    """
    exp_c = [ocr_canonical(t) for t in exp]
    obs_c = [ocr_canonical(t) for t in obs]
    matched = [False] * len(exp)
    explained = [False] * len(obs)
    raw_matched = [False] * len(exp)
    suspect_evidence = [False] * len(exp)
    glyph_ambiguous_id = [False] * len(exp)

    def mark(i: int, expected_raw: str, observed_raw: str) -> None:
        matched[i] = True
        if expected_raw == observed_raw:
            raw_matched[i] = True
        elif _suspicious_pair(expected_raw, observed_raw):
            suspect_evidence[i] = True

    # single-token equivalence (mark every equivalent observed copy)
    for i, ec in enumerate(exp_c):
        for j, oc in enumerate(obs_c):
            if ec == oc:
                mark(i, exp[i], obs[j])
                explained[j] = True

    # split: consecutive recorded tokens -> one observed token
    for i in range(len(exp)):
        for size in (2, 3, 4):
            if i + size > len(exp):
                break
            if all(matched[i : i + size]):
                continue
            concat_raw = "".join(exp[i : i + size])
            concat_c = ocr_canonical(concat_raw)
            if len(concat_c) < MIN_BLOCK:
                continue
            for j, oc in enumerate(obs_c):
                if oc == concat_c:
                    rawok = concat_raw == obs[j]
                    # SPLIT path: consecutive RECORDED tokens OCR-glued into
                    # one observed token. The glyph-vulnerable-identifier flag
                    # is NOT set on the whole concatenation (a name adjacent to
                    # a numeric field, 'Evelyn'+'A743380', would look like one
                    # letter+digit+homoglyph identifier). Instead each recorded
                    # FRAGMENT is flagged individually in the unified post-pass
                    # below, on its raw-matched status — so a confusable-glyph
                    # NUMERIC/alnum fragment ('0061') of a split identifier
                    # triggers ABSTAIN (the 9th reopening's split case) while
                    # the adjacent pure-alpha name fragment does not. No latent
                    # split hole remains.
                    for m in range(i, i + size):
                        matched[m] = True
                        if rawok:
                            raw_matched[m] = True
                        elif _suspicious_pair(concat_raw, obs[j]):
                            suspect_evidence[m] = True
                    explained[j] = True

    # join: one recorded token -> consecutive observed tokens
    for i, token in enumerate(exp):
        if matched[i] or len(exp_c[i]) < MIN_BLOCK:
            continue
        for j in range(len(obs)):
            for size in (2, 3, 4):
                if j + size > len(obs):
                    break
                concat_raw = "".join(obs[j : j + size])
                if ocr_canonical(concat_raw) == exp_c[i]:
                    mark(i, token, concat_raw)
                    for m in range(j, j + size):
                        explained[m] = True
                    break
            if matched[i]:
                break

    # Unified glyph-vulnerable-identifier flag (the 9th wrong-patient
    # reopening): a property of the RECORDED token, charged on ANY match path
    # (single / split / join). A recorded identifier-position token that
    # matched RAW (byte-identically after squashing) AND carries a confusable
    # O/0 or l/1/I glyph is flagged — the raw equality may be an OCR
    # glyph-collapse of a DIFFERENT patient's identifier. Keying it here, on
    # ``raw_matched`` alone, closes the split hole: a fragment of an
    # OCR-split identifier ('0061') is a recorded token in its own right, so
    # it is flagged exactly like an unsplit one. A CONFUSION-only match
    # (raw-unequal) is NOT flagged here — the strings differ, which is
    # affirmative different-identifier evidence handled by the suspect rule
    # (→ mismatch), not the abstain gate.
    for i in range(len(exp)):
        if raw_matched[i] and _is_glyph_vulnerable_identifier(exp[i]):
            glyph_ambiguous_id[i] = True
    return (
        matched,
        explained,
        raw_matched,
        suspect_evidence,
        glyph_ambiguous_id,
    )


def _contradicted(
    exp: list[str],
    obs: list[str],
    matched: list[bool],
    explained: list[bool],
    *,
    contradiction_sim: float,
) -> list[bool]:
    """Mark recorded tokens whose absence is a NEAR-MISS, not dropout.

    A wrong sibling row does not merely lack the recorded name — it shows
    a near-name in its place. Five contradiction shapes (rationale on the
    constants above):

    - near-miss similarity: an observed >=3-char token is canonically
      similar at >= ``contradiction_sim``;
    - semantic extension: one token canonically contains the other with
      alphabetic residue ('Phil' in 'Phillipa');
    - replacement: the recorded token is alphabetic and some UNexplained
      alphabetic observed token exists ('Amy' -> 'Kim', similarity 0);
    - short-token replacement (2026-07-10 review, Blocker 2): a 1-2 char
      alphabetic recorded token, unmatched, with an unexplained observed
      alphabetic token of the SAME length that is not
      confusion-equivalent — a changed middle initial ('J' -> 'K'), the
      SEX column ('M' -> 'F'), a 2-char name ('Al' -> 'Bo'). These sat
      below MIN_BLOCK and were invisible to every rule;
    - generational suffix on either side, unmatched.
    """
    exp_c = [ocr_canonical(t) for t in exp]
    obs_c = [ocr_canonical(t) for t in obs]
    contradicted = [False] * len(exp)
    unexplained_alpha = any(
        not explained[j] and len(o) >= MIN_BLOCK and _alpha_dominated(o)
        for j, o in enumerate(obs)
    )
    obs_suffix_unexplained = any(
        not explained[j] and _is_generational_suffix(o) for j, o in enumerate(obs)
    )
    for i, token in enumerate(exp):
        if matched[i]:
            continue
        if _is_generational_suffix(token):
            contradicted[i] = True
            continue
        if len(token) < MIN_BLOCK:
            # Short-token replacement (Blocker 2) is handled with
            # COUNT-based accounting in band_match: a replaced initial
            # that happens to duplicate another band token (a middle
            # initial 'F' colliding with the sex column's 'F') would look
            # "explained" here per-pair while the multiset clearly shows
            # one copy missing and a foreign copy standing in its place.
            continue
        if unexplained_alpha and _alpha_dominated(token):
            contradicted[i] = True
            continue
        ec = exp_c[i]
        for j, oc in enumerate(obs_c):
            if len(obs[j]) < MIN_BLOCK or oc == ec:
                continue
            shorter, longer = sorted((ec, oc), key=len)
            if (
                len(shorter) >= MIN_BLOCK
                and shorter in longer
                and any(ch.isalpha() for ch in longer.replace(shorter, "", 1))
            ):
                contradicted[i] = True
                break
            ratio = difflib.SequenceMatcher(None, ec, oc, autojunk=False).ratio()
            if ratio >= contradiction_sim:
                contradicted[i] = True
                break
    if obs_suffix_unexplained:
        # A generational suffix the recorded band does not have: the
        # observed row is a different generation of the same name. Charge
        # it to the nearest recorded name evidence: mark ALL unmatched
        # recorded tokens contradicted, and if everything matched, the
        # caller's suffix flag below still forces the contradiction.
        for i in range(len(exp)):
            if not matched[i]:
                contradicted[i] = True
    return contradicted


def band_match(
    expected_text: str,
    observed_text: str,
    *,
    contradiction_sim: float = CONTRADICTION_SIM,
) -> BandMatch:
    """Match a recorded band against a live band, token-wise.

    Order-insensitive (see :func:`_match_tokens`) with two kinds of
    residue, tracked separately because they mean different things:

    - **uncovered runs** — walking the recorded tokens in order,
      contiguous runs of UNMATCHED tokens accumulate their squashed
      lengths: a wrong entity is a contiguous mismatch ("Jane Li"
      replaced by "Ann Wu" leaves a 6-char uncovered run) even when long
      shared row text keeps overall coverage high;
    - **contradicted chars** — recorded tokens with a NEAR-MISS in the
      observed band (see :func:`_contradicted`): 'Phil' vs 'Philip' is
      only a 4-char absence (within any workable run cap) but it is
      affirmative evidence of a sibling, so it is charged to a separate,
      much stricter budget.

    An observed generational suffix absent from the recorded band
    contradicts even when every recorded token matched ('Belford, Phil'
    vs 'Belford, Phil Jr'): the returned ``contradicted_chars`` is
    forced positive.

    Args:
        expected_text: The recorded (or parameter-substituted) band text.
        observed_text: The live band text.
        contradiction_sim: Near-miss similarity threshold (exposed for
            the ROC harness; production uses ``CONTRADICTION_SIM``).

    Returns:
        A :class:`BandMatch` (coverage, max_uncovered_run,
        contradicted_chars, suspect_chars, unexplained_name_tokens,
        max_absent_alpha_token, glyph_ambiguous_id_chars).
    """
    exp = tokenize(expected_text)
    if not exp:
        return BandMatch(0.0, 0, 0)
    obs_raw = [tok for tok in observed_text.split() if squash(tok)]
    obs = [squash(tok) for tok in obs_raw]
    exp_c = [ocr_canonical(t) for t in exp]
    obs_c_all = [ocr_canonical(t) for t in obs]
    (
        matched,
        explained,
        raw_matched,
        suspect_evidence,
        glyph_ambiguous_id,
    ) = _match_tokens(exp, obs)
    contradicted = _contradicted(
        exp, obs, matched, explained, contradiction_sim=contradiction_sim
    )

    matched_chars = 0
    total_chars = 0
    contradicted_chars = 0
    suspect_chars = 0
    # Squashed chars of RAW-matched glyph-vulnerable identifier tokens (an
    # identifier-position token, numeric or alphanumeric, carrying an O/0 or
    # l/1/I near-homoglyph;
    # see _is_glyph_vulnerable_identifier). ALWAYS charged, on either the
    # letter (O/l/I) or the digit (0/1) side -- the 8th wrong-patient
    # reopening: a matched name+DOB does NOT license a collapsible MRN,
    # because it cannot rule out a same-name/same-DOB HOMONYM whose
    # distinguishing MRN glyph OCR collapsed onto the recorded one. (#26/#27
    # let a matched name "carry" the identity and suppressed the digit-side
    # budget; a same-name/same-DOB homonym defeats that, so the suppression
    # is removed and the OCR tier ABSTAINS whenever a confusable identifier
    # is present -- verify_target_identity turns this budget into abstain.)
    glyph_id_chars = 0
    max_absent_alpha = 0
    uncovered_runs: list[int] = []
    current_run = 0
    for i, token in enumerate(exp):
        total_chars += len(token)
        if matched[i]:
            matched_chars += len(token)
            if not raw_matched[i] and suspect_evidence[i]:
                # Matched ONLY by letter-letter confusion equivalence:
                # the Neil/Nell collision class (Blocker 1).
                suspect_chars += len(token)
            if glyph_ambiguous_id[i]:
                # RAW-matched glyph-vulnerable identifier: the raw equality
                # may be an OCR glyph-collapse of a DIFFERENT identifier
                # (letter O/l/I <-> digit 0/1). Charged in FULL regardless of
                # which side the homoglyph sits, and regardless of a matched
                # name/DOB: a matched name+DOB cannot rule out a same-name/
                # same-DOB homonym whose distinguishing MRN glyph collapsed
                # (the 8th reopening). The OCR tier then ABSTAINS.
                glyph_id_chars += len(token)
            if current_run:
                uncovered_runs.append(current_run)
                current_run = 0
        else:
            current_run += len(token)
            if contradicted[i]:
                contradicted_chars += len(token)
            if _alpha_dominated(token) and _name_plausible(token):
                # Absent name-like token (Major 4): identity must not
                # verify when its identity token was never read.
                max_absent_alpha = max(max_absent_alpha, len(token))
    if current_run:
        uncovered_runs.append(current_run)
    if contradicted_chars == 0 and any(
        not explained[j] and _is_generational_suffix(o) for j, o in enumerate(obs)
    ):
        # Fully-matched band plus an unexplained observed Jr/Sr/II: the
        # generation differs even though every recorded token matched.
        contradicted_chars = max(
            (len(o) for o in obs if _is_generational_suffix(o)), default=2
        )
    # Short-token replacement (Blocker 2), multiset accounting: 'J' -> 'K'
    # middle initials, the SEX column 'M' -> 'F', 2-char names 'Al' ->
    # 'Bo'. Count-based on canonical forms, because a replaced initial
    # can DUPLICATE another band token ('Frank R ... F' -> 'Frank F ...
    # F' leaves both observed 'F's "explained" per-pair while the
    # multiset shows one 'R' missing and a foreign 'F' in its place).
    # Absence alone (OCR dropped a short token) stays tolerated: a
    # replacement needs a missing copy AND a same-length foreign copy.
    exp_short = Counter(
        exp_c[i] for i, t in enumerate(exp) if len(t) < MIN_BLOCK and t.isalpha()
    )
    obs_short = Counter(
        obs_c_all[j] for j, o in enumerate(obs) if len(o) < MIN_BLOCK and o.isalpha()
    )
    missing_short = exp_short - obs_short
    excess_short = obs_short - exp_short
    replaced = [a for a in missing_short for b in excess_short if len(a) == len(b)]
    if replaced:
        contradicted_chars += max(len(a) for a in replaced)
    # Observed-side superset (Blocker 3): name-shaped observed tokens the
    # recorded band cannot explain — appended names, merged second rows,
    # message/cc rows that merely MENTION the recorded patient.
    unexplained_names = sum(
        1
        for j, raw in enumerate(obs_raw)
        if not explained[j] and _name_shaped(raw, obs[j])
    )
    # 8th wrong-patient reopening: ANY raw-matched glyph-vulnerable identifier
    # charges the zero glyph budget, REGARDLESS of a matched name/DOB. The
    # #26/#27 "name+DOB carries, so a digit-body MRN only corroborates"
    # suppression was unsound: two DIFFERENT patients can share NAME and DOB
    # and differ only in an MRN glyph OCR collapses (AC50061 vs AC5OO61 ->
    # byte-identical OCR), so a matched name+DOB CANNOT rule out the homonym.
    # verify_target_identity reads a positive charge here as ABSTAIN (OCR
    # cannot certify), not verify and not mismatch.
    glyph_ambiguous_id_chars = glyph_id_chars
    return BandMatch(
        matched_chars / total_chars,
        max(uncovered_runs, default=0),
        contradicted_chars,
        suspect_chars,
        unexplained_names,
        max_absent_alpha,
        glyph_ambiguous_id_chars,
    )


def _token_belongs_to(token: str, value_squashed: str) -> bool:
    """Whether a band token is (part of) a parameter's demonstrated value."""
    if not token or not value_squashed:
        return False
    if token in value_squashed or value_squashed in token:
        return True
    ratio = difflib.SequenceMatcher(None, token, value_squashed, autojunk=False).ratio()
    # Same-token-with-OCR-jitter question ("Phi1" ~ "Phil"): the token
    # similarity tier's threshold applies.
    return ratio >= TOKEN_SIM_RATIO


def substitute_param(band_text: str, example: str, value: str) -> str:
    """Replace a parameter's demonstrated value in the band with the run's.

    Verbatim occurrences are replaced in place (case-insensitive). When
    the example does not appear verbatim (OCR mangled it into the band),
    tokens belonging to the example are dropped and the run's value is
    appended — band matching is order-insensitive, so the position of the
    substituted value does not matter.
    """
    if not example.strip():
        return band_text
    pattern = re.compile(re.escape(example), re.IGNORECASE)
    if pattern.search(band_text):
        return pattern.sub(value, band_text)
    example_squashed = squash(example)
    kept = [
        tok
        for tok in band_text.split()
        if not _token_belongs_to(squash(tok), example_squashed)
    ]
    return " ".join(kept + [value])


def embedded_params(context_text: str, param_examples: dict[str, str]) -> list[str]:
    """Names of parameters whose demonstrated value is embedded in the band.

    A parameter's example value counts as embedded when its squashed form is
    covered >= ``COVERAGE_THRESHOLD`` inside the squashed context (OCR may
    mangle the odd character of the recorded band).
    """
    ctx = squash(context_text)
    names = []
    for name, example in param_examples.items():
        ex = squash(example or "")
        if len(ex) < MIN_PARAM_CHARS:
            continue
        if coverage(ex, ctx) >= COVERAGE_THRESHOLD:
            names.append(name)
    return names


def _band_ok_sans_glyph(match: BandMatch) -> bool:
    """Every pinned budget EXCEPT the glyph-confusable-identifier one
    (docs/validation/IDENTITY_EVIDENCE.md): coverage, uncovered-run,
    contradiction, suspect (letter-letter collision), unexplained-name and
    absent-name. These are the AFFIRMATIVE different-entity signals; a band
    that fails any of them is a real ``mismatch``. The glyph budget is
    handled separately (see :func:`band_verdict`) because a collapsible
    identifier is an ABSTAIN (OCR cannot tell), not affirmative evidence."""
    return (
        match.coverage >= COVERAGE_THRESHOLD
        and match.max_uncovered_run <= UNCOVERED_RUN_CAP
        and match.contradicted_chars <= CONTRADICTED_CHARS_CAP
        and match.suspect_chars <= SUSPECT_CHARS_CAP
        and match.unexplained_name_tokens <= UNEXPLAINED_NAME_TOKENS_CAP
        and match.max_absent_alpha_token <= ABSENT_NAME_TOKEN_CAP
    )


def _band_ok(match: BandMatch) -> bool:
    """The pinned operating point: all of :func:`_band_ok_sans_glyph` AND the
    glyph-ambiguous-identifier budget. A fully-clean band (verifies)."""
    return (
        _band_ok_sans_glyph(match)
        and match.glyph_ambiguous_id_chars <= GLYPH_AMBIGUOUS_ID_CHARS_CAP
    )


def band_verdict(match: BandMatch) -> Literal["verified", "mismatch", "abstain"]:
    """Three-way OCR-tier verdict for a matched band: ``verified`` /
    ``mismatch`` / ``abstain`` (the 8th wrong-patient reopening).

    - ``mismatch`` -- an AFFIRMATIVE different-entity budget failed (wrong
      name, edited DOB, replaced token, sibling superset, ...): the band is
      readable and provably a different entity.
    - ``abstain`` -- every affirmative budget passes (name+DOB match) BUT the
      band rests on a glyph-confusable identifier OCR may have collapsed
      (glyph budget exceeded). OCR cannot honestly certify SAME (a same-name/
      same-DOB homonym with a one-glyph-different MRN is indistinguishable
      after collapse) NOR assert DIFFERENT (it may well be the recorded
      patient). It DEFERS; the ladder falls through to any higher-fidelity
      tier and otherwise HALTs.
    - ``verified`` -- every budget passes and no confusable identifier: a
      clean name+DOB (optionally corroborated by a non-confusable identifier)
      genuinely discriminates.
    """
    if not _band_ok_sans_glyph(match):
        return "mismatch"
    if match.glyph_ambiguous_id_chars > GLYPH_AMBIGUOUS_ID_CHARS_CAP:
        return "abstain"
    return "verified"


def verify_target_identity(
    context_text: str,
    observed_text: str,
    *,
    params: Optional[dict[str, str]] = None,
    param_examples: Optional[dict[str, str]] = None,
) -> IdentityCheck:
    """Judge whether the live band matches the recorded target's identity.

    Args:
        context_text: The anchor's recorded context band text.
        observed_text: Live band text (joined OCR lines) around the
            RESOLVED click point.
        params: Effective run parameter values (run values merged over the
            recorded defaults).
        param_examples: The workflow's recorded example values per param.

    Returns:
        An :class:`~openadapt_flow.ir.IdentityCheck`:

        - ``verified`` — the target's identity evidence matched AND it does
          not rest on a glyph-confusable identifier.
        - ``mismatch`` — the band is readable and AFFIRMATIVELY a different
          entity: the resolver found something at a plausible position that
          is not the recorded target (or, in param mode, not the run's
          entity).
        - ``abstain`` — the band's name+DOB match but it rests on a
          glyph-confusable identifier (an MRN/account token with an O/0 or
          l/1/I) OCR may have collapsed: a same-name/same-DOB homonym whose
          distinguishing glyph collapsed cannot be ruled out, so OCR can
          neither certify SAME nor assert DIFFERENT (the 8th wrong-patient
          reopening). The OCR tier DEFERS; on a pixel-only substrate with no
          pixel-crop or VLM tier the ladder then HALTs.
        - ``unreadable`` — OCR produced no usable text in the live band;
          identity cannot be judged either way.
    """
    params = params or {}
    param_examples = param_examples or {}
    hay = squash(observed_text)
    expected = context_text

    if len(squash(context_text)) < MIN_CONTEXT_CHARS:
        # A band this short ("Active High 3") is generic: any sibling row
        # sharing the generic columns would verify. The compiler no longer
        # stores such bands; when one arrives via an older bundle, identity
        # cannot be judged — proceed flagged, never verified.
        return IdentityCheck(
            status="unreadable", expected=expected, observed=observed_text
        )

    in_band = embedded_params(context_text, param_examples)
    if in_band:
        # Param mode: the demonstrated band embeds a parameter's demo value,
        # so that PART of the band describes the demo's entity — substitute
        # the run's value into the recorded band and verify the WHOLE
        # substituted band. The recorded non-param residue is never
        # discarded: a band that merely contains the run's value somewhere
        # (any row mentioning "Susan") must not verify.
        if not hay:
            return IdentityCheck(status="unreadable", mode="param", expected=expected)
        substituted = context_text
        for name in in_band:
            substituted = substitute_param(
                substituted,
                param_examples[name],
                params.get(name, param_examples[name]),
            )
        for name in in_band:
            value = squash(params.get(name, param_examples[name]))
            run = longest_run(value, hay)
            need = required_run(len(value))
            if run < need:
                return IdentityCheck(
                    status="mismatch",
                    mode="param",
                    coverage=(run / need) if need else 0.0,
                    expected=params.get(name, param_examples[name]),
                    observed=observed_text,
                    param=name,
                )
        match = band_match(substituted, observed_text)
        return IdentityCheck(
            status=band_verdict(match),
            mode="param",
            coverage=round(match.coverage, 4),
            expected=substituted,
            observed=observed_text,
            param=in_band[0],
        )

    if not hay:
        return IdentityCheck(status="unreadable", expected=expected)
    match = band_match(context_text, observed_text)
    return IdentityCheck(
        status=band_verdict(match),
        coverage=round(match.coverage, 4),
        expected=expected,
        observed=observed_text,
    )


# ---------------------------------------------------------------------------
# Structured-text identity tier + the extensible identity ladder
# ---------------------------------------------------------------------------
#
# The OCR context band (everything above) is the identity signal for
# pure-PIXEL substrates. It cannot be the WHOLE story: an adversarial review
# proved the OCR-only path cannot close the same-name/same-DOB glyph-collapse
# case. Two DIFFERENT patients whose MRN differs only by an O/0 or l/1 glyph
# (MG4408 vs MG44O8) render to a BYTE-IDENTICAL OCR band -- literally the same
# string a legit re-read of the true row produces -- so no function downstream
# of OCR can separate them (same input, no distinguishing output). This is an
# impossibility result for OCR-based identity, not a tuning gap.
#
# The escape is to stop trusting OCR for identity where a higher-fidelity
# signal exists. When the backend exposes STRUCTURED text
# (openadapt_flow.backend.IdentityBackend.structured_text_at -- the DOM on a
# browser, the UIA/AX tree on native desktop) the recorded target's structured
# identity string and the live structured string at the resolved point are
# compared DIRECTLY: an exact/normalized compare in which O and 0 are distinct
# characters. The glyph-collapse cannot occur -- the two rows are different
# strings in the DOM/a11y tree -- so the class closes with NO OCR ambiguity and
# NO availability cost (identity no longer depends on OCR reading the MRN
# glyph-for-glyph).
#
# Identity is therefore an EXTENSIBLE LADDER of verifier tiers, each returning
# an IdentityCheck (verified / mismatch) or None (this tier is UNAVAILABLE for
# this substrate -- fall through to the next):
#
#   tier 1  structured text (DOM / UIA / AX)   -- verify_structured_identity
#   tier 2  pixel-compare identifier crop      -- verify_pixel_identity
#           -- for pure-pixel substrates (Citrix/RDP/VDI, broken a11y) that
#           expose NO structured text. OCR collapses O/0 and l/1, but the
#           PIXELS do not -- a different patient's MRN renders to different
#           pixels even when OCR reads them identically -- so a localized
#           pixel comparison of the recorded vs live identifier crop catches
#           the glyph-collapse where there is no DOM/a11y text. VERIFIES on a
#           matching render, MISMATCHES on a localized glyph change, ABSTAINS
#           under drift (validated: benchmark/pixel_identity).
#   tier 3  local-VLM veto (OPTIONAL)           -- verify_vlm_identity
#           -- injected, OFF by default. Only when identity rests on a
#           glyph-confusable identifier AND the cheaper tiers abstained (drift
#           the pixel tier can't judge): a local open VLM answers same/
#           different, VETO-ONLY (different/unsure -> halt; never grants a
#           pass, never overrides an earlier mismatch). Validated:
#           benchmark/vlm_identity.
#   tier N  OCR name+DOB-primary band (#27)    -- the pixel-substrate fallback,
#           with its proven-irreducible same-name/same-DOB residual that HALTS
#           on the sole-ambiguous-identifier case (docs/LIMITS.md).
#
# A higher tier's verdict is FINAL: a lower tier must never OVERRIDE it (that
# would re-admit the very ambiguity the higher tier removed). Every tier is
# FAIL-SAFE: unsure -> abstain to the next; if all abstain -> HALT. No tier
# can turn a wrong patient into a verified one.


def normalize_structured(text: str) -> str:
    """Normalize structured (DOM / a11y) identity text for exact compare.

    Collapses runs of whitespace to single spaces and casefolds, and NOTHING
    ELSE -- in particular it does NOT apply the OCR confusion canonicalization
    (:func:`ocr_canonical`): the whole point of the structured tier is that O
    and 0, l and 1 are DISTINCT characters here (the DOM/a11y layer read the
    real glyph), so folding them would throw away exactly the signal that
    closes the glyph-collapse class.
    """
    return " ".join((text or "").split()).casefold()


def structured_identity_match(recorded: str, live: str) -> bool:
    """Whether two structured identity strings are the same entity.

    Exact compare after :func:`normalize_structured` (whitespace/case only).
    No OCR tolerance: a one-glyph MRN difference is a real different patient in
    the DOM/a11y tree and MUST NOT match.
    """
    return normalize_structured(recorded) == normalize_structured(live)


def verify_structured_identity(
    recorded: Optional[str], live: Optional[str]
) -> Optional[IdentityCheck]:
    """Structured-text identity tier (tier 1 of the ladder).

    Args:
        recorded: The anchor's recorded structured identity text
            (``Anchor.structured_identity``), or None when the recording
            backend did not provide it.
        live: The live structured text at the RESOLVED point
            (``backend.structured_text_at(point)``), or None when the live
            backend is pixel-only / has no a11y node there.

    Returns:
        None when the tier is UNAVAILABLE -- structured text is missing on
        EITHER side, so this substrate cannot use it and the ladder must fall
        through to the next tier. Otherwise a definitive
        :class:`~openadapt_flow.ir.IdentityCheck` with ``mode="structured"``:
        ``verified`` on an exact/normalized match, ``mismatch`` otherwise. A
        mismatch here is authoritative -- the OCR fallback never overrides it.
    """
    if not recorded or not live:
        return None
    ok = structured_identity_match(recorded, live)
    return IdentityCheck(
        status="verified" if ok else "mismatch",
        mode="structured",
        coverage=1.0 if ok else 0.0,
        expected=recorded,
        observed=live,
    )


def run_identity_ladder(
    tiers: Iterable[Any],
) -> IdentityCheck:
    """Run identity verifier tiers in order; the first definitive verdict wins.

    Args:
        tiers: An ordered iterable of zero-argument callables, each returning
            an :class:`~openadapt_flow.ir.IdentityCheck` (a definitive
            verified/mismatch/unreadable verdict for its tier) or None (the
            tier is UNAVAILABLE for this substrate -- try the next). Ordered
            highest-fidelity first (structured text, then -- future -- a
            pixel/perceptual tier, then the OCR fallback).

    Returns:
        The first non-None tier verdict. A higher tier's verdict is FINAL: a
        structured-text mismatch is never reconsidered by a lower (OCR) tier.
        If every tier is unavailable, an ``unreadable`` check (identity could
        not be judged -- the caller applies its proceed-flagged / irreversible
        policy).
    """
    for tier in tiers:
        verdict = tier()
        if verdict is not None:
            return verdict
    return IdentityCheck(status="unreadable")


# ---------------------------------------------------------------------------
# Pixel-compare identity tier (tier 2) + optional local-VLM veto (tier 3)
# ---------------------------------------------------------------------------
#
# These two tiers close the ladder's SEAM between structured text and OCR for
# PURE-PIXEL substrates (Citrix/RDP/VDI, broken a11y) that expose no
# DOM/a11y string. Both were validated as standalone probes before promotion:
#
#   tier 2  PIXEL COMPARE   (benchmark/pixel_identity[_aligned]) -- the rendered
#           pixels retain the O/0 and l/1 distinction OCR collapses, so a
#           sub-pixel-ALIGNED localized distance of the recorded vs live
#           identifier crop separates the glyph-collapse wrong-patient pairs. A
#           global translation register (dominated by the matching glyphs)
#           removes the cross-render JITTER that used to defeat a same/different
#           threshold, leaving a genuine one-glyph change as a surviving
#           localized spike. It is HALT-BIASED: MISMATCHES only when the
#           difference is a UNIQUELY localized spike on an otherwise matching
#           render (a single differing glyph), ABSTAINS the moment a WHOLE-crop
#           change signals drift (dark theme / heavy scale) OR the render sits
#           in the uncertain band, and VERIFIES only when the whole crop matches
#           after alignment (worst aligned window inside the clean gap between
#           same-record ~0.052 and any different-record ~0.070). VERIFY is
#           CONFIG-GATED (``PIXEL_VERIFY_ENABLED``, default OFF pending a real
#           remote-display battery); MISMATCH/ABSTAIN are always live. It can
#           never false-accept: MATCH requires a whole-crop distance no
#           different identifier produces on the battery. Free, no model.
#
#   tier 3  VLM VETO         (benchmark/vlm_identity, PR #28) -- a LOCAL open
#           VLM (Qwen3-VL-4B via MLX, ~0.8s/call, ZERO API calls) asked
#           "same identifier or different?". VETO-ONLY: it can only REJECT
#           (different/unsure -> halt), never grant a pass a cheaper tier
#           refused, and it never overrides an earlier tier's mismatch (the
#           ladder order guarantees it runs only after the cheaper tiers
#           ABSTAINED). OPTIONAL and config-gated like the grounder: injected
#           via an ``IdentityVLM`` verifier, ``None`` by default, so the
#           default install needs no model. On the digit-flanked O/0 collapse
#           surface it scored 0% false-accept + 100% detection and 0%
#           over-halt under theme drift (where pixel-compare breaks); it
#           over-halts (safely) on zoom/font.
#
# When both pixel and VLM abstain, the ladder falls to the OCR name+DOB tier
# (#27) and then HALT -- the disclosed residual. No tier can ever turn a
# wrong patient into a verified one; the worst any drift can force is a HALT.

# Legacy canonical size, kept because tests build crops at this size so the
# aspect-preserving canonicalizer is an identity resize on them.
PIXEL_CANON = (48, 240)  # (H, W)

# --- Jitter-robust pixel identity metric ----------------------------------
# The scale-invariant localized spike (max window mean-abs-diff MINUS the
# per-window median floor) MISMATCHES a one-glyph-different MRN at any crop
# scale (a localized spike survives), and ABSTAINS under whole-crop drift (a
# high floor). But an UN-aligned spike could not safely VERIFY: sub-pixel
# cross-render JITTER of the SAME value shifts every anti-aliased glyph edge,
# spiking one window LARGER (~0.1) than a genuine one-glyph change (~0.038). No
# fixed threshold on the un-aligned spike separates "same value, jittered" from
# "one glyph different", so VERIFY was hard-gated (docs/LIMITS.md).
#
# The fix is a GLOBAL sub-pixel REGISTRATION (:func:`_align_translation`) before
# comparison. Both crops are canonicalized to a fixed HEIGHT (aspect preserved,
# so a glyph keeps a consistent width at any crop scale), then the live crop is
# translation-aligned onto the recorded crop by ECC (sub-pixel, whole-crop, so
# the many matching glyphs dominate the estimate). After alignment:
#   - SAME value + jitter  -> the translational jitter is corrected -> the WORST
#     aligned window mean-abs-diff collapses toward 0 (a matching render).
#   - ONE glyph different   -> the matching glyphs pin the global shift, so the
#     single differing glyph SURVIVES as a localized spike a rigid translation
#     cannot remove (it would have to warp locally).
# The alignment is capped at PIXEL_ALIGN_MAX_SHIFT_FRAC * height so it corrects
# jitter but can never slide one identifier onto another.
#
# On the adversarial battery (benchmark/pixel_identity_aligned: cv2-rendered
# MRNs + the committed real-render crops, under sub-pixel jitter, JPEG q<=10,
# 105-150% DPI, and theme inversion), the WORST aligned window (spike + floor)
# CLEANLY SEPARATES: same-record matching renders top out at ~0.052 while EVERY
# different-record (glyph-collapse siblings AND wrong MRNs) stays >= ~0.070 --
# a real gap, not a knife edge. VERIFY sits inside that gap.
PIXEL_SI_HEIGHT = 48  # canonical HEIGHT (aspect preserved) for every crop
PIXEL_WIN_FRAC = 0.55  # sliding window width as a fraction of height (~1 glyph)
PIXEL_ALIGN_MAX_SHIFT_FRAC = 0.35  # reject an alignment shift beyond this*height
PIXEL_DRIFT_FLOOR = 0.10  # per-window median >= this => whole-crop drift => abstain
PIXEL_MISMATCH_SPIKE = 0.030  # localized spike above the floor => a glyph change
PIXEL_MISMATCH_UNIQUENESS = 1.25  # top spike must exceed the 2nd (non-overlapping)
#                                   window spike by this ratio (a real glyph
#                                   change is uniquely localized; broadband JPEG
#                                   noise is not) -- keeps compression from
#                                   forcing a false MISMATCH
PIXEL_VERIFY_MAX_WINDOW = 0.040  # VERIFY: worst aligned window mean-abs-diff <= this
#                                  (0.040 sits between same_max ~0.052 and
#                                  diff_min ~0.070 with margin; abstains above)

# VERIFY default. The verifier can now MATCH safely on the SYNTHETIC cross-render
# jitter battery with zero false-accept and a real margin, but the DEFAULT stays
# OFF: there is no captured REAL RDP/Citrix/HDX identifier battery yet (that
# substrate is Research/Exploratory, STATUS.md), and a pixel false-accept is a
# SILENT WRONG RECORD -- the worst possible outcome. So VERIFY is config-gated
# with a safe default: MISMATCH and ABSTAIN are always live (they only ever add
# a HALT, never a pass); the positive MATCH path is reached only when a caller
# opts in (``verify_pixel_identity(..., enable_verify=True)``) or this default is
# flipped. The exact bar to flip the default is in docs/LIMITS.md: reproduce the
# benchmark/pixel_identity_aligned zero-false-accept battery on a REAL captured
# remote-display corpus. Enabling MATCH can never introduce a false-accept the
# battery does not already exercise; it only lets the tier certify a correct
# record instead of deferring to (a possibly over-halting) OCR.
PIXEL_VERIFY_ENABLED = False


class PixelIdentityDistance(NamedTuple):
    """Aligned localized distance between two identifier crops.

    Attributes:
        max_window: worst (largest) sliding-window mean-abs-diff after global
            sub-pixel alignment, in ``[0, 1]`` -- the whole-crop match statistic
            the VERIFY gate reads (low iff EVERY glyph matches).
        floor: the per-window MEDIAN mean-abs-diff -- the whole-crop drift
            detector (high under a theme/heavy-scale wash where no local
            decision is trustworthy).
        spike: ``max_window - floor`` -- a single glyph-scale change spikes one
            window above the floor; the MISMATCH statistic.
        uniqueness: ``spike`` over the next-tallest NON-overlapping window's
            spike -- a genuine one-glyph change is uniquely localized (>1),
            broadband compression noise is not (~1).
        aligned: whether ECC sub-pixel registration converged within the
            plausible-jitter shift cap (VERIFY requires it; MISMATCH does not,
            since a localized spike is only more visible without alignment).
    """

    max_window: float
    floor: float
    spike: float
    uniqueness: float
    aligned: bool


def _pixel_canon_aspect(png: bytes) -> Optional[Any]:
    """Decode PNG to grayscale canonicalized to a FIXED HEIGHT, aspect
    PRESERVED (so a glyph keeps a consistent width at any crop scale). Returns
    None on undecodable bytes."""
    import cv2
    import numpy as np

    img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    h, w = gray.shape[:2]
    nw = max(8, int(round(w * PIXEL_SI_HEIGHT / max(1, h))))
    return cv2.resize(gray, (nw, PIXEL_SI_HEIGHT), interpolation=cv2.INTER_AREA)


def _align_translation(recorded_gray: Any, live_gray: Any) -> tuple[Any, bool]:
    """Sub-pixel translation-register ``live_gray`` onto ``recorded_gray``.

    Estimates a WHOLE-crop translation by ECC (so the many matching glyphs
    dominate; a single differing glyph cannot swing it) and warps the live crop
    by it. Returns ``(aligned_live, ok)``; ``ok`` is False -- and the live crop
    is returned UNCHANGED -- when ECC fails to converge (e.g. a low-texture
    crop) or the estimated shift exceeds ``PIXEL_ALIGN_MAX_SHIFT_FRAC * height``
    (an implausible jitter that would risk sliding one identifier onto another).
    A False here disqualifies VERIFY (never a MATCH on an un-registered pair)
    while leaving MISMATCH free to read the un-aligned crops.
    """
    import cv2
    import numpy as np

    a = recorded_gray.astype(np.float32)
    b = live_gray.astype(np.float32)
    warp = np.eye(2, 3, dtype=np.float32)
    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 50, 1e-4)
    try:
        _, warp = cv2.findTransformECC(
            a, b, warp, cv2.MOTION_TRANSLATION, criteria, None, 5
        )
    except cv2.error:
        return live_gray, False
    dx, dy = float(warp[0, 2]), float(warp[1, 2])
    if not (np.isfinite(dx) and np.isfinite(dy)):
        return live_gray, False
    cap = PIXEL_ALIGN_MAX_SHIFT_FRAC * PIXEL_SI_HEIGHT
    if abs(dx) > cap or abs(dy) > cap:
        return live_gray, False
    aligned = cv2.warpAffine(
        b,
        warp,
        (a.shape[1], a.shape[0]),
        flags=cv2.INTER_LINEAR + cv2.WARP_INVERSE_MAP,
        borderMode=cv2.BORDER_REPLICATE,
    )
    return aligned, True


def pixel_identity_distance(
    recorded_png: bytes, live_png: bytes
) -> Optional[PixelIdentityDistance]:
    """Jitter-robust localized distance between two identifier crops.

    Both crops are canonicalized to a fixed height (aspect preserved), the live
    crop is sub-pixel translation-aligned onto the recorded crop
    (:func:`_align_translation`), and per-window (~one-glyph-wide) mean-abs-diffs
    are computed on the shared width. Returns a :class:`PixelIdentityDistance`
    or None when either crop is undecodable.
    """
    import numpy as np

    ra = _pixel_canon_aspect(recorded_png)
    la = _pixel_canon_aspect(live_png)
    if ra is None or la is None:
        return None
    w = int(min(ra.shape[1], la.shape[1]))
    a = ra[:, :w]
    b = la[:, :w]
    aligned_b, ok = _align_translation(a, b)
    d = np.abs(a.astype(np.float32) - aligned_b.astype(np.float32)) / 255.0
    win = max(4, int(PIXEL_WIN_FRAC * PIXEL_SI_HEIGHT))
    step = max(1, win // 4)
    windows = [
        (x0, float(d[:, x0 : x0 + win].mean()))
        for x0 in range(0, max(1, d.shape[1] - win + 1), step)
    ]
    if not windows:
        return PixelIdentityDistance(0.0, 0.0, 0.0, 0.0, bool(ok))
    means = np.array([m for _, m in windows], dtype=np.float64)
    floor = float(np.median(means))
    max_window = float(means.max())
    spike = max_window - floor
    # Uniqueness: the tallest window's spike vs the next tallest window that does
    # NOT overlap it (a real glyph change is one tall window; JPEG noise is many).
    order = sorted(range(len(windows)), key=lambda i: windows[i][1], reverse=True)
    top_x = windows[order[0]][0]
    second = floor
    for i in order[1:]:
        if abs(windows[i][0] - top_x) >= win:
            second = windows[i][1]
            break
    second_spike = max(0.0, second - floor)
    uniqueness = spike / (second_spike + 1e-6)
    return PixelIdentityDistance(max_window, floor, spike, uniqueness, bool(ok))


def verify_pixel_identity(
    recorded_png: Optional[bytes],
    live_png: Optional[bytes],
    *,
    enable_verify: Optional[bool] = None,
) -> Optional[IdentityCheck]:
    """Pixel-compare identity tier (tier 2 of the ladder), jitter-robust.

    Compares the recorded vs live identifier crops with a sub-pixel-aligned,
    scale-invariant localized distance (:func:`pixel_identity_distance`).
    Three-way, HALT-biased (a false MATCH is the worst outcome, so the tier
    abstains whenever it is not confident):

    - **mismatch** -- a UNIQUELY localized glyph-scale spike
      (>= :data:`PIXEL_MISMATCH_SPIKE`, uniqueness
      >= :data:`PIXEL_MISMATCH_UNIQUENESS`) that is NOT riding a whole-crop
      drift floor (floor < :data:`PIXEL_DRIFT_FLOOR`): a different identifier
      -> HALT. Scale-invariant; fires on a realistic wide cell.
    - **verify** -- the whole crop matches after alignment (worst aligned
      window <= :data:`PIXEL_VERIFY_MAX_WINDOW`, not drifted, alignment
      converged) AND verify is enabled. On the battery this window CLEANLY
      separates same-record matching renders (max ~0.052) from every
      different-record (min ~0.070), so a MATCH cannot be a false-accept.
    - **abstain** (``None``) -- whole-crop drift (theme/heavy scale), or a
      render whose worst window sits in the uncertain band between the MATCH
      gate and a clean glyph spike, or verify disabled: fall through to the
      next tier (VLM / OCR) or HALT. Also when a crop is missing/undecodable.

    ``enable_verify`` gates ONLY the positive MATCH path: None (default) reads
    the module default :data:`PIXEL_VERIFY_ENABLED` (currently False, pending a
    real remote-display battery -- docs/LIMITS.md); True/False overrides it for
    a caller that has its own evidence/risk policy. MISMATCH and ABSTAIN are
    unaffected -- they can only ever add a HALT, never grant a pass.

    The ``observed`` text carries only DISTANCES, never crop pixels or decoded
    identifier text, so an identity verdict never logs PHI.
    """
    if enable_verify is None:
        enable_verify = PIXEL_VERIFY_ENABLED
    if not recorded_png or not live_png:
        return None
    dist = pixel_identity_distance(recorded_png, live_png)
    if dist is None:
        return None
    if dist.floor < PIXEL_DRIFT_FLOOR and (
        dist.spike >= PIXEL_MISMATCH_SPIKE
        and dist.uniqueness >= PIXEL_MISMATCH_UNIQUENESS
    ):
        return IdentityCheck(
            status="mismatch",
            mode="pixel",
            coverage=0.0,
            expected="recorded identifier crop",
            observed=(
                f"identifier pixels differ locally (spike {dist.spike:.3f}, "
                f"floor {dist.floor:.3f}) — a different identifier"
            ),
        )
    if (
        enable_verify
        and dist.aligned
        and dist.floor < PIXEL_DRIFT_FLOOR
        and dist.max_window <= PIXEL_VERIFY_MAX_WINDOW
    ):
        return IdentityCheck(
            status="verified",
            mode="pixel",
            coverage=1.0,
            expected="recorded identifier crop",
            observed=(
                f"live identifier crop matches after alignment "
                f"(worst window {dist.max_window:.3f})"
            ),
        )
    return None  # drift, uncertain band, or verify-gated -> abstain to next tier


@runtime_checkable
class IdentityVLM(Protocol):
    """Protocol for the optional local-VLM same/different identity comparator.

    Implementations answer whether two identifier crops show the SAME
    characters. VETO-ONLY by contract: an implementation must fold any
    non-confident answer to ``"different"`` (the memo's rule -- the model may
    only reject, never grant a pass). See
    :class:`openadapt_flow.runtime.identity_vlm.MLXIdentityVLM`.
    """

    def same_or_different(self, recorded_png: bytes, live_png: bytes) -> str:
        """Return ``"same"`` or ``"different"`` for the two identifier crops."""
        ...


def identity_rests_on_confusable_identifier(text: Optional[str]) -> bool:
    """Whether identity here rests on a GLYPH-CONFUSABLE identifier.

    True iff any token in ``text`` is an identifier-like string carrying an
    O/0 or l/1/I near-homoglyph (see :func:`_is_glyph_vulnerable_identifier`)
    -- the only case where the expensive VLM veto earns its cost, because a
    plain name+DOB is already discriminated by the cheaper tiers.
    """
    if not text:
        return False
    return any(_is_glyph_vulnerable_identifier(squash(tok)) for tok in tokenize(text))


def verify_vlm_identity(
    recorded_png: Optional[bytes],
    live_png: Optional[bytes],
    *,
    verifier: Optional[IdentityVLM],
    glyph_confusable: bool,
) -> Optional[IdentityCheck]:
    """Optional local-VLM veto tier (tier 3 of the ladder).

    Gated three ways -- returns None (abstain) unless ALL hold: a ``verifier``
    is injected (the tier is OFF by default), identity rests on a
    ``glyph_confusable`` identifier (else the cheaper tiers suffice), and both
    crops are present.

    TRULY VETO-ONLY: the verifier can only REJECT. A ``"different"`` answer
    (and anything else, or any error from a broken/missing model) is a
    ``mismatch`` -> HALT. A ``"same"`` answer does NOT grant a pass: it
    ABSTAINS (returns None), leaving the decision to prior/other evidence.
    A local open VLM reading a glyph-confusable identifier is trustworthy to
    REJECT a wrong patient (100% detection on the collapse surface) but NOT
    to CERTIFY a right one -- so a "same" answer may only FAIL TO VETO, never
    upgrade an otherwise-unverified target. When the VLM is the sole signal,
    "same" -> abstain -> the ladder HALTs. It never overrides an earlier tier
    (the ladder only reaches it after the cheaper tiers abstained).
    """
    if verifier is None or not glyph_confusable:
        return None
    if not recorded_png or not live_png:
        return None
    try:
        verdict = verifier.same_or_different(recorded_png, live_png)
    except Exception:
        verdict = "different"  # veto-only: a broken model halts, never passes
    same = str(verdict).strip().lower() == "same"
    if same:
        # Veto-only: a "same" answer cannot by itself certify identity. Abstain
        # so a higher-fidelity signal (or, absent one, HALT) decides.
        return None
    return IdentityCheck(
        status="mismatch",
        mode="vlm",
        coverage=0.0,
        expected="recorded identifier crop",
        observed="local-VLM verdict: different",
    )


def crop_region(frame_png: bytes, region: Region) -> Optional[bytes]:
    """Crop ``region`` (x, y, w, h) from a PNG frame; return it as PNG bytes.

    Feeds the pixel/VLM identity tiers with the live identifier crop re-cut at
    the resolved point. Returns None when the frame cannot be decoded or the
    clamped region is empty (so the tiers abstain). cv2/numpy are lazy to keep
    this module import-light.
    """
    import cv2
    import numpy as np

    img = cv2.imdecode(np.frombuffer(frame_png, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    h, w = img.shape[:2]
    x0, y0 = max(0, int(region[0])), max(0, int(region[1]))
    x1 = min(w, int(region[0]) + int(region[2]))
    y1 = min(h, int(region[1]) + int(region[3]))
    if x1 <= x0 or y1 <= y0:
        return None
    ok, buf = cv2.imencode(".png", img[y0:y1, x0:x1])
    return buf.tobytes() if ok else None


def upscale_crop(frame_png: bytes, region: Region, factor: int = 2) -> Optional[bytes]:
    """Crop ``region`` from a frame and upscale it (cubic) for a re-OCR.

    Dense small text (real EMR tables) is undercounted by OCR at native
    resolution; a 2x retry recovers most dropped lines (same technique as
    ``benchmark.verify``). Returns None when the frame cannot be decoded or
    the region is empty.
    """
    import cv2  # lazy: keep this module import-light for unit tests
    import numpy as np

    img = cv2.imdecode(np.frombuffer(frame_png, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    h, w = img.shape[:2]
    x0, y0 = max(0, region[0]), max(0, region[1])
    x1 = min(w, region[0] + region[2])
    y1 = min(h, region[1] + region[3])
    if x1 <= x0 or y1 <= y0:
        return None
    crop = img[y0:y1, x0:x1]
    up = cv2.resize(crop, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)
    ok, buf = cv2.imencode(".png", up)
    return buf.tobytes() if ok else None
