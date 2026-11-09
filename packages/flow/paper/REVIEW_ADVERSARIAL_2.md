# Adversarial Peer Review 2 — the independent-harness, statistics, second-domain, and benchmark lens

**Reviewer stance:** harsh program-committee member at NeurIPS main and a
Datasets & Benchmarks (D&B) reviewer, reviewing the *strengthened* draft that
introduces a genuinely independent end-to-end silent-wrong-effect harness plus a
second (lending) domain and a standalone benchmark package.

**This is a review-only deliverable.** Nothing here is fixed; every finding is
for the remedy agent.

---

## 0. Which version I reviewed (READ THIS FIRST)

I reviewed branch **`feat/effect-e2e-independent-swer`** at HEAD **`e7f9aab`**
(`paper/main.tex` + `paper/sections/*` + `paper/workshop/main.tex`), and I
cross-read the two contributions that are supposed to complete the
strengthening but live on *separate, unmerged* branches:

- **PR #208** `feat/lending-effect-domain-20260721` — the MockLoan second domain
  (`benchmark/lending_fault_model/`, `openadapt_flow/mockloan/`).
- **PR #205** `feat/effectbench-standalone-20260721` — the packaged benchmark
  (`benchmark/effectbench/`).

**Central meta-finding: the reconciliation is INCOMPLETE, and the paper as it
stands is internally contradicted by its own repository.** The real, independent
numbers now exist and are machine-checked *as an artifact*
(`benchmark/effect_e2e/results.json`; `paper/check_artifacts.py` lines
185–205 assert screen `54`, REST-oracle `9`, complete-read-path `0`), and the
harness's own README (`benchmark/effect_e2e/EFFECT_E2E.md`, lines 5–11) states in
writing that the *old* benchmark's "`0/90` is circular by construction." **Yet
the paper prose, the abstract, both headline figures, and the prose-binding half
of `check_artifacts.py` still cite the discredited circular `50 of 90 → 0 of
90`:**

- `paper/main.tex:55` (abstract): "silently accepted 50 of 90 fault runs … the
  effect check caught every one (0 of 90)."
- `paper/sections/01_introduction.tex:39`, `05_results.tex:9,42–48`,
  `03_governance.tex:65` — all `50 … 0 of 90`.
- `paper/check_artifacts.py:133–176` still binds the prose to
  `benchmark/silent_wrong_action/results.json` (`screen=50`, `effect=0`), and
  lines 451–463 bind the *workshop* prose to the same circular file.

