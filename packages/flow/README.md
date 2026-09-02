# openadapt-flow

[![CI](https://github.com/OpenAdaptAI/openadapt-flow/actions/workflows/ci.yml/badge.svg)](https://github.com/OpenAdaptAI/openadapt-flow/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/openadapt-flow)](https://pypi.org/project/openadapt-flow/)
[![Python](https://img.shields.io/pypi/pyversions/openadapt-flow)](https://pypi.org/project/openadapt-flow/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Record yourself doing a task in a browser or a desktop app. openadapt-flow
compiles the recording into a script that runs on your machine. The default
healthy path makes no generative-model API call. Before a governed run reports
`VERIFIED`, Flow confirms every declared effect through an independent
system-of-record read. A pixel-only Citrix path with no such read cannot be
`VERIFIED`: the run halts, or it ends `RECONCILIATION_REQUIRED`. The screen
says saved, the configured verifier finds no declared effect, and the run
stops.

It's for work you do the same way every week and have to be able to prove
afterwards: claims entry, referrals, eligibility checks, invoice posting. If
you're automating something once, use an agent instead.

[Docs](https://docs.openadapt.ai) ·
[Try it in your browser](https://app.openadapt.ai/demo) ·
[Website](https://openadapt.ai) ·
[Discussions](https://github.com/OpenAdaptAI/openadapt-flow/discussions)

![One demonstration, two UIs, same compiled script. The right side re-resolves under a theme it has never seen](docs/showcase/demo.gif)

Left: the UI the demo was recorded on. Right: a theme it had never seen, where
each step re-resolves through OCR or geometry and each proposed repair appears
in the run evidence as a diff you can read. Neither run makes a generative-model
API call. Both runs are real and their artifacts are in
[`docs/showcase/`](docs/showcase).

## Try it

```bash
pip install 'openadapt-flow[browser]'
openadapt-flow tutorial
```

Python 3.10 through 3.12; 3.13 isn't supported yet. On Windows `cmd.exe`,
quote with double quotes: `pip install "openadapt-flow[browser]"`.

`tutorial` records a demonstration against a bundled demo EMR, compiles it,
certifies the result against the shipped `clinical-write` policy, replays it,
and confirms the write by querying the record store out of band:

```
[1/5] Record the demonstration against a real persistence boundary
[2/5] Compile, mining the effect contract from the observed delta
      2 system-of-record effect(s) derived from the demonstration's record delta on step_005
[3/5] Certify against the clinical-write policy
[4/5] Admit and execute under the standard profile
      VERIFIED in 4.1s; 0 model calls; the system of record holds 1 record(s)
[5/5] Emit the local run receipt

VERIFIED: <out>/run/REPORT.md
  transaction     VERIFIED
  metering class  billable (this local tutorial was not reported or charged)
  profile         standard
  model calls     0
  effects         2/2 confirmed at evidence tier 1 (independent system of record)
```

That's real output from 1.34.0, run on macOS on 2026-08-28, with the run
directory shortened and the receipt paths cut. Now break it on purpose:

```bash
openadapt-flow tutorial --break-it
```

Same certified bundle, same script, but this time the backend rejects the write
*after* the app has painted its success banner. Every check that reads the
screen still passes:

```
  The screen claimed:  every on-screen check passed -- the app painted its success banner
                       (observed on screen: "Encountersaved-")
  The verifier found:  1/2 declared effect(s) REFUTED by an independent
                       read of the system of record, which holds 0 record(s)
  The engine did:      HALTED at the consequential step instead of claiming
                       success (transaction: RECONCILIATION_REQUIRED, billable: no)
```

The halted run writes a local report and no shareable receipt, because only a
`VERIFIED` run may use the success rail. It doesn't retry the write either:
delivery is uncertain, so the transaction ends in `RECONCILIATION_REQUIRED` for
a person to settle. Longer walkthrough, including the `--guided` presentation
mode and the hand-driven stages: [docs/TUTORIAL.md](docs/TUTORIAL.md).

## Reference Execute server

You can host the public Execute HTTP contract on this machine, in one process.

```bash
pip install 'openadapt-flow[execute]'
openadapt-flow serve-execute --port 8787 --seed-mockmed
```

That command binds loopback, generates an Ed25519 key on first start, and
keeps it under `~/.openadapt/execute-ref/`. Health is `GET /health`. Submit
`openadapt.execute-request/v1` to `POST /v1/executions`, poll
`GET /v1/executions/{id}` until `terminal`, then read
`GET /v1/executions/{id}/receipt`. The same process also speaks MCP at
`POST /mcp`.

Receipts are self-signed with that local key. They aren't OpenAdapt
production Seals. `GET /seals/{id}` is the local verify page; it shows the
key fingerprint and a $0 meter. Counterparties that require an OpenAdapt Seal
still use Cloud.

## Record and rehearse your workflow

Install the extras for the surface that will record and replay the workflow:

| Surface | Install |
|---|---|
| Browser | `pip install 'openadapt-flow[browser]'` |
| Native Windows | `pip install "openadapt-flow[capture,windows]"` |
| Native macOS | `pip install 'openadapt-flow[capture,macos]'` |
| Native Linux | `pip install 'openadapt-flow[capture,linux]'`, plus the AT-SPI packages in [`docs/desktop/LINUX_NATIVE.md`](docs/desktop/LINUX_NATIVE.md) |
| Network RDP | Install `openadapt-flow[capture]` in the recorded session and `openadapt-flow[rdp]` on the runner |
| Local RDP or Citrix client window | On macOS: `pip install 'openadapt-flow[capture,macos]'`. On Windows: `pip install "openadapt-flow[capture,windows]"` |

Start with a local browser rehearsal:

```bash
openadapt-flow record --backend web --url https://your.app --out rec
openadapt-flow compile rec --out bundle --name my-task
openadapt-flow qualify propose bundle --recording rec --out proposal.json
openadapt-flow qualify accept bundle --proposal proposal.json
openadapt-flow lint bundle --strict
openadapt-flow replay bundle --backend web --url https://your.app
```

Demo once, get a checked program. `qualify propose` fills the production-shaped
pins from the recording: application identity, environment fingerprint,
identity-gate fields, and the effect oracle from the write the demo actually
observed. `qualify accept` confirms every pin in one command. If a pin isn't
there, Flow HALTs. It will not guess.

`qualify accept` also runs that proposed oracle against a `--break-it` fault
before it can succeed. MockMed is the default fixture: the banner says the
row was saved, the store did not change. If the oracle would have accepted
the lie, the proposal stays draft or halted. Actor bytes and oracle bytes
must be disjoint. Re-reading the acting screen or the same-session banner
HALTs and names the shared channel. An API, SQL, file, or second-session
read is allowed; Flow will not invent an endpoint. No second read in the
recording means HALT: do not automate until a second read exists.

`--policy-pack community` is the local/MockMed pack. `cloud` and `regulated`
bind the stricter shipped policy. They do not skip a pin. On MockMed, add
`--admit-local` to mint a signed local admission. That test key cannot enter a
production trust map. The pin list and the failure matrix (`--break-it`, plus
identity-swap or extra-field when the demo has parameters) are in
[`docs/QUALIFICATION_PROJECT.md`](docs/QUALIFICATION_PROJECT.md).

To generalize a task from several recordings, induce a program:

```bash
openadapt-flow induce rec1 rec2 --out program
```

`induce` emits a program when the traces agree, and a `record-next:` worklist of missing demonstrations when a consequential branch or loop stays underdetermined. The healthy replay path still makes no model call.

`replay` is the permissive rehearsal path. It stays available while a bundle has
certification gaps. For governed execution, complete the remaining idempotency
and postcondition contracts, then use the gated path:

```bash
openadapt-flow certify bundle --config deploy.yaml
openadapt-flow run bundle --profile standard --config deploy.yaml
```

`run --profile standard` enforces the policy again. It still refuses the bundle
if the standalone `certify` command failed. A new recording will usually fail
`clinical-write` until its contracts are complete. Two built-in policies ship:
`permissive` and `clinical-write`.

For a native Windows app, Capture records one local window and an in-guest agent
drives it at replay:

```bash
openadapt-flow record --backend windows --window "Target App" --out rec
openadapt-flow compile rec --out bundle --name my-task
openadapt-flow replay bundle --agent-url http://localhost:5001
```

Citrix and VDI have no DOM or accessibility tree. This macOS example drives one
exact Workspace window through its pixels and won't send input until the
readiness text is on screen:

```bash
openadapt-flow record --backend citrix --window "Citrix Viewer" \
  --rdp-window "Citrix Viewer" --rdp-window-title "Ward A" \
  --rdp-readiness-text "Appointments" --out rec
openadapt-flow compile rec --out bundle --name my-task
openadapt-flow replay bundle --rdp-window "Citrix Viewer" \
  --rdp-window-title "Ward A" --rdp-readiness-text "Appointments"
```

That path is qualified against a no-DOM stand-in, 3 healthy runs and 3 drift
halts, and the retained artifact records `ica_hdx_accepted=false`, so a live
ICA/HDX session is something you qualify in your own deployment rather than
inherit from ours. Pixels plus a green banner are not a system-of-record
read. Without REST, FHIR, SQL, a file oracle, or a separately authenticated
session, Standard and Regulated cannot report `VERIFIED`; they halt or return
`RECONCILIATION_REQUIRED`. See [docs/LIMITS.md](docs/LIMITS.md).

macOS, Linux, network RDP, and Windows-hosted Citrix have different target
flags. Some apply only to `replay` or `run` because Capture can't use them.
[docs/SURFACES.md](docs/SURFACES.md) has the exact commands for all six
surfaces, and [docs/PRODUCT_STATUS.md](docs/PRODUCT_STATUS.md) gives the evidence
for each one. A compiled bundle is bound to its recorded surface, so `--backend`
is optional on `replay`.

Compiling a bundle is not the same as clearing it to run. `lint` reports what a
bundle failed to cover and grades each gap. Without `--strict`, warnings don't
make `lint` exit nonzero. `certify` enforces the selected policy and refuses a
bundle that doesn't meet it.

### Workflows that use more than one application

A URL or window flag selects the execution surface, not the number of screens
in the task. One bundle can move through screens and same-origin routes inside
its bound surface. An RDP workflow can also switch among windows inside one
remote desktop. The public [multi-window fixture](benchmark/rdp_multiapp/README.md)
is designed to exercise that path through one FreeRDP backend. Its three task
windows belong to one synthetic fixture process, and the repository does not
contain a completed campaign result. A real deployment needs its own
qualification.

The boundary is one backend per bundle. Worklists repeat that bundle over input
records, and subflows reuse steps inside it. Neither one switches backends.
Browser recording owns one tab and refuses popups or new tabs. Browser attach
mode stays on one origin. macOS and Linux bind one exact app and window, while a
governed Windows run binds its application identity.

If a task crosses a browser and a native app, or otherwise changes backend,
record one bundle per surface. `openadapt-flow compose` sequences the compiled
bundles:

```bash
openadapt-flow compose \
  --child intake=./intake-bundle \
  --child posting=./posting-bundle \
  --handoff intake.patient_id=posting.patient_id \
  --out composed
openadapt-flow certify composed --policy clinical-write
openadapt-flow run composed --config deploy.yaml
openadapt-flow visualize composed -o composed.html
```

Child A must end `VERIFIED` (or a halt class you named with `--allow-halt`)
before child B starts. Handoffs copy parameter values that A's confirmed
effect contract already bound. The parent will not guess a window title or a
URL. Missing evidence stops the run. Qualify each handoff and the end-to-end
result verifier before deployment.

`visualize` on that parent draws one node per child, on the surface that child
was recorded on. Edges follow `--after`. Handoff edges label the effect-bound
parameters they copy. The parent ends at "End of declared steps", which isn't
a live `VERIFIED` verdict. See [docs/VISUALIZE.md](docs/VISUALIZE.md).

Compose will not retarget one recording onto a second backend. Program authors
can bind individual steps to different HTTP systems, but that is API actuation
rather than recorded GUI backend switching. If you installed the OpenAdapt
launcher, `openadapt flow compose` is the same command.

A V0 process contract sequences Flow children that already have signed
qualification admissions. The parent names the order or DAG and the confirmed
effect-bound facts that may copy as handoffs:

```bash
openadapt-flow process \
  --child intake=./intake-bundle \
  --admission intake=./intake-admission.json \
  --child posting=./posting-bundle \
  --admission posting=./posting-admission.json \
  --handoff intake.patient_id=posting.patient_id \
  --out process
openadapt-flow certify process --policy clinical-write
openadapt-flow run process --config deploy.yaml
```

`openadapt-flow replay process` refuses; governed `run` is the path, same as
compose. `openadapt flow process` is the launcher form. Compose still sequences
recordings. Don't wrap a `composition.json` and call it admitted. Operator
detail lives in [docs/PROCESS_CONTRACT.md](docs/PROCESS_CONTRACT.md).

ProcessContract v1 adds sealed Python children, signed human tasks, and
verified content-addressed artifact edges to that same parent:

```bash
openadapt-flow process --spec process-v1.json --out process
openadapt-flow run process \
  --run-dir runs/process-001 \
  --code-trust code-signers.json \
  --code-runtime-environment-digest sha256:... \
  --allow-trusted-code \
  --process-receipt-private-key runner-ed25519.key \
  --config deploy.yaml
```

An admitted transform doesn't prove its output. The named verifier must confirm
the exact artifact digest before another child can read it. Human completion
records authority and intent; the declared verifier still proves the effect.
`RECONCILIATION_REQUIRED` stops the parent and is never retried as a general
halt. The [V1 design](docs/design/PROCESS_CONTRACT_V1.md) records the execution,
authentication, portability, and isolation boundaries.

## How a step finds its target

An anchored step keeps the evidence available on its execution surface. A
browser or native step can carry a structural locator alongside template, OCR,
and geometry evidence. A pixel-only step starts with visual evidence, while a
pure keyboard or wait step may have no anchor at all. At replay the ladder tries
structure first when it exists, then the available visual rungs. Model grounding
stays off unless you enable it in the CLI or deployment config.

A healthy step stops on its first valid match. Under drift, a lower rung can
resolve the same target. The runtime records the proposed repair under the
run's `heals/` evidence. Pass `--save-healed-to` to create a complete candidate
bundle for review; Flow never promotes that candidate into the active workflow
automatically. When no rung matches, the run halts rather than click something
plausible.

The runtime drives a pure pixel surface when that is all there is, and uses the
structured layer as the top rung wherever one exists. On a desktop drift
benchmark the structural rung resolved 21/21 targets where visual replay alone
managed 6/21
([`benchmark/structural_action/`](benchmark/structural_action/STRUCTURAL_ACTION.md)).

Structure never skips the identity gate. Rung-by-rung detail:
[docs/RESOLUTION_LADDER.md](docs/RESOLUTION_LADDER.md).

## When you shouldn't use this

- **The task has an API.** Call it. Driving a GUI is what you do when the
  vendor gave you no other door.
- **You'll run it twice.** Recording, compiling, certifying, and writing an
  effect contract costs more than doing it by hand or pointing an agent at it.
- **You want a Citrix number you can quote.** The pixel path runs, but its
  counted evidence is a stand-in, not a live ICA/HDX session. Yours is a
  qualification exercise, not a lookup.
- **You want the halt behaviour without doing the work.** Effect verification
  only fires against effects you declared. `scaffold-verifier` drafts a contract
  from a recording's write-shaped steps, and the draft needs a human to edit it.
  Skip that and you're back to trusting the screen, which is the failure mode
  `--break-it` demonstrates.
- **You expect every click to be checked.** The identity gate covers *armed*
  steps, and bundles today arm a subset: the live OpenEMR bundle armed 4 to 7
  of 12. An unarmed click has no identity check.

The full boundary, capability by capability, with what each evidence basis does
and doesn't mean: [docs/LIMITS.md](docs/LIMITS.md).

## Against agents and RPA

For a task nobody has automated before, an agent is the right tool. Use one.
For the 500th referral this month, compiled replay follows retained evidence and
doesn't need a model call on each healthy run. The comparisons below cover two
exact benchmark configurations, not every agent or RPA product. Both arms drove
the same interface and used the same OCR success check. Neither arm's own
success report counted.

Both comparisons ran on 2026-07-08 from a pre-v0.2.0 checkout declaring Flow
0.1.0. The exact runtime commit wasn't retained, so these aren't current-release
numbers:

| Task | Compiled replay | Computer-use agent |
|---|---|---|
| OpenEMR public demo, 18-step field run ([method](benchmark/openemr/BENCHMARK.md)) | 19/20 under the saved-row OCR check, 39.2s p50, 0 recorded model API calls and $0 in model API charges; run 20 halted safely | 10/10 under the same check, 70.4s p50, about $0.55/run in model API charges |
| MockMed bundled fixture, CI-reproducible ([method](benchmark/BENCHMARK.md)) | 100/100 under the OCR check, 4.9s p50, 0 recorded model API calls and $0 in model API charges | 20/20 under the same check, 37.5s p50, about $0.27/run in model API charges |

The dollar figures cover model API usage at list price. They don't include
infrastructure, authoring, review, or maintenance.

The OpenEMR run is the interesting one because the app is not ours: it's the
official public demo, with fake patients, that other people mutate and that
resets daily. That also makes it impossible to reproduce in CI, and the sample
is small.

Method, caveats, the pinned Frappe lending environment, and EffectBench (the
standalone Silent Wrong-Effect Rate benchmark) are all in
[docs/BENCHMARKS.md](docs/BENCHMARKS.md).

RPA products differ in how they repair selectors and verify effects. Compare
their evidence contracts. Start with target selection and independent result
proof, then inspect what happens after uncertain delivery. See the
dimension-by-dimension comparison with UiPath, Power Automate, and browser
agents at [openadapt.ai/compare](https://openadapt.ai/compare).

## What runs where

Record, compile, compose, lint, certify, replay, and run are local. By default, a healthy
replay makes no generative-model API call. Grounding, identity, and state
verification integrations can make calls when enabled. This is not the same as
no network: the app you're driving, a remote backend, and any effect verifier
still talk to their configured services. Treat the run's model-call counter as
diagnostic data, not provider billing telemetry.

For regulated work, PHI scrubbing on the persist and log paths comes from the
`privacy` extra, backed by
[openadapt-privacy](https://github.com/OpenAdaptAI/openadapt-privacy):

```bash
pip install 'openadapt-flow[privacy]' && python -m spacy download en_core_web_sm
export OPENADAPT_FLOW_SCRUB=on
```

That scrubs the shareable report and the console logs. The bundle and
`report.json` keep literal identifiers on purpose, because the identity check
and the audit trail need them. The full map of what is scrubbed and what isn't
is [docs/PRIVACY.md](docs/PRIVACY.md). To encrypt a bundle at rest, set
`OPENADAPT_BUNDLE_KEY`, then make a sealed copy:

```bash
openadapt-flow seal ./bundle --out ./bundle-sealed
```

The command encrypts `workflow.json` and the template crops with AES-256-GCM.
An encrypted governed run reuses the key for its durable checkpoints. Other
bundle files aren't covered by this command, and every source bundle must still
be treated as PHI. See [docs/phi_at_rest.md](docs/phi_at_rest.md).

OpenAdapt Cloud at `app.openadapt.ai` is an optional managed control plane
covering browser workflows. Desktop and Citrix runs are self-hosted or on-prem.
See [docs/HOSTED.md](docs/HOSTED.md).

## Production admission

Flow reports `Production` only while a signed, expiring, revocable release
admission is valid for the exact build. Workflow admission is separate: a
release admission doesn't qualify a customer's bundle, application,
environment, or verification contract. The current admission state is
machine-readable at
[openadapt.ai/production-lifecycle.json](https://openadapt.ai/production-lifecycle.json).

The registry in [`claims.yaml`](claims.yaml) assigns each registered public
capability claim a tier and names the tests or benchmark artifacts behind it.
The build fails when a registered claim outranks its evidence. See
[docs/CLAIMS_AND_QUALIFICATION.md](docs/CLAIMS_AND_QUALIFICATION.md) and the
generated [docs/VERIFICATION.md](docs/VERIFICATION.md). For security review,
[docs/ENTERPRISE_ARCHITECTURE.md](docs/ENTERPRISE_ARCHITECTURE.md).

There is more here than this page covers: workflow programs with states, loops
and guarded transitions; data-driven `for-each` over a worklist; composing
separately recorded bundles; multi-trace induction that quarantines an
underdetermined intent instead of guessing;
pluggable SQL, REST, FHIR and document-hash effect oracles; durable
checkpoint and resume; Agent Skill and MCP emission. Those are in
[docs/CAPABILITIES.md](docs/CAPABILITIES.md), and the whole documentation set
is at [docs.openadapt.ai](https://docs.openadapt.ai).

## Development

```bash
git clone https://github.com/OpenAdaptAI/openadapt-flow && cd openadapt-flow
pip install -e '.[dev]'
playwright install chromium   # optional; otherwise downloaded on first launch
pytest -q
```

Contributions welcome, see [CONTRIBUTING.md](CONTRIBUTING.md). If you want a
first one that is genuinely useful: pick a module off the mypy type-debt
burn-down list (`[[tool.mypy.overrides]]` in `pyproject.toml`), tighten its
annotations, and delete it from the list.

The demo GIF is generated from real run artifacts by
`scripts/make_demo_gif.py`.

## License

OpenAdapt-authored package code is licensed under the [MIT License](LICENSE).

A Git checkout or GitHub-generated source archive also contains an isolated
openIMIS reference environment with adapted configuration files under
`AGPL-3.0-only`. The MIT license does not relicense those files. Their exact
provenance, file-local scope, and complete upstream license are recorded in the
repository-only
[third-party notice](https://github.com/OpenAdaptAI/openadapt-flow/blob/main/THIRD_PARTY_NOTICES.md).

Published PyPI wheels and source distributions exclude the openIMIS benchmark
surface and stay within the declared MIT package boundary.
