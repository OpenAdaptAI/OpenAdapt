# OpenAdapt: AI-First Process Automation with Large Multimodal Models (LMMs)

[![Build Status](https://github.com/OpenAdaptAI/OpenAdapt/workflows/Python%20CI/badge.svg?branch=main)](https://github.com/OpenAdaptAI/OpenAdapt/actions)
[![PyPI version](https://img.shields.io/pypi/v/openadapt.svg)](https://pypi.org/project/openadapt/)
[![Downloads](https://img.shields.io/pypi/dm/openadapt.svg)](https://pypi.org/project/openadapt/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)

**OpenAdapt** is the **open** source software **adapt**er between Large Multimodal Models (LMMs) and traditional desktop and web GUIs.

Record GUI demonstrations, train ML models, and evaluate agents - all from a unified CLI.

[Join us on Discord](https://discord.gg/yF527cQbDG) | [Documentation](https://docs.openadapt.ai) | [OpenAdapt.ai](https://openadapt.ai)

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

See the full [Architecture Evolution](docs/architecture-evolution.md) for detailed documentation.

### Three-Phase Pipeline

```mermaid
flowchart TB
    %% ═══════════════════════════════════════════════════════════════════════
    %% DATA SOURCES (Multi-Source Ingestion)
    %% ═══════════════════════════════════════════════════════════════════════
    subgraph DataSources["Data Sources"]
        direction LR
        HUMAN["Human Demos"]
        SYNTH["Synthetic Data"]:::future
        BENCH_DATA["Benchmark Tasks"]
    end

    %% ═══════════════════════════════════════════════════════════════════════
    %% PHASE 1: DEMONSTRATE (Observation Collection)
    %% ═══════════════════════════════════════════════════════════════════════
    subgraph Demonstrate["1. DEMONSTRATE (Observation Collection)"]
        direction TB
        CAP["Capture<br/>openadapt-capture"]
        PRIV["Privacy<br/>openadapt-privacy"]
        STORE[("Demo Library")]

        CAP --> PRIV
        PRIV --> STORE
    end

    %% ═══════════════════════════════════════════════════════════════════════
    %% PHASE 2: LEARN (Policy Acquisition)
    %% ═══════════════════════════════════════════════════════════════════════
    subgraph Learn["2. LEARN (Policy Acquisition)"]
        direction TB

        subgraph RetrievalPath["Retrieval Path"]
            EMB["Embed"]
            IDX["Index"]
            SEARCH["Search"]
            EMB --> IDX --> SEARCH
        end

        subgraph TrainingPath["Training Path"]
            LOADER["Load"]
            TRAIN["Train"]
            CKPT[("Checkpoint")]
            LOADER --> TRAIN --> CKPT
        end

        subgraph ProcessMining["Process Mining"]
            ABSTRACT["Abstract"]:::future
            PATTERNS["Patterns"]:::future
            ABSTRACT --> PATTERNS
        end
    end

    %% ═══════════════════════════════════════════════════════════════════════
    %% PHASE 3: EXECUTE (Agent Deployment)
    %% ═══════════════════════════════════════════════════════════════════════
    subgraph Execute["3. EXECUTE (Agent Deployment)"]
        direction TB

        subgraph AgentCore["Agent Core"]
            OBS["Observe"]
            POLICY["Policy<br/>(Demo-Conditioned)"]
            GROUND["Grounding<br/>openadapt-grounding"]
            ACT["Act"]

            OBS --> POLICY
            POLICY --> GROUND
            GROUND --> ACT
        end

        subgraph SafetyGate["Safety Gate"]
            VALIDATE["Validate"]
            CONFIRM["Confirm"]:::future
            VALIDATE --> CONFIRM
        end

        subgraph Evaluation["Evaluation"]
            EVALS["Evals<br/>openadapt-evals"]
            METRICS["Metrics"]
            EVALS --> METRICS
        end

        ACT --> VALIDATE
        VALIDATE --> EVALS
    end

    %% ═══════════════════════════════════════════════════════════════════════
    %% THE ABSTRACTION LADDER (Side Panel)
    %% ═══════════════════════════════════════════════════════════════════════
    subgraph AbstractionLadder["Abstraction Ladder"]
        direction TB
        L0["Literal<br/>(Raw Events)"]
        L1["Symbolic<br/>(Semantic Actions)"]
        L2["Template<br/>(Parameterized)"]
        L3["Semantic<br/>(Intent)"]:::future
        L4["Goal<br/>(Task Spec)"]:::future

        L0 --> L1
        L1 --> L2
        L2 -.-> L3
        L3 -.-> L4
    end

    %% ═══════════════════════════════════════════════════════════════════════
    %% MODEL LAYER
    %% ═══════════════════════════════════════════════════════════════════════
    subgraph Models["Model Layer (VLMs)"]
        direction TB
        subgraph APIModels["API Models"]
            direction LR
            CLAUDE["Claude"]
            GPT["GPT-4o"]
            GEMINI["Gemini"]
        end
        subgraph OpenSource["Open Source / Fine-tuned"]
            direction LR
            QWEN3["Qwen3-VL"]
            UITARS["UI-TARS"]
            OPENCUA["OpenCUA"]
        end
    end

    %% ═══════════════════════════════════════════════════════════════════════
    %% MAIN DATA FLOW
    %% ═══════════════════════════════════════════════════════════════════════

    %% Data sources feed into phases
    HUMAN --> CAP
    SYNTH -.-> LOADER
    BENCH_DATA --> EVALS

    %% Demo library feeds learning
    STORE --> EMB
    STORE --> LOADER
    STORE -.-> ABSTRACT

    %% Learning outputs feed execution
    SEARCH -->|"demo context"| POLICY
    CKPT -->|"trained policy"| POLICY
    PATTERNS -.->|"templates"| POLICY

    %% Model connections
    POLICY --> Models
    GROUND --> Models

    %% ═══════════════════════════════════════════════════════════════════════
    %% FEEDBACK LOOPS (Evaluation-Driven)
    %% ═══════════════════════════════════════════════════════════════════════
    METRICS -->|"success traces"| STORE
    METRICS -.->|"training signal"| TRAIN

    %% Retrieval in BOTH training AND evaluation
    SEARCH -->|"eval conditioning"| EVALS

    %% ═══════════════════════════════════════════════════════════════════════
    %% STYLING
    %% ═══════════════════════════════════════════════════════════════════════

    %% Phase colors
    classDef phase1 fill:#3498DB,stroke:#1A5276,color:#fff
    classDef phase2 fill:#27AE60,stroke:#1E8449,color:#fff
    classDef phase3 fill:#9B59B6,stroke:#6C3483,color:#fff

    %% Component states
    classDef implemented fill:#2ECC71,stroke:#1E8449,color:#fff
    classDef future fill:#95A5A6,stroke:#707B7C,color:#fff,stroke-dasharray: 5 5
    classDef futureBlock fill:#f5f5f5,stroke:#95A5A6,stroke-dasharray: 5 5
    classDef safetyBlock fill:#E74C3C,stroke:#A93226,color:#fff

    %% Model layer
    classDef models fill:#F39C12,stroke:#B7950B,color:#fff

    %% Apply styles
    class CAP,PRIV,STORE phase1
    class EMB,IDX,SEARCH,LOADER,TRAIN,CKPT phase2
    class OBS,POLICY,GROUND,ACT,VALIDATE,EVALS,METRICS phase3
    class CLAUDE,GPT,GEMINI,QWEN models
    class L0,L1,L2 implemented
```

### Core Innovation: Demo-Conditioned Prompting

OpenAdapt's key differentiator is **demonstration-conditioned automation** - "show, don't tell":

| Traditional Agent | OpenAdapt Agent |
|-------------------|-----------------|
| User writes prompts | User records demonstration |
| Ambiguous instructions | Grounded in actual UI |
| Requires prompt engineering | No technical expertise needed |
| Context-free | Context from similar demos |

**Retrieval powers BOTH training AND evaluation**: Similar demonstrations are retrieved as context for the VLM, improving accuracy from 33% to 100% on first-action benchmarks.

### Key Concepts

- **Policy/Grounding Separation**: The Policy decides *what* to do; Grounding determines *where* to do it
- **Safety Gate**: Runtime validation layer before action execution (confirm mode for high-risk actions)
- **Abstraction Ladder**: Progressive generalization from literal replay to goal-level automation
- **Evaluation-Driven Feedback**: Success traces become new training data

**Legend:** Solid = Implemented | Dashed = Future

---

## Terminology (Aligned with GUI Agent Literature)

| Term | Description |
|------|-------------|
| **Observation** | What the agent perceives (screenshot, accessibility tree) |
| **Action** | What the agent does (click, type, scroll, etc.) |
| **Trajectory** | Sequence of observation-action pairs |
| **Demonstration** | Human-provided example trajectory |
| **Policy** | Decision-making component that maps observations to actions |
| **Grounding** | Mapping intent to specific UI elements (coordinates) |

## Meta-Package Structure

OpenAdapt v1.0+ uses a **modular architecture** where the main `openadapt` package acts as a meta-package that coordinates focused sub-packages:

- **Core Packages**: Essential for the three-phase pipeline
  - `openadapt-capture` - DEMONSTRATE phase: Collects observations and actions
  - `openadapt-ml` - LEARN phase: Trains policies from demonstrations
  - `openadapt-evals` - EXECUTE phase: Evaluates agents on benchmarks

- **Optional Packages**: Enhance specific workflow phases
  - `openadapt-privacy` - DEMONSTRATE: PII/PHI scrubbing before storage
  - `openadapt-retrieval` - LEARN + EXECUTE: Demo conditioning for both training and evaluation
  - `openadapt-grounding` - EXECUTE: UI element localization (SoM, OmniParser)

- **Cross-Cutting**:
  - `openadapt-viewer` - Trajectory visualization at any phase

### Two Paths to Automation

1. **Custom Training Path**: Demonstrate -> Train policy -> Deploy agent
   - Best for: Repetitive tasks specific to your workflow
   - Requires: `openadapt[core]`

2. **API Agent Path**: Use pre-trained VLM APIs (Claude, GPT-4V, etc.) with demo conditioning
   - Best for: General-purpose automation, rapid prototyping
   - Requires: `openadapt[evals]`

---

## Installation Paths

Choose your installation based on your use case:

```
What do you want to do?
|
+-- Just evaluate API agents on benchmarks?
|   +-- pip install openadapt[evals]
|
+-- Train custom models on your demonstrations?
|   +-- pip install openadapt[core]
|
+-- Full suite with all optional packages?
|   +-- pip install openadapt[all]
|
+-- Minimal CLI only (add packages later)?
    +-- pip install openadapt
```

| Installation | Included Packages | Use Case |
|-------------|-------------------|----------|
| `openadapt` | CLI only | Start minimal, add what you need |
| `openadapt[evals]` | + evals | Benchmark API agents (Claude, GPT-4V) |
| `openadapt[core]` | + capture, ml, viewer | Full training workflow |
| `openadapt[all]` | + privacy, retrieval, grounding | Everything including optional enhancements |

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
