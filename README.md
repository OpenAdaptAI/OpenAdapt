# OpenAdapt
## AI-First Process Automation with Large Multimodal Models

[![Build Status](https://github.com/OpenAdaptAI/OpenAdapt/actions/workflows/main.yml/badge.svg)](https://github.com/OpenAdaptAI/OpenAdapt/actions/workflows/main.yml)
[![PyPI version](https://img.shields.io/pypi/v/openadapt.svg)](https://pypi.org/project/openadapt/)
[![Downloads](https://img.shields.io/pypi/dm/openadapt.svg)](https://pypi.org/project/openadapt/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![Discord](https://img.shields.io/discord/1031468618276167690?logo=discord&logoColor=white&label=Discord)](https://discord.gg/yF527cQbDG)

**OpenAdapt** is the open-source adapter between Large Multimodal Models (LMMs) and traditional desktop and web interfaces. Transform GUI automation through demonstration-based learning rather than complex programming.

**🎯 Show, don't tell** → Record demonstrations, train intelligent agents, and deploy automation that adapts to any software environment.

### Quick Links
[🚀 Get Started](#installation) | [💬 Join Discord](https://discord.gg/yF527cQbDG) | [📖 Documentation](https://docs.openadapt.ai) | [🌐 Website](https://openadapt.ai)

---

## ✨ What Makes OpenAdapt Different

| Traditional Automation | OpenAdapt |
|------------------------|-----------|
| ❌ Complex scripting required | ✅ Record demonstrations visually |
| ❌ Brittle, breaks with UI changes | ✅ AI adapts to interface variations |
| ❌ Limited to predefined workflows | ✅ Learns from human expertise |
| ❌ Programming knowledge needed | ✅ Anyone can create automations |

---

## Architecture

OpenAdapt v1.0+ uses a **modular meta-package architecture**. The main `openadapt` package provides a unified CLI and depends on focused sub-packages via PyPI:

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
| `openadapt-agent` | Production execution engine | [openadapt-agent](https://github.com/OpenAdaptAI/openadapt-agent) |

### Applications & Tools

| Package | Description | Repository |
|---------|-------------|------------|
| `openadapt-tray` | System tray application | [openadapt-tray](https://github.com/OpenAdaptAI/openadapt-tray) |
| `openadapt-wright` | AI-powered dev automation | [openadapt-wright](https://github.com/OpenAdaptAI/openadapt-wright) |
| `openadapt-consilium` | Multi-LLM consensus system | [openadapt-consilium](https://github.com/OpenAdaptAI/openadapt-consilium) |
| `openadapt-web` | Marketing website | [openadapt-web](https://github.com/OpenAdaptAI/openadapt-web) |
| `openadapt-telemetry` | Error tracking and analytics | [openadapt-telemetry](https://github.com/OpenAdaptAI/openadapt-telemetry) |

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

OpenAdapt transforms GUI automation through a **three-phase demo-conditioned approach** that learns from human demonstrations rather than relying solely on programmatic instructions.

### The Three-Phase Pipeline

```
1. DEMONSTRATE → 2. LEARN → 3. EXECUTE
    ↓               ↓           ↓
  Record         Train       Deploy
  Actions        Models      Agents
```

#### **Phase 1: Demonstrate**
Record human demonstrations of GUI tasks using `openadapt-capture`. All recordings are processed through `openadapt-privacy` for PII/PHI scrubbing before storage.

#### **Phase 2: Learn**
Choose your learning approach:
- **Retrieval Path**: Index demonstrations with `openadapt-retrieval` for runtime context
- **Training Path**: Fine-tune vision-language models using `openadapt-ml`
- **Hybrid**: Combine both for maximum effectiveness

#### **Phase 3: Execute**
Deploy intelligent agents via `openadapt-agent` that:
- Observe the current screen state
- Apply learned policies with demonstration context
- Ground actions to specific UI elements via `openadapt-grounding`
- Execute actions with built-in safety validation

### 🧠 Core Innovation: Demo-Conditioned Automation

Instead of complex prompts, OpenAdapt learns from **visual demonstrations**:

| Traditional Approach | Demo-Conditioned |
|---------------------|------------------|
| Write detailed prompts | Record demonstration once |
| Debug when things break | AI adapts to UI changes |
| Program every edge case | Learn from human intuition |
| Maintain complex scripts | Visual examples as documentation |

**Results**: In controlled benchmarks, demonstration context improved first-action accuracy from 46.7% to 100%. Similar demonstrations provide rich context that helps Vision Language Models understand both the *what* and *how* of GUI interactions.

### 🔑 Key Concepts

- **Smart Decision Making**: AI decides *what* to do, precise grounding determines *where* to click
- **Built-in Safety**: Actions are validated before execution to prevent unintended consequences
- **Progressive Learning**: From exact replay to intelligent adaptation as the system learns
- **Self-Improving**: Successful automations become training data for even better performance


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

- https://twitter.com/abrichr/status/1784307190062342237
- https://www.loom.com/share/9d77eb7028f34f7f87c6661fb758d1c0

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
