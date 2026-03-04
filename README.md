# OpenAdapt: AI-First Process Automation with Large Multimodal Models (LMMs)

[![Build Status](https://github.com/OpenAdaptAI/OpenAdapt/actions/workflows/main.yml/badge.svg)](https://github.com/OpenAdaptAI/OpenAdapt/actions/workflows/main.yml)
[![PyPI version](https://img.shields.io/pypi/v/openadapt.svg)](https://pypi.org/project/openadapt/)
[![Downloads](https://img.shields.io/pypi/dm/openadapt.svg)](https://pypi.org/project/openadapt/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![Discord](https://img.shields.io/discord/1084481804896374814?color=7289da&label=Discord&logo=discord&logoColor=white)](https://discord.gg/yF527cQbDG)

**OpenAdapt** is the **open** source software **adapt**er between Large Multimodal Models (LMMs) and traditional desktop and web GUIs.

Record GUI demonstrations, train ML models, and evaluate agents - all from a unified CLI.

[Join us on Discord](https://discord.gg/yF527cQbDG) | [Documentation](https://docs.openadapt.ai) | [Blog](https://blog.openadapt.ai) | [OpenAdapt.ai](https://openadapt.ai)

---

## Architecture

OpenAdapt v1.0+ uses a **modular meta-package architecture**. The main `openadapt` package provides a unified CLI and depends on focused sub-packages via PyPI:

| Package | Description | Repository |
|---------|-------------|------------|
| `openadapt` | Meta-package with unified CLI | This repo |
| `openadapt-capture` | Event recording and storage | [openadapt-capture](https://github.com/OpenAdaptAI/openadapt-capture) |
| `openadapt-ml` | ML engine, training, inference | [openadapt-ml](https://github.com/OpenAdaptAI/openadapt-ml) |
| `openadapt-evals` | Benchmark evaluation | [openadapt-evals](https://github.com/OpenAdaptAI/openadapt-evals) |
| `openadapt-viewer` | HTML visualization | [openadapt-viewer](https://github.com/OpenAdaptAI/openadapt-viewer) |
| `openadapt-grounding` | UI element localization | [openadapt-grounding](https://github.com/OpenAdaptAI/openadapt-grounding) |
| `openadapt-retrieval` | Multimodal demo retrieval | [openadapt-retrieval](https://github.com/OpenAdaptAI/openadapt-retrieval) |
| `openadapt-privacy` | PII/PHI scrubbing | [openadapt-privacy](https://github.com/OpenAdaptAI/openadapt-privacy) |
| `openadapt-wright` | Dev automation | [openadapt-wright](https://github.com/OpenAdaptAI/openadapt-wright) |
| `openadapt-herald` | Social media from git history | [openadapt-herald](https://github.com/OpenAdaptAI/openadapt-herald) |
| `openadapt-crier` | Telegram approval bot | [openadapt-crier](https://github.com/OpenAdaptAI/openadapt-crier) |
| `openadapt-consilium` | Multi-model consensus | [openadapt-consilium](https://github.com/OpenAdaptAI/openadapt-consilium) |
| `openadapt-desktop` | Desktop GUI application | [openadapt-desktop](https://github.com/OpenAdaptAI/openadapt-desktop) |
| `openadapt-tray` | System tray app | [openadapt-tray](https://github.com/OpenAdaptAI/openadapt-tray) |
| `openadapt-agent` | Production execution engine | [openadapt-agent](https://github.com/OpenAdaptAI/openadapt-agent) |
| `openadapt-telemetry` | Error tracking | [openadapt-telemetry](https://github.com/OpenAdaptAI/openadapt-telemetry) |

---

## Installation

Install what you need:

```bash
pip install openadapt              # Minimal CLI only
pip install openadapt[capture]     # GUI capture/recording
pip install openadapt[ml]          # ML training and inference
pip install openadapt[evals]       # Benchmark evaluation
pip install openadapt[privacy]     # PII/PHI scrubbing
pip install openadapt[all]         # Everything
```

**Requirements:** Python 3.10+

---

## Quick Start

### 1. Record a demonstration

```bash
openadapt capture start --name my-task
# Perform actions in your GUI, then press Ctrl+C to stop
```

### 2. Train a model

```bash
openadapt train start --capture my-task --model qwen3vl-2b
```

### 3. Evaluate

```bash
openadapt eval run --checkpoint training_output/model.pt --benchmark waa
```

### 4. View recordings

```bash
openadapt capture view my-task
```

---

## Ecosystem

### Core Platform Components

| Package | Description | Repository |
|---------|-------------|------------|
| `openadapt` | Meta-package with unified CLI | This repo |
| `openadapt-capture` | Event recording and storage | [openadapt-capture](https://github.com/OpenAdaptAI/openadapt-capture) |
| `openadapt-ml` | ML engine, training, inference | [openadapt-ml](https://github.com/OpenAdaptAI/openadapt-ml) |
| `openadapt-evals` | Benchmark evaluation | [openadapt-evals](https://github.com/OpenAdaptAI/openadapt-evals) |
| `openadapt-viewer` | HTML visualization | [openadapt-viewer](https://github.com/OpenAdaptAI/openadapt-viewer) |
| `openadapt-grounding` | UI element localization | [openadapt-grounding](https://github.com/OpenAdaptAI/openadapt-grounding) |
| `openadapt-retrieval` | Multimodal demo retrieval | [openadapt-retrieval](https://github.com/OpenAdaptAI/openadapt-retrieval) |
| `openadapt-privacy` | PII/PHI scrubbing | [openadapt-privacy](https://github.com/OpenAdaptAI/openadapt-privacy) |

### Applications and Tools

| Package | Description | Repository |
|---------|-------------|------------|
| `openadapt-desktop` | Desktop GUI application | [openadapt-desktop](https://github.com/OpenAdaptAI/openadapt-desktop) |
| `openadapt-tray` | System tray app | [openadapt-tray](https://github.com/OpenAdaptAI/openadapt-tray) |
| `openadapt-agent` | Production execution engine | [openadapt-agent](https://github.com/OpenAdaptAI/openadapt-agent) |
| `openadapt-wright` | Dev automation | [openadapt-wright](https://github.com/OpenAdaptAI/openadapt-wright) |
| `openadapt-herald` | Social media from git history | [openadapt-herald](https://github.com/OpenAdaptAI/openadapt-herald) |
| `openadapt-crier` | Telegram approval bot | [openadapt-crier](https://github.com/OpenAdaptAI/openadapt-crier) |
| `openadapt-consilium` | Multi-model consensus | [openadapt-consilium](https://github.com/OpenAdaptAI/openadapt-consilium) |
| `openadapt-telemetry` | Error tracking | [openadapt-telemetry](https://github.com/OpenAdaptAI/openadapt-telemetry) |

---

## CLI Reference

```
openadapt capture start --name <name>    Start recording
openadapt capture stop                    Stop recording
openadapt capture list                    List captures
openadapt capture view <name>             Open capture viewer

openadapt train start --capture <name>    Train model on capture
openadapt train status                    Check training progress
openadapt train stop                      Stop training

openadapt eval run --checkpoint <path>    Evaluate trained model
openadapt eval run --agent api-claude     Evaluate API agent
openadapt eval mock --tasks 10            Run mock evaluation

openadapt serve --port 8080               Start dashboard server
openadapt version                         Show installed versions
openadapt doctor                          Check system requirements
```

---

## How It Works

See the full [Architecture Evolution](docs/architecture-evolution.md) for detailed documentation.

### Three-Phase Pipeline

OpenAdapt follows a streamlined **Demonstrate → Learn → Execute** pipeline:

**1. DEMONSTRATE (Observation Collection)**
- **Capture**: Record user actions and screenshots with `openadapt-capture`
- **Privacy**: Scrub PII/PHI from recordings with `openadapt-privacy`
- **Store**: Build a searchable demonstration library

**2. LEARN (Policy Acquisition)**
- **Retrieval Path**: Embed demonstrations, index them, and enable semantic search
- **Training Path**: Load demonstrations and fine-tune Vision-Language Models (VLMs)
- **Abstraction**: Progress from literal replay to template-based automation

**3. EXECUTE (Agent Deployment)**
- **Observe**: Take screenshots and gather accessibility information
- **Policy**: Use demonstration context to decide actions via VLMs (Claude, GPT-4o, Qwen3-VL)
- **Ground**: Map intentions to specific UI coordinates with `openadapt-grounding`
- **Act**: Execute validated actions with safety gates
- **Evaluate**: Measure success with `openadapt-evals` and feed results back for improvement

### Core Approach: Trajectory-Conditioned Disambiguation

Zero-shot VLMs fail on GUI tasks not due to lack of capability, but due to **ambiguity in UI affordances**. OpenAdapt resolves this by conditioning agents on human demonstrations — "show, don't tell."

| | No Retrieval | With Retrieval |
|---|---|---|
| **No Fine-tuning** | 46.7% (zero-shot baseline) | **100%** first-action (n=45, shared entry point) |
| **Fine-tuning** | Standard SFT (baseline) | **Demo-conditioned FT** (planned) |

The bottom-right cell is OpenAdapt's unique value: training models to **use** demonstrations they haven't seen before, combining retrieval with fine-tuning for maximum accuracy. Phase 2 (retrieval-only prompting) is validated; Phase 3 (demo-conditioned fine-tuning) is in progress.

**Validated result**: On a controlled macOS benchmark (45 System Settings tasks sharing a common navigation entry point), demo-conditioned prompting improved first-action accuracy from 46.7% to 100%. A length-matched control (+11.1 pp only) confirms the benefit is semantic, not token-length. See the [research thesis](https://github.com/OpenAdaptAI/openadapt-ml/blob/main/docs/research_thesis.md) for methodology and the [publication roadmap](docs/publication-roadmap.md) for limitations.

**Industry validation**: [OpenCUA](https://github.com/xlang-ai/OpenCUA) (NeurIPS 2025 Spotlight, XLANG Lab) [reused OpenAdapt's macOS accessibility capture code](https://arxiv.org/html/2508.09123v3) in their AgentNetTool, but uses demos only for model training — not runtime conditioning. No open-source CUA framework currently does demo-conditioned inference, which remains OpenAdapt's architectural differentiator.

### Key Concepts

- **Policy/Grounding Separation**: The Policy decides *what* to do; Grounding determines *where* to do it
- **Safety Gate**: Runtime validation layer before action execution (confirm mode for high-risk actions)
- **Abstraction Ladder**: Progressive generalization from literal replay to goal-level automation
- **Evaluation-Driven Feedback**: Success traces become new training data

---

## Terminology

| Term | Description |
|------|-------------|
| **Observation** | What the agent perceives (screenshot, accessibility tree) |
| **Action** | What the agent does (click, type, scroll, etc.) |
| **Trajectory** | Sequence of observation-action pairs |
| **Demonstration** | Human-provided example trajectory |
| **Policy** | Decision-making component that maps observations to actions |
| **Grounding** | Mapping intent to specific UI elements (coordinates) |

---

## Demos

**Legacy Version (v0.46.0) Examples:**
- [Twitter Demo](https://twitter.com/abrichr/status/1784307190062342237) - Early OpenAdapt demonstration
- [Loom Video](https://www.loom.com/share/9d77eb7028f34f7f87c6661fb758d1c0) - Process automation walkthrough

*Note: These demos show the legacy monolithic version. For current v1.0+ modular architecture examples, see the [documentation](https://docs.openadapt.ai).*

---

## Permissions

**macOS:** Grant Accessibility, Screen Recording, and Input Monitoring permissions to your terminal. See [permissions guide](./legacy/permissions_in_macOS.md).

**Windows:** Run as Administrator if needed for input capture.

---

## Legacy Version

The monolithic OpenAdapt codebase (v0.46.0) is preserved in the `legacy/` directory.

**To use the legacy version:**
```bash
pip install openadapt==0.46.0
```

See [docs/LEGACY_FREEZE.md](docs/LEGACY_FREEZE.md) for migration guide and details.

---

## Contributing

1. [Join Discord](https://discord.gg/yF527cQbDG)
2. Pick an issue from the relevant sub-package repository
3. Submit a PR

For sub-package development:
```bash
git clone https://github.com/OpenAdaptAI/openadapt-ml  # or other sub-package
cd openadapt-ml
pip install -e ".[dev]"
```

---

## Related Projects

- [OpenAdaptAI/SoM](https://github.com/OpenAdaptAI/SoM) - Set-of-Mark prompting
- [OpenAdaptAI/pynput](https://github.com/OpenAdaptAI/pynput) - Input monitoring fork
- [OpenAdaptAI/atomacos](https://github.com/OpenAdaptAI/atomacos) - macOS accessibility

---

## Support

- **Discord:** https://discord.gg/yF527cQbDG
- **Issues:** Use the relevant sub-package repository
- **Architecture docs:** [GitHub Wiki](https://github.com/OpenAdaptAI/OpenAdapt/wiki/OpenAdapt-Architecture-(draft))

---

## License

MIT License - see [LICENSE](LICENSE) for details.
