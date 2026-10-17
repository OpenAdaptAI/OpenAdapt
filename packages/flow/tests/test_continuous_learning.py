"""Continuous skill learning: the versioned, governed learn/promote loop.

Exercises the loop end-to-end over a SYNTHETIC drift stream (no live app, no
model calls):

- a new dialog appears mid-stream -> the loop detects the novelty, induces +
  validates + PROMOTES a revised program that covers it (and the version history
  reflects it);
- a batch of only noise/failures (no novel successful structure) -> NO promotion
  (stability);
- a candidate that would REGRESS identity / effect / risk (via a rigged inducer)
  -> REJECTED by the regression gate reused from PR #70, active version retained;
- a novelty the (additive) inducer cannot cover -> the canary refuses the
  inadequate candidate;
- the symbolic Phase-2 coverage interpreter and the versioned SkillLibrary units.

The inducer is always a deterministic STUB (the reference structural inducer, or
a purpose-built rigged one) -- the real multi-trace inducer is a sibling PR wired
in at merge via the ``Inducer`` Protocol.
"""

from __future__ import annotations

from typing import Optional

import pytest

from openadapt_flow.ir import (
    ProgramGraph,
    StateKind,
    Step,
)
from openadapt_flow.learning import (
    ExecutionTrace,
    SkillLibrary,
    cluster_traces,
    learn_from_traces,
    program_regression_gate,
    program_reproduces,
)
from openadapt_flow.learning.loop import Inducer
from openadapt_flow.learning.synth_stream import (
    Drift,
    StructuralDiffInducer,
    generate_stream,
    mockmed_base_program,
)
from openadapt_flow.runtime.identity_template import build_identity_template

# -- stub inducers ------------------------------------------------------------


class _NoOpInducer:
    """Returns the base program unchanged (a stub that learns nothing)."""

    def induce(
        self, traces: list[ExecutionTrace], *, base: Optional[ProgramGraph] = None
    ) -> ProgramGraph:
        assert base is not None
        return base.model_copy(deep=True)


class _RiggedInducer:
    """A stub that emits a WEAKENED revision.

    It takes the reference structural revision (which correctly covers the new
    dialog) and then silently damages one safety property of a SURVIVING step --
    the exact class of regression PR #70's gate exists to refuse. Which property
    is chosen by ``mode``: ``identity`` drops the armed patient row's context
    band, ``effect`` drops the SAVE step's ``record_written`` effect, ``risk``
    downgrades SAVE from irreversible to reversible.
    """

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self._ref = StructuralDiffInducer()

    def induce(
        self, traces: list[ExecutionTrace], *, base: Optional[ProgramGraph] = None
    ) -> ProgramGraph:
        graph = self._ref.induce(traces, base=base)
        for state in graph.states.values():
            if state.kind is not StateKind.ACTION or state.step is None:
                continue
            step: Step = state.step
            if self.mode == "identity" and step.id == "s_open" and step.anchor:
                step.anchor.context_text = None
            elif self.mode == "effect" and step.id == "s_save":
                step.effects = []
            elif self.mode == "risk" and step.id == "s_save":
                step.risk = "reversible"
        return graph


@pytest.fixture
def library(tmp_path):
    lib = SkillLibrary(tmp_path / "skills")
    lib.create_skill("mockmed", mockmed_base_program())
    return lib


# -- the Inducer Protocol -----------------------------------------------------


def test_stub_inducers_satisfy_protocol():
    assert isinstance(StructuralDiffInducer(), Inducer)
    assert isinstance(_NoOpInducer(), Inducer)
    assert isinstance(_RiggedInducer("identity"), Inducer)


# -- coverage interpreter -----------------------------------------------------


def test_base_program_reproduces_baseline_but_not_drift():
    base = mockmed_base_program()
    stream = generate_stream(n_baseline=3, n_drift=3, drift=Drift.CONSENT_DIALOG)
    baseline = [t for t in stream if not t.facts]
    drifted = [t for t in stream if t.facts]

    assert all(program_reproduces(base, t).reproduced for t in baseline)
    for t in drifted:
        r = program_reproduces(base, t)
        assert not r.reproduced
        assert "Acknowledge consent" in r.reason


def test_revised_program_reproduces_both_variants():
    base = mockmed_base_program()
    stream = generate_stream(n_baseline=3, n_drift=3, drift=Drift.CONSENT_DIALOG)
    revised = StructuralDiffInducer().induce(
        [t for t in stream if t.succeeded], base=base
    )
    # The revised program covers BOTH the no-dialog and dialog runs: the new
    # step is guarded (skip) so it is a no-op when the dialog is absent.
    for t in stream:
        assert program_reproduces(revised, t).reproduced, t.trace_id


