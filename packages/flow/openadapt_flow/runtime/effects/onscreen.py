"""On-screen OCR read-back verifier — the no-API, out-of-the-box effect oracle.

Every other :class:`~openadapt_flow.runtime.effects.effect.EffectVerifier` in
this package (``RestRecordVerifier``, ``FhirEffectVerifier``,
``DocumentHashVerifier``) certifies a write by reading an **independent system of
record** (a JSON API, a FHIR endpoint, a filesystem document). A **Citrix-only /
Accuro-over-Citrix** substrate exposes none of those to the local box: there is no
DB, no reachable FHIR, no local file the write lands in — only the **pixels of the
remote-display client window**. This verifier is the honest fallback for that
case: after a write, it re-OCRs the saved value and compares it to the expected
one. It is the DEFAULT oracle for a GUI-only recording — auto-derived from the
demonstration (``compiler.effect_mining``), so effect verification works with NO
connector.

Two strengths, one gate — read ``ReadbackSpec.different_path``:

- **DIFFERENT-PATH (strong, default-eligible).** Before reading, the verifier
  RE-NAVIGATES to the record by a path distinct from the write flow (the
  re-navigation the demonstrator performed to re-view the saved record: clear
  the form, search, re-open). Re-opening forces the app to actually FETCH the
  record, so it defeats the "the form still shows what I typed but nothing
  persisted" phantom/optimistic-save class. The measured false-CONFIRM rate is
  ~0 (``benchmark/effect_readback/``), so this is safe to be the default.

- **SAME-SURFACE (weak, NON-default).** It re-reads the SAME region on the SAME
  screen the action drove. A rendered "Saved" that the record never received
  still reads as "Saved", so it CANNOT see a phantom/optimistic/partial save
  (the measured false-CONFIRM rate is > 0). It is wired but never a default
  pass — a best-effort presence/consistency signal an operator opts into.

HONESTY — even different-path is SAME-APPLICATION, not an independent system of
record. It cannot catch a partial save the app re-renders optimistically, a
duplicate/double-submit, a lost update by a concurrent writer, or a read served
from a stale cache/BFF. So the pixel-only safety guarantee does **not** rest on
this read-back — it rests on the **identity gate** (right record) and
**halt-on-ambiguity** (never guess a target). This read-back is *additive*
assurance, and the structured SoR oracle remains the transactional guarantee
where a read API exists (``docs/LIMITS.md``). The verifier is fail-safe either
way: it HALTs (INDETERMINATE) when the region is unreadable or re-navigation
cannot be performed, and HALTs (REFUTED) when the region shows a readable value
that is NOT expected — it never guesses CONFIRMED.

It implements the :class:`EffectVerifier` protocol so it composes with the same
runtime plumbing, with ``substrate="onscreen"``.
"""

from __future__ import annotations

import difflib
import re
from typing import Any, Optional

from openadapt_flow.assurance import SCREEN_READBACK, UNCHECKED
from openadapt_flow.runtime.effects.adapter import ConnectionProbe, VerifierAdapterBase
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectKind,
    EffectState,
    EffectVerdict,
    ReadbackGuard,
    ReadbackNav,
    ReadbackReference,
    Region,
    Verdict,
)
from openadapt_flow.runtime.effects.readback_values import compare_value
from openadapt_flow.verification import VerificationTier

# Fuzzy-match floor for accepting an OCR read-back as the expected value.
# Deliberately strict (mirrors resolver.OCR_MIN_RATIO): a near-miss on a
# consequential read-back must HALT, not silently confirm.
READBACK_MIN_RATIO = 0.9

# Minimum OCR confidence for a line to count as "readable text present". Below
# this the region is treated as unreadable -> INDETERMINATE (halt), never a
# fabricated REFUTED/CONFIRMED.
READABLE_CONF = 0.3

# GATE (measured, ``benchmark/effect_readback/``): whether an auto-derived
# DIFFERENT-PATH read-back is trusted as the out-of-the-box DEFAULT oracle
# (auto-wired with no deployment config). Enabled because the measured
# false-CONFIRM rate for different-path read-back is ~0 on the fixture. A
# SAME-SURFACE read-back is NEVER a default (measured false-CONFIRM > 0); it
# is verifiable only when an operator explicitly wires ``effects.kind:
# onscreen``, which acknowledges the weaker signal.
DIFFERENT_PATH_IS_DEFAULT_ORACLE = True


