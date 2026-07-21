# Publication options for the workflow-program IR

This note weighs whether and how to publish the workflow-program IR
(`docs/design/WORKFLOW_PROGRAM_IR.md`). It covers the paper appendix, a standalone
spec, an external venue, and doing nothing, with tradeoffs and a recommendation.
It also records the source-availability boundary that constrains any of them.

## What the IR is, for publication purposes

The IR is a mechanism and an interface: a typed state machine for demonstrated
GUI work, its operational semantics, and a runtime that walks it deterministically.
It is not a dataset, a tuned threshold, or a customer-specific recipe. Under the
open-core boundary in `AGENTS.md` section 4.2, mechanism and interface are public
and data, recipes, and empirical tuning are private. The IR falls entirely on the
public side. The Pydantic models are already in the public MIT engine
(`openadapt_flow/ir.py`), so publishing the specification exposes nothing new.

The line to hold: describe the IR, the semantics, and MockMed-shaped examples.
Do not include real system-of-record oracle recipes, deployment-derived
thresholds, the hardening failure corpus, tuned adversary parameters, or
real-EMR-tied datasets. Those are the crown jewels and stay private regardless of
which publication option is chosen.

## Option A: appendix in the arXiv paper

The paper (`paper/`) already introduces the IR in the system section: it states
that the linear trace is the simplest program and that the IR also supports
states, guarded transitions, branches, loops, and subflows, with
quarantine-on-underdetermined. An appendix would give the full state kinds,
transition-selection rule, effect verdicts, and the degenerate-lift equivalence.

Pros:

- One artifact for reviewers. The safety argument in the paper (refuse rather
  than guess) is stronger when the reader can see the fail-safe semantics stated
  precisely rather than described.
- Zero extra venue work. It rides the submission already in progress.
- The degenerate-lift equivalence and the zero-model-call invariant are natural
  formal content for a systems reader and support the paper's central claims.

Cons:

- Page budget. The full IR is long. An appendix has to compress to the semantics
  and the schema summary, not the full document.
- The paper is a moving target with its own submission blockers (author list,
  arXiv category, disclosure review). Coupling the IR's first public writeup to
  that timeline delays it.

## Option B: standalone versioned spec in the repo

Keep `WORKFLOW_PROGRAM_IR.md` as the canonical spec, add a published JSON Schema
(`Workflow.model_json_schema()` written to a versioned file per release), and add
conformance tests for the section 6 semantics. Link it from the README and the
paper.

Pros:

- It already exists and passes the docs-consistency gate. The marginal cost is a
  schema export and a small test suite.
- A versioned, diffable schema is the thing integrators actually need. It is more
  useful to a downstream implementer than a frozen PDF appendix.
- It tracks the code. The paper is a snapshot; the repo spec stays current as the
  IR evolves (for example when a real effect probe lands).

Cons:

- Lower academic visibility on its own. A repo doc is not indexed or cited the way
  a paper or a tech report is.
- Needs light maintenance discipline so the spec and the models do not drift. The
  proposed conformance tests address this.

## Option C: external venue (systems or PL)

The IR sits in a real research lineage: programming-by-demonstration, program
synthesis from demonstrations (WebRobot, PLDI 2022), and FSM skill induction
(Skill-DisCo). A focused paper could target a workshop or a short-paper track at a
PL or HCI venue (for example a PLDI or UIST workshop), framed around one
contribution: a fail-safe, zero-model-call execution semantics for demonstrated
GUI programs, with quarantine-on-underdetermined induction.

Pros:

- Best fit for the intellectual contribution. The semantics and the induction
  quarantine rule are the novel parts, and a PL or HCI audience is the right one.
- Citable and reviewed, which helps credibility with regulated buyers and
  collaborators.

Cons:

- Real cost. A separate paper needs an evaluation section beyond the main paper's
  scope, most likely an induction study over multiple traces with a measured
  quarantine and error rate.
- Overlap risk with the main paper. Two papers from the same system need a clean
  split of claims to avoid self-competition.
- The strongest evaluation depends on a real workflow, which is the same customer
  dependency the roadmap already calls out.

## Option D: nothing beyond the current doc

Leave the spec in the repo, unversioned, and mention it only in the README and the
paper's system section, as today.

Pros: no work.

Cons: the spec keeps drifting from the code (the reason for this rewrite), and the
IR's clearest asset, a precise public semantics, stays underused.

## Recommendation

Do Option B now and Option A next, and hold Option C until there is a real
workflow.

1. **Now (Option B).** Keep this document as the canonical spec. Add two small,
   clearly-correct pieces: export the JSON Schema to a versioned file on release,
   and add table-driven conformance tests for the section 6 semantics (transition
   selection, fail-safe halting, loop bounds, effect verdicts). This is low risk,
   it is fully public under section 4.2, and it makes the spec load-bearing for
   integrators instead of decorative.
2. **Next (Option A).** Add a compact IR appendix to the arXiv paper: the state
   kinds, the transition-selection and effect-verdict rules, and the degenerate-lift
   equivalence. This costs little, strengthens the paper's safety argument, and
   gives the IR a citable home without waiting on a second submission.
3. **Later (Option C), conditional.** Once a real customer workflow exists and an
   induction study can report a measured quarantine and error rate, a focused PL
   or HCI workshop paper on the induction semantics is worth it. Not before: the
   contribution that would carry it (induction that refuses rather than guesses on
   real workflows) is exactly the part that needs real data to evaluate honestly.

The through-line matches the source-availability boundary. Publish the mechanism
and the interface freely, in whatever venue serves the reader. Keep the data,
the oracle recipes, and the tuning private. The moat is data, control plane,
trust, and speed, not secrecy about the IR.
</content>
