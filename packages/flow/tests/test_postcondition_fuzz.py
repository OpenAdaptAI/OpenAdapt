"""Property-based (fuzz) testing of postcondition evaluation INVARIANTS.

``Replayer._postcondition_passes`` is the semantic-drift gate: a wrong verdict
here either green-lights a run whose expected screen state was never reached, or
halts a correct run. The unit tests in :mod:`tests.test_replayer` pin specific
scenarios; this module SEARCHES the space of on-screen text sets, query text,
and region_stable parameters with Hypothesis and asserts the invariants that
must hold for EVERY input. Only ``vision`` is faked (the FakeVision pattern from
:mod:`tests.test_replayer`) — no browser or OCR stack is loaded.

Invariants encoded:

1. **Presence wiring is exact (both directions).** ``text_present`` passes IFF
   the text is present; ``text_absent`` passes IFF it is absent. The
   safety-critical direction — ``text_absent`` must NEVER pass while the text IS
   present (e.g. a "the error dialog is gone" check must not green-light while
   the error is still up) — is asserted head-on.

2. **``None`` text is vacuous only in the documented way.** A ``text_present``
   with no ``text`` never passes; a ``text_absent`` with no ``text`` always
   passes (``pc.text is None or not present``).

3. **Evaluation is deterministic.** The same frame + postcondition yields the
   same verdict on repeated calls, across every kind — ``_postcondition_passes``
   carries no hidden state, wall-clock, or randomness.

4. **``region_stable`` is vacuously-true only when region OR phash is missing.**
   With both present (and no template crop) the verdict is exactly
   ``phash_distance <= phash_tolerance`` — it genuinely checks, never passes
   blindly.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from openadapt_flow.ir import Postcondition, PostconditionKind
from openadapt_flow.runtime.replayer import Replayer

pytestmark = pytest.mark.timeout(600)

_MAX = 300

_COMMON_SETTINGS = dict(
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)

_FRAME = b"frame-bytes-ignored-by-fake-vision"
# Constant bundle dir with NO template crops, so region_stable never takes the
# template path (built once; read-only across generated inputs).
_BUNDLE = Path(tempfile.mkdtemp(prefix="postcond_fuzz_bundle_"))


class FakeVision:
    """Scripted vision namespace for postcondition evaluation.

    ``text_present`` returns whether the queried text is in ``present``;
    ``phash_png`` / ``phash_distance`` are scripted for region_stable.
    """

    def __init__(self, present=(), phash_dist=0):
        self.present = set(present)
        self.phash_dist = phash_dist

    def text_present(self, screen_png, text, *, region=None, min_ratio=0.8):
        return text in self.present

    def find_template(
        self,
        screen_png,
        template_png,
        *,
        search_region=None,
        prefer_near=None,
        scales=(0.85, 1.0, 1.18),
        threshold=0.82,
    ):
        return None

    def phash_png(self, png, region=None):
        return "live-hash"

    def phash_distance(self, a, b):
        return self.phash_dist


class FakeBackend:
    def __init__(self, viewport=(300, 200)):
        self._viewport = viewport

    @property
    def viewport(self):
        return self._viewport


def _replayer(vision) -> Replayer:
    return Replayer(FakeBackend(), vision=vision)


def _passes(replayer, pc) -> bool:
    return replayer._postcondition_passes(pc, _FRAME, _BUNDLE, start_state={})


# Short printable text tokens (the "words" a postcondition asserts about).
_TOKENS = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126),
    min_size=1,
    max_size=12,
)


# --------------------------------------------------------------------------- #
# Invariant 1 — presence wiring is exact in both directions
# --------------------------------------------------------------------------- #
@st.composite
def _present_and_query(draw):
    """A present-text set and a query — the query is drawn FROM the set half the
    time so the safety-critical ``present`` branch is exercised, not just the
    (statistically dominant) absent branch."""
    present = draw(st.sets(_TOKENS, min_size=1, max_size=6))
    query = draw(st.one_of(st.sampled_from(sorted(present)), _TOKENS))
    return present, query


@settings(max_examples=_MAX, **_COMMON_SETTINGS)
@given(pq=_present_and_query())
def test_text_present_and_absent_track_presence(pq):
    present, query = pq
    replayer = _replayer(FakeVision(present=present))
    is_present = query in present

    present_pc = Postcondition(kind=PostconditionKind.TEXT_PRESENT, text=query)
    absent_pc = Postcondition(kind=PostconditionKind.TEXT_ABSENT, text=query)

    assert _passes(replayer, present_pc) is is_present, (
        f"text_present verdict != presence for query={query!r} "
        f"present={sorted(present)!r}"
    )
    # Safety-critical: text_absent must NEVER pass while the text is present.
    assert _passes(replayer, absent_pc) is (not is_present), (
        f"WRONG text_absent verdict for query={query!r} present={is_present} "
        f"(present set={sorted(present)!r})"
    )


# --------------------------------------------------------------------------- #
# Invariant 2 — None text is vacuous only in the documented way
# --------------------------------------------------------------------------- #
@settings(max_examples=100, **_COMMON_SETTINGS)
@given(present=st.sets(_TOKENS, max_size=6))
def test_none_text_vacuous_direction(present):
    replayer = _replayer(FakeVision(present=present))
    present_pc = Postcondition(kind=PostconditionKind.TEXT_PRESENT, text=None)
    absent_pc = Postcondition(kind=PostconditionKind.TEXT_ABSENT, text=None)
    # No text to look for: presence can never be confirmed, absence is trivially
    # satisfied (documented behavior).
    assert _passes(replayer, present_pc) is False
    assert _passes(replayer, absent_pc) is True


# --------------------------------------------------------------------------- #
# Invariant 3 — evaluation is deterministic (same input -> same verdict)
# --------------------------------------------------------------------------- #
@st.composite
def _any_postcondition(draw):
    kind = draw(
        st.sampled_from(
            [
                PostconditionKind.TEXT_PRESENT,
                PostconditionKind.TEXT_ABSENT,
                PostconditionKind.REGION_STABLE,
            ]
        )
    )
    if kind in (PostconditionKind.TEXT_PRESENT, PostconditionKind.TEXT_ABSENT):
        return Postcondition(kind=kind, text=draw(st.none() | _TOKENS))
    region = draw(
        st.none()
        | st.tuples(
            st.integers(0, 100),
            st.integers(0, 100),
            st.integers(1, 100),
            st.integers(1, 100),
        )
    )
    phash = draw(
        st.none() | st.text(alphabet="0123456789abcdef", min_size=2, max_size=16)
    )
    tol = draw(st.integers(0, 32))
    return Postcondition(kind=kind, region=region, phash=phash, phash_tolerance=tol)


@settings(max_examples=_MAX, **_COMMON_SETTINGS)
@given(
    pc=_any_postcondition(),
    present=st.sets(_TOKENS, max_size=6),
    dist=st.integers(0, 64),
)
def test_evaluation_is_deterministic(pc, present, dist):
    replayer = _replayer(FakeVision(present=present, phash_dist=dist))
    first = _passes(replayer, pc)
    second = _passes(replayer, pc)
    assert first == second, (
        f"non-deterministic verdict {first} != {second} for pc={pc!r}"
    )


# --------------------------------------------------------------------------- #
# Invariant 4 — region_stable is vacuous only when region OR phash is missing
# --------------------------------------------------------------------------- #
@settings(max_examples=_MAX, **_COMMON_SETTINGS)
@given(
    region=st.none()
    | st.tuples(
        st.integers(0, 100),
        st.integers(0, 100),
        st.integers(1, 100),
        st.integers(1, 100),
    ),
    phash=st.none() | st.text(alphabet="0123456789abcdef", min_size=2, max_size=16),
    dist=st.integers(0, 64),
    tol=st.integers(0, 32),
)
def test_region_stable_vacuous_only_when_incomplete(region, phash, dist, tol):
    replayer = _replayer(FakeVision(phash_dist=dist))
    pc = Postcondition(
        kind=PostconditionKind.REGION_STABLE,
        region=region,
        phash=phash,
        phash_tolerance=tol,
    )
    verdict = _passes(replayer, pc)

    if region is None or phash is None:
        # Documented vacuous pass: no recorded reference to check against.
        assert verdict is True, (
            f"expected vacuous True for region={region!r} phash={phash!r}"
        )
    else:
        # Genuine check: exactly the phash-distance tolerance test (no template
        # crop configured, so the template short-circuit never fires). In
        # particular it must NOT pass vacuously when the region has drifted
        # beyond tolerance.
        assert verdict is (dist <= tol), (
            f"region_stable verdict {verdict} != (dist<=tol) for dist={dist} tol={tol}"
        )
