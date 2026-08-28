# openadapt-flow

[![CI](https://github.com/OpenAdaptAI/openadapt-flow/actions/workflows/ci.yml/badge.svg)](https://github.com/OpenAdaptAI/openadapt-flow/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/openadapt-flow)](https://pypi.org/project/openadapt-flow/)
[![Python](https://img.shields.io/pypi/pyversions/openadapt-flow)](https://pypi.org/project/openadapt-flow/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Record yourself doing a task in a browser or a desktop app. openadapt-flow
compiles the recording into a script that runs on your machine and makes zero
model calls on a healthy run. Before it reports success it reads the system of
record directly, on a path the app never touches, against the effects you
declared in the contract. Screen says saved, database holds nothing, the run
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
each step re-resolves through OCR or geometry and each fix comes back as a diff
you can read. No model calls on either side. Both runs are real and their
artifacts are in [`docs/showcase/`](docs/showcase).

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

## Point it at your own app

```bash
openadapt-flow record --backend web --url https://your.app --out rec
openadapt-flow compile rec --out bundle --name my-task
openadapt-flow lint bundle
openadapt-flow certify bundle --policy clinical-write
openadapt-flow replay bundle --backend web --url https://your.app
```

A native Windows app works the same way. Capture records the local window;
an in-guest agent drives it at replay:

```bash
openadapt-flow record --backend windows --window "Target App" --out rec
openadapt-flow compile rec --out bundle --name my-task
openadapt-flow replay bundle --agent-url http://localhost:5001
```

Citrix and VDI have no DOM and no accessibility tree, so Flow drives one exact
Workspace window through its pixels and won't send input until the readiness
text is on screen:

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
inherit from ours.

macOS, Linux, and network RDP follow the same three commands with their own
targeting flags: [docs/SURFACES.md](docs/SURFACES.md) has all six, and
[docs/PRODUCT_STATUS.md](docs/PRODUCT_STATUS.md) has the evidence under each.
A compiled bundle is bound to the surface it was recorded on, so `--backend` is
optional on `replay`.

Compiling a bundle is not the same as clearing it to run. `lint` reports what a
bundle failed to cover and grades each gap. `certify` enforces a policy and
exits nonzero, refusing the bundle before anyone deploys it. Two policies ship:
a permissive default and a strict `clinical-write.yaml`.

## How a step finds its target

Compilation stores five things per step: a template crop, an OCR label,
geometry landmarks, a structural locator, and postconditions derived from what
the demonstration changed on screen. At replay a ladder tries them in order,
starting with the structural element match and ending, only if you opt in, at a
grounding model. A healthy script resolves on the first rung in milliseconds.
Under drift a lower rung finds the same target and writes the fix back as a
reviewable diff. When nothing matches, the run halts rather than click
something plausible.

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
For the 500th referral this month, re-reasoning through the whole task on every
run costs money and makes no two runs alike, and at the end of it the agent
still reports success from what it saw on screen.

Two measured comparisons, both run 2026-07-08 on the same pre-v0.2.0 source
build:

| Task | Compiled replay | Computer-use agent |
|---|---|---|
| OpenEMR public demo, 18-step field run ([method](benchmark/openemr/BENCHMARK.md)) | 19/20 effect-verified, 39.2s median, $0 model cost; run 20 was a safe halt | 10/10, 70.4s median, about $0.55/run |
| MockMed bundled fixture, CI-reproducible ([method](benchmark/BENCHMARK.md)) | 100/100, 4.9s p50, $0 model cost | 20/20, 37.5s p50, about $0.27/run |

The OpenEMR run is the interesting one because the app is not ours: it's the
official public demo, with fake patients, that other people mutate and that
resets daily. That also makes it impossible to reproduce in CI, and the sample
is small.

Both of those are one rehearsed task. The number to look at before you trust
this on a workflow we have never seen is the breadth one, and it is worse: on
29 public web applications recorded and replayed once each
([method](benchmark/reliability/RELIABILITY.md)), all 29 compiled, 17 replays
verified, 10 halted safely, and 2 reported success while the independent oracle
disagreed. One observation per app is failure discovery, not a rate you can
plan against.

Method, caveats, the pinned Frappe lending environment, and EffectBench (the
standalone Silent Wrong-Effect Rate benchmark) are all in
[docs/BENCHMARKS.md](docs/BENCHMARKS.md).

RPA replays deterministically too, so the interesting comparison is at the
edges. RPA selectors break silently under drift, and RPA reads the session's
own success signals to decide whether it worked, which is exactly what
`--break-it` renders worthless. Dimension-by-dimension comparisons against
UiPath, Power Automate, and browser agents:
[openadapt.ai/compare](https://openadapt.ai/compare).

## What runs where

Record, compile, lint, certify, replay, and run are local. A healthy replay
makes no model calls, and the run report counts them so you can check. That is
not the same as no network: the app you're driving, a remote backend, and any
effect verifier all still talk to whatever they normally talk to. Model
grounding stays off unless you pass `--allow-model-grounding`.

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
is [docs/PRIVACY.md](docs/PRIVACY.md). At rest, `OPENADAPT_BUNDLE_KEY` seals
the workflow, the template crops, and durable checkpoints with AES-256-GCM.
Treat every source bundle as PHI: [docs/phi_at_rest.md](docs/phi_at_rest.md).

OpenAdapt Cloud at `app.openadapt.ai` is an optional managed control plane
covering browser workflows. Desktop and Citrix runs are self-hosted or on-prem.
See [docs/HOSTED.md](docs/HOSTED.md).

## Product state

Flow enters Production only through a signed, expiring, revocable release
admission. Missing, expired, revoked, or bound to a different release, and it
is **not actively admitted**. A release admission says nothing about your
workflow; each workflow carries its own. Current state is machine-readable at
[openadapt.ai/status.json](https://openadapt.ai/status.json).

Every claim this project makes in public is registered in
[`claims.yaml`](claims.yaml) with a tier and the tests or benchmark artifacts
that back it, and the build fails when a claim outranks its evidence. See
[docs/CLAIMS_AND_QUALIFICATION.md](docs/CLAIMS_AND_QUALIFICATION.md) and the
generated [docs/VERIFICATION.md](docs/VERIFICATION.md). For security review,
[docs/ENTERPRISE_ARCHITECTURE.md](docs/ENTERPRISE_ARCHITECTURE.md).

There is more here than this page covers: workflow programs with states, loops
and guarded transitions; data-driven `for-each` over a worklist; multi-trace
induction that quarantines an underdetermined intent instead of guessing;
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