So I reviewed the paper **as written** (still circular in the prose) while
**accounting for the incoming real numbers** (I do not re-litigate the
already-diagnosed circularity of the old benchmark — the first review and the
authors' own README already own it). My review targets (a) whether the *new*
independent harness actually earns its "non-circular" claim, (b) the statistics,
(c) the second domain, (d) EffectBench as a D&B artifact, and (e) the residual
overclaim that survives even a *correct* reconciliation. Where I say "the paper
should," I mean the reconciled paper the remedy agent will produce.

---

## 1. Summary of contributions (as I restate them)

1. A demonstration compiler + backend-neutral IR that turns one GUI recording
   into a deterministic, zero-model-call replay program (browser reference
   backend; scoped Windows-UIA / macOS / RDP mechanisms).
2. A resolution ladder (structured → template → OCR → geometry → optional
   grounding model) that records drift repairs as reviewable patches.
3. **System-of-record effect verification as a safety instrument** — the paper's
   real thesis — reframing "silent wrong action" as a first-class, measurable
   failure mode that screen-only agents and selector RPA never score.
4. A now-*independent* end-to-end harness for that instrument
   (`benchmark/effect_e2e/`), a second (lending) domain, and a packaged
   benchmark (EffectBench / SWER) with a recompute-and-reject leaderboard.
5. An evaluation protocol that reports task success *jointly with* silent
   incorrect success and over-halt, plus machine-checked paper constants.

## 2. Strengths (real, and improved since the first review)

- **The independent harness is a genuine methodological upgrade.** Unlike the
  in-process benchmark it replaces, `benchmark/effect_e2e/` drives every write
  through the *real* `Replayer → ApiActuator → HTTP → on-disk SQLite` path, and
  the ground-truth judge (`ground_truth.py`) opens the SQLite file on its own
  read-only connection, bypasses the service, and classifies with its own
  before/after logic. The write's HTTP success flag never reaches the judge.
  This is the right architecture and directly answers the first review's 3.2.
- **Unusual, commendable candor in the artifacts.** `EFFECT_E2E.md` explicitly
  labels the old result circular and reports what *still* slips through the
  realistic single-surface oracle (`collateral_unaudited`, 9/90). Self-indicting
  documentation of this quality is rare.
- **EffectBench has real D&B hygiene** (PR #205): a goal-only SUT interface
  (`adapter.py` — "NEVER a step list … a fairness requirement"), an oracle not
  reachable through the env handle, Wilson intervals, mandatory joint over-halt
  reporting, per-`(category × substrate)` decomposition, and a `score`
  sub-command that recomputes every headline from raw rows and rejects mismatches
  (`LEADERBOARD.md`). This is materially more rigorous than the main paper's own
  statistics.
- **The writing is disciplined.** Scope disclaimers ("field, not production,"
  "one run is not a reliability claim," "does not transfer to Citrix") are
  present throughout and are mostly honest.

---

## 3. Weaknesses and major concerns (cross-checked to code)

### 3.1 The reconciliation is unfinished — the paper contradicts its own repo (BLOCKER)
As documented in §0, the prose/abstract/figures/workshop and half of the
machine-check still assert the circular `50→0`. Submitting this as-is is a
desk-reject risk: a reviewer who opens the repo finds a README stating the
headline is "circular by construction." **The remedy agent must migrate the
prose to the `effect_e2e` numbers, update `fig:silentbar` coordinates
(`05_results.tex:42–43`: `50→54`, `10→9`) and `fig:oracle`
(`03_governance.tex:65`), and repoint `check_artifacts.py:133–176,451–463` from
`silent_wrong_action` to `effect_e2e`.** Until then, no number in the safety
headline is trustworthy on its face.

### 3.2 "Non-circular" is earned at the code level but NOT at the specification level — and the ground truth is itself a closed 2-table world (MAJOR)
The three-path separation (write / out-of-band verifier / ground truth) is real
at the level of *connections and code paths*, but two residual couplings mean the
`0/90` is still partly definitional, one meta-level up from the old circularity:

1. **Shared audit primitive.** `ground_truth.py:29` imports
   `audit_table_deltas` from `openadapt_flow.runtime.effects.sql` — *the same
   function the composite SQL effect verifier uses* (`verifiers.py` routes to
   `SqlRecordVerifier`; the README, `EFFECT_E2E.md:25–26`, confirms both consume
   the kit's `audit_table_deltas`). So the "complete SQL read path" effect arm
   and the privileged ground truth share a code dependency *and the same audited
   table set*. The `effect_full` arm reaching `0/90` against a ground truth built
   on the same primitive is close to tautological: it says "an oracle that audits
   exactly the tables the judge audits agrees with the judge."
2. **The judge's world is two tables.** `ground_truth.py:31`,
   `_AUDITED_TABLES = ("encounters", "billing")`. Any mutation outside those two
   tables (an audit-log row, a third clinical surface, an outbound HL7/queue
   write, a filesystem/side-channel effect) is **invisible to BOTH the
   complete-read-path arm AND the ground truth**. The harness's honesty note is
   asymmetric: it flags that the *REST oracle* "catches exactly what its read
   path can read," but never says the same limit binds the ground truth itself.
   The `0/90` therefore means "zero silent-wrong-effects *in a closed
   two-table world*," not "zero silent-wrong-effects." A NeurIPS reviewer will
   read the headline "reduced to zero" and this caveat is load-bearing.
3. **Shared correctness spec.** Both the effect contract and the judge encode
   the same business intent (`TARGET_PATIENT`/`TARGET_TYPE`/`intended_note`,
   `ground_truth.py:96–175`). Independence of *code* is not independence of
   *specification*; a wrong shared spec (e.g. a fault class no one thought to
   define) is invisible to all three paths. This is exactly the "unreported
   fault classes the harness cannot see" the review brief asks about — and there
   is at least one structural blind spot (surfaces beyond `encounters`/`billing`)
   that the paper must disclose.

### 3.3 The realistic number is 9/90 (10%), not 0/90 — and the paper must not headline the least-realistic arm (MAJOR)
There are three arms (`EFFECT_E2E.md:30–34`): screen `54/90` (60%), out-of-band
REST record oracle `9/90` (10%), complete SQL read-path `0/90`. **The `0/90`
comes only from the `effect_full` arm, whose read path audits *every* mutable
table — the least realistic deployment**, and (per §3.2) the same closed world
as the judge. The realistic condition an actual customer ships — one out-of-band
record oracle over the surface the workflow touches — is **`9/90` = 10% residual
silent-wrong**, driven entirely by one fault class (`collateral_unaudited`,
`check_artifacts.py:196–200`). A reconciliation that merely swaps `50→54` and
keeps "fell to zero" would be presenting the best-case, closed-world arm as *the*
result — a fresh overclaim. **The honest headline is a ladder: 60% → 10% → 0%,
with the middle number foregrounded as the realistic effect-verification
outcome and the `0%` explicitly conditioned on a read path that covers every
mutable surface.** This is the single most important framing decision in the
reconciliation.

### 3.4 Statistical rigor: "90 runs" is ~9–10 deterministic scenarios, not 90 independent trials (MAJOR)
The study has **zero sampling variance by construction**: localhost, no model
calls, deterministic fault injection. The old prose even admits "10 consistent
repeats per class." Ten identical deterministic replays of one scenario are one
observation with multiplicity 10, not ten Bernoulli trials. Consequences:

- Reporting "silent-wrong-effect **rate** 60.0%" and "`54/90`" implies a
  population estimate with sampling error; it is really a **coverage matrix**
  over 6 differentiating fault classes × 9 repeats. Confidence intervals are
  vacuous here (variance ≈ 0) — which the paper should *say*, rather than
  waving at "we report run counts rather than confidence intervals"
  (`04_methodology.tex:25`) as if that were a neutral choice.
- The entire screen-vs-effect *difference* (`54→9`) rests on **6 fault
  classes**, and the entire REST-vs-full difference (`9→0`) rests on **exactly
  one** (`collateral_unaudited`). A reader should be told the result is driven by
  a handful of hand-authored classes, not a distribution.
- **Fault distribution is hand-picked, not sampled from any incidence model.**
  The taxonomy (partial/duplicate/optimistic/stale/wrong-record/collateral/…) is
  reasonable and defensible (`gray1981transaction` lineage), but there is no
  claim or evidence that these frequencies resemble any real EMR/lending
  incident distribution. The paper should frame SWER as *fault coverage under an
  adversarial taxonomy*, never as an expected production rate.
- Elsewhere the small-n problem is worse and already disclaimed but worth the
  panel's attention: drift-repair is **n=1 per arm** (`05_results.tex:82–88`);
  the substrate table is **n=3 per cell** (`05_results.tex:117–126`); OpenEMR
  agent arm is **n=10** on a *shared mutable public site* (not reproducible).
  None of these support any reliability claim, and the paper (to its credit)
  mostly says so — but the abstract's "compiled replay reduced latency and
  per-run inference while preserving independently checked effects" still reads
  as a general capability claim built on n∈{1,3,10}.

### 3.5 The second domain is a genuinely different record shape but an isomorphic synthetic mirror — it does not establish generalizability (MAJOR for D&B)
`benchmark/lending_fault_model/faults.py` states outright that MockLoan's
outcome taxonomy is "**identical to the clinical study's** … only the record
shape … and the domain stakes … differ," and it is a self-built SPA
(`openadapt_flow/mockloan/static/app.js` with a `?fault=` hook). So the "second
domain" is a *reskin at the level of business nouns* (loan/disbursement/ledger
vs patient/encounter/note) over the *same* fault model, the *same* authors'
assumptions, and the *same* purpose-built-fixture methodology. It shows the
*pattern transfers across two record shapes the authors designed*; it does **not**
show it transfers to a real, hostile enterprise system of record. Two synthetic
toy apps built by the same team to the same template are weak evidence of
generalizability, and the paper must not claim otherwise.

**Additional inconsistency to resolve:** MockLoan's `effect_verify` arm reports
`SWER 0/33` with a *single* `RestRecordVerifier` reading `/api/db`
(`SWER.md`). But the healthcare harness's own finding is that a *single*
out-of-band oracle leaves a `9/90` collateral residual — `0` requires the
complete read path. Either the lending study has no collateral-write fault class
(so it is easier than the healthcare one and its `0/33` is not comparable), or
`/api/db` is a full-ledger read (i.e. the `effect_full`-equivalent, not the
realistic single-surface arm). The paper cannot present a healthcare `10%
realistic residual` and a lending `0% realistic residual` without reconciling why
the same oracle class yields different floors. **This looks like the lending
study omits the one fault class that produces the healthcare residual.**

### 3.6 EffectBench as a D&B artifact: strong engineering, but the *public, reusable* surface is one synthetic app and it ships the hard part (MAJOR for D&B)
The protocol is well-built, but a D&B panel scores *the reusable test set a third
party can actually run and be fairly ranked on*, and here it is thin:

- **The public MIT sample is one synthetic fixture, 5 of 7 fault classes, one
  substrate.** `SPEC.md §2.2`: the public sample is `effectbench.tasks.mockmed`
  covering "C1–C5 + controls on the `web` substrate." C6 (homonym), C7
  (no-op/wrong-target), and the desktop/remote-display substrates are *defined*
  but sit behind "container-gated real-system-of-record packs and the private
  hardened corpus … outside this MIT synthetic sample." So the interesting,
  hard, differentiating parts of the benchmark are **not third-party-runnable**.
  What outsiders can reproduce is a single OpenAdapt-authored toy app.
- **The benchmark ships the oracle — i.e. abstracts away the entire cost.** The
  `EnvHandle.product_effect_verifier()` (`adapter.py`) hands the SUT "its OWN
  independent record-readback verifier" for the synthetic app. In the real world
  *authoring that verifier for a legacy system of record is the whole problem*
  (the paper itself concedes "effects are currently authored per deployment; the
  compiler does not infer them," `03_governance.tex:18`). A benchmark on which
  "use the effect verifier we handed you and you reach SWER 0" therefore bakes in
  OpenAdapt's core assumption (that a cheap, correct, independent oracle exists)
  rather than testing it. A competitor system that *cannot* cheaply build such an
  oracle — the realistic case — is not measured on that difficulty at all.
- **Overfitting surface.** Because the public taxonomy and fixture are small,
  fixed, and open, a submission can special-case C1–C5 on MockMed. The private
  hardened corpus is the stated mitigation, but a benchmark whose non-gameability
  depends on a corpus reviewers cannot see is, to a D&B panel, not yet
  demonstrated to be non-gameable.
- **Reference baselines are OpenAdapt's own arms** (`ScreenOnlySUT`,
  `EffectVerifiedSUT`). Fair in principle (goal-only interface), but there is no
  independent third system scored yet, so "a competitor would be scored fairly"
  is asserted, not shown.

Net: EffectBench is a *promising* D&B contribution, but in its current public
scope it is closer to a well-instrumented unit test of OpenAdapt's own thesis
than a community benchmark with a diverse, reusable, third-party-scoreable test
set. It also is **not described in the paper at all** (see §3.8).

### 3.7 "Machine-checked paper constants" verify fidelity, not validity (MODERATE, and currently harmful)
The contribution is framed as binding "every headline number … to a released
benchmark file" (`01_introduction.tex:56–58`). But `check_artifacts.py` only
guarantees the *prose faithfully transcribes an artifact* — it cannot tell a
sound artifact from a circular one. Right now it is actively *certifying the
circular number*: lines 133–149 assert the paper says the `silent_wrong_action`
`50/0`, the very result the repo's own README calls circular. A green
`check_artifacts` therefore currently provides false assurance. The paper should
(a) stop advertising the check as if it were a validity guarantee, and (b) once
repointed, the check should bind the *three-arm ladder* (54/9/0), not a single
best-case number.

### 3.8 Residual overclaims that survive even a correct reconciliation (MODERATE)
- **The D&B contributions are invisible in the paper.** Grep of
  `paper/sections` + `main.tex` + `workshop/main.tex` for
  `effectbench|lending|mockloan|second domain|SWER|wilson` returns *nothing*
  except the one line saying confidence intervals are *not* reported
  (`04_methodology.tex:31`). If this paper is aimed at D&B, the benchmark and the
  second domain must actually be *presented, described, and scoped* in the text —
  they cannot remain code-only on unmerged branches while the abstract implies a
  single-domain fault study.
- **Abstract/intro "caught every one (0 of 90)"** is the strong-claim version of
  §3.3 and must become the honest ladder.
- **Positioning table (`01_related_work.tex:63–75`)** gives OpenAdapt a clean
  sweep of ✓ across all five columns while every competitor row is blank/partial.
  It is self-serving in the usual way: the columns are chosen to be exactly the
  properties OpenAdapt designed for. This is fine as a *design* comparison but
  should be labeled as such, not as an empirical capability comparison.
- **Agent baseline** remains single-attempt, un-scaffolded `claude-sonnet-5` with
  no retry/verification harness (`04_methodology.tex:11–17`). This is the fair
  *lower* bound but the paper should acknowledge that a production agent
  deployment would add its own verification/retry, narrowing the gap — otherwise
  the cost/latency deltas (agent `$0.55`/`70.4s` vs compiled `$0`/`39.2s`) read
  as more decisive than they are.

---

## 4. Section-by-section comments

- **Abstract (`main.tex:42–66`).** Contains the stale `50/90 → 0/90`. Must adopt
  the three-arm ladder and name the realistic `10%` residual. "Bounded
  experiments span …" is honest; keep it.
- **Intro (`01`).** `line 39` stale. The five-contributions list should be
  revised to actually include EffectBench + the second domain if those are part
  of the submission; contribution 5's "machine-checked constants" overstates what
  the check guarantees (§3.7).
- **Related work (`01_related_work`).** Solid lineage (Sikuli, FlashFill,
  Rousillon, runtime verification, end-to-end argument, oracle problem). Missing:
  positioning vs *other silent-failure / effect-checking* work and vs test-oracle
  benchmarks; the paper claims a novel metric (SWER) but does not situate it
  against existing regression-oracle or metamorphic-testing literature. Add.
- **System (`02`).** Clear. No overclaim. Fine.
- **Governance (`03`).** `fig:oracle` caption `line 65` stale. §effect
  verification honestly flags effects are authored per deployment — keep that
  prominent; it is the concession that undercuts the EffectBench "SWER 0" story
  (§3.6).
- **Methodology (`04`).** The taxonomy is good. The single weakest sentence is
  `line 25`: it treats "run counts rather than confidence intervals" as a style
  choice; it should instead state the runs are *deterministic* (variance ≈ 0) so
  the counts are coverage, not estimates (§3.4). Comparative conditions are well
  disclosed.
- **Results (`05`).** Stale headline + figure. The substrate table and identity
  ladder are honestly captioned (n and over-halt disclosed). The public-web
  `2/29 wrong-action` result is honestly characterized as "failure discovery,
  not a generalization rate" — good, keep verbatim.
- **Limitations (`06`).** Strong and honest, but does **not** disclose the
  ground-truth's own closed-world (two-table) blind spot (§3.2) — add it.
- **Reproducibility (`07`).** The four evidence labels are excellent. Should note
  the shared-public-OpenEMR arm is not CI-reproducible (it does, indirectly).
- **Conclusion (`08`).** Fine; the "next evidence target = longitudinal real
  workflow" is the right ask and honest.

---

## 5. Questions to the authors

1. The `effect_full` arm and the ground truth both use
   `openadapt_flow.runtime.effects.sql.audit_table_deltas` over the *same*
   `(encounters, billing)` table set. In what sense is the `0/90` a measurement
   rather than a restatement of the judge's own audit scope? What fault would be
   silently accepted by *both*?
2. What mutable surfaces exist beyond `encounters`/`billing` in a real EMR, and
   what is the silent-wrong-effect rate when the ground truth is *not* allowed to
   see all of them? (i.e. is `0/90` robust to enlarging the world?)
3. Given the harness shows a single out-of-band oracle leaves `9/90`, why does
   the lending study's single-oracle `effect_verify` reach `0/33`? Does MockLoan
   include a `collateral_unaudited`-equivalent fault class? If not, the two `0`s
   are not comparable.
4. Are the 9 (or 10) repeats per class independent trials or deterministic
   replays? If deterministic, why report rates and `n=90` rather than a coverage
   matrix over ~10 scenarios?
5. What is the incidence basis for the fault taxonomy? Absent one, on what
   grounds is `60%` screen SWER a meaningful number rather than an artifact of
   choosing 6 fault classes and 1 control?
6. EffectBench hands the SUT `product_effect_verifier()` for the synthetic app.
   How would a third party be scored on a *real* system of record where authoring
   that verifier is the actual cost? What is the public, third-party-runnable
   test-set size and diversity, excluding gated/private packs?
7. Has any system *other than OpenAdapt's own two baselines* been scored on
   EffectBench? Without one, what evidence supports "a competitor would be scored
   fairly"?
8. Will the paper text actually present EffectBench and the second domain, or do
   they remain repo-only? If repo-only, why are they claimed as contributions?

---

## 6. Scores (NeurIPS style, 1–10 / soundness-contribution-presentation 1–4)

### As the paper currently reads (prose still circular `50→0`)
| Axis | Score | Note |
|---|---|---|
| Soundness | **2 / 4 (fair)** | Headline number is the one the repo's own README calls circular; abstract/figures unreconciled. |
| Contribution | **2 / 4 (fair)** | Real thesis, but empirical base is synthetic fixtures; D&B artifacts not in the text. |
| Presentation | **3 / 4 (good)** | Well written and mostly honest, but leads with a discredited number. |
| **Overall (NeurIPS main)** | **3 / 10 (reject)** | Desk-reject risk from the self-contradicting repo. |

### After a *correct* reconciliation (three-arm ladder, realistic 10% foregrounded, closed-world disclosed, D&B artifacts written up)
| Axis | Score | Note |
|---|---|---|
| Soundness | **3 / 4 (borderline)** | Independent harness is sound; residual closed-world + deterministic-n caveats remain. |
| Contribution | **3 / 4 (borderline)** | SWER-as-instrument is a genuine, useful reframing. |
| Presentation | **3 / 4 (good)** | Strong once honest. |
| **Overall (NeurIPS main)** | **4 / 10 (borderline-reject)** | Synthetic-only evidence, n∈{1,3,9,10}, single author, no real deployment → below the bar for the main track. Lean **reject**. |
| **Overall (NeurIPS D&B)** | **5 / 10 (borderline)** | EffectBench's engineering is D&B-grade, but the public reusable test set is one synthetic app, it ships the oracle, and no third-party system has been scored. Lean **borderline-reject**, upgradable to **borderline-accept** if the public pack gains a real system of record + an independent scored baseline and the paper actually presents it. |

**Confidence: 4 / 5.** I read the paper, the harness (`run.py`, `verifiers.py`,
`ground_truth.py`), both new domains, the EffectBench spec/adapter/leaderboard,
and `check_artifacts.py`, and cross-checked every headline number against the
JSON. I did not execute the harnesses.

**Verdict:** *Reject* for NeurIPS main as-is; *borderline* for D&B and only after
the reconciliation is finished and the benchmark is genuinely presented and
opened.

---

## 7. Prioritized findings (impact × effort) — feed to the remedy agent

### P0 — must-fix, cheap, non-negotiable integrity (do first)
1. **Finish the reconciliation (§3.1).** Migrate prose/abstract/figures from the
   circular `50→0` to the `effect_e2e` ladder; update `fig:silentbar`
   coordinates (`50→54`, `10→9`) and `fig:oracle` caption; repoint
   `check_artifacts.py:133–176,451–463` from `silent_wrong_action` to
   `effect_e2e`. *High impact, low effort.*
2. **Foreground the realistic `9/90` (10%), not `0/90` (§3.3).** Present the
   three-arm ladder 60% → 10% → 0% and condition `0%` explicitly on a read path
   covering every mutable surface. *Highest framing impact, low effort.*
3. **Disclose the ground-truth closed-world (§3.2).** State in Limitations that
   the judge audits only `(encounters, billing)`, shares `audit_table_deltas`
   with the `effect_full` arm, and cannot see surfaces outside that set — so
   `0/90` is "zero in a closed two-table world." *High integrity, low effort.*
4. **Reframe the statistics (§3.4).** Say the runs are deterministic (variance
   ≈ 0), report a coverage matrix over ~10 scenarios, and drop any implication
   that `60%`/`54/90` is a population rate. *Medium impact, low effort.*

### P1 — needed to be competitive at a venue (higher effort, founder call)
5. **Resolve the lending `0/33` vs healthcare `9/90` inconsistency (§3.5).** Add
   a `collateral_unaudited`-equivalent fault class to the lending study or state
   why `/api/db` is a complete read path; do not present two non-comparable
   `0`s. *High impact, medium effort.*
6. **Actually present EffectBench + the second domain in the paper (§3.8).** If
   D&B is the target, they must be described, scoped, and their public-vs-gated
   split stated honestly. *High impact, medium effort.*
7. **Broaden EffectBench's public, third-party-runnable surface (§3.6)** — at
   least one real (non-self-built) system of record in the *public* pack, and at
   least one scored baseline that is not OpenAdapt's own arm; stop shipping the
   oracle as a freebie, or measure the cost of authoring it. *Highest D&B impact,
   high effort.*
8. **Temper the positioning table + agent-baseline framing (§3.8).** Label the
   table as design-intent, and note a scaffolded agent would narrow the
   cost/latency gap. *Medium impact, low effort.*

### P2 — related work + honesty polish
9. Situate SWER against metamorphic/regression-oracle and test-oracle-benchmark
   literature (§4). *Medium, medium.*
10. Stop advertising `check_artifacts` as a validity guarantee; it checks
    transcription fidelity only (§3.7). *Low, low.*

---

*Reviewer note: this review is deliberately complementary to
`paper/REVIEW_ADVERSARIAL.md` (which covered honesty, the definitional flagship,
and out-of-band-oracle ambiguity). It does not re-flag the already-diagnosed
circularity of the retired in-process benchmark; it audits whether the
replacement independent harness, the statistics, the second domain, and the
packaged benchmark actually deliver on the strengthening they promise.*
