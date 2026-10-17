"""CI gate for the vision-hardening flywheel — the monotone silent-wrong ratchet.

These tests are the enforcement half of the flywheel
(``openadapt_flow/validation/hardening/``). They run the REAL resolver over the
TEMPLATE-tier fixture x perturbation sweep (font-free, cv2-deterministic, ~5s)
and assert:

1. the sweep is deterministic (the ratchet is meaningful),
2. every fixture resolves CORRECTLY on the unperturbed control (the resolver is
   not broken, and a future hardening fix has not over-hardened it into halting
   on clean input),
3. **the silent-wrong count does not exceed the committed ratchet** — the
   monotone gate: it can only ever go DOWN,
4. the committed failure corpus is faithful to HEAD (freeze discipline), and
5. the seeded adversarial search deterministically finds confident-wrong cases.

The OCR tier is exercised by the CLI (``python -m
openadapt_flow.validation.hardening``) and reported in ``results.json`` but is
NOT gated here (RapidOCR output varies across engine/platform versions).
"""

from __future__ import annotations

import pytest

from openadapt_flow.validation.hardening import corpus as C
from openadapt_flow.validation.hardening import fixtures as fx
from openadapt_flow.validation.hardening import harness as H
from openadapt_flow.validation.hardening.harness import Outcome
from openadapt_flow.validation.hardening.perturbations import identity

pytestmark = pytest.mark.timeout(600)


@pytest.fixture(scope="module")
def template_rows() -> list[H.ResultRow]:
    """The template-tier sweep, computed once for the whole module."""
    return H.sweep(include_ocr=False)


def test_template_sweep_is_deterministic() -> None:
    """The ratchet is only meaningful if the sweep is reproducible."""
    a = H.summarize(H.sweep(include_ocr=False)).counts
    b = H.summarize(H.sweep(include_ocr=False)).counts
    assert a == b, f"non-deterministic template sweep: {a} != {b}"


def test_identity_control_resolves_correctly() -> None:
    """Every fixture must resolve on its OWN unperturbed frame.

    This is the over-hardening guard: a future safety fix that makes the ladder
    HALT on ambiguity must still resolve the true target when it is present and
    undegraded, or it has broken availability on the happy path. It also proves
    the harness fixtures are well-formed (the recorded template really locates
    the true target)."""
    control = identity()
    fixtures = list(fx.template_tier_fixtures())
    if fx.ocr_available():
        fixtures += list(fx.ocr_tier_fixtures())
    for fixture in fixtures:
        row = H.classify_case(fixture, control)
        assert row.outcome is Outcome.CORRECT, (
            f"resolver failed on the UNPERTURBED control for {fixture.key()}: "
            f"{row.outcome.value} (rung={row.rung}, dist_true={row.dist_true})"
        )


def test_silent_wrong_ratchet(template_rows: list[H.ResultRow]) -> None:
    """THE monotone gate: silent-wrong may not exceed the committed ratchet.

    A change that raises the silent-wrong count fails here. A hardening fix that
    LOWERS it must regenerate the corpus (``python -m
    openadapt_flow.validation.hardening --write``) so the ratchet ratchets down.
    """
    measured = sum(1 for r in template_rows if r.outcome is Outcome.SILENT_WRONG)
    ceiling = C.ratchet_max()
    assert measured <= ceiling, (
        f"SILENT-WRONG REGRESSION: template-tier silent-wrong rose to "
        f"{measured}, above the committed ratchet {ceiling}. A change increased "
        f"the resolver's silent-mis-resolution rate. If this is intentional it "
        f"must be justified and the ratchet raised in a reviewed decision; "
        f"otherwise fix the regression."
    )
    # Also assert the corpus ceiling equals its own case count (self-consistent).
    doc = C.load_corpus()
    assert doc["ratchet_max_silent_wrong"] == len(doc["cases"])


