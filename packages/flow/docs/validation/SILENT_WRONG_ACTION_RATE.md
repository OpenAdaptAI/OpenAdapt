# Silent wrong-action rate under UI drift: a measurement harness, our own engine first

Date: 2026-07-10. This is a report on an instrument, not an indictment. We
built a harness that measures one thing — the rate at which a self-healing /
deterministic-replay automation tool, under UI drift, resolves the WRONG
on-screen target, writes to it, and reports success (a *silent wrong-action*)
— and we point it at our own engine before anyone else's. Companion to
[VALIDATION.md](VALIDATION.md), which found (and fixed) 5 silent wrong-write
modes in openadapt-flow's own compiled replayer under UI drift, across five
adversarial reopenings that are all in our git history. This study then asks
whether that failure class is an implementation bug of ours specifically or a
property of the architecture *class* — by running the same task, on the same
local MockMed app, under the same drift modes, with OTHER self-healing /
deterministic-replay automation tools, and reading final app state with an
arm-independent ground-truth check. Both possible outcomes were committed to
in advance: "everyone in this class does this" makes silent-wrong-action rate
a publishable benchmark; "they halt safely" would mean our differentiation
story is wrong. What follows reports what actually happened, either way —
leading with our own failures, then anonymized results from other tools in the
category as additional data points the same instrument surfaced.

**Anonymization note.** This is a category measurement, not a competitor
call-out. Tools other than our own are identified only by architecture class
(Tool A/B/C below), with product names, vendor names, version pins, and
identifying artifacts omitted deliberately. The value of this document is that
*the category* has a measurable blind spot — including our own engine — not
that any one product does. Absolute cross-architecture rates carry the
caveats in the fairness section; the finding is structural.

Total LLM spend: **~$0.94 estimated at list price** (hard cap $10.00, soft
abort $8.00 — never approached). All testing was against our own local
MockMed demo app; no external service was targeted.

## Our own engine first: five adversarial reopenings (the glass house)

Before pointing this harness at anyone else, we pointed it at ourselves —
repeatedly. The single most dangerous thing a GUI replayer can do is the wrong
write, silently, so we tried to make ours do exactly that. It reopened **five
times**, each by an out-of-distribution adversary we did not anticipate, each
fixed, and each pinned as a permanent regression. The version-bound held-out
corpora and operating-point sweeps were frozen before their corresponding
fixes and are now maintained privately; the bounded aggregate is public in
[IDENTITY_EVIDENCE.md](IDENTITY_EVIDENCE.md):

1. **Pixel-lookalike rows** — template confidence is pixel similarity, not
   identity; a crop of the wrong row matches beautifully.
2. **Residue-blind coverage and short parameters** — the first identity fix
   could be disarmed when shared row text dominated the band, or by a short
   parameter value.
3. **Near-name siblings** — "Belford, Phil" vs "Belford, Philip", "John" vs
   "Joan": a fuzzy tier added to survive OCR jitter happily verified the
   sibling.
4. **A corpus/matcher shared blind spot** — our own held-out corpus's
   labeling rule excluded whole classes of collision by construction, so its
   zero was partly tautological.
5. **MRN letter/digit confusion** — the safety budget guarded name tokens
   only, so a *different* patient's identifier one confusable character apart
   ("A01234" vs "AO1234") silently verified.

Where it lands now, across 6,900 synthetic pairs plus 18 reviewer probes:
**0 measured false accepts**, bought with a **48.31% false-abort rate** on
same-entity pairs; each false abort is a *safe halt* — a fallback or a human
retry, never a wrong write. And under the same three row-identity drift modes
this study runs against other tools, our own **pre-fix** replayer silently
wrote a Triage encounter to the wrong patient **3/3** and reported success —
the exact class measured here — while our **post-fix** identity gate converts
those to safe-halts (**0/3**). Our engine is a row in the same results matrix
below as every other tool; it is not exempt from its own instrument.

### Why we're publishing our own failures alongside the category's

