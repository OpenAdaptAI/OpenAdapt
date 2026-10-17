# Adversarial Peer Review

**Paper:** *Compile Once, Govern Every Repair: Deterministic Replay for Repeated GUI Work* (plus the workshop condensation *A Green Screen Is Not a Saved Record*).

**Reviewer stance:** rigorous, skeptical program-committee member at a top venue (NeurIPS main and the Datasets & Benchmarks track). Claims below were cross-checked against the repository, not taken from the paper's own summary.

---

## 1. Summary of contributions (as I would restate them)

The paper argues that the dominant reliability metric for GUI and computer-use agents, task success judged from the final screen, is the wrong metric for consequential work, because a rendered "Saved" banner is not a persisted write. It proposes:

1. A demonstration compiler that turns one recorded GUI trace into a deterministic replay program with a backend-neutral intermediate representation; healthy replay makes zero model calls.
2. A six-rung resolution ladder (structured identity, local/global template match, OCR, landmark geometry, optional grounding model) that re-resolves drifted targets and records each change as a reviewable "repair" patch.
3. System-of-record effect verification: consequential steps declare a typed effect checked against the application's own state (the "strong oracle") rather than the screen (the "weak oracle"), returning confirmed / refuted / indeterminate and halting on anything but confirmed.
4. Governance mechanisms separating "runnable" from "certified," with explicit refusal on unverifiable identity or effect.
5. An evaluation protocol reporting silent incorrect success and over-halt alongside task success, with machine-checked constants binding every headline number to a released artifact.

The empirical spine is an injected-fault study (screen-only verification silently accepts 50 of 90 fault runs; effect verification 0 of 90), a small comparative latency/cost study against a computer-use agent (OpenEMR, MockMed), a one-run 29-app breadth corpus, an adversarial identity study, and three single-task substrate qualifications (Windows UIA, macOS, RDP).

## 2. Strengths

- **The core thesis is correct, important, and under-served.** "Score the effect, not the screen" is a genuinely useful reframing of how the computer-use-agent community evaluates reliability. The distinction between silent incorrect success and over-halt is the right pair of quantities, and the community does not currently report either. The workshop condensation states this position cleanly and is, in my judgment, the strongest single artifact in the submission.
- **Unusual methodological honesty.** The outcome taxonomy (success / safe-halt / over-halt / false-abort / silent-incorrect-success) is principled. The limitations section is candid about sample sizes, selection bias, single-author bias, and non-reproducible field evidence. The paper repeatedly refuses to convert small-n demonstrations into capability claims.
- **`check_artifacts.py` is exemplary reproducibility engineering.** Binding every prose constant to a released JSON artifact, and asserting the workshop and full report share identical constants and bibliography, is rare and commendable discipline. It caught prose/artifact drift by construction.
- **The mechanism is real, not vaporware.** I verified the effect verifiers (`openadapt_flow/runtime/effects/rest.py`, `sql.py`, `fhir.py`) genuinely read an independent system of record and are fail-safe: transport errors, non-2xx, expired tokens, or unparseable bodies all map to INDETERMINATE, which halts, so the system never upgrades "cannot read the record" into "success." The no-verifier gate in `replayer.py` halts a declared-effect step when no verifier is configured. The resolution ladder (`resolver.py`, `RUNG_ORDER`) is a real six-rung ordering with a single, default-off model rung. The RDP backend is implemented.

## 3. Weaknesses and major concerns

### 3.1 Evaluation rigor: this is a set of demonstrations, not a benchmark

Every quantitative result is at a sample size where inference is not possible, and the paper knows it: OpenEMR is 20 vs 10 on a shared mutable public site the authors admit is not reproducible; MockMed is a purpose-built fixture; the breadth corpus is one run per app; the three substrate rows are 3/3, 1/1, and 3/3. No confidence intervals, no repeated-workflow longitudinal data, no natural drift. The paper's own hedges ("The unequal, small samples do not establish superior reliability"; "One run is not a reliability claim") are honest, but they also concede that essentially none of the numeric results support a general claim. A "3/3" with an independent oracle is an existence proof that the mechanism can work once, not evidence of a rate. For the Datasets & Benchmarks track in particular, a benchmark must be a defined, reusable, documented task suite with baselines and a protocol others can run; what is presented is a portfolio of bespoke one-off studies wired to one codebase.