def test_coverage_gap_on_leftover_actions():
    """A trace that does MORE than the program has states for is not reproduced
    (the loop must never call a partial match 'covered')."""
    base = mockmed_base_program()
    extra = generate_stream(n_baseline=1, n_drift=0)[0]
    extra.steps.append(extra.steps[-1].model_copy())  # an unexplained extra action
    r = program_reproduces(base, extra)
    assert not r.reproduced
    assert "unconsumed" in r.reason


# -- clustering ---------------------------------------------------------------


def test_cluster_partitions_success_failure_and_novelty():
    base = mockmed_base_program()
    stream = generate_stream(
        n_baseline=4, n_drift=3, drift=Drift.CONSENT_DIALOG, n_failures=2
    )
    clusters = cluster_traces(stream, base)
    assert len(clusters.successes) == 7
    assert len(clusters.failures) == 2
    assert clusters.has_novelty
    # exactly one novel signature (the consent-dialog variant), 3 traces of it
    assert len(clusters.novel_variants) == 1
    assert len(clusters.novel_traces()) == 3


def test_baseline_only_has_no_novelty():
    base = mockmed_base_program()
    stream = generate_stream(n_baseline=5, n_drift=0)
    clusters = cluster_traces(stream, base)
    assert not clusters.has_novelty


# -- the learn/promote loop: PROMOTE ------------------------------------------


def test_new_dialog_midstream_is_detected_and_promoted(library):
    """The headline case: a new consent dialog appears mid-stream; the loop
    detects novelty, induces a revision, validates it, and PROMOTES it."""
    stream = generate_stream(n_baseline=6, n_drift=6, drift=Drift.CONSENT_DIALOG)

    # Feed the clean prefix: nothing to learn.
    pre = learn_from_traces(
        library, "mockmed", stream[:6], inducer=StructuralDiffInducer()
    )
    assert pre.action == "no_change"
    assert library.active_version("mockmed").version == 1

    # Feed the drifted tail: detect + induce + validate + promote.
    out = learn_from_traces(
        library, "mockmed", stream[6:], inducer=StructuralDiffInducer()
    )
    assert out.action == "promoted"
    assert out.coverage_after > out.coverage_before
    assert out.gate is not None and out.gate.passed

    # The new active version reproduces BOTH variants.
    active = library.active_version("mockmed")
    assert active.version == 2
    for t in stream:
        assert program_reproduces(active.graph, t, subflows=active.subflows).reproduced


def test_promotion_persists_across_reload(library, tmp_path):
    stream = generate_stream(n_baseline=4, n_drift=4, drift=Drift.CONSENT_DIALOG)
    learn_from_traces(library, "mockmed", stream, inducer=StructuralDiffInducer())
    assert library.active_version("mockmed").version == 2

    # A fresh library over the same root sees the promoted version + lineage.
    reloaded = SkillLibrary(tmp_path / "skills")
    skill = reloaded.get("mockmed")
    assert [(v.version, v.status) for v in skill.versions] == [
        (1, "superseded"),
        (2, "active"),
    ]
    assert reloaded.active_version("mockmed").validation_score > 0


# -- the learn/promote loop: STABILITY (no promotion on noise) ----------------


def test_noise_and_failures_do_not_promote(library):
    """A batch of only failures + already-covered successes must NOT revise the
    program (no chasing red runs)."""
    stream = generate_stream(n_baseline=4, n_drift=0, n_failures=4)
    out = learn_from_traces(library, "mockmed", stream, inducer=StructuralDiffInducer())
    assert out.action == "no_change"
    assert library.active_version("mockmed").version == 1
    # Only the bootstrap version exists; no candidate was even created.
    assert len(library.get("mockmed").versions) == 1


def test_noop_inducer_novelty_but_no_coverage_gain_is_rejected(library):
    """Novelty is present but the (no-op) inducer produces no improvement -> the
    canary refuses to promote; active version retained."""
    stream = generate_stream(n_baseline=4, n_drift=4, drift=Drift.CONSENT_DIALOG)
    out = learn_from_traces(library, "mockmed", stream, inducer=_NoOpInducer())
    assert out.action == "quarantined"
    assert "canary" in out.reason.lower()
    assert library.active_version("mockmed").version == 1


# -- the learn/promote loop: GATE rejects a regressing revision ---------------


@pytest.mark.parametrize("mode", ["identity", "effect", "risk"])
def test_regressing_revision_is_rejected_by_gate(library, mode):
    """A rigged inducer that would weaken identity / effect / risk on a surviving
    step is REJECTED by the regression gate (reused from PR #70); the active
    version is retained and the candidate is quarantined with the reason."""
    stream = generate_stream(n_baseline=5, n_drift=5, drift=Drift.CONSENT_DIALOG)
    out = learn_from_traces(library, "mockmed", stream, inducer=_RiggedInducer(mode))
    assert out.action == "quarantined"
    assert out.gate is not None and not out.gate.passed
    assert f"{mode} regression" in " ".join(out.gate.failures)

    # Active version unchanged; the rejected candidate is recorded as rolled_back
    # with its reason (audit trail), never applied.
    assert library.active_version("mockmed").version == 1
    versions = library.get("mockmed").versions
    rejected = [v for v in versions if v.status == "rolled_back"]
    assert len(rejected) == 1
    assert rejected[0].reason