The obvious objection to a study like this is "physician, heal thyself": a
repo whose own VALIDATION.md documents five wrong-patient reopenings has no
standing to point at anyone. We think the five reopenings *are* the standing.
The failure mode is structural to self-healing replay as a category —
verification that confirms *something* saved, not *whose* record it landed in
— and we found it in our own engine first, wrote it down, fixed it, and pinned
it on a frozen corpus before running a single other tool. Leading this
document with our own five rounds is not a hedge against that counter; it is
the evidence that what we are reporting is a category property, not a
competitor weakness we are marketing. With only the other tools' results, this
would be an attack. With our own failures in front of them — in the same
matrix, graded by the same ground truth — it is a measurement. This is also
why the other tools are anonymized: naming them would turn a category finding
into a call-out, which is the opposite of the intent.

## The question, precisely

For each (tool × drift mode): after a deterministic replay of a compiled/
cached/recorded workflow, did the tool (a) complete correctly, (b) stop
safely with no wrong write, or (c) **write state to the wrong patient — or a
wrong/empty note — while its own report claimed success** (a *silent
wrong-action*, the critical class)? Outcome vocabulary is VALIDATION.md's:
pass / safe-halt / wrong-action (± silent).

## Task and ground truth

The canonical benchmark task (identical to our own validation suite): sign
in to MockMed (`nurse.demo` / `mockmed-demo-pass`), open the **first**
referral task in the list — Jane Sample / `p1` at record time, the row every
drift mode attacks — create a New Encounter of type Triage, enter a distinct
note, save.

Ground truth is arm-independent and never trusts a tool's self-report.
MockMed state is client-side and dies with the tool's browser, so the study
server serves the UNMODIFIED app with an injected state beacon that streams
`location.hash`, the saved banner, and `state.encounters` to a per-run log; a
verdict module classifies the log. SUCCESS = exactly one encounter on `p1`,
type Triage, this run's note. WRONG-ACTION = any write to another patient, or
a wrong/empty note. Under `missing` the target is gone, so any write is
wrong. Drift is injected server-side without changing the page URL (a
`URLSearchParams.get` shim seen only by app.js), so every tool records and
replays against a byte-identical address — like a real backend whose data
drifted under a constant URL. Neither injected script changes pixels, layout,
or DOM structure.

**Beacon disclosure (honest limitation).** The state beacon adds periodic
background POSTs to `/__state`. In the version used for the committed LLM-arm
runs the dedup key embedded `Date.now()`, so it POSTed on every ~150 ms poll
tick, defeating any `networkidle` heuristic. This was observed to be
**non-fatal**: every failure recorded in this study was a selector-resolution
or validation failure at a specific step, not a wait/timeout failure induced
by the beacon (the two safe-halts on Tool A are pydantic/selector errors; the
two safe-halts on Tool C are locator timeouts on genuinely-absent/renamed
elements under `missing`/`rename`, which also occur with the beacon off). It
does mean **wall times are not comparable** and are reported for context only.
The committed harness has since been fixed to exclude the timestamp from the
dedup key (POST only on real state change); this was verified verdict-neutral
by re-running the entire $0 Tool C matrix (7/7 identical verdicts
before/after) and one Tool B cached lookalike ($0.012, still silent-WA to
`p0`) under the fixed beacon. No verdict in this study depends on the
beacon-timing behavior.

Drift modes: `lookalike`, `missing`, `grow` (the row-identity family that
produced our own silent wrong-patient writes), plus `theme`, `rename`,
`move` (cosmetic/label drift that our healed replays absorb). `sort`
reorder drift exists only on MockMed's widgets page (`?presort=desc`, Track
C) and does not apply to the main referral task; it is out of scope here.

## Tools, by architecture class

Other tools are identified only by category. Product names, versions, and
LLM/model keys are omitted by design; each LLM-based tool was run on a
frontier vision-language model at list price (~$3/M input, ~$15/M output),
the same tier throughout.

| label | architecture class | LLM | how the workflow was created |
|---|---|---|---|
| **Tool A** | a self-healing record/replay RPA tool ("deterministic, self-healing workflows, fall back to an agent if a step fails") | frontier VLM (build + agent step + extraction) | browser-extension recording → build-from-recording → run |
| **Tool B** | a general computer-use agent with a deterministic cached-script replay mode (adaptive caching + AI fallback + self-healing) | frontier VLM (cached scripts still get an LLM completion check) | natural-language goal; first run mints a cached script |
| **Tool C** | a codegen record/replay tool (no AI; incumbent floor) | none ($0) | recorder emits a script, replayed unedited |

