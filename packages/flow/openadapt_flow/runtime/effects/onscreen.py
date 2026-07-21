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
from typing import Any, Optional

from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectKind,
    EffectState,
    EffectVerdict,
    ReadbackNav,
    Region,
    Verdict,
)

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


def is_default_readback_effect(effect: Any) -> bool:
    """Whether ``effect`` is an on-screen read-back that may be auto-verified as
    the out-of-the-box default (a DIFFERENT-PATH read-back, gated by
    :data:`DIFFERENT_PATH_IS_DEFAULT_ORACLE`)."""
    spec = getattr(effect, "readback", None)
    return bool(
        DIFFERENT_PATH_IS_DEFAULT_ORACLE
        and spec is not None
        and spec.different_path
        and spec.renavigation
    )


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


class OnScreenReadbackVerifier:
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
    """

    substrate = "onscreen"

    def __init__(
        self,
        backend: Any = None,
        region: Optional[Region] = None,
        *,
        min_ratio: float = READBACK_MIN_RATIO,
        vision: Any = None,
    ) -> None:
        self._backend = backend
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
        want = _normalize(expected)
        if not want:
            return False
        if want in haystack:
            return True
        # Longest contiguous run (tolerant of the value embedded in surrounding
        # label text, e.g. "saved note for patient 3" containing the value).
        squashed_want = "".join(want.split())
        squashed_hay = "".join(haystack.split())
        matcher = difflib.SequenceMatcher(
            None, squashed_want, squashed_hay, autojunk=False
        )
        longest = max((b.size for b in matcher.get_matching_blocks()), default=0)
        return longest >= self._min_ratio * len(squashed_want)

    # -- re-navigation (DIFFERENT-PATH only) ---------------------------------

    def _renavigate(self, steps: list[ReadbackNav]) -> Optional[str]:
        """Replay the recorded re-navigation to re-open the record.

        Returns ``None`` on success, or a short failure reason (the read-back
        then rules INDETERMINATE -> HALT). Fail-safe: an action the backend
        cannot perform, or an unknown/ill-formed nav step, never proceeds to a
        CONFIRM — it halts so a human re-navigates.
        """
        backend = self._backend
        for i, nav in enumerate(steps):
            action = (nav.action or "").strip().lower()
            try:
                if action == "click":
                    if nav.point is None or not hasattr(backend, "click"):
                        return f"re-navigation step {i} (click) not performable"
                    backend.click(int(nav.point[0]), int(nav.point[1]))
                elif action == "type":
                    if nav.text is None or not hasattr(backend, "type_text"):
                        return f"re-navigation step {i} (type) not performable"
                    backend.type_text(nav.text)
                elif action == "key":
                    if not nav.key or not hasattr(backend, "press"):
                        return f"re-navigation step {i} (key) not performable"
                    backend.press(nav.key)
                else:
                    return f"re-navigation step {i} has unknown action {nav.action!r}"
            except Exception as e:  # noqa: BLE001 — any backend error => HALT
                return f"re-navigation step {i} ({action}) failed: {e}"
        return None

    # -- direct API ----------------------------------------------------------

    def read_back(
        self, expected_text: str, region: Optional[Region] = None
    ) -> EffectVerdict:
        """Rule on whether ``expected_text`` is on the saved-value region NOW.

        - CONFIRMED   — the expected value reads back (fuzzy) in the region.
        - INDETERMINATE — the region has no readable text -> HALT (cannot certify).
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
        if self._match(text, expected_text):
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
            nav_error = self._renavigate(spec.renavigation)
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

        verdict = self.read_back(expected_text, region=region)
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
            }
        )

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