def test_committed_corpus_is_faithful_to_head(
    template_rows: list[H.ResultRow],
) -> None:
    """Freeze discipline: the committed corpus is exactly the current
    template-tier silent-wrong set.

    The public synthetic baseline demonstrates the same freeze contract used by
    the private grown-corpus pack: a resolver change that flips a known
    silent-wrong to safe (the goal!) or introduces a new one must
    regenerate the corpus in the same reviewed change, so the catalog never
    silently drifts from reality. If this fails after a deliberate resolver
    change, run ``python -m openadapt_flow.validation.hardening --write``.
    """
    measured = {
        C.row_key(r) for r in template_rows if r.outcome is Outcome.SILENT_WRONG
    }
    committed = C.corpus_case_keys()
    missing = committed - measured  # committed cases that no longer reproduce
    extra = measured - committed  # newly discovered, uncatalogued cases
    assert not extra, (
        f"{len(extra)} NEW silent-wrong case(s) not in the corpus (regenerate "
        f"with `python -m openadapt_flow.validation.hardening --write`): "
        f"{sorted(extra)[:3]}"
    )
    assert not missing, (
        f"{len(missing)} corpus case(s) no longer reproduce (a fix? regenerate "
        f"the corpus + lower the ratchet): {sorted(missing)[:3]}"
    )


def test_adversarial_search_finds_hits_deterministically() -> None:
    """The attacker works: a bounded seeded search finds confident-wrong cases,
    reproducibly.

    Targets a fixture that STILL has a residual silent-wrong after the
    locality/uniqueness gate + the local-rung and global-suspicion hardening
    (``bars`` n=3, true_idx=0 under deep left-region drift: the true target
    blurs below the ambiguity-suspicion floor while a crisp decoy remains, so it
    reads as a legitimately moved unique target). The attacker must still surface
    it -- if a future fix closes it, this fixture stops producing hits and the
    test should be re-pointed at whatever the frozen corpus still lists.
    """
    fixture = fx.repeated_icons(n=3, true_idx=0, glyph="bars")
    # Seed 1 reaches a committed local-drift corpus case in the bounded search.
    # The harness derives its PRNG state with SHA-256, so this sequence is
    # identical even when Python's per-process hash randomization changes.
    hits_a = H.adversarial_search(fixture, iters=30, seed=1)
    hits_b = H.adversarial_search(fixture, iters=30, seed=1)
    assert hits_a, "adversarial search found no silent-wrong on a look-alike surface"
    assert [h.perturbation.key() for h in hits_a] == [
        h.perturbation.key() for h in hits_b
    ], "adversarial search is not deterministic for a fixed seed"
    # Every reported hit is genuinely a silent-wrong.
    assert all(h.row.outcome is Outcome.SILENT_WRONG for h in hits_a)


def test_results_snapshot_declares_template_tier_gated() -> None:
    """results.json exists, parses, and marks the template tier as CI-gated."""
    import json

    doc = json.loads(C.results_path().read_text())
    assert doc["template_tier"]["gated_in_ci"] is True
    assert doc["template_tier"]["silent_wrong"] == C.ratchet_max()


def test_corpus_dir_defaults_to_public_and_is_env_overridable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    """Public default keeps CI green; the private corpus is opt-in via env.

    The public synthetic baseline is the default so public tests + the
    credibility story run without the private OpenAdaptAI/openadapt-corpus repo.
    Setting OPENADAPT_HARDENING_CORPUS_DIR points internal ratchet runs at a
    private corpus, but that path is never a build- or import-time dependency.
    """
    monkeypatch.delenv(C.CORPUS_DIR_ENV, raising=False)
    assert C.corpus_dir() == C.public_corpus_dir()
    assert C.corpus_path().is_file()  # committed public baseline is present

    override = str(tmp_path)  # type: ignore[arg-type]
    monkeypatch.setenv(C.CORPUS_DIR_ENV, override)
    from pathlib import Path

    assert C.corpus_dir() == Path(override)
    assert C.corpus_path() == Path(override) / "corpus.json"
    assert C.results_path() == Path(override) / "results.json"