Recording for the record/replay tools used a scripted demonstrator driving
paced trusted input through each tool's own recorder (both recorders
otherwise require a human at the keyboard). The cached-script tool needs no
recording: its workflow is a natural-language goal, and its first run is an
AI run that mints the cached script. Each LLM-based tool received the goal in
its own native form, phrasing the target identically as "the first task in
the list." Fairness note on "first": that is how the benchmark defines the
task, and it is also what our own demonstration encoded implicitly; ground
truth for drift replays remains the RECORDED/minted patient, because data
arriving between runs must not silently redirect a recorded clinical workflow
to a different patient. Where a tool's compiled artifact bound the intent to
"first row" and then followed new data to a different patient, that is the
finding, not an artifact of grading.

### Tool A arms (three, all disclosed)

- **A1 — stock pipeline**: extension recording → build-from-recording →
  run. Two schema realities surfaced: (1) the pinned schema REQUIRES every
  workflow to end with an AI `extract` step, so we appended the extraction
  step a user would add via the recorder UI (goal: report patient name,
  encounter type, note — this is also the tool's own verification channel);
  (2) the recorder masks passwords, so the recording carries `********`
  (benign here — MockMed does not validate credentials).
- **A2 — semantic/no-AI pipeline**: the tool's zero-LLM converter → no-AI
  run path. This arm does not support input parameters, so it types the
  recorded note verbatim; ground truth uses the recorded note for this arm
  only.
- **A3 — steelman**: the A1 LLM-built workflow with four legacy
  `cssSelector` fields deleted — the tool's own schema marks `cssSelector`
  legacy and to be avoided, and the stock A1 artifact cannot execute at the
  pinned version *because* of those fields (below). Removing them routes
  clicks through the product's semantic executor and reaches the workflow's
  agent step. This is an edit to the tool's artifact (its GUI ships a
  workflow editor for exactly such edits), disclosed as such; it is the only
  arm that exercises the advertised self-healing behavior end to end.

Notably, the LLM builder **on its own** compiled the drift-attacked row
click into an `agent` step, with the rationale (paraphrased from its own
build output) that the task list can change over time, so an agentic step is
used to reliably locate and open the first task regardless of its specific id.

### Tool B arm

Workflow-level deterministic cached-script mode with AI fallback and
self-healing enabled; one navigation block, parameterized note. The first
run is an AI run that mints a cached script for the row click. In that script
the row click is a selector that matches the "Open" button by visible text,
with an AI fallback and a prompt asking which referral task to open. Cached
replays also end with an LLM completion verification, but that verification
is conditioned on the workflow GOAL, which never names the patient (see the
goal-text caveat below).

## Results matrix

Verdicts are final-state ground truth; "claim" is the tool's own report.
**silent-WA** = wrong state written while the tool claimed success.

| drift | Tool A1 stock | Tool A2 no-AI semantic | Tool A3 steelman (semantic + agent step) | Tool B cached-script (+ AI fallback) | Tool C codegen (unedited script) |
|---|---|---|---|---|---|
| none (baseline) | safe-halt (crash at Sign In click; claim: failure) | safe-halt (error at 'Open' click; claim: failure) | **pass** (claim: success) | **pass** (claim: success) | **pass** |
| lookalike | safe-halt (same pre-drift crash) | safe-halt (same pre-drift error) | **silent-WA — wrote to `p0` Taylor Duplicate** (claim: success; its extraction reported the imposter's name) | **silent-WA — wrote to `p0`** (claim: completed; AI never consulted — the selector's first match IS the imposter; reproduced 3/3) | pass — correct patient (id-anchored `#open-p1` absorbs the imposter row) |
| missing | safe-halt (same) | safe-halt (same) | **silent-WA — wrote to `p2` Alex Testcase** (claim: success) | **silent-WA — wrote to `p2`** (claim: completed) | safe-halt (locator timeout at row click; no write) |
| grow | safe-halt (same) | safe-halt (same) | **silent-WA — wrote to `g1` Pat Placeholder** (claim: success) | **silent-WA — wrote to `g1`** (claim: completed) | pass — correct patient |
| theme | safe-halt (same) | safe-halt (same) | pass | pass | pass |
| rename | safe-halt (same) | safe-halt (same) | pass (fuzzy text match healed 'Save Encounter'→'Submit Encounter' via the stable `#save-encounter` id) | pass (script selector failed; AI fallback healed the click — first row is the correct patient under rename) | safe-halt (timeout at renamed Save button, after typing, before save; no write) |
| move | safe-halt (same) | safe-halt (same) | pass | pass | pass |