def _valid_renavigation_step(nav: ReadbackNav) -> bool:
    """Whether one persisted-reacquisition action is fully performable."""

    action = (nav.action or "").strip().lower()
    if action == "click":
        return nav.point is not None
    if action == "type":
        return nav.text is not None or bool(nav.param) or nav.reference
    if action == "key":
        return bool(nav.key)
    return False


#: Words that label a control which writes. The read-back's navigation never
#: clicks one (:class:`~openadapt_flow.runtime.effects.effect.ReadbackGuard`).
COMMIT_WORDS: frozenset[str] = frozenset(
    {
        "save",
        "submit",
        "delete",
        "remove",
        "confirm",
        "sign",
        "send",
        "post",
        "finalize",
        "finalise",
        "approve",
        "discard",
        "overwrite",
    }
)

#: Modifier spellings, folded to one name each before a chord is judged.
_MODIFIERS: dict[str, str] = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "ctl": "ctrl",
    "cmd": "cmd",
    "command": "cmd",
    "meta": "cmd",
    "super": "cmd",
    "win": "cmd",
    "windows": "cmd",
    "alt": "alt",
    "option": "alt",
    "opt": "alt",
    "altgr": "alt",
    "shift": "shift",
}

#: Keys that write with any modifier held (Ctrl+S, Alt+S, Ctrl+Enter,
#: Shift+Delete, ...). Enter alone is allowed: a search needs it.
_COMMIT_KEYS_WITH_MODIFIER: frozenset[str] = frozenset(
    {"s", "enter", "return", "kp_enter", "delete", "del", "backspace"}
)

#: Keys that delete with no modifier at all.
_COMMIT_KEYS_ALONE: frozenset[str] = frozenset({"delete", "del", "backspace"})

#: Examples of chords the guard refuses (see :func:`is_commit_chord`).
COMMIT_CHORDS: frozenset[str] = frozenset(
    {
        "ctrl+s",
        "cmd+s",
        "alt+s",
        "ctrl+enter",
        "cmd+enter",
        "ctrl+return",
        "shift+delete",
        "delete",
        "del",
        "backspace",
    }
)


def _chord_parts(key: str) -> tuple[frozenset[str], frozenset[str]]:
    """``(modifiers, keys)`` of a chord, modifier spellings folded
    (control -> ctrl, command/meta -> cmd, option -> alt), in any order."""

    parts = [p for p in re.split(r"[+\s-]+", key.strip().lower()) if p]
    modifiers = frozenset(_MODIFIERS[p] for p in parts if p in _MODIFIERS)
    keys = frozenset(p for p in parts if p not in _MODIFIERS)
    return modifiers, keys


def is_commit_chord(key: str) -> bool:
    """Whether pressing ``key`` could save, submit or delete.

    Refused: any chord with a modifier held (in any spelling or order) whose
    key is ``s``, ``enter``, ``return``, ``delete``, ``del`` or
    ``backspace``, and ``delete``, ``del`` or ``backspace`` alone.
    """

    modifiers, keys = _chord_parts(key)
    if modifiers:
        return bool(keys & _COMMIT_KEYS_WITH_MODIFIER)
    return bool(keys & _COMMIT_KEYS_ALONE)


_WORD = re.compile(r"[a-z]+")

#: Half-size of the box read around a click point to find its label.
_LABEL_BOX = (120, 24)


def _commit_label(text: str, commit_labels: list[str]) -> Optional[str]:
    """The commit word or recorded save label that ``text`` shows, if any."""

    norm = _normalize(text)
    for label in commit_labels:
        want = _normalize(label)
        if want and want == norm:
            return label
    words = set(_WORD.findall(norm))
    hit = sorted(words & COMMIT_WORDS)
    return hit[0] if hit else None


def _distinct_matches(pattern: str, lines: list[str]) -> list[str]:
    compiled = re.compile(pattern)
    seen: list[str] = []
    for line in lines:
        for match in compiled.finditer(line):
            value = match.group(1)
            if value and value not in seen:
                seen.append(value)
    return seen


