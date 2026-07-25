# OpenAdapt

[![CI](https://github.com/OpenAdaptAI/OpenAdapt/actions/workflows/main.yml/badge.svg)](https://github.com/OpenAdaptAI/OpenAdapt/actions/workflows/main.yml)
[![PyPI](https://img.shields.io/pypi/v/openadapt.svg)](https://pypi.org/project/openadapt/)
[![Downloads](https://img.shields.io/pypi/dm/openadapt.svg)](https://pypi.org/project/openadapt/)
[![Python 3.10–3.12](https://img.shields.io/badge/python-3.10%E2%80%933.12-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-3e6b4f.svg)](LICENSE)
[![Discord](https://img.shields.io/badge/Discord-community-5865F2?logo=discord&logoColor=white)](https://discord.gg/yF527cQbDG)

**Automate the UI-only work your APIs can’t reach.**

OpenAdapt is the verified execution layer for consequential work trapped behind
human interfaces. It turns a demonstration into an inspectable workflow that
runs across browser, Windows, macOS, Linux, RDP, and Citrix/VDI. Healthy runs
use no generative-model calls. Consequential actions are identity-gated,
declared results are verified, and uncertainty halts for review instead of
becoming a wrong click.

Use a supported API when one fits. Use OpenAdapt when the interface is
unavoidable and the outcome needs proof.

[Website](https://openadapt.ai) ·
[Documentation](https://docs.openadapt.ai) ·
[Desktop downloads](https://openadapt.ai/download) ·
[OpenAdapt Cloud](https://app.openadapt.ai) ·
[Qualify a workflow](https://openadapt.ai/qualify)

> **Repository role:** this is the flagship OpenAdapt project, the source of
> `pip install openadapt`, and the stable community entry point. The compiler
> and governed runtime are implemented in
> [`openadapt-flow`](https://github.com/OpenAdaptAI/openadapt-flow). This
> repository provides the unified `openadapt` CLI and compatibility surface,
> not a second engine. Lifecycle: **Beta**.

## Try it locally

OpenAdapt requires Python 3.10–3.12. The base package includes the compiler and
runtime:

```bash
python -m pip install --upgrade openadapt
```

Run the bundled synthetic workflow. It needs no account, target application,
API key, or operating-system automation permissions:

```bash
openadapt flow demo-record --out rec
openadapt flow compile rec --out bundle --name mockmed-triage
openadapt flow certify bundle --policy permissive
openadapt flow replay bundle --run-dir run
```

You now have:

- `rec/`: the demonstrated interaction and retained target evidence
- `bundle/`: the inspectable compiled workflow
- `run/REPORT.md`: the ordered actions, evidence, outcome, and any halt reason

Inspect the program and its deployment gaps:

```bash
openadapt flow visualize bundle --out graph.html
openadapt flow lint bundle
```

The bundled workflow is a tutorial, not a production certification. Qualifying
a real workflow adds its application boundary, action risks, identities,
effect verifiers, fault cases, and deployment policy. Continue with the
[five-minute walkthrough](https://docs.openadapt.ai/get-started/).

## Record your workflow

The browser path is available in the base installation:

```bash
openadapt flow record --backend web --url https://your-app.example --out rec
openadapt flow compile rec --out bundle --name my-workflow
openadapt flow replay bundle --url https://your-app.example --run-dir run
```

Install only the capabilities needed for native or remote work:

```bash
python -m pip install "openadapt[capture]"          # local human demonstration
python -m pip install "openadapt[capture,windows]"  # Windows UI Automation
python -m pip install "openadapt[capture,macos]"    # macOS Accessibility
python -m pip install "openadapt[capture,linux]"    # Linux AT-SPI
python -m pip install "openadapt[capture,rdp]"      # RDP transport
python -m pip install "openadapt[privacy]"          # PII/PHI scrubbing
```

For the visual authoring and review experience, install
[OpenAdapt Desktop](https://openadapt.ai/download).

![OpenAdapt Desktop showing a completed compiled workflow with all 11 steps verified, an 8.2 second run, and no model calls](https://raw.githubusercontent.com/OpenAdaptAI/OpenAdapt/main/media/desktop-replay-verified.png)

## What makes it different

### Verified business effects

A click succeeding is not proof that the intended transaction committed.
OpenAdapt separates action delivery from outcome verification. Workflows can
bind consequential writes to an independent interface, a separate read-only
session, or persisted-state reacquisition before reporting `VERIFIED`.

### Fail-closed execution

Before a consequential action, the runtime can check authorization, workflow
state, record identity, target uniqueness, and the fresh application view.
Afterward it waits for settled state and evaluates the declared effect. If the
contract cannot be established, it returns evidence and halts.

### Deterministic healthy runs

The compiler retains structural, accessibility, visual, OCR, spatial, and
transition evidence from the demonstration. The runtime uses the strongest
signals available on each surface. A generative model may propose a governed
repair when explicitly allowed, but it is not on the healthy execution path.

### Governed repair

Repairs are versioned changes, not permission to improvise. Candidate repairs
can be reviewed, tested against the workflow’s qualification contract,
promoted, and rolled back.

## One workflow model, multiple surfaces

OpenAdapt keeps portable workflow intent separate from environment-specific
bindings:

| Product family | Execution surfaces | Strongest available evidence |
|---|---|---|
| Browser | Chromium-based web applications | DOM, accessibility, visual, OCR |
| Native desktop | Windows, macOS, Linux | UI Automation, Accessibility, AT-SPI, visual |
| Remote applications | RDP, Citrix Workspace, VDI | External pixels, OCR, anchors, keyboard, mouse |

Remote execution operates from a customer-controlled runner through the visible
client. It does not require OpenAdapt software inside the remote session.
Every workflow is qualified against its exact application, version,
environment, identity contract, and effect verifier rather than inheriting a
blanket platform claim.

See the [substrate model](https://docs.openadapt.ai/concepts/substrate-model/),
[qualification evidence](https://docs.openadapt.ai/get-started/what-works-today/),
and [CLI reference](https://docs.openadapt.ai/reference/cli/) for the full
contracts.

## Local, customer-controlled, or managed

| Operating model | Best for | Where application data and execution live |
|---|---|---|
| Local / self-hosted | Community use and local automation | Your machine or infrastructure |
| Customer-controlled | Sensitive data, native apps, RDP, Citrix, private networks | Your declared boundary; Cloud can coordinate approved metadata and artifacts |
| Managed execution | Approved browser and non-sensitive workflows | OpenAdapt-managed runners and control plane |

Raw recordings and live observations stay local by default. Artifacts cross a
boundary only through explicit sanitization and exact-byte approval. Review the
[trust center](https://openadapt.ai/security) before choosing a deployment.

The local launcher, compiler/runtime, Desktop application, substrate adapters,
verification interfaces, and basic qualification tools are MIT licensed.
OpenAdapt Cloud is the commercial multi-tenant control plane for managed
operation, fleet governance, billing, and enterprise integrations. Local
safety-critical verification is not paywalled.

## Evidence

| Evidence | Result |
|---|---|
| Public OpenEMR reference workflow | 20/20 effect-verified runs, 39.2s median, 0 model calls |
| Heart-care RVU audit customer case | Approximately $75,000/year in recovered billables and several hours of monthly audit work saved |

Read the [benchmark method and comparison](https://openadapt.ai/compare) and
the [RVU audit case study](https://openadapt.ai/customers/rvu-audit-heart-care).
Results belong to their named task and environment; workflow qualification
defines what can be claimed for a new deployment.

## Project map

- **This repository:** installer, unified CLI, release compatibility, and
  stable project URL
- **[`openadapt-flow`](https://github.com/OpenAdaptAI/openadapt-flow):**
  canonical compiler, governed runtime, CLI implementation, and conformance
  tests
- **[Documentation](https://docs.openadapt.ai):** installation, workflow
  authoring, qualification, operation, deployment, and reference material
- **[Desktop](https://github.com/OpenAdaptAI/openadapt-desktop):** native
  record, inspect, qualify, execute, and review application

The pre-1.0 monolith remains available under [`legacy/`](legacy/) for migration
history. New product and engine development belongs in `openadapt-flow`.

## Contributing and support

Launcher, packaging, and unified-CLI changes belong here. Compiler, runtime,
verification, repair, and backend changes belong in `openadapt-flow`.

- [Contribution guide](CONTRIBUTING.md)
- [Open an issue](https://github.com/OpenAdaptAI/OpenAdapt/issues)
- [GitHub Discussions](https://github.com/OpenAdaptAI/OpenAdapt/discussions)
- [Discord community](https://discord.gg/yF527cQbDG)
- [Report a vulnerability privately](SECURITY.md)

OpenAdapt is maintained by [OpenAdaptAI](https://github.com/OpenAdaptAI) and
released under the [MIT License](LICENSE).
