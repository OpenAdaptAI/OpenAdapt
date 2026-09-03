# OpenAdapt

[![CI](https://github.com/OpenAdaptAI/OpenAdapt/actions/workflows/main.yml/badge.svg)](https://github.com/OpenAdaptAI/OpenAdapt/actions/workflows/main.yml)
[![PyPI](https://img.shields.io/pypi/v/openadapt.svg)](https://pypi.org/project/openadapt/)
[![Python 3.10–3.12](https://img.shields.io/badge/python-3.10%E2%80%933.12-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-3e6b4f.svg)](LICENSE)
[![Discord](https://img.shields.io/badge/Discord-community-5865F2?logo=discord&logoColor=white)](https://discord.gg/yF527cQbDG)

**Verified last-mile execution for agents.** Give agents verified hands for
the GUI your APIs can't reach.

A person or a local agent they watch authors a demonstration. OpenAdapt
compiles it into a program that runs again without a generative-model API
in the control loop. A named human admits that program. A calling agent
invokes it. Before a governed run reports `VERIFIED`, it checks every
declared effect at the required evidence tier. Never summarize halt as
success.

Computer-use agents are the user of OpenAdapt. They are not the executor
inside it. The person who admits the program is authority, not the daily
operator. When the agent clicks during the demonstration, that person
watches.

This is the installer and the `openadapt` command. The compiler and the runtime
live in [`openadapt-flow`](https://github.com/OpenAdaptAI/openadapt-flow), and
this repository doesn't reimplement them. `openadapt-agent` is the default
runtime interface for the calling agent. The CLI remains.

[Documentation](https://docs.openadapt.ai) ·
[Start locally](https://openadapt.ai/start) ·
[Walkthrough](https://docs.openadapt.ai/get-started/) ·
[Desktop downloads](https://openadapt.ai/download) ·
[Website](https://openadapt.ai) ·
[15s proof](https://x.com/OpenAdaptAI/status/2093535221324935360) ·
[Discord](https://discord.gg/yF527cQbDG)

## Run it

A solutions engineer gets `VERIFIED` on synthetic MockMed, then hands an agent
the run tool:

```bash
python -m pip install --upgrade openadapt
openadapt doctor --backend web
openadapt flow tutorial
openadapt-agent serve --allow-run
```

Python 3.10 through 3.12. The local tutorial needs no account or API key. The
base install includes Playwright and `openadapt-agent`. On the first browser
action, OpenAdapt asks Playwright to download its matching Chromium build. That
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
It records and compiles a task in MockMed, a synthetic practice-management
fixture, certifies it against the shipped clinical-write policy, runs it under
the Standard profile, then confirms the saved record through a read-only API
that the screen doing the writing never touches:

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

That's real output from Flow 1.34.0 (launcher 1.16.0) on macOS, 2026-08-28,
with the run directory shortened. The tutorial writes the recording, the
compiled bundle, and the run receipt under its output directory (default
`tutorials/tutorial-<UTC timestamp>`). `openadapt quickstart` is a wrapper
around the same engine path. The `VERIFIED` receipt comes from `tutorial`.

Then watch it refuse to lie to you:

```bash
openadapt quickstart --break-it
```

Same certified bundle. This time the backend rejects the write after the app
has already painted its success banner, so every on-screen check passes and the
run halts anyway, because the independent read of the record store disagrees.
The store is unchanged. That halt is the whole product in one command.

The bundled workflow is a tutorial. Qualifying a real one means declaring its
application boundary, its action risks, its identities, its effect verifiers,
its fault cases, and its deployment policy. Start with the
[five-minute walkthrough](https://docs.openadapt.ai/get-started/).

Tutorial `VERIFIED` is a local receipt on synthetic MockMed. It is not a
production Seal. `--break-it` is the aha: the banner can lie, and the
independent read stops the run. When you qualify a real job, that same
independent check is what a Seal attests. Public synthetic verify:
[openadapt.ai/seals](https://openadapt.ai/seals). The Seal contract:
[docs/commercial/seal.md](https://docs.openadapt.ai/commercial/seal/).

`openadapt-agent serve --allow-run` then generates the public MockMed bundle
at serve time and keeps the app up. A client calls `run_local_quickstart`.
You'll get a receipt. You never see frames. Local unsigned success isn't a
Seal; treat unsigned production success as failure.

Inspect what compiled, and what it failed to cover:

```bash
openadapt flow visualize <out>/bundle --out graph.html
openadapt flow lint <out>/bundle
```

## Author a workflow

Do not pass `--backend`. With no `--url`, record captures this OS. Pass
`--url` when the surface is a browser.

```bash
openadapt flow record --out rec
openadapt flow record --url https://your-app.example --out rec
openadapt flow compile rec --out bundle --name my-workflow
openadapt flow replay bundle --url https://your-app.example --run-dir run
```

These commands record one surface and run a permissive local rehearsal.
They do not certify the bundle. One bundle uses one execution surface and does
not switch between browser, native, RDP, or Citrix backends.

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
| Public OpenEMR field test | 19/20 attempts passed the saved-row OCR check at 39.2s median; the compiled arm recorded 0 model API calls, and run 20 halted safely |
| Founding-team heart-care RVU audit | About $75,000/year in estimated recovered billables, and several hours of monthly audit work |

Both belong to their named task and environment. Qualifying a new workflow is
what decides what can be claimed about it. Method and comparison:
[openadapt.ai/compare](https://openadapt.ai/compare) and the
[RVU audit case study](https://openadapt.ai/customers/rvu-audit-heart-care).

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

<!-- BEGIN PRODUCTION LIFECYCLE -->
The signed ledger has seven active target admissions. Their evidence class
is `remote-safe-synthetic`, not MockMed `production_acceptance`. They remain
valid until revoked; there's no expiry date. `pip install openadapt` is the
admitted runtime (launcher 1.16.0, Flow 1.34.0). Desktop's admitted artifact
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

The pre-1.0 monolith (v0.46.0) is frozen under [`legacy/`](legacy/) and still
installable with `pip install openadapt==0.46.0`. Migration guide:
[docs/LEGACY_FREEZE.md](docs/LEGACY_FREEZE.md). Early demonstrations are on
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
