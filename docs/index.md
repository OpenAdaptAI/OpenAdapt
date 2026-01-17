# OpenAdapt

**AI-First Process Automation with Large Multimodal Models (LMMs)**

OpenAdapt is the **open** source software **adapt**er between Large Multimodal Models (LMMs) and traditional desktop and web GUIs.

Collect human demonstrations, learn agent policies, and evaluate autonomous execution - all from a unified CLI.

[Join Discord](https://discord.gg/yF527cQbDG){ .md-button .md-button--primary }
[View on GitHub](https://github.com/OpenAdaptAI/OpenAdapt){ .md-button }

---

## What is OpenAdapt?

OpenAdapt bridges the gap between powerful AI models and everyday software automation. Instead of writing complex scripts or learning APIs, you simply:

1. **Demonstrate** - Show the agent how to perform a task by doing it yourself
2. **Learn** - Let OpenAdapt learn an agent policy from your demonstration trajectory
3. **Execute** - Deploy your trained agent to autonomously perform the task
4. **Evaluate** - Measure agent performance on standardized benchmarks

```mermaid
flowchart LR
    subgraph Demonstrate["1. Demonstrate"]
        A[Human Trajectory] --> B[Capture]
    end

    subgraph Learn["2. Learn"]
        B --> C[Policy Learning]
    end

    subgraph Execute["3. Execute"]
        C --> D[Trained Policy]
        D --> E[Agent Deployment]
    end

    subgraph Evaluate["4. Evaluate"]
        D --> F[Benchmark]
        F --> G[Metrics]
    end

    GROUND[Grounding] -.-> E
    RETRIEVE[Retrieval] -.-> C
    PRIV[Privacy] -.-> B
```

---

## Key Features

### Model Agnostic
Works with any Large Multimodal Model - Claude, GPT-4V, Gemini, Qwen-VL, or your own fine-tuned models.

### Learn from Demonstration
No manual prompt engineering required. OpenAdapt learns agent policies directly from your demonstration trajectories.

### Universal GUI Support
Works with all desktop GUIs including native applications, web browsers, and virtualized environments.

### Open Source
MIT licensed. Full transparency, community-driven development, and no vendor lock-in.

---

## Quick Start

Install OpenAdapt with the features you need:

```bash
pip install openadapt[all]  # Everything
```

Collect a demonstration:

```bash
openadapt capture start --name my-task
# Perform your task, then press Ctrl+C
```

Learn a policy:

```bash
openadapt train start --capture my-task --model qwen3vl-2b
```

Evaluate:

```bash
openadapt eval run --checkpoint training_output/model.pt --benchmark waa
```

See the [Installation Guide](getting-started/installation.md) for detailed setup instructions.

---

## Architecture

OpenAdapt v1.0+ uses a **modular meta-package architecture**. The main `openadapt` package provides a unified CLI and depends on focused sub-packages:

| Package | Description |
|---------|-------------|
| [openadapt-capture](packages/capture.md) | Demonstration collection and storage |
| [openadapt-ml](packages/ml.md) | Policy learning, training, inference |
| [openadapt-evals](packages/evals.md) | Benchmark evaluation |
| [openadapt-viewer](packages/viewer.md) | Trajectory visualization |
| [openadapt-grounding](packages/grounding.md) | UI element grounding |
| [openadapt-retrieval](packages/retrieval.md) | Trajectory retrieval |
| [openadapt-privacy](packages/privacy.md) | PII/PHI scrubbing |

See the full [Architecture Documentation](architecture.md) for detailed diagrams.

---

## Demos

- [Twitter Demo](https://twitter.com/abrichr/status/1784307190062342237)
- [Loom Walkthrough](https://www.loom.com/share/9d77eb7028f34f7f87c6661fb758d1c0)

---

## Community

- **Discord**: [Join our community](https://discord.gg/yF527cQbDG)
- **GitHub**: [OpenAdaptAI](https://github.com/OpenAdaptAI)
- **Twitter**: [@OpenAdaptAI](https://twitter.com/OpenAdaptAI)

---

## License

OpenAdapt is released under the [MIT License](https://github.com/OpenAdaptAI/OpenAdapt/blob/main/LICENSE).
