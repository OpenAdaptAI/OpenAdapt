# OpenAdapt: Launcher for the OpenAdapt Flow Compiler

[![Build Status](https://github.com/OpenAdaptAI/OpenAdapt/actions/workflows/main.yml/badge.svg)](https://github.com/OpenAdaptAI/OpenAdapt/actions/workflows/main.yml)
[![PyPI version](https://img.shields.io/pypi/v/openadapt.svg)](https://pypi.org/project/openadapt/)
[![Downloads](https://img.shields.io/pypi/dm/openadapt.svg)](https://pypi.org/project/openadapt/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![Discord](https://img.shields.io/badge/Discord-Join%20the%20community-7289da?logo=discord&logoColor=white)](https://discord.gg/yF527cQbDG)

> **Lifecycle: Beta launcher/meta-package.** The active compiler and governed
> runtime are developed in
> [`openadapt-flow`](https://github.com/OpenAdaptAI/openadapt-flow). This
> repository preserves the high-visibility `openadapt` install and unified CLI;
> it is not a second implementation of the engine. The pre-1.0 monolith is
> historical and frozen under [`legacy/`](legacy/).

**Compile repeated GUI work into deterministic, governed workflows.**

OpenAdapt compiles a demonstrated workflow into a locally executable program.
Healthy runs make no model calls. When an interface drifts, the runtime first
tries deterministic re-resolution, can optionally propose a reviewable repair,
and halts when configured identity, postcondition, effect, or certification
checks fail. The proven self-serve path today is browser automation; native
desktop and remote-display backends remain experimental.

`pip install openadapt` installs the launcher and the compiler, exposed as
`openadapt flow ...`. Native capture, privacy scrubbing, and the separate ML
research toolkits remain optional extras.

[Join us on Discord](https://discord.gg/yF527cQbDG) | [Documentation](https://docs.openadapt.ai) | [OpenAdapt.ai](https://openadapt.ai)

---

## Why a compiler

Every automation tool assumes an API. The systems that actually run regulated
work often don't: legacy EMRs, Citrix desktops, and internal apps a team still
drives by hand. General computer-use agents can operate those GUIs, but
re-reasoning through every step with a large model is slow, expensive, and
non-deterministic, and a wrong click writes to the wrong record.

OpenAdapt takes the opposite path for workflows you run over and over. You
demonstrate the task once. `openadapt-flow` **compiles** that demonstration into
a script that replays **deterministically and locally**, with no model calls on
a healthy run. When the UI changes, the resolution ladder may re-resolve the
target and persist a reviewable repair. When configured checks cannot establish
identity or success, the run **halts with a report** instead of continuing.

---

## Installation

```bash
pip install openadapt              # CLI + demonstration compiler (openadapt flow …)
pip install openadapt[capture]     # + native GUI capture/recording
pip install openadapt[privacy]     # + Presidio-backed PII/PHI scrubbing
pip install openadapt[all]         # Everything, including research extras
```

The flagship compiler ships in the base install, so `openadapt flow …` works
right after `pip install openadapt`. Install `openadapt-flow[hosted]` when you
want OS-keychain token storage; environment-based token configuration remains
available on headless systems.

**Requirements:** Python 3.10+

---

## Quick Start

Run the bundled MockMed workflow first. This is the reproducible path exercised
by `openadapt-flow` CI and requires no account or target application:

```bash
openadapt flow demo-record --out rec
openadapt flow compile rec --out bundle --name mockmed-triage
openadapt flow certify bundle --policy permissive
openadapt flow replay bundle --run-dir run-baseline
openadapt flow replay bundle --drift theme --run-dir run-drift \
  --save-healed-to bundle-healed
```

Each replay writes an illustrated `REPORT.md` in its run directory. The drift
run demonstrates bounded deterministic re-resolution; it is not a claim that
arbitrary UI changes can be repaired.

Now apply the stricter deployment gates:

```bash
openadapt flow lint bundle
openadapt flow certify bundle --policy clinical-write
```

These two commands currently exit nonzero. That is intentional: the bundled
workflow is runnable under the permissive demo policy, but it has unarmed
clicks, a vacuous postcondition, and no configured system-of-record effect for
its write. The strict policy refuses to call it safe. A policy pass means only
that the bundle satisfies that named policy.

To run the workflow through OpenAdapt Cloud, create and review a sanitized
derivative locally, approve its exact bytes, then upload that immutable
derivative with an ingest token created in the Cloud dashboard:

```bash
openadapt flow sanitize rec --kind recording --out rec-sanitized
openadapt flow review-sanitized rec-sanitized --original rec
openadapt flow approve-sanitized rec-sanitized --original rec --reviewer "$USER"
openadapt flow login --token oai_ingest_...
openadapt flow push rec-sanitized --kind recording

openadapt flow compile rec-sanitized --out bundle --name workflow
openadapt flow lint bundle --strict
openadapt flow certify bundle --policy permissive
openadapt flow replay bundle --url https://app.example/login --run-dir run
openadapt flow sanitize bundle --kind bundle --out bundle-sanitized
openadapt flow review-sanitized bundle-sanitized --original bundle
openadapt flow approve-sanitized bundle-sanitized --original bundle \
  --reviewer "$USER"
openadapt flow validate-hosted --recording rec-sanitized \
  --bundle bundle-sanitized --run-dir run --policy permissive \
  --risk-class low --environment staging-v1 \
  --target-url https://app.example/login --out validation.json
openadapt flow push bundle-sanitized --kind bundle \
  --validation-attestation validation.json
```

The original recording remains local. Sanitization accounts for every file and
refuses unsupported content rather than copying it. Review is local and
approval is bound to the exact archive hash; live observations can contain PHI
again and therefore remain inside the workflow's declared execution boundary.

Then record your own browser application:

```bash
openadapt flow record --url https://your.app --out rec
openadapt flow compile rec --out bundle --name my-workflow
openadapt flow replay bundle --url https://your.app
```

> `openadapt flow <verb>` is the recommended path. The standalone
> `openadapt-flow <verb>` command keeps working and behaves identically.

---

## How the compiler works

Each compiled step carries a template crop, an OCR label, geometry landmarks,
and postconditions derived from what the demo changed on screen. At replay time
a resolution ladder tries them in order (local match, global match, OCR,
landmark geometry, then optionally a grounding model), so healthy runs cost
milliseconds and make no model calls.

- **Deterministic replay.** No large model on the hot path, so a compiled run
  is repeatable and has no model API charge on a healthy run. It still consumes
  local or hosted compute, storage, monitoring, and exception-handling effort.
- **Bounded re-resolution and repair.** Deterministic fallback rungs can recover
  from supported drift. Optional model- or human-assisted repairs are governed
  changes to the bundle, not unconstrained reasoning on every run.
- **Explicit verification.** Compiled steps can carry postconditions. For
  consequential writes, effect verification must also be configured against a
  system of record; screen evidence alone cannot prove a transaction committed.
- **Refusal where armed.** Identity-verified or policy-gated steps can refuse a
  low-confidence match and halt with a report. Coverage is not automatic for
  every click, so lint and certification are part of deployment.

The reference backend is a **headless browser**, which is why the whole loop
runs in CI with no OS permissions. Desktop, Citrix, and RDP backends are
adapters in progress that we are validating with design partners, not yet
production paths. Compiled workflows can also be emitted as Agent Skills or MCP
servers so other agents can invoke them.

In one field test against a computer-use agent on a real third-party EMR
(OpenEMR's public demo), compiled replay matched the agent's success (20/20
compiled vs 10/10 agent) at roughly half the median latency and with zero model
calls in the measured runs; the agent's measured model charge was about $0.55
per run. This comparison does not include authoring, maintenance, compute,
storage, review, or exception-handling cost. It is a small-sample result on a
shared, daily-resetting public demo, so it is not CI-reproducible; a
CI-reproducible control and the
adversarial safety measurements are published alongside it.

See [openadapt-flow](https://github.com/OpenAdaptAI/openadapt-flow) for the
compiler, validation methodology, and known limits.

---

## Repository Role and Lifecycle

This repository is the stable URL and install route; `openadapt-flow` is the
canonical implementation. Lifecycle labels describe support intent, not a
blanket production-readiness claim.

| Package | Role | Maturity | Repository |
|---------|------|----------|------------|
| `openadapt` | Installer + unified CLI (`openadapt flow ...`) | **Beta** | This repo |
| `openadapt-flow` | Compiler + governed runtime | **Beta**; browser path proven, other backends experimental | [openadapt-flow](https://github.com/OpenAdaptAI/openadapt-flow) |
| `openadapt-capture` | Optional native recorder | **Experimental** | [openadapt-capture](https://github.com/OpenAdaptAI/openadapt-capture) |
| `openadapt-privacy` | Optional PII/PHI scrubbing | **Experimental** | [openadapt-privacy](https://github.com/OpenAdaptAI/openadapt-privacy) |

`openadapt-flow` also ships standalone on PyPI (`pip install openadapt-flow`);
the standalone `openadapt-flow <verb>` command behaves identically to
`openadapt flow <verb>`.

### CLI reference

```
openadapt flow record --url <app> --out <dir>    Record a workflow once
openadapt flow compile <rec> --out <bundle>       Compile a recording into a bundle
openadapt flow replay <bundle>                    Replay a bundle (local, $0)
openadapt flow lint <bundle>                       Report a bundle's coverage gaps
openadapt flow certify <bundle> --policy <name>    Enforce a safety policy on a bundle
openadapt flow sanitize <path> --kind <kind> --out <dir>
openadapt flow review-sanitized <dir> --original <path>
openadapt flow approve-sanitized <dir> --original <path> --reviewer <identity>
openadapt flow login --token <oai_ingest_token>
openadapt flow validate-hosted --recording <dir> --bundle <dir> --run-dir <dir> ...
openadapt flow push <sanitized-recording-dir> --kind recording
openadapt flow push <sanitized-bundle-dir> --kind bundle \
  --validation-attestation <attestation.json> [--workflow-id <uuid>] \
  [--resolves-run-id <halted-run-uuid>]
openadapt flow report-break <run-dir> --workflow-id <id>

openadapt capture start --name <name>    Start a native recording (requires [capture])
openadapt capture stop                    Stop recording
openadapt capture list                    List captures
openadapt capture view <name>             Open capture viewer

openadapt version                         Show installed versions
openadapt doctor                          Check system requirements
```

---

## Research (not the product)

These packages are **research**, not part of the supported product. They explore
whether human demonstrations can improve the accuracy of general computer-use
models. They are not required to record, compile, or replay a workflow, and the
compiler above makes no model calls on its hot path.

| Package | Research focus | Repository |
|---------|----------------|------------|
| `openadapt-ml` | Training and inference for multimodal GUI-action models | [openadapt-ml](https://github.com/OpenAdaptAI/openadapt-ml) |
| `openadapt-evals` | Benchmark evaluation for GUI agents | [openadapt-evals](https://github.com/OpenAdaptAI/openadapt-evals) |
| `openadapt-retrieval` | Multimodal demonstration retrieval | [openadapt-retrieval](https://github.com/OpenAdaptAI/openadapt-retrieval) |
| `openadapt-grounding` | UI element localization / grounding models | [openadapt-grounding](https://github.com/OpenAdaptAI/openadapt-grounding) |

Install with `pip install openadapt[ml,evals]`. The `openadapt train` and
`openadapt eval` commands become available once those extras are installed.

<details>
<summary><strong>Research thesis: demonstration-conditioned agents</strong></summary>

The research line asks a different question from the compiler: instead of
compiling one demonstration into a deterministic script, can a model *use*
demonstrations at inference time to disambiguate unfamiliar GUIs?

- **Demonstrate** — record user actions and screenshots, scrub PII/PHI, build a
  searchable demonstration library.
- **Learn** — embed and index demonstrations for retrieval, and/or fine-tune
  Vision-Language Models on them.
- **Execute** — condition a policy on retrieved demonstrations, ground intent to
  coordinates, act behind safety gates, and evaluate to feed results back.

**Validated result (research, not product):** on a controlled macOS benchmark
(45 System Settings tasks sharing a common navigation entry point),
demonstration-conditioned prompting improved first-action accuracy from 46.7% to
100%, with a length-matched control (+11.1 pp) confirming the benefit is
semantic, not token-length. Phase 2 (retrieval-only prompting) is validated;
Phase 3 (demo-conditioned fine-tuning) is in progress. See the
[research thesis](https://github.com/OpenAdaptAI/openadapt-ml/blob/main/docs/research_thesis.md)
for methodology and limits.

**Industry note:** [OpenCUA](https://github.com/xlang-ai/OpenCUA) (NeurIPS 2025
Spotlight, XLANG Lab)
[reused OpenAdapt's macOS accessibility capture code](https://arxiv.org/html/2508.09123v3)
in their AgentNetTool, but uses demonstrations only for model training, not
runtime conditioning.

</details>

---

## Migration and Deprecated Paths

- **New automation work:** install `openadapt` and use `openadapt flow ...`; make
  engine changes in `openadapt-flow`.
- **`openadapt-agent`: Deprecated.** Do not build new integrations on it. Its
  execution responsibilities have moved to the governed runtime in
  `openadapt-flow`.
- **Pre-1.0 monolith: Historical.** Version 0.46.0 and its source remain
  available for existing users, but receive no feature development.

---

## Legacy Version

The monolithic OpenAdapt codebase (v0.46.0) is preserved in the `legacy/`
directory.

```bash
pip install openadapt==0.46.0
```

See [docs/LEGACY_FREEZE.md](docs/LEGACY_FREEZE.md) for the migration guide.

Early demonstrations of the legacy version:
[Twitter demo](https://twitter.com/abrichr/status/1784307190062342237) ·
[Loom walkthrough](https://www.loom.com/share/9d77eb7028f34f7f87c6661fb758d1c0).
For the current architecture, see the [documentation](https://docs.openadapt.ai).

---

## Permissions

The default headless-browser path needs no OS permissions. Native desktop
capture does:

**macOS:** Grant Accessibility, Screen Recording, and Input Monitoring
permissions to your terminal. See [permissions guide](./legacy/permissions_in_macOS.md).

**Windows:** Run as Administrator if needed for input capture.

---

## Contributing

1. [Join Discord](https://discord.gg/yF527cQbDG)
2. Pick an issue from the relevant repository
3. Submit a PR

For sub-package development:

```bash
git clone https://github.com/OpenAdaptAI/openadapt-flow  # or another package
cd openadapt-flow
pip install -e ".[dev]"
```

---

## Related Projects

- [OpenAdaptAI/SoM](https://github.com/OpenAdaptAI/SoM) — Set-of-Mark prompting
- [OpenAdaptAI/pynput](https://github.com/OpenAdaptAI/pynput) — input monitoring fork
- [OpenAdaptAI/atomacos](https://github.com/OpenAdaptAI/atomacos) — macOS accessibility

> **Product surfaces:** OpenAdapt Cloud provides the hosted browser-workflow
> control plane, with live execution enabled only when its production
> dependencies pass readiness checks. `openadapt-desktop` and native/remote
> backends remain experimental. **Internal tooling:** `openadapt-wright`, `openadapt-herald`,
> `openadapt-crier`, `openadapt-consilium`, `openadapt-telemetry`, and
> `openadapt-viewer` support development and operations; they are not required
> by the compiler runtime.

---

## Support

- **Discord:** https://discord.gg/yF527cQbDG
- **Documentation:** https://docs.openadapt.ai
- **Issues:** use the relevant repository

---

## License

MIT License — see [LICENSE](LICENSE) for details.