def test_uncoverable_drift_is_refused(library):
    """A structural drift the additive inducer cannot express (a renamed field):
    novelty is detected but the candidate still fails to cover it -> refused."""
    stream = generate_stream(n_baseline=4, n_drift=4, drift=Drift.FIELD_RENAME)
    out = learn_from_traces(library, "mockmed", stream, inducer=StructuralDiffInducer())
    assert out.action == "quarantined"
    assert library.active_version("mockmed").version == 1


# -- version history ----------------------------------------------------------


def test_version_history_is_correct_across_multiple_cycles(library):
    """Two drift epochs: after each promotion the prior active is superseded, a
    rejected candidate is rolled_back, and exactly one version stays active."""
    # Epoch 1: consent dialog -> promote v2.
    s1 = generate_stream(
        n_baseline=4, n_drift=4, drift=Drift.CONSENT_DIALOG, prefix="e1"
    )
    learn_from_traces(library, "mockmed", s1, inducer=StructuralDiffInducer())
    assert library.active_version("mockmed").version == 2

    # A rejected candidate in between: a fresh variant that is novel to v2 (a
    # renamed field), with a rigged inducer that also regresses an effect.
    s_rename = generate_stream(
        n_baseline=0, n_drift=3, drift=Drift.FIELD_RENAME, prefix="e2"
    )
    out_bad = learn_from_traces(
        library, "mockmed", s_rename, inducer=_RiggedInducer("effect")
    )
    assert out_bad.action == "quarantined"

    skill = library.get("mockmed")
    statuses = {v.version: v.status for v in skill.versions}
    assert statuses[1] == "superseded"
    assert statuses[2] == "active"
    assert statuses[3] == "rolled_back"  # the rejected candidate
    # Exactly one active version at all times.
    assert sum(1 for v in skill.versions if v.status == "active") == 1


def test_provenance_records_parentage_and_traces(library):
    stream = generate_stream(n_baseline=4, n_drift=4, drift=Drift.CONSENT_DIALOG)
    learn_from_traces(library, "mockmed", stream, inducer=StructuralDiffInducer())
    v2 = library.get("mockmed").by_version(2)
    assert v2 is not None
    assert v2.provenance.parent_version == 1
    assert v2.provenance.trace_ids  # the fit traces it was induced from


# -- the program-level gate unit (direct reuse of PR #70) ---------------------


def test_program_gate_passes_for_additive_revision():
    """Adding a new (unarmed, reversible) optional step regresses nothing on the
    surviving steps -> the gate passes."""
    base = mockmed_base_program()
    stream = generate_stream(n_baseline=2, n_drift=2, drift=Drift.CONSENT_DIALOG)
    revised = StructuralDiffInducer().induce(
        [t for t in stream if t.succeeded], base=base
    )
    report = program_regression_gate(base, revised)
    assert report.passed
    # Every original step survives; the gate ruled on each.
    assert len(report.per_step) == 5
    assert not report.removed


def test_program_gate_allows_locator_change_on_unchanged_template_armed_step():
    base = mockmed_base_program()
    old_anchor = base.states["s_open"].step.anchor
    assert old_anchor is not None
    tmpl = build_identity_template(
        old_anchor.context_text,
        salt_hex="22" * 16,
    )
    assert tmpl is not None
    old_anchor.context_text = None
    old_anchor.identity_template = tmpl

    revised = base.model_copy(deep=True)
    revised_anchor = revised.states["s_open"].step.anchor
    assert revised_anchor is not None
    revised_anchor.region = (125, 90, 20, 20)
    revised_anchor.click_point = (135, 100)

    report = program_regression_gate(base, revised)
    assert report.passed, report.failures
    verdict = next(v for v in report.per_step if v.step_id == "s_open")
    assert verdict.result.identity_ok is True


def test_program_gate_flags_removed_armed_step():
    """Dropping the identity-armed patient-selection step is surfaced as a removed
    armed step (a coverage change the loop can review, not silently accept)."""
    base = mockmed_base_program()
    trimmed = base.model_copy(deep=True)
    # Rewire s_login straight to s_encounter and drop s_open entirely.
    trimmed.states["s_login"].transitions[0].target = "s_encounter"
    del trimmed.states["s_open"]
    report = program_regression_gate(base, trimmed)
    assert "s_open" in report.removed
    assert "s_open" in report.armed_removed
