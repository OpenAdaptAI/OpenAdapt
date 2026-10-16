# OpenAdapt

[![CI](https://github.com/OpenAdaptAI/OpenAdapt/actions/workflows/main.yml/badge.svg)](https://github.com/OpenAdaptAI/OpenAdapt/actions/workflows/main.yml)
[![PyPI](https://img.shields.io/pypi/v/openadapt.svg)](https://pypi.org/project/openadapt/)
[![Python 3.10–3.12](https://img.shields.io/badge/python-3.10%E2%80%933.12-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-3e6b4f.svg)](LICENSE)
[![Discord](https://img.shields.io/badge/Discord-community-5865F2?logo=discord&logoColor=white)](https://discord.gg/yF527cQbDG)

**A person or an agent shows OpenAdapt a task in your EHR, portal, or desktop app. It becomes a program on your computer that repeats the same steps with new details, with no model calls.**

By default, a person approves each save. Where your system allows a
second read, OpenAdapt reads the saved record back. When something doesn't
match, it stops and asks a person instead of guessing.

To watch it work first, open the [live demo](https://app.openadapt.ai/demo).
It shows recorded runs that use synthetic data.

**Status:** the engine is free, MIT-licensed, and installs from PyPI. Each
workflow is tested on its own app and environment before it runs on real
records.

| Platform | How OpenAdapt drives it | Tested so far |
|---|---|---|
| Web apps | Playwright (Chromium) | Every change in CI; OpenEMR benchmark below |
| Windows apps | UI Automation | 3 of 3 trials on an in-tree WinForms app |
| macOS apps | Accessibility | 3 of 3 trials on TextEdit, one Mac |
| Linux apps | AT-SPI on X11 | A GTK3 app in CI; Wayland needs a portal session |
| Remote desktop (RDP) | Screen pixels, keyboard, mouse | 3 of 3 trials into Windows 11 over a real network |
| Citrix | Screen pixels, keyboard, mouse | A stand-in without a live Citrix session |

[What works today](https://docs.openadapt.ai/get-started/what-works-today/)
has the details for each platform.

## One referral, three ways

| | By hand | AI agent | OpenAdapt |
|---|---|---|---|
| Median time per referral | Not timed* | 36.5 s | 29.7 s |
| Correct saves, all warm runs | Not scored | 69 of 70 | 103 of 105 |
| Model fees per referral | None | about $0.08 (median) | $0 |
| Screens sent to a model | None | 10 (median) | None |
| Path | Up to the person | 48 actions (median), varies | Same 41 steps |

*One baseline run at a person's pace, not timed across repeated runs. By hand: one run of a test script at a person's pace. AI agent: Claude Sonnet, list price.

Same test OpenEMR, same synthetic referrals, 14 and 15 Oct 2026. Times run from sign-in to a saved record that a separate database query read back, 12 of 12 fields. The medians are over 62 timed warm runs each (the first block of each arm is left out because another process shared the CPU then), so OpenAdapt was about 19% faster. The two OpenAdapt misses stopped without saving a chart; the agent miss finished without saving one. Engine certification was off in this benchmark, so these runs end "finished, not checked" by the engine's own label; the separate read-back is what scored them. [How we measured](https://openadapt.ai/compare/computer-use-agents).

<!--
  IMAGE PLACEHOLDER for the design wave. Replace this comment with the image.
  File: media/before-after-referral.png, 1600x800, solid light background so it
  reads in GitHub dark mode. Label it "Illustration".
  Reference it by its absolute raw.githubusercontent.com URL. PyPI shows this
  README too, and a relative image path doesn't render there.
  Left column, "Today": a staff member keys a referral into a generic EMR
  window and trusts the "Saved" banner.
  Right column, "With OpenAdapt": the same screens, the saved record read back
  and compared with the input, and a mismatch that stops the run and sends one
  question to a person.
  No vendor trade dress or logos. Alt text must describe both columns.
-->

## What changes for your team

Here's one example: a faxed referral that staff enter into the EMR.

| | Today | With OpenAdapt |
|---|---|---|
| Entering it | A staff member keys each referral by hand. Two hospital time studies put one faxed referral at about 10 to 12 minutes of staff time. | OpenAdapt enters the approved fields in the same EMR screens your staff use. |
| Knowing it saved | Someone sees "Saved" and moves on. | OpenAdapt reads the saved record back and compares it with what it entered. |
| When something's off | It turns up later, at booking or billing. | The run stops before it guesses, and a person decides what happens next. |
| Your staff's part | Type every referral. | Review the runs that OpenAdapt stops on. |
| What you set up | Nothing new. | No EMR integration. The record check needs a second way to read the record, such as a report, an API, or a read-only login. |

Sources for the "Today" times:
[UCSF Pediatric Access Center, JAMIA Open 2020](https://pmc.ncbi.nlm.nih.gov/articles/PMC7660949/)
(mean 719 seconds per faxed referral, including a call to the patient), and
[Calderdale and Huddersfield NHS Foundation Trust, HFMA case study 2024](https://www.hfma.org.uk/system/files/2024-04/Using%20digital%20technologies%20to%20process%20admin%20tasks%20Case%20Study%20v7.pdf)
(10 minutes per referral). Nobody has measured an "after" time with OpenAdapt
yet, so the table doesn't claim a time saving.

## What you see and control

Every run ends with one of these results:

| Result | What it means |
|---|---|
| Done and checked | It saved the entry and read the record back to confirm it. |
| Stopped before saving | Something didn't match, so it stopped before it changed anything and asked a person. Nothing was written. <!-- vocab-allow: no-change-claim -- defines the result that only a proven no-effect outcome gets --> |
| Blocked before saving | A rule, an approval, or the right-record check blocked the run before it saved anything. Nothing was written. <!-- vocab-allow: no-change-claim -- defines the result that only a proven no-effect outcome gets --> |
| Check the record | A save may have gone through. A person checks the record before anything is retried. |
| Finished, not checked | It did the steps but didn't confirm the saved result, as in a practice run. |
| Didn't finish | It couldn't complete the task. Its run report says what's known about whether anything was written. |

- You get a run report for every run. A run that's done and checked also
  gets a receipt that shows what was checked and the result. The receipt
  leaves out typed values and screenshots.
- Before it types, it can confirm that it's in the right patient's chart.
- When a screen changes, it finds the same field again or stops. A change to
  the automation goes live only after a person approves it.
- It runs on a computer you control, or on one we host. Recordings and screens
  stay on that computer unless you approve sharing them.
- A routine run makes no AI model calls.

## Two ways to start

### Have a workflow to hand off

To hand off a task your team repeats, tell us about it at
[openadapt.ai/contact](https://openadapt.ai/contact).

1. You show us the task once, on your own system.
2. We build the automation and test it with your own cases before it goes
   live.
3. We keep it working. When a screen changes, we propose a fix and a person on
   your team approves it.

We scope services per workflow. Hosted plans are on the
[pricing page](https://openadapt.ai/pricing). If your own AI product decides
what to enter, OpenAdapt can make that final entry for it and return the
result. See [OpenAdapt Execute](https://openadapt.ai/execute).

### Run it yourself

The engine is free and open source under the MIT license. With Python 3.10,
3.11, or 3.12, run:

```bash
python -m pip install --upgrade openadapt
openadapt quickstart --break-it
```

This runs a synthetic clinic task twice on your computer. The first run is
done and checked. In the second run, the test server rejects the save after
the app shows that it saved. The record check finds no saved record, so the
run stops and leaves the record for a person to check. On Python 3.13 or
newer, install with `uv tool install --python 3.12 openadapt` instead.

The [developer guide](#developer-guide) below covers authoring your own
workflow, and [Evidence](#evidence) lists what we've measured so far.

## Give your agent a program, not a mouse

Your agent decides what to enter. Instead of sending screenshots to a model
and clicking, it calls a taught workflow as one tool and gets back an outcome:
done and checked, stopped before saving, check the record, or didn't run.

```bash
openadapt agent serve --bundles <bundle or directory of bundles> --allow-run
openadapt agent emit-skill <bundle> --out ~/.claude/skills
```

`serve` starts an MCP server over stdio; leave out `--allow-run` to let the
client inspect workflows without starting them. `emit-skill` writes an Agent
Skill that tells the agent how to call the workflow. See
[Give an agent the run tool](#give-an-agent-the-run-tool) and the
[`openadapt-agent` README](https://github.com/OpenAdaptAI/openadapt-agent).

---

## Developer guide

[Documentation](https://docs.openadapt.ai) ·
[Start locally](https://openadapt.ai/start) ·
[Walkthrough](https://docs.openadapt.ai/get-started/) ·
[Desktop downloads](https://openadapt.ai/download) ·
[Live demo](https://app.openadapt.ai/demo) ·
[Website](https://openadapt.ai) ·
[Discord](https://discord.gg/yF527cQbDG)

A person, or a local agent that person watches, records a demonstration.
OpenAdapt compiles it into a program that runs again without a
generative-model API in the control loop. A named person admits that program
before production use. Before a governed run reports `VERIFIED`, it checks
every declared effect at the required evidence tier. A caller must report a
halt as a halt, never as success.

Computer-use agents can call OpenAdapt programs. They aren't the executor
inside OpenAdapt. When an agent clicks during a demonstration, the person who
admits the program watches.

This repository is the installer and the `openadapt` command. The compiler and
the runtime live in
[`openadapt-flow`](https://github.com/OpenAdaptAI/openadapt-flow), and this
repository doesn't reimplement them.
[`openadapt-agent`](https://github.com/OpenAdaptAI/openadapt-agent) exposes
compiled programs to an agent over MCP.

## Run the tutorial

A solutions engineer gets `VERIFIED` on synthetic MockMed:

```bash
python -m pip install --upgrade openadapt
openadapt doctor --backend web
openadapt flow tutorial
```

OpenAdapt supports Python 3.10 through 3.12. The engine doesn't install on
Python 3.13 or newer yet. There, use `uv tool install --python 3.12 openadapt`,
which fetches Python 3.12 for you. The local tutorial needs no account or API
key. The base install includes Playwright. On the first browser action,
OpenAdapt asks Playwright to download its matching Chromium build. That
download needs network access.

Minimal Linux hosts can lack Chromium's shared libraries. `openadapt doctor`
lists the missing libraries before any browser download. If it finds any, run:

```bash
python -m playwright install-deps chromium
openadapt doctor --backend web
```

Playwright asks for administrator access when the package manager needs it.
Use the interpreter-specific command from `doctor` if you installed OpenAdapt
with an isolated tool runner.

`openadapt flow tutorial` is the launcher spelling of `openadapt-flow tutorial`.
Every `openadapt-flow <verb>` command works as `openadapt flow <verb>`, and
that form also works after `uv tool install`, which puts only `openadapt` on
your PATH. The tutorial records and compiles a task in MockMed, a synthetic
practice-management fixture, certifies it against the shipped clinical-write
policy, runs it under the Standard profile, then confirms the saved record
through a read-only API that the screen doing the writing never touches:

```
[1/5] Record the demonstration against a real persistence boundary
[2/5] Compile and propose a fixture-specific effect contract
      2 system-of-record effect proposal(s) derived from the fixture's record delta on step_005; review is required for a real deployment
[3/5] Certify against the clinical-write policy
[4/5] Admit and execute under the standard profile
      VERIFIED in 9.1s; 0 model calls; the system of record holds 1 record(s)
[5/5] Emit the local run receipt

VERIFIED: <out>/run/REPORT.md
  transaction     VERIFIED
  metering class  billable (this local tutorial was not reported or charged)
  profile         standard
  model calls     0
  effects         2/2 confirmed by an independent system-of-record read (Seal Oracle tier 2)
```

That's real output from Flow 1.35.1 on macOS, 2026-10-08, with the run
directory shortened and the bundle digest left out. The tutorial writes the
recording, the compiled bundle, and the run receipt under its output directory
(default `tutorials/tutorial-<UTC timestamp>`). `openadapt quickstart` is a
wrapper around the same engine path.

Then watch it refuse to trust the screen:

```bash
openadapt quickstart --break-it
```

Same certified bundle. This time the backend rejects the write after the app
has already painted its success banner, so every on-screen check passes:

```
  The screen claimed:  every on-screen check passed -- the app painted its success banner
  The verifier found:  1/2 declared effect(s) REFUTED by an independent
                       read of the system of record, which holds 0 record(s)
  The engine did:      HALTED at the consequential step instead of claiming
                       success (transaction: RECONCILIATION_REQUIRED, billable: no)
```

The independent read finds no saved record, so the run halts. Its transaction
outcome is `RECONCILIATION_REQUIRED`, so nothing is retried until a person
checks the record.

The bundled workflow is a tutorial. Qualifying a real one means declaring its
application boundary, its action risks, its identities, its effect verifiers,
its fault cases, and its deployment policy. Start with the
[five-minute walkthrough](https://docs.openadapt.ai/get-started/).

Tutorial `VERIFIED` is a local receipt on synthetic MockMed. It isn't a
production Seal. When you qualify a real job, the same independent check is
what a Seal attests. Public synthetic verify:
[openadapt.ai/seals](https://openadapt.ai/seals). The Seal contract:
[docs/commercial/seal.md](https://docs.openadapt.ai/commercial/seal/).

Inspect what compiled, and what it failed to cover:

```bash
openadapt flow visualize <out>/bundle --out graph.html
openadapt flow lint <out>/bundle
```

## Give an agent the run tool

`openadapt-agent` serves compiled programs to an MCP client. Launcher releases
after 1.16.0 install it with `openadapt`. With 1.16.0, install it by name:

```bash
python -m pip install --upgrade openadapt-agent
openadapt-agent serve --bundles <bundle or directory of bundles> --allow-run
```

`uv tool install` and pipx put only `openadapt` on your PATH. With a launcher
release after 1.16.0, run `openadapt agent serve ...` there instead. It passes
every argument to `openadapt-agent`.

Leave out `--allow-run` when the client should inspect workflows but not start
them. A halted or refused run comes back as a halt or a refusal, never as a
success. Local unsigned success isn't a Seal; treat unsigned production success
as failure. The
[`openadapt-agent` README](https://github.com/OpenAdaptAI/openadapt-agent) has
the client setup.

## Author a workflow

Do not pass `--backend`. With no `--url`, record captures this OS. Pass
`--url` when the surface is a browser.

```bash
openadapt flow record --out rec
openadapt flow record --url https://staging.your-app.example --out rec
openadapt flow compile rec --out bundle --name my-workflow
openadapt flow replay bundle --url https://staging.your-app.example --run-dir run
```

`replay` drives the real app at `--url` and performs every recorded action,
including saves you can't undo. It doesn't ask first, it isn't a dry run, and
it doesn't certify the bundle. Point `--url` at a test or staging copy of the
app, never at production data.

One bundle uses one execution surface and does not switch between browser,
native, RDP, or Citrix backends.

If the task crosses a browser and a native app, or otherwise changes backend,
record one bundle per surface. `openadapt flow compose` sequences the compiled
bundles:

```bash
openadapt flow compose \
  --child intake=./intake-bundle \
  --child posting=./posting-bundle \
  --handoff intake.patient_id=posting.patient_id \
  --out composed
openadapt flow certify composed --policy clinical-write
openadapt flow run composed --config deploy.yaml
```

Child A has to end `VERIFIED` (or a halt class you named with `--allow-halt`)
before child B starts. Handoffs copy parameter values that A's confirmed
effect contract already bound. Missing evidence stops the run. Compose will
not retarget one recording onto a second backend. The
[`openadapt-flow` README](https://github.com/OpenAdaptAI/openadapt-flow#workflows-that-use-more-than-one-application)
has the full boundary.

Native and remote workflows never start or download Chromium. Install only the
capability you need:

```bash
python -m pip install "openadapt[capture]"          # local human demonstration
python -m pip install "openadapt[capture,windows]"  # Windows UI Automation
python -m pip install "openadapt[capture,macos]"    # macOS Accessibility
python -m pip install "openadapt[capture,linux]"    # Linux AT-SPI
python -m pip install "openadapt[capture,rdp]"      # RDP transport
python -m pip install "openadapt[privacy]"          # PII/PHI scrubbing
```

For visual authoring and review, there's
[OpenAdapt Desktop](https://openadapt.ai/download).

## How a run ends

A click landing is not evidence that a transaction committed. Every terminal
run says what the runtime actually knows about the business effect, and these
outcomes are not interchangeable:

| Outcome | Meaning |
|---|---|
| `VERIFIED` | Every declared effect and collateral-effect check passed at the required evidence tier. The only production success. |
| `HALTED_BEFORE_EFFECT` | The run stopped, with positive evidence that no consequential effect occurred. |
| `RECONCILIATION_REQUIRED` | Delivery or persistence is uncertain, conflicting, or temporarily unverifiable. Never blind-retried. |
| `FAILED_PLATFORM` | An OpenAdapt failure before any possible business effect. |
| `CANCELED` | Canceled before any business effect. |
| `REJECTED_POLICY` | Authorization, identity, qualification, or environment policy refused execution before any effect. |
| `COMPLETED_UNVERIFIED` | A Demo run finished without production-grade effect evidence. |
| `ROLLED_BACK` | A duplicate or collateral write was compensated and re-verified. |

The plain results at the top of this page map to these outcomes:

| Plain result | Transaction outcome |
|---|---|
| Done and checked | `VERIFIED` |
| Stopped before saving | `HALTED_BEFORE_EFFECT`, `CANCELED` |
| Blocked before saving | `REJECTED_POLICY` |
| Check the record | `RECONCILIATION_REQUIRED`, `ROLLED_BACK`, and a `HALTED` run with no transaction outcome |
| Finished, not checked | `COMPLETED_UNVERIFIED` |
| Didn't finish | `FAILED_PLATFORM` |

A surface derives the plain result from the transaction outcome, never from a
coarse `HALTED` label alone. A value a surface doesn't recognize reads as
Check the record.

A resumed `RECONCILIATION_REQUIRED` run has to reacquire and reconcile the live
state before it does anything else.

Before a consequential action the runtime can check authorization, workflow
state, record identity, target uniqueness, and a fresh view of the application.
Afterwards it waits for state to settle and evaluates the declared effect. When
it can't establish the contract, it returns the evidence and stops.

When it stops, a halted run can send one signed question to the
[phone view](https://app.openadapt.ai/demo/attention): which record, which of
these two targets, did you complete the manual step. The phone shows only what
the sealed pause capability permits. It never clicks the application and it
never declares success. After an answer, the customer-controlled runner reads
the live application again and rechecks pause, session, workflow state,
identity, target, and effect before continuing. Screenshots stay on the runner.

Repairs are versioned changes rather than permission to improvise. A candidate
repair gets reviewed, tested against the workflow's qualification contract,
promoted, and rolled back if it turns out wrong.

## Surfaces

Workflow intent stays portable; the bindings are environment-specific. Each
qualification pins the exact surface, application, version, environment,
identity contract, and effect verifier, so nothing inherits a blanket platform
claim.

| Substrate | Evidence available to a qualified workflow |
| --- | --- |
| Browser (web) | DOM, accessibility, visual, OCR, field geometry, source-time secret exclusion |
| Native desktop (Windows, macOS, Linux) | Visual, OCR, local window scope, plus UI Automation, Accessibility, or AT-SPI where the adapter supplies it |
| Remote display (RDP) | External pixels, OCR, anchors, keyboard, mouse, fresh-frame verification |
| Citrix / VDI | External pixels, OCR, anchors, keyboard, mouse, deployment-bound verification |

Remote execution drives the visible client from a customer-controlled runner.
Nothing gets installed inside the remote session. See the
[substrate model](https://docs.openadapt.ai/concepts/substrate-model/),
[what works today](https://docs.openadapt.ai/get-started/what-works-today/), and
the [CLI reference](https://docs.openadapt.ai/reference/cli/).

## Where your data lives

| Operating model | Best for | Where data and execution live |
|---|---|---|
| Local / self-hosted | Community use and local automation | Your machine |
| Customer-controlled | Sensitive data, native apps, RDP, Citrix, private networks | Your declared boundary; Cloud coordinates approved metadata only |
| Managed execution | Approved browser and non-sensitive workflows | OpenAdapt-managed runners |

Raw recordings and live observations stay local by default. An artifact crosses
a boundary only through explicit sanitization and exact-byte approval. Read the
[trust center](https://openadapt.ai/security) before you pick one.

The launcher, compiler, runtime, Desktop app, substrate adapters, verification
interfaces, and basic qualification tools are MIT. OpenAdapt Cloud is the
commercial multi-tenant control plane for managed operation, fleet governance,
billing, and enterprise integrations. Local safety-critical verification is not
paywalled.

## Evidence

| Evidence | Result |
|---|---|
| OpenEMR referral benchmark, 14 and 15 Oct 2026 | See [One referral, three ways](#one-referral-three-ways): median 29.7 s for OpenAdapt and 36.5 s for a Claude Sonnet agent over 62 warm runs each, with $0 model fees for OpenAdapt. Read-back matched 12 of 12 fields on each correct save; engine certification was off. |
| Fault test on a local test record system, measured 2026-07-21 | 72 of 90 runs left the record wrong: missing, partial, duplicated, or on the wrong record. A check that trusted the success banner passed 54 of those 72. One separate read of the record passed 9 of the 72, all of one kind: a write to a billing table that the read didn't cover. Every check also stopped 9 of the 18 runs that had saved correctly, because the client never got the reply. We chose these faults and injected them on our own test server, so this is a coverage test over a fixed fault list, not a real-world error rate. |

Each result belongs to its named task and environment. Qualifying a new
workflow is what decides what can be claimed about it. Sources:
[how we measured the OpenEMR benchmark](https://openadapt.ai/compare/computer-use-agents),
[fault test method and caveats](https://github.com/OpenAdaptAI/openadapt-flow/blob/aee094193b232f472f991be6fa9b33c3c4b3f9be/benchmark/effect_e2e/EFFECT_E2E.md),
and the [comparison](https://openadapt.ai/compare).
OpenAdapt isn't affiliated with the OpenEMR project.

## OpenAdapt Execute

[OpenAdapt Execute](https://openadapt.ai/execute) is a private partner service
for providers who need an authorized transaction completed inside an
application they cannot integrate with. The partner sends structured input and
business authorization; OpenAdapt runs the qualified workflow in the
customer-controlled environment, verifies the declared effect, and returns an
asynchronous receipt with `VERIFIED` or a precise non-success outcome. It
starts with one named transaction in one qualified environment, and it isn't a
self-service API. Partner contract and qualification process:
[the Execute guide](https://docs.openadapt.ai/commercial/oem-brief/).

## Release status for IT reviewers

The block below is generated from the signed public lifecycle record.

<!-- BEGIN PRODUCTION LIFECYCLE -->
The signed ledger has seven active target admissions. Their evidence class
is `remote-safe-synthetic`, not MockMed `production_acceptance`. They remain
valid until revoked; there's no expiry date. `pip install openadapt` is the
admitted runtime (launcher 1.16.0, Flow 1.35.1). Desktop's admitted artifact
is the 0.16.0 wheel. Native Mac DMGs stay ad-hoc signed 0.15.0; don't treat
those as admitted. A Production run still needs a workflow admission for the
exact compiled bundle, application, and environment.
[Check the live signed Production record](https://docs.openadapt.ai/production-lifecycle.json).
<!-- END PRODUCTION LIFECYCLE -->

## Where the code is

- **Here:** installer, the unified `openadapt` CLI, release compatibility, and
  the stable project URL.
- **[`openadapt-flow`](https://github.com/OpenAdaptAI/openadapt-flow):** the
  compiler, the governed runtime, the CLI implementation, conformance tests.
- **[`openadapt-agent`](https://github.com/OpenAdaptAI/openadapt-agent):** the
  MCP bridge that serves compiled programs to agents.
- **[`openadapt-capture`](https://github.com/OpenAdaptAI/openadapt-capture):**
  native screen, mouse, keyboard, timing, and window-scope capture.
- **[`openadapt-privacy`](https://github.com/OpenAdaptAI/openadapt-privacy):**
  local sanitization and review for approved derivatives.
- **[Desktop](https://github.com/OpenAdaptAI/openadapt-desktop):** the native
  record, inspect, qualify, execute, and review application.

<details>
<summary><strong>Research packages and the pre-1.0 monolith</strong></summary>

None of this is required to record, compile, replay, or verify a workflow.

A separate research line asks whether human demonstrations can improve the
accuracy of general computer-use models. That's a different question from
compiling one demonstration into a deterministic script, and it lives in
[openadapt-ml](https://github.com/OpenAdaptAI/openadapt-ml) (training and
inference for multimodal GUI-action models),
[openadapt-evals](https://github.com/OpenAdaptAI/openadapt-evals) (benchmark
evaluation for GUI agents),
[openadapt-retrieval](https://github.com/OpenAdaptAI/openadapt-retrieval)
(multimodal demonstration retrieval), and
[openadapt-grounding](https://github.com/OpenAdaptAI/openadapt-grounding) (UI
element localization). Install with `pip install "openadapt[ml,evals]"`; the
[research thesis](https://github.com/OpenAdaptAI/openadapt-ml/blob/main/docs/research_thesis.md)
has the methodology, results, and limits.

`openadapt-wright`, `openadapt-herald`, `openadapt-crier`,
`openadapt-consilium`, `openadapt-telemetry`, and `openadapt-viewer` support
development and operations. The runtime doesn't need any of them.

The pre-1.0 monolith (v0.46.0) is frozen under [`legacy/`](legacy/) and
published on PyPI as `openadapt==0.46.0` for Python 3.10 and 3.11. A plain
`pip install` of it fails today.
[docs/LEGACY_FREEZE.md](docs/LEGACY_FREEZE.md) has install steps that work and
the migration guide. Early demonstrations are on
[Twitter](https://twitter.com/abrichr/status/1784307190062342237) and
[Loom](https://www.loom.com/share/9d77eb7028f34f7f87c6661fb758d1c0).

</details>

## Contributing

Launcher, packaging, and CLI changes belong here. Compiler, runtime,
verification, repair, and backend changes belong in `openadapt-flow`.

[Contribution guide](CONTRIBUTING.md) ·
[Issues](https://github.com/OpenAdaptAI/OpenAdapt/issues) ·
[Discussions](https://github.com/OpenAdaptAI/OpenAdapt/discussions) ·
[Discord](https://discord.gg/yF527cQbDG) ·
[Report a vulnerability](SECURITY.md)

Maintained by [OpenAdaptAI](https://github.com/OpenAdaptAI) under the
[MIT License](LICENSE).