### 3.2 The flagship 0-of-90 result is close to definitional and is not measured through the live replayer

This is my single most serious concern, and it is not disclosed in the paper.

- The headline "screen 50/90, effect 0/90" comes from `openadapt_flow/benchmark/silent_wrong_action.py`, **not** from the browser study. That harness does not drive the browser, does not run OCR, and does not invoke the `Replayer`. It issues raw `requests.post` calls that replicate what the app's JavaScript would send, encodes the screen oracle as a hardcoded rule over HTTP status codes, and calls `RestRecordVerifier.verify(...)` directly.
- In that harness the **effect oracle and the ground-truth judge read the same in-process store**: the effect verifier reads `GET /api/db` and the ground-truth `_business_effect` reads `FaultDB.snapshot()`, which is the same `FaultDB`. Worse, the effect contract (`record_written` exactly once AND `field_equals` on the note) is essentially a restatement of the ground-truth definition of "correct." So "effect silent-wrong = 0 of 90" is guaranteed by construction: the oracle and the ground truth are computed from the same bytes with near-identical logic. The number is a property of the fixture, not an empirical finding.
- The genuinely end-to-end harness (`benchmark/fault_model/run.py`, real Playwright + OCR + `Replayer`) measures **only the screen-only baseline**. Its own recommendations section describes effect verification as a *proposed* fix that the reference postcondition system does not yet implement ("The reference postcondition system is vision-only today, so on this corpus it provides no transactional-safety coverage"). The effect-verifier-through-the-replayer path for these fault classes exists only in unit tests (`tests/test_effect_fault_matrix.py`), and those tests also call `verifier.verify(...)` directly rather than driving the `Replayer`.
- Consequently the Results sentence "those five classes refuted and halted **through the live replayer**" overstates what was measured. The live replayer never ran the effect verifier on these 90 runs. The suspiciously clean integers (exactly 50, exactly 0, exactly 10) are deterministic-fixture artifacts, not a measured rate.

To be fair to the authors: the *mechanism* (read the record, not the screen) is real and the qualitative claim (screen-only verification cannot distinguish these faults; a record read can) is true and worth publishing. But the paper presents a constructed identity as if it were an experimental result, and it attributes the effect-verified number to an execution path that did not produce it.

### 3.3 Construct validity of SWER and the fault taxonomy: principled but bespoke

The seven fault classes are hand-picked transactional hazards (partial save, duplicate, optimistic-then-reject, stale overwrite, double delivery, timeout-after-commit, session loss). They are reasonable and grounded in the transactional-systems literature the paper cites. But: (a) the "system of record" is an in-process Python list, not a database, so idempotency, concurrency, and staleness are simulated at a single localhost boundary; (b) there is no evidence that these are the faults that occur in real EMRs, or that their relative frequencies resemble the 5-of-7 injected mix, so the 50/90 "rate" has no external validity as a rate; (c) the fault set, the injector, and the metric are all authored by the same party that authors the system that passes them. SWER is a good idea for a *metric*, but as presented it is a bespoke instrument validated against itself, not a community benchmark. A skeptical reviewer reads 50/90 as "we chose five fault classes the screen cannot see and confirmed the screen cannot see them."

### 3.4 Baseline fairness: same-backend, but a vanilla single-shot agent

