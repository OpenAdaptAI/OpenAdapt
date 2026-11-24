# vendored from 576f87b41483:packages/flow/openadapt_flow/verify_run.py, lines 2958-2995, for the harness self-test only (re-indented)
# ruff: noqa: F401, F841, I001
"""S9: verify_run's folds before this change; latent behind min_length=1.

Only the lines between ``# fmt: off`` and ``# fmt: on`` markers that
follow a ``# vendored:`` comment are verbatim; the wrapper ``def`` lines,
the ``return`` and the stubs above them are not."""

from __future__ import annotations

from typing import Any, Literal, Optional, Sequence

from openadapt_flow.runtime.effects.effect import Verdict

EffectVerdict = Any
VerificationTier = Any
EffectVerdictLabel = Literal["CONFIRMED", "REFUTED", "INDETERMINATE"]
ReadbackClass = Literal["independent_readback", "screen_readback", "unchecked"]


# vendored: 576f87b41483:packages/flow/openadapt_flow/verify_run.py 2958-2995
# fmt: off
def _aggregate(verdicts: Sequence[EffectVerdict]) -> EffectVerdictLabel:
    if any(v.verdict is Verdict.INDETERMINATE for v in verdicts):
        return "INDETERMINATE"
    if any(v.verdict is Verdict.REFUTED for v in verdicts):
        return "REFUTED"
    return "CONFIRMED"


def _observed(verdicts: Sequence[EffectVerdict]) -> str:
    seen = {v.observed_effect for v in verdicts}
    if "unknown" in seen:
        return "unknown"
    if "conflicting" in seen or seen == {"absent", "present"}:
        # A wrong record, or some effects landed and others did not.
        return "conflicting"
    return "absent" if "absent" in seen else "present"


def readback_class(
    verdicts: Sequence[EffectVerdict], tiers: Sequence[Optional[VerificationTier]]
) -> ReadbackClass:
    """RFC-0002 tier rule, weakest effect wins.

    ``unchecked`` when any effect was not read (no tier, store unreachable,
    stale). ``independent_readback`` only when every effect was read from the
    system of record without the UI (Flow tier 1). Anything else, including a
    separate signed-in UI session (tier 2), is ``screen_readback``.
    """

    if (
        any(t is None for t in tiers)
        or any(v.unavailable for v in verdicts)
        or any(v.stale for v in verdicts)
    ):
        return "unchecked"
    if all(t is not None and t.reads_system_of_record() for t in tiers):
        return "independent_readback"
    return "screen_readback"
# fmt: on