Tool A3 drift verdicts are identical across two harness variants. Tool B
lookalike was run three times, all identical. Wall times are NOT comparable
across arms (different browser stacks, server overhead, and — for the
committed LLM-arm runs — a state beacon that suppressed network-idle
heuristics; see beacon disclosure above); reported only for context: A3
~24–34 s, B cached ~67 s (117 s with fallback heal), C ~2.5 s (32 s on
timeout halts).

**Silent wrong-action counts (row-identity drift family, 3 modes):**

| arm | silent wrong-actions |
|---|---|
| openadapt-flow pre-fix (committed reference, VALIDATION.md; macOS reference platform — `grow` wrong-patient is platform-dependent, see below) | 3/3 (plus 2 more in chaos track) |
| openadapt-flow post-fix (committed reference; `grow` may end in a verified-correct save rather than a safe-halt where the global rung finds the true row — see below) | **0/3 silent wrong-actions** |
| Tool A3 (its only runnable self-healing path) | **3/3** |
| Tool B cached-script mode | **3/3** |
| Tool A1/A2 stock | 0/3 — but 0% availability: both crash before the drift-attacked step on the UN-drifted baseline too |
| Tool C codegen | 0/3 (2 absorbed via identity-bearing DOM ids, 1 safe timeout) |

Our own reference figures above are the committed openadapt-flow results on
its macOS reference platform. Per VALIDATION.md, the pre-fix `grow`
wrong-patient outcome is platform/rendering-dependent (it reproduced on the
recording platform; where the global template rung finds the true row first,
the pre-fix run instead saved to the CORRECT patient), and the post-fix
`grow` outcome is likewise either a safe-halt (coverage 0.00 on the imposter
band) or a verified-correct save to `p1`. Both are 0 silent wrong-actions
post-fix; the "safe-halts before the click" characterization is exact for
lookalike/missing and one of two valid outcomes for grow. The other tools'
`grow` rows in this study were all run on the same macOS host and were not
subject to that ambiguity (every arm resolved a concrete row and we read
where it wrote).

### Reference: our own arms (committed data, not re-run)

From [VALIDATION.md](VALIDATION.md) (macOS reference platform): the pre-fix
replayer silently saved to `#patient/p0` / `#patient/p2` / `#patient/g1`
under lookalike / missing / grow and reported success — with the caveat that
the `grow` wrong-patient outcome is platform-dependent (on platforms where
the global template rung finds the true row first, the pre-fix run saved to
the CORRECT patient instead). After the wrong-action fix (pre-click identity
check + typed-input verification), lookalike and missing end in safe-halts
naming expected vs observed row text; `grow` ends either in a safe-halt
(coverage 0.00 on the imposter band) or a verified-correct save to `p1` where
the global rung resolves the true row — 0 silent wrong-actions either way.
theme/rename/move heal and pass. Those numbers come from the committed
characterization suites and are cited, not regenerated here.

## Mechanism notes (why each tool did what it did)

- **Tool A3 / agent step**: the compiled artifact bound intent to a list
  position ("first task"). The agent then executed that literally and
  *knowingly*: under lookalike its own reasoning log recorded the four rows
  in order, identified the imposter row (Taylor Duplicate) as "first," set
  its next goal to click that row's Open button, and clicked it. The trailing
  extraction step — the workflow's verification — then reported the wrong
  patient's name back as a clean result, and the run still reported success,
  because nothing compares the extraction to intent. Verification exists;
  identity grounding does not.