The agent baseline (`openadapt_flow/benchmark/agent_baseline.py`) is not a strawman in the crude sense: it drives the same Playwright backend, uses a current computer-use model and tool, and is handed generous task information (credentials, exact patient, exact note text, a hint that the app is slow). But it is a **single-attempt, un-scaffolded** loop: no system prompt, no planning/reflection/ReAct, no whole-task retry, and hard caps (25 actions, $1.50, last-3-screenshots context). Modern agent results lean heavily on scaffolding and retries; comparing a compiled program against a vanilla single-shot agent inflates the latency and cost deltas. The paper's hedges are appropriate, but the abstract and introduction still lead with the comparison in a way that implies a capability gap the design does not license. At minimum the baseline's single-shot, no-scaffold nature belongs in the main text next to the comparison table, not only implicitly in the code.

### 3.5 The strong-oracle claim is ambiguous exactly where it matters most (pixel-only substrates)

The paper says the verifier checks "REST, FHIR, or document state" and calls application state the "strong oracle," but the main text never states the mechanism precisely: that this read is performed **out of band**, through the system of record's own interface (SQL / REST / FHIR / file), independently of the substrate carrying the write, and that a locked-down pixel-only session that exposes none of these has no strong oracle available. The repository is more honest than the paper here. `openadapt_flow/runtime/effects/onscreen.py` documents that on a Citrix-class pixel-only substrate there is no DB, no reachable FHIR, and no local file, so the only fallback is a same-application OCR read-back, which the module itself states **cannot** catch partial saves, duplicates, stale overwrites, or lost updates, and that on such substrates the safety guarantee rests on the identity gate and halt-on-ambiguity, not on effect verification. This is a load-bearing caveat: the paper spends most of its scoping effort on RDP and Citrix, yet the very safety property it foregrounds silently degrades on those substrates unless an out-of-band read path exists. Any careful reviewer will ask "how do you read app state when there is no DOM or API in the pixel stream, and what happens on a locked-down Citrix session?" The paper must answer this in the main text. (The RDP study sidesteps the problem by reading the effect through guest-tools file access, which is itself an out-of-band channel a locked-down Citrix deployment would not grant; this should be stated, not left implicit.)

### 3.6 Novelty and positioning: modest, and the positioning table is self-serving

The individual ingredients are not new: runtime verification and the test-oracle problem (cited), transactional idempotency hazards (cited), programming-by-demonstration with redundant evidence (cited), visual scripting (cited), computer-use agents (cited). Verifying the backend rather than the UI is standard practice in end-to-end software QA (e.g., UI test suites that assert against the database). The contribution is the *integration* plus the *evaluation lens*, which is a legitimate systems/HCI contribution but a modest one for NeurIPS main. The positioning table (Table 1) is constructed so that only OpenAdapt earns all five checkmarks; the columns are chosen to match the system's feature set, which is persuasive rhetoric rather than an objective taxonomy. Missing from related work: RPA platforms that already support API-level or database assertions, and the substantial software-testing literature on backend oracles and end-to-end assertions.

### 3.7 Reproducibility: strong binding, weak independent reproducibility

`check_artifacts.py` binds prose to JSON, which is excellent, but it binds prose to numbers, not numbers to reality: it cannot detect that the flagship number is fixture-definitional (3.2). The headline field study (OpenEMR) is explicitly not reproducible. The comparative and substrate studies depend on a paid model whose exact identifier ("claude-sonnet-5", "computer_20251124", a dated beta header) may not be pinnable or even available at review time, and on specific VMs and hardware. The "benchmark" is the repository, not a packaged, versioned, independently runnable suite with a documented protocol and a baseline leaderboard, which is what a D&B submission is expected to deliver.

### 3.8 Residual overclaims (after the honesty sweep)

The paper has clearly been through an honesty pass and is much better hedged than typical, but residual overclaims remain:
- The abstract's "the effect check caught every one (0 of 90)" is technically true but rests on the definitional fixture (3.2); it reads as a clean empirical win.
- "The same governed semantics across four backends" (Section 5 heading) oversells three single-task 3/3-or-less demonstrations as cross-substrate generality.
- "Refuted and halted through the live replayer" (3.2) attributes a number to a path that did not produce it.
- Table 1's all-checkmarks row for OpenAdapt.

None of these are dishonest in the way the paper is careful to avoid, but each is a place where the framing outruns the evidence.