def _is_persisted_reacquisition(spec: Any) -> bool:
    """Conservatively recognize the compiler's different-path invariant."""

    if spec is None or not spec.different_path or spec.region is None:
        return False
    _, _, width, height = spec.region
    reset = getattr(spec, "reset", None)
    if reset is None or not 0 <= reset.at < len(spec.renavigation):
        # Reopen credit needs a state-busting step (a reload, a new search, a
        # reopened window) inside the navigation. Without one the read could
        # come from a form buffer or a page the save left behind, however
        # many steps the navigation has.
        return False
    return (
        width > 0
        and height > 0
        and all(_valid_renavigation_step(nav) for nav in spec.renavigation)
    )


def _searches_by_run_input(spec: Any) -> bool:
    """Whether the reopen finds the record by typing a run input (a search
    for this run's patient) rather than an identifier only this save has."""

    navs = list(getattr(spec, "renavigation", None) or [])
    return any(nav.types_run_input for nav in navs) and not any(
        nav.reference for nav in navs
    )


def is_default_readback_effect(effect: Any) -> bool:
    """Whether ``effect`` is an on-screen read-back that may be auto-verified as
    the out-of-the-box default (a DIFFERENT-PATH read-back, gated by
    :data:`DIFFERENT_PATH_IS_DEFAULT_ORACLE`)."""
    spec = getattr(effect, "readback", None)
    return bool(DIFFERENT_PATH_IS_DEFAULT_ORACLE and _is_persisted_reacquisition(spec))


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def value_matches(
    haystack: str, expected: str, *, min_ratio: float = READBACK_MIN_RATIO
) -> bool:
    """Whether ``expected`` is present in normalized ``haystack`` at ``min_ratio``."""
    want = _normalize(expected)
    if not want:
        return False
    if want in haystack:
        return True
    # Longest contiguous run (tolerant of the value embedded in surrounding
    # label text, e.g. "saved note for patient 3" containing the value).
    squashed_want = "".join(want.split())
    squashed_hay = "".join(haystack.split())
    matcher = difflib.SequenceMatcher(None, squashed_want, squashed_hay, autojunk=False)
    longest = max((b.size for b in matcher.get_matching_blocks()), default=0)
    return longest >= min_ratio * len(squashed_want)