- **Tool B cached script**: the cached artifact was minted against `p1` (the
  baseline run resolved and clicked Jane Sample's row). On replay, the
  text-matching selector resolves to the first match; under row drift the
  first match is a different row, the selector *succeeds*, so the AI fallback
  is never consulted — the direct analogue of our pre-fix finding that
  "confidence was highest precisely when the click was wrongest." The
  unrebuttable framing: nothing in cache-mode replay — selector, AI fallback,
  self-healing, or the run-final LLM completion verification — binds a replay
  to the entity the cache was minted on. The completion check passes because
  it is conditioned on the goal WE wrote ("open the first referral task …
  complete when the encounter-saved banner shows"), which is satisfied on the
  drifted row; it carries no notion of *which patient* the cached script
  originally targeted (see the goal-text caveat under fairness — an
  identity-naming goal is unmeasured). Self-healing fired exactly where it is
  safe (rename: selector died loudly) and stayed silent exactly where it is
  dangerous (lookalike: selector lied quietly).
- **Tool A1 stock**: unrunnable at the pinned version — the deterministic
  step runner filters `cssSelector` into "workflow metadata" while its own
  click model requires it, so every recorded cssSelector click fails
  validation and aborts the run (safe direction, zero availability).
  Separately, the advertised automatic agent fallback on failed deterministic
  steps has been commented out in code for over a year — the advertised
  self-healing behavior does not exist at HEAD; a failed step raises.
- **Tool A2 no-AI**: its semantic extractor names a button by the text of the
  *previous table cell*, so the three Open buttons map to keys
  'High'/'Medium'/'Low' and the recorded `target_text='Open'` can never
  match → hard error at the row click in every run including baseline. Safe,
  0% availability.
- **Tool C codegen**: because all three row buttons share the accessible name
  "Open", codegen fell back to `page.locator("#open-p1")` — and MockMed's DOM
  ids happen to encode patient identity, so the id anchor *is* an identity
  check. Row drift is absorbed trivially (lookalike/grow → correct patient)
  or fatally-but-safely (missing → timeout). This is an architecture artifact,
  not a general property: on apps with index-based or unstable ids the same
  recorder emits position-bound locators (e.g. `.first`/`nth()`), which would
  reproduce the wrong-row class. Label drift (rename) is fatal-but-safe.

## Spend accounting

Prices: frontier VLM list ~$3/M input, ~$15/M output; token counts are the
providers' own API-reported usage (cache-creation tokens counted at 1.25x and
cache reads at full price — conservative). The table below is grouped by
phase; the "runs" column is the exact number of ledger entries in each group.

| phase | runs | LLM calls | in / out tokens (sum) | USD |
|---|---|---|---|---|
| Tool A build (1 LLM call) | 1 | 1 | 4,697 / 2,596 | 0.0530 |
| Tool A3 replay (baseline + 6 drift × 2 harness variants) | 13 | 33 | 142,103 / 8,887 | 0.5596 |
| Tool A1/A2 (all runs) | — | 0 | — | 0.0000 |
| Tool B code-generation baseline (AI run) | 1 | 5 | 32,697 / 4,306 | 0.1627 |
| Tool B cached replays (baseline + 6 drift + 2 lookalike repeats) | 9 | 9 | 29,093 / 4,939 | 0.1614 |
| Tool C codegen (record + 7 replays + 7 beacon re-verify) | — | 0 | — | 0.0000 |
| **total** | | **48** | | **$0.9367** |

Per-run figures vary widely and should be read from the per-run digest, not
averaged from the group sums: the A3 baseline was a cheap 2-call run that
resolved the row on the geometry rung with a minimal agent turn, while each
A3 drift run made 2–3 calls at 7.4–16K input. Tool B cached replays are ~2.5K
tokens each except the two that triggered self-healing (baseline re-mint
6.7K; rename heal 5.0K).

## Methodology and fairness caveats

- **The drift modes were designed against OUR replayer's resolution
  strategy** (pixel templates with the name column outside the crop). A mode
  can be trivially absorbed by a different architecture (codegen's id-anchored
  locator) or trivially fatal to it (A2's extractor naming). That asymmetry is
  part of the result, not noise — but comparisons of *absolute* rates across
  architectures should carry this caveat.
- **Ground truth binds drift replays to the recorded/minted patient.** A tool
  whose artifact encodes "first row" is graded wrong when new data makes a
  different patient first. We consider this the correct grading for a recorded
  clinical workflow (and our own pre-fix system was graded the same way), but
  a reader who believes "first row" is the true intent should read the
  lookalike/grow rows as intent ambiguity rather than malfunction. On the
  `missing` row this objection is answered only for the **recording-based
  arm** (Tool A): it recorded Jane Sample (`p1`) specifically, so writing to
  the neighbour `p2` under `missing` is unambiguously wrong for it. The
  cached-script tool (B) is NOT recording-based — it got only the goal text
  "open the first referral task," so under `missing` its write to the new
  first row is goal-compliant by that text; the unrebuttable finding for it is
  not "it violated intent on missing" but the one stated in its mechanism
  note: its cached script was minted against `p1` and cache-mode replay
  contains no mechanism binding the replay to that entity.
- **The cached-script tool's completion verification is conditioned on the
  goal WE wrote**, which never names the patient. An identity-naming goal —
  e.g. "open Jane Sample's referral" instead of "open the first referral
  task" — might let the completion check catch the redirect. We did NOT test
  that; the goal-conditioned-not-identity-conditioned finding is scoped to the
  position-phrased goal, which is the natural phrasing for "open the first
  task" and the one a user replaying a list-processing workflow would write. A
  stronger goal is an available mitigation on that tool's side and is left
  unmeasured deliberately, not hidden.
- Each tool's intended use case differs from ours (Tool A is an
  early-development RPA project; Tool B is primarily an agent platform where
  cached scripts are an optimization; Tool C is a test-authoring aid). This
  study measures them only on the shared claim their deterministic/self-healing
  replay surfaces make.
- A3 required deleting legacy selector fields from the built artifact
  (disclosed above) because the stock pipeline cannot run at the pinned
  version; A1/A2 rows document the stock behavior. No tool source code was
  modified anywhere; the two shims (a converter method rename absorbing
  upstream bitrot, and a dummy API key to satisfy an import-time cloud-LLM
  constructor on a $0 path) are documented and behavior-neutral.
- Tool A's recorder masks passwords (`********` replayed verbatim); MockMed
  does not validate credentials, so no run outcome was affected. On a real
  login this would be a replay-blocking limitation of that pipeline.
- A2 and Tool C cannot parameterize the note (replay the recorded note); A3
  and Tool B received a distinct note per run and it was verified.
- Wall times are not comparable across arms (different browser stacks, server
  overhead) and are reported only for context.
- The cached-script tool's pure-agent mode was not run across the drift matrix
  — the study targets the deterministic-replay surface, and the baseline AI
  run already characterizes agent behavior once. Its drift behavior under
  agent mode remains unmeasured here.
- Runs per cell: A3 2x (identical), Tool B lookalike 3x (identical), all other
  cells 1x; no nondeterminism was observed in any repeated cell.

## Verdict

**The instrument thesis holds, with one architecture-shaped exception.**
Both LLM-era tools in this category whose self-healing replay path could
execute the task — the record/replay tool's semantic+agent pipeline and the
cached-script tool's replay mode — silently wrote a Triage encounter to the
WRONG PATIENT in 3/3 row-identity drift modes and reported success, the exact
silent-wrong-action class our own pre-fix replayer exhibited and our post-fix
identity gate now converts to safe-halts. In both tools the failure is
structural, not incidental: verification is goal- or completion-conditioned
and carries no notion of *which entity* the recording meant, so their checks
approved — and in one case literally printed the name of — the wrong patient.
The exception is instructive rather than exculpatory: the no-AI codegen tool
produced zero wrong actions here only because MockMed's DOM ids happen to
encode patient identity, turning its selector into an accidental identity
check — the strategy stops being available the moment ids are positional or
unstable, and its label-drift availability is poor (safe timeouts). Silent
wrong-action rate under row-identity drift is therefore a real, discriminating,
and to our knowledge unmeasured benchmark across this tool class — a
structural property of the architecture, our own pre-fix engine included, that
the instrument exists to surface. The finding is not "they are unsafe and we
are safe"; it is that identity-blind verification is a category-wide failure
mode, we exhibited it too, and the discriminator is whether a tool converts it
to a safe-halt — which ours does only after the five reopenings on the record
above.

## Reproduce

The harness is a study server that serves the unmodified MockMed app with a
state beacon, per-tool record/build/replay drivers, and an arm-independent
verdict module that classifies final app state. The MockMed app, drift
injector, ground-truth verdict logic, and our own engine's committed
pre/post-fix characterization are the reproducible core; per-tool drivers are
thin adapters over each tool's own record and replay entry points. Aggregate,
anonymized per-run results (tool class, drift, tool-claim, ground-truth
verdict, writes, token counts, note string) back every cell in the matrix
above. Raw per-run evidence and any tool-identifying artifacts are held
privately and are not part of this public category report.