## 4. Detailed comments, section by section

- **Abstract.** Tighten "caught every one (0 of 90)"; add one clause that the effect-verified arm is a mechanism study against the record, not an end-to-end replayer measurement. "Bounded experiments span ..." is a good sentence; keep it.
- **Introduction.** The five contributions are clearly stated. Contribution 5's "machine-checked paper constants that bind every headline number to a released benchmark file" should not be oversold: it binds prose to JSON, not JSON to ground truth. "Citrix ICA/HDX remains outside the measured set" is a good scoping sentence but is a one-liner; a reader wants to know whether the *mechanism* is expected to transfer and why it is not yet measured (see Part B).
- **Related work.** Add RPA-with-backend-assertions and end-to-end test-oracle practice. Soften Table 1's framing or add a note that the columns are the properties the system targets, not a neutral taxonomy.
- **System design.** The effect-verification subsection (moved to Governance) needs the out-of-band mechanism stated here or there; currently the "strong oracle" is asserted without saying how state is read on a substrate with no DOM/API (see 3.5, Part B). The resolution-ladder figure is clear and matches the code.
- **Governance.** Good. This is the right home for the out-of-band-oracle clarification. State explicitly what happens on a pixel-only substrate with no reachable system of record: weak same-app read-back or halt, and that the transactional guarantee needs a structured read path.
- **Methodology.** The comparative-conditions paragraph should state that the agent is single-attempt and un-scaffolded (3.4). The transactional-fault paragraph should state that the "system of record" is an in-process store and that the effect-verified arm is measured by a direct-verifier harness, not through the browser/replayer (3.2, 3.3).
- **Results.** Fix "through the live replayer" (3.2). "Same governed semantics across four backends" should be softened to "on one named task per substrate." The identity result is the most honest and arguably the most interesting; consider foregrounding it.
- **Limitations.** Strong. Add the pixel-substrate oracle caveat and the fixture-definitional caveat, and a concrete Citrix future-work item (Part B).
- **Reproducibility.** Note the model-pinning fragility and that the field study is not reproducible by design.
- **Conclusion.** Fine; the "next evidence target" paragraph is the right forward-looking note.

## 5. Questions to the authors

1. The 50/90 and 0/90 numbers come from `silent_wrong_action.py`, which bypasses the browser, OCR, and `Replayer` and reads the same in-process store for both the effect oracle and the ground truth. Given that the effect contract restates the ground-truth definition, in what sense is 0/90 an experimental result rather than a definitional consequence of the fixture? Has the effect verifier ever been run end-to-end through the `Replayer` on these fault classes, and if so, what were the counts?
2. What is the strong oracle on a locked-down Citrix/ICA session that exposes no DB, no reachable API, and no local file? If the answer is same-application OCR read-back, please state that it cannot catch partial/duplicate/stale/lost-update faults, and quantify how often the strong-oracle guarantee is simply unavailable in your target deployments.
3. The RDP effect oracle uses guest-tools file readback. Is that channel available in your intended Citrix deployments, or is it an artifact of the Parallels test harness? If the latter, the RDP result does not evidence effect verification on a real pixel-only remote session.
4. How was the agent baseline tuned? Would a scaffolded agent (planning, reflection, one retry) change the latency/cost deltas materially? What is the justification for the 25-action and $1.50 caps?
5. What is the real-world frequency of the seven fault classes in a production EMR, and how sensitive is the 50/90 headline to that mix?
6. What would it take to package this as a standalone, versioned benchmark (fixed task suite, documented protocol, baseline results) that a third party could run without your codebase?

## 6. Scores (NeurIPS style)

- **Soundness: 2 (fair).** The mechanism is real and fail-safe, but the flagship result is fixture-definitional and mis-attributed to the replayer path, and no sample size supports the framing.
- **Presentation: 3 (good).** Very well written, unusually honest, clear figures. Loses a point for framing that outruns evidence in the abstract, Section 5 heading, and Table 1.
- **Contribution: 2 (fair).** An important evaluation lens and a working integrated system, but modest technical novelty and a bespoke, self-validated evaluation.

