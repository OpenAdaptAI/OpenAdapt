# OpenAdapt: The Demonstration Compiler for Desktop and Web GUIs

[![Build Status](https://github.com/OpenAdaptAI/OpenAdapt/actions/workflows/main.yml/badge.svg)](https://github.com/OpenAdaptAI/OpenAdapt/actions/workflows/main.yml)
[![PyPI version](https://img.shields.io/pypi/v/openadapt.svg)](https://pypi.org/project/openadapt/)
[![Downloads](https://img.shields.io/pypi/dm/openadapt.svg)](https://pypi.org/project/openadapt/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![Discord](https://img.shields.io/badge/Discord-Join%20the%20community-7289da?logo=discord&logoColor=white)](https://discord.gg/yF527cQbDG)

**Show it once. It runs forever. On your premises.**

**OpenAdapt** compiles a single recorded GUI demonstration into a deterministic,
self-healing automation that replays locally at near-zero cost, heals when the
UI drifts, verifies its own effects, and halts rather than guessing when the
screen stops matching. No API required, no per-run model calls, no data leaving
your machine on the default path.

Everything installs from one package and runs from one CLI: `pip install
openadapt` ships the compiler (`openadapt flow …`) out of the box. Recording,
scrubbing, and the research toolkits are optional extras you add only if you
need them.

[Join us on Discord](https://discord.gg/yF527cQbDG) | [Documentation](https://docs.openadapt.ai) | [OpenAdapt.ai](https://openadapt.ai)

---

## Why a compiler

Every automation tool assumes an API. The systems that actually run regulated
work often don't: legacy EMRs, Citrix desktops, and internal apps a team still
drives by hand. General computer-use agents can operate those GUIs, but
re-reasoning through every step with a large model is slow, expensive, and
non-deterministic, and a wrong click writes to the wrong record.

OpenAdapt takes the opposite path for workflows you run over and over. You
demonstrate the task once. OpenAdapt **compiles** that demonstration into a
script that replays **deterministically and locally**, with no model calls on
the hot path. When the UI changes, a lower resolution rung re-resolves the
target and the fix lands back in the bundle as a reviewable diff. When the
screen stops matching expectations, the run **halts with a report** instead of
guessing.

---

## Installation

```bash
pip install openadapt              # CLI + demonstration compiler (openadapt flow …)
pip install openadapt[capture]     # + native GUI capture/recording
pip install openadapt[privacy]     # + PII/PHI scrubbing
pip install openadapt[all]         # Everything, including research extras
```

The flagship compiler ships in the base install, so `openadapt flow …` works
right after `pip install openadapt`.

**Requirements:** Python 3.10+

---

## Quick Start

Record a workflow once, compile it into a deterministic bundle, and replay it
locally at near-zero cost:

```bash
openadapt flow record --url <app> --out rec     # record a workflow once
openadapt flow compile rec --out bundle          # compile it
openadapt flow replay bundle                     # run it, local, $0
```

Inspect and gate compiled bundles before you ship them:

```bash
openadapt flow lint bundle                       # report coverage gaps
openadapt flow certify bundle --policy clinical-write   # enforce a safety policy
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
  is repeatable and costs effectively nothing per run.
- **Self-healing.** When the UI drifts, a lower rung re-resolves the target and
  the fix lands back in the bundle as a reviewable diff, without a human in the
  loop.
- **Effect verification.** Steps carry postconditions derived from what the demo
  changed on screen, so a run confirms it did the right thing rather than
  assuming a click landed.
- **Halt on ambiguity.** When the screen stops matching expectations the run
  halts with a report, and identity-verified steps (for example a wrong-record
  check) refuse to act on a low-confidence match rather than click the wrong
  target.

The reference backend is a **headless browser**, which is why the whole loop
runs in CI with no OS permissions. Desktop, Citrix, and RDP backends are
adapters in progress that we are validating with design partners, not yet
production paths. Compiled workflows can also be emitted as Agent Skills or MCP
servers so other agents can invoke them.

In one field test against a computer-use agent on a real third-party EMR
(OpenEMR's public demo), compiled replay matched the agent's success (20/20
compiled vs 10/10 agent) at roughly half the median latency and near-zero
marginal cost: the agent cost about $0.55 per run, the compiled replay makes
zero model calls. This is a small-sample result on a shared, daily-resetting
public demo, so it is not CI-reproducible; a CI-reproducible control and the
adversarial safety measurements are published alongside it.

See [openadapt-flow](https://github.com/OpenAdaptAI/openadapt-flow) for the
compiler, validation methodology, and known limits.

---

## What ships (the product)

The product is the compiler and the governed runtime around it. These are the
supported packages:

| Package | Role | Maturity | Repository |
|---------|------|----------|------------|
| `openadapt` | Install + unified CLI (`openadapt flow …`) | Web path usable today | This repo |
| `openadapt-flow` | Demonstration compiler + governed runtime (replay, self-heal, effect-verify, halt-on-ambiguity, policies) | Web path usable today; desktop/Citrix/RDP validating with design partners | [openadapt-flow](https://github.com/OpenAdaptAI/openadapt-flow) |
| `openadapt-capture` | Optional native recorder for desktop GUI events | Optional extra | [openadapt-capture](https://github.com/OpenAdaptAI/openadapt-capture) |
| `openadapt-privacy` | Optional PII/PHI scrubbing (Presidio-backed) | Optional extra | [openadapt-privacy](https://github.com/OpenAdaptAI/openadapt-privacy) |

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

## Deprecated

- **`openadapt-agent`** is being folded into `openadapt-flow` and should not be
  built on. Its execution engine, safety gates, and session handling duplicate
  the governed runtime that now lives in `openadapt-flow`. New work belongs in
  `openadapt flow`.

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

> **Internal tooling** (not product architecture, listed for transparency):
> `openadapt-wright` (dev automation), `openadapt-herald` (social posts from git
> history), `openadapt-crier` (approval bot), `openadapt-consilium` (multi-model
> consensus), `openadapt-telemetry` (error tracking), `openadapt-viewer` (HTML
> visualization), and `openadapt-desktop` / `openadapt-tray` (GUI shells). These
> support development and operations; they are not part of the compiler product.

---

## Support

- **Discord:** https://discord.gg/yF527cQbDG
- **Documentation:** https://docs.openadapt.ai
- **Issues:** use the relevant repository

---

## License

MIT License — see [LICENSE](LICENSE) for details.