class OnScreenReadbackVerifier(VerifierAdapterBase):
    """Verify a write by OCR-reading the saved value off the screen.

    Args:
        backend: Anything exposing ``screenshot() -> PNG bytes`` (the
            remote-display backend under test). For a DIFFERENT-PATH read-back
            it must also expose the standard action methods (``click`` /
            ``type_text`` / ``press``) so the recorded re-navigation can be
            replayed; a backend that cannot perform an action makes the
            read-back INDETERMINATE (HALT), never a guessed CONFIRM. May be
            ``None`` at construction and supplied later via
            :meth:`bind_backend` (the auto-wired default path binds the live
            replay backend).
        region: The ``(x, y, w, h)`` saved-value region to OCR. May be ``None``
            when each effect carries its own :attr:`ReadbackSpec.region`
            (the auto-derived path); a per-effect region wins over this one.
        min_ratio: Fuzzy-match floor to accept the expected value.
        vision: Namespace exposing ``ocr(png, region=...)`` (injectable for
            tests); defaults to :mod:`openadapt_flow.vision`.
        reference_value: A reference number read before this verifier runs
            (by :mod:`~openadapt_flow.runtime.effects.fresh_session`, from the
            acting session); skips reading it off this backend's screen.
    """

    substrate = "onscreen"
    verification_tier = VerificationTier.IMMEDIATE_SCREEN
    #: Explicit demotion (adapter platform): a screen read-back shares the
    #: application surface the actuation drove, so its CONFIRMED is a
    #: LOWER-CONFIDENCE CONSISTENCY CHECK -- never independent
    #: system-of-record proof. Execution profiles gate on the tier
    #: (``verification_tier_for``: different-path re-navigation earns tier 3,
    #: same-surface stays tier 4); this flag makes the demotion visible to any
    #: caller inspecting the verifier directly.
    independent_system_of_record = False

    @staticmethod
    def verification_tier_for(effect: Effect) -> VerificationTier:
        """Distinguish persisted reacquisition from immediate screen evidence."""

        spec = effect.readback
        if _is_persisted_reacquisition(spec):
            return VerificationTier.PERSISTED_STATE_REACQUISITION
        return VerificationTier.IMMEDIATE_SCREEN

    @staticmethod
    def requires_readable_pre_state_for(effect: Effect) -> bool:
        """Read-back proof occurs after input and has no pre-action delta baseline."""
        return False

    def __init__(
        self,
        backend: Any = None,
        region: Optional[Region] = None,
        *,
        min_ratio: float = READBACK_MIN_RATIO,
        vision: Any = None,
        reference_value: Optional[str] = None,
    ) -> None:
        self._backend = backend
        #: A reference number already read elsewhere (the acting session, by
        #: a fresh-session check); used instead of reading this screen.
        self._reference_value = reference_value
        self._region = region
        self._min_ratio = min_ratio
        if vision is None:
            from openadapt_flow import vision as _vision

            vision = _vision
        self._vision = vision

    def bind_backend(self, backend: Any) -> None:
        """Attach the live replay backend (used by the auto-wired default path
        where the verifier is built before the backend exists)."""
        self._backend = backend

    def test_connection(self, context: Any = None) -> ConnectionProbe:
        """Prove that the local observation boundary can provide a fresh frame."""

        screenshot = getattr(self._backend, "screenshot", None)
        if not callable(screenshot):
            return ConnectionProbe(
                ok=False,
                substrate=self.substrate,
                reason="the live backend has no screenshot boundary",
            )
        try:
            frame = screenshot()
        except Exception as exc:  # noqa: BLE001 - a probe never escapes
            return ConnectionProbe(
                ok=False,
                substrate=self.substrate,
                reason=f"screenshot probe raised: {type(exc).__name__}",
            )
        return ConnectionProbe(
            ok=isinstance(frame, bytes) and bool(frame),
            substrate=self.substrate,
            reason=(
                "live observation boundary reachable"
                if isinstance(frame, bytes) and bool(frame)
                else "live observation boundary returned no frame"
            ),
        )

    # -- region read ---------------------------------------------------------

    def _read_region(self, region: Region) -> tuple[str, float]:
        """OCR ``region``; return (joined normalized text, max line confidence)."""
        png = self._backend.screenshot()
        lines = self._vision.ocr(png, region=region)
        if not lines:
            return "", 0.0
        text = _normalize(" ".join(line.text for line in lines))
        conf = max((float(line.confidence) for line in lines), default=0.0)
        return text, conf

    def _match(self, haystack: str, expected: str) -> bool:
        """Whether ``expected`` is present in ``haystack`` at ``min_ratio``."""
        return value_matches(haystack, expected, min_ratio=self._min_ratio)

    # -- re-navigation (DIFFERENT-PATH only) ---------------------------------

    def _label_at(self, x: int, y: int) -> Optional[str]:
        """The text of the control under ``(x, y)``, read by OCR, or None."""

        half_w, half_h = _LABEL_BOX
        box = (max(0, x - half_w), max(0, y - half_h), 2 * half_w, 2 * half_h)
        lines = self._vision.ocr(self._backend.screenshot(), region=box)
        hits = []
        for line in lines or []:
            where = getattr(line, "region", None)
            if where is None:
                continue  # a line with no position can't label the click
            bx, by, bw, bh = where
            if bx - 4 <= x <= bx + bw + 4 and by - 4 <= y <= by + bh + 4:
                hits.append(line.text)
        return " ".join(hits) if hits else None

    def _guard_refusal(
        self, nav: ReadbackNav, index: int, guard: Optional[ReadbackGuard]
    ) -> Optional[str]:
        """Why the guard refuses ``nav``, or None when it may run."""

        action = (nav.action or "").strip().lower()
        if action == "key" and nav.key:
            if is_commit_chord(nav.key):
                return f"re-navigation step {index} presses {nav.key!r}, which writes"
        if action == "click" and nav.point is not None:
            labels = list(guard.commit_labels) if guard is not None else []
            try:
                text = self._label_at(int(nav.point[0]), int(nav.point[1]))
            except Exception as exc:  # noqa: BLE001 - unreadable => refuse
                return (
                    f"re-navigation step {index}: could not read the control "
                    f"under the click ({type(exc).__name__})"
                )
            if not text:
                # No readable label: an icon-only button may be Save or
                # Delete. Refuse rather than click blind.
                return (
                    f"re-navigation step {index}: no readable label under the "
                    "click, so the guard can't tell it doesn't write"
                )
            hit = _commit_label(text, labels)
            if hit is not None:
                return (
                    f"re-navigation step {index} would click a control labelled "
                    f"{hit!r}, which writes"
                )
        return None

    def _renavigate(
        self,
        steps: list[ReadbackNav],
        params: Optional[dict] = None,
        *,
        guard: Optional[ReadbackGuard] = None,
        reference: Optional[str] = None,
    ) -> Optional[str]:
        """Replay the recorded re-navigation to re-open the record.

        Returns ``None`` on success, or a short failure reason (the read-back
        then rules INDETERMINATE -> HALT). Fail-safe: an action the backend
        cannot perform, an unknown/ill-formed nav step, or a step the read-only
        guard refuses never proceeds to a CONFIRM — it halts so a human
        re-navigates. The guard runs on every surface; see
        :class:`~openadapt_flow.runtime.effects.effect.ReadbackGuard`.
        """
        backend = self._backend
        for i, nav in enumerate(steps):
            action = (nav.action or "").strip().lower()
            try:
                refusal = self._guard_refusal(nav, i, guard)
                if refusal is not None:
                    return f"read-only guard: {refusal}"
                if action == "click":
                    if nav.point is None or not hasattr(backend, "click"):
                        return f"re-navigation step {i} (click) not performable"
                    backend.click(int(nav.point[0]), int(nav.point[1]))
                elif action == "type":
                    text = nav.text
                    if nav.reference:
                        text = reference
                    elif nav.param:
                        value = (params or {}).get(nav.param)
                        text = None if value is None else str(value)
                    if text is None or not hasattr(backend, "type_text"):
                        return f"re-navigation step {i} (type) not performable"
                    backend.type_text(text)
                elif action == "key":
                    if not nav.key or not hasattr(backend, "press"):
                        return f"re-navigation step {i} (key) not performable"
                    backend.press(nav.key)
                else:
                    return f"re-navigation step {i} has unknown action {nav.action!r}"
            except Exception as e:  # noqa: BLE001 — any backend error => HALT
                return f"re-navigation step {i} ({action}) failed: {e}"
        return None

    def _capture_reference(self, ref: ReadbackReference) -> tuple[Optional[str], str]:
        """Read the reference number off the current screen.

        Exactly one distinct value must match, or there is no reference: two
        different numbers on screen could open the wrong record.
        """

        if self._backend is None or not callable(
            getattr(self._backend, "screenshot", None)
        ):
            return None, "no screen to read the reference number from"
        try:
            lines = self._vision.ocr(self._backend.screenshot(), region=ref.region)
        except Exception as exc:  # noqa: BLE001 - unreadable => no reference
            return None, f"could not read the reference number ({type(exc).__name__})"
        found = _distinct_matches(
            ref.pattern,
            [line.text for line in lines or [] if line.confidence >= READABLE_CONF],
        )
        if not found:
            return None, "the screen shows no reference number after the save"
        if len(found) > 1:
            return None, (
                f"the screen shows {len(found)} different reference numbers "
                "after the save"
            )
        return found[0], ""

    def _untied_to_this_save(
        self, spec: Any, reference: Optional[str]
    ) -> Optional[str]:
        """Why a search by a run input can't tell this save's record from an
        older one, or None when it can.

        A search for this run's patient finds the patient's latest matching
        record whether or not this save landed, so an older note with the
        same text would confirm a save that failed. There is no baseline to
        compare with (reading the record before the save would leave the
        form), so the reopened record must show the reference number this
        save showed, and no other one.
        """

        if spec.reference is None or reference is None:
            return (
                "the check finds the record by a run input and nothing ties "
                "the record it found to this save (no reference number), so "
                "an older record with the same value could pass"
            )
        try:
            lines = self._vision.ocr(self._backend.screenshot(), region=None)
        except Exception as exc:  # noqa: BLE001 - unreadable => untied
            return f"could not read the reopened record ({type(exc).__name__})"
        shown = _distinct_matches(
            spec.reference.pattern,
            [line.text for line in lines or [] if line.confidence >= READABLE_CONF],
        )
        if shown != [reference]:
            return (
                "the reopened record does not show this save's reference "
                "number alone, so it may be an older record"
            )
        return None

    # -- direct API ----------------------------------------------------------

    def read_back(
        self,
        expected_text: str,
        region: Optional[Region] = None,
        *,
        value_kind: str = "text",
        date_order: Optional[str] = None,
    ) -> EffectVerdict:
        """Rule on whether ``expected_text`` is on the saved-value region NOW.

        - CONFIRMED   — the expected value reads back in the region (fuzzy
          for free text; normalized-exact for codes, dates and amounts).
        - INDETERMINATE — the region has no readable text, or the read differs
          from the expected value only in glyphs OCR confuses (``O``/``0``,
          ``l``/``1``) or is a date that reads two ways -> HALT.
        - REFUTED     — the region has readable text that is NOT the expected
          value -> HALT (affirmative on-screen contradiction).
        """
        use_region = region if region is not None else self._region
        if use_region is None:
            return EffectVerdict(
                verdict=Verdict.INDETERMINATE,
                kind=EffectKind.FIELD_EQUALS,
                substrate=self.substrate,
                reason="no read-back region configured or derivable — cannot certify",
                expected_value=expected_text,
            )
        text, conf = self._read_region(use_region)
        if not text or conf < READABLE_CONF:
            return EffectVerdict(
                verdict=Verdict.INDETERMINATE,
                kind=EffectKind.FIELD_EQUALS,
                substrate=self.substrate,
                reason=(
                    "saved-value region unreadable (no OCR text above "
                    f"confidence {READABLE_CONF}); cannot certify write"
                ),
                observed_value=text or None,
                expected_value=expected_text,
            )
        comparison = compare_value(
            text,
            expected_text,
            kind=value_kind,  # type: ignore[arg-type]
            source="ocr",
            date_order=date_order,
            exact=False,
        )
        if comparison == "ambiguous":
            return EffectVerdict(
                verdict=Verdict.INDETERMINATE,
                kind=EffectKind.FIELD_EQUALS,
                substrate=self.substrate,
                reason=(
                    "the read differs from the expected value only in "
                    "characters OCR can't tell apart (O/0, l/1) or reads as "
                    "more than one date; cannot certify write"
                ),
                observed_value=text,
                expected_value=expected_text,
            )
        matched = comparison == "match" or (
            value_kind == "text" and self._match(text, expected_text)
        )
        if matched:
            return EffectVerdict(
                verdict=Verdict.CONFIRMED,
                kind=EffectKind.FIELD_EQUALS,
                substrate=self.substrate,
                reason="expected value read back on-screen",
                observed_value=text,
                expected_value=expected_text,
            )
        return EffectVerdict(
            verdict=Verdict.REFUTED,
            kind=EffectKind.FIELD_EQUALS,
            substrate=self.substrate,
            reason=(
                "saved-value region shows readable text that is NOT the "
                "expected value (on-screen contradiction)"
            ),
            observed_value=text,
            expected_value=expected_text,
        )

    # -- EffectVerifier protocol --------------------------------------------

    def capture_pre_state(self, context: Any = None) -> EffectState:
        """Snapshot the region's text BEFORE the write (baseline for audit).

        Region-agnostic: with no constructed region (the per-effect path) the
        baseline is simply recorded as unreachable — the verdict is decided by
        the post-action :meth:`verify`, which reads the effect's own region.
        """
        if self._region is None or self._backend is None:
            return EffectState(
                substrate=self.substrate,
                reachable=False,
                records=[],
                detail={"same_surface": True, "region": None},
            )
        text, conf = self._read_region(self._region)
        return EffectState(
            substrate=self.substrate,
            reachable=bool(text) and conf >= READABLE_CONF,
            records=[{"text": text}] if text else [],
            detail={"region": list(self._region), "same_surface": True},
        )

    def verify(
        self, expected: Effect, before: EffectState, context: Any = None
    ) -> EffectVerdict:
        """Verify ``expected`` (a ``field_equals``/``record_written`` Effect).

        The expected string is taken from ``expected.value`` (field_equals) or,
        failing that, any single ``match`` selector value — resolved against a
        ``context`` mapping of run params when present. The region and (for the
        DIFFERENT-PATH variant) the re-navigation come from
        ``expected.readback``; when absent, the verifier's constructed region is
        used (a hand-configured deployment).
        """
        params = context if isinstance(context, dict) else {}
        expected_text = self._expected_text(expected, params)
        if expected_text is None:
            return EffectVerdict(
                verdict=Verdict.INDETERMINATE,
                kind=expected.kind,
                substrate=self.substrate,
                reason="no expected value derivable from the effect contract",
            )

        spec = expected.readback
        different_path = bool(spec is not None and spec.different_path)
        region = spec.region if spec is not None else None

        # A check taught by demonstration confirms nothing until a person
        # admits it. Effect.resolve drops an admission made for another check.
        if spec is not None and spec.demonstrated and spec.admission is None:
            return EffectVerdict(
                verdict=Verdict.INDETERMINATE,
                kind=expected.kind,
                substrate=self.substrate,
                reason=(
                    "the read-back was taught by demonstration and no person "
                    "has admitted it; cannot certify — HALT"
                ),
                expected_value=expected_text,
                assurance=UNCHECKED,
            )

        # A reference number the save left on screen is read first, before
        # any navigation moves away from it.
        reference: Optional[str] = None
        if spec is not None and spec.reference is not None:
            reference, why = (
                (self._reference_value, "")
                if self._reference_value
                else self._capture_reference(spec.reference)
            )
            if reference is None:
                return EffectVerdict(
                    verdict=Verdict.INDETERMINATE,
                    kind=expected.kind,
                    substrate=self.substrate,
                    reason=f"{why}; cannot certify — HALT",
                    expected_value=expected_text,
                    assurance=UNCHECKED,
                )

        # DIFFERENT-PATH: re-open the record by an independent path BEFORE
        # reading. Any failure to re-navigate => INDETERMINATE (HALT), never a
        # CONFIRM off the write's own surface.
        if spec is not None and spec.renavigation:
            if self._backend is None:
                return EffectVerdict(
                    verdict=Verdict.INDETERMINATE,
                    kind=expected.kind,
                    substrate=self.substrate,
                    reason="different-path read-back needs a backend; none bound — HALT",
                    expected_value=expected_text,
                )
            nav_error = self._renavigate(
                spec.renavigation, params, guard=spec.guard, reference=reference
            )
            if nav_error is not None:
                return EffectVerdict(
                    verdict=Verdict.INDETERMINATE,
                    kind=expected.kind,
                    substrate=self.substrate,
                    reason=(
                        "different-path read-back could not re-open the record "
                        f"({nav_error}); cannot certify — HALT"
                    ),
                    expected_value=expected_text,
                )

        if spec is not None and spec.fields:
            verdict = self._read_fields(spec.fields, params)
        else:
            verdict = self.read_back(expected_text, region=region)
        if verdict.verdict is Verdict.CONFIRMED and _searches_by_run_input(spec):
            untied = self._untied_to_this_save(spec, reference)
            if untied is not None:
                return EffectVerdict(
                    verdict=Verdict.INDETERMINATE,
                    kind=expected.kind,
                    substrate=self.substrate,
                    reason=f"{untied}; cannot certify — HALT",
                    expected_value=expected_text,
                    assurance=UNCHECKED,
                )
        posture = (
            "DIFFERENT-PATH (re-opened by an independent path; same-application, "
            "not independent system of record)"
            if different_path
            else "SAME-SURFACE (re-read of the write's own region; presence/"
            "consistency signal only, NOT independent confirmation)"
        )
        return verdict.model_copy(
            update={
                "kind": expected.kind,
                "reason": f"{verdict.reason} — {posture}",
                "assurance": (
                    SCREEN_READBACK
                    if verdict.verdict is Verdict.CONFIRMED
                    else UNCHECKED
                ),
            }
        )

    def _read_structure(
        self,
        expected: str,
        region: Region,
        *,
        value_kind: str = "text",
        date_order: Optional[str] = None,
    ) -> EffectVerdict:
        """Read the element at ``region``'s center from the page structure.

        ``displayed_text_at`` reads the DOM in the browser and the
        accessibility tree on macOS (AX) and Linux (AT-SPI). A pixel-only
        backend (RDP, Citrix) and the Windows backend, whose UIA read returns
        a whole row rather than one element, don't offer it, and the read is
        INDETERMINATE; it never falls back to OCR.
        """
        reader = getattr(self._backend, "displayed_text_at", None)
        if not callable(reader):
            return EffectVerdict(
                verdict=Verdict.INDETERMINATE,
                kind=EffectKind.FIELD_EQUALS,
                substrate=self.substrate,
                reason="this backend can't read a field from the page structure",
                expected_value=expected,
            )
        x, y, w, h = region
        try:
            text = reader(x + w // 2, y + h // 2)
        except Exception:  # noqa: BLE001 - an unreadable field is uncertain
            text = None
        if not text or not text.strip():
            return EffectVerdict(
                verdict=Verdict.INDETERMINATE,
                kind=EffectKind.FIELD_EQUALS,
                substrate=self.substrate,
                reason="no element text at the marked field",
                expected_value=expected,
            )
        observed = _normalize(text)
        # Equality, not containment: a container element would contain the
        # value next to other fields and confirm by accident.
        comparison = compare_value(
            observed,
            expected,
            kind=value_kind,  # type: ignore[arg-type]
            source="structure",
            date_order=date_order,
            exact=True,
        )
        matched = comparison == "match"
        contained = (
            not matched
            and compare_value(
                observed,
                expected,
                kind=value_kind,  # type: ignore[arg-type]
                source="structure",
                date_order=date_order,
                exact=False,
            )
            == "match"
        )
        if not matched and (comparison == "ambiguous" or contained):
            # The element holds more than the field (an accessibility row,
            # a container) or a date that reads two ways: not proof either way.
            return EffectVerdict(
                verdict=Verdict.INDETERMINATE,
                kind=EffectKind.FIELD_EQUALS,
                substrate=self.substrate,
                reason=(
                    "the marked element shows the value among other text, or "
                    "a date that reads two ways"
                ),
                observed_value=observed,
                expected_value=expected,
            )
        return EffectVerdict(
            verdict=Verdict.CONFIRMED if matched else Verdict.REFUTED,
            kind=EffectKind.FIELD_EQUALS,
            substrate=self.substrate,
            reason=(
                "expected value read from the page structure"
                if matched
                else "the marked element shows another value"
            ),
            observed_value=observed,
            expected_value=expected,
        )

    def _read_fields(self, fields: list, params: dict) -> EffectVerdict:
        """Read every marked field; all must read back.

        One unreadable field makes the whole read INDETERMINATE. A readable
        field that shows another value makes it REFUTED. CONFIRMED needs every
        field to show its expected value.
        """
        observed: list[str] = []
        expected_all: list[str] = []
        refuted: list[str] = []
        for field in fields:
            want = field.value.resolve(params)
            if not want:
                return EffectVerdict(
                    verdict=Verdict.INDETERMINATE,
                    kind=EffectKind.FIELD_EQUALS,
                    substrate=self.substrate,
                    reason=f"no expected value for field {field.name!r}",
                )
            kind = getattr(field, "value_kind", "text")
            order = getattr(field, "date_order", None)
            if getattr(field, "source", "ocr") == "structure":
                one = self._read_structure(
                    want, tuple(field.region), value_kind=kind, date_order=order
                )
            else:
                one = self.read_back(
                    want, region=tuple(field.region), value_kind=kind, date_order=order
                )
            expected_all.append(f"{field.name}={want}")
            observed.append(f"{field.name}={one.observed_value or ''}")
            if one.verdict is Verdict.INDETERMINATE:
                return one.model_copy(
                    update={"reason": f"field {field.name!r}: {one.reason}"}
                )
            if one.verdict is Verdict.REFUTED:
                refuted.append(field.name)
        if refuted:
            return EffectVerdict(
                verdict=Verdict.REFUTED,
                kind=EffectKind.FIELD_EQUALS,
                substrate=self.substrate,
                reason=(
                    "readable fields that do not show the expected value: "
                    + ", ".join(refuted)
                ),
                observed_value="; ".join(observed),
                expected_value="; ".join(expected_all),
            )
        return EffectVerdict(
            verdict=Verdict.CONFIRMED,
            kind=EffectKind.FIELD_EQUALS,
            substrate=self.substrate,
            reason=f"all {len(fields)} marked fields read back on-screen",
            observed_value="; ".join(observed),
            expected_value="; ".join(expected_all),
        )

    def verify_current_state(
        self, expected: Effect, current: EffectState, context: Any = None
    ) -> EffectVerdict:
        """Reacquire and verify persisted state after attended or durable input."""

        return self.verify(expected, current, context)

    @staticmethod
    def _expected_text(expected: Effect, params: dict) -> Optional[str]:
        if expected.value is not None:
            resolved = expected.value.resolve(params)
            if resolved:
                return resolved
        for v in expected.match.values():
            resolved = v.resolve(params)
            if resolved:
                return resolved
        return None