**Overall rating.**
- *NeurIPS main:* **3 (reject, leaning clear reject).** The paper is a systems-and-evaluation contribution with little novel machine learning; the empirical claims do not meet main-track rigor; the central number is definitional.
- *NeurIPS Datasets & Benchmarks:* **4 (borderline, leaning reject).** The idea (report silent incorrect success and over-halt) genuinely belongs in the benchmark conversation, but the submission is not yet a reusable, independently runnable benchmark, and its flagship number is not an empirical measurement. With the packaging and scale changes in Section 7 it could become a borderline-accept.
- *Workshop (the condensation):* **plausible accept.** The narrow, well-argued position piece with one clean fault study is a good fit for a NeurIPS workshop on agents/reliability, provided 3.2's definitional caveat is disclosed.

**Confidence: 4 (high).** I read the paper and cross-checked the harnesses, verifiers, backends, and `check_artifacts.py` against the claims.

**One-paragraph justification.** The paper identifies a real and important gap in how GUI-agent reliability is measured and backs it with an honest, well-engineered artifact. But the headline evidence does not do what the framing says: the 0-of-90 result is a property of a fixture in which the oracle and the ground truth read the same store with near-identical logic, it is produced by a harness that bypasses the browser and the replayer, and it is nonetheless attributed to "the live replayer." Combined with sample sizes that cannot support any rate claim and a bespoke, self-validated benchmark, this puts the submission below the bar for a main-track accept and at the borderline for D&B. The fixes are largely about honesty of framing and packaging rather than the mechanism, which is sound.

## 7. Prioritized recommendations (impact x effort)

### Must-fix before arXiv (cheap, high-integrity, non-negotiable)

1. **Disclose that the effect-verified 0/90 is a mechanism/definitional result, not an end-to-end replayer measurement (3.2).** State that the effect oracle and ground truth read the same store, that the number is produced by a direct-verifier harness, and remove or correct "through the live replayer." *High impact, low effort.*
2. **State the out-of-band strong-oracle mechanism explicitly, and its unavailability on locked-down pixel-only substrates (3.5).** This is the most likely reviewer question and the paper currently leaves it ambiguous. *High impact, low effort.* (Applied in this PR, see Part B.)
3. **Reframe the Citrix one-liner as a precise scoped limitation plus a concrete future-work experiment (validate the vision-only ladder and out-of-band oracle in a real ICA/HDX environment).** *Medium impact, low effort.* (Applied in this PR.)
4. **Put the agent baseline's single-shot, un-scaffolded nature in the main text next to the comparison (3.4).** *Medium impact, low effort.*
5. **Soften "same governed semantics across four backends" to "one named task per substrate," and annotate Table 1's columns as system-targeted rather than a neutral taxonomy (3.6, 3.8).** *Medium impact, low effort.*

### Needed to be competitive at a venue (expensive, for the founder to decide)

6. **Run the effect verifier end-to-end through the `Replayer` + browser on the fault suite, with an oracle that reads a real database independent of the write path, and report those counts.** This converts the definitional result into an empirical one. *High impact, high effort.*
7. **Scale the comparative study (dozens of workflows, repeated over weeks of natural drift, with confidence intervals) and add a scaffolded agent baseline.** *High impact, high effort.*
8. **Package a standalone, versioned SWER benchmark: fixed task suite, documented protocol, model-agnostic baselines, runnable without the OpenAdapt codebase, with a small leaderboard.** This is the difference between "our system passes our test" and "a benchmark the community can adopt." *High impact, high effort; this is the actual path to a D&B accept.*
9. **Add at least one non-healthcare domain and one hostile/enterprise UI** to counter the generalizability concern (3.1). *Medium-high impact, high effort.*
10. **Independent replication** of at least the identity and fault studies by a third party. *High impact, high effort.*

---

*Prepared as a review deliverable. Nothing here has been or should be submitted to any venue.*
