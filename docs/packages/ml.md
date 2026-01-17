# openadapt-ml

Policy learning, training, and inference for GUI automation agents.

**Repository**: [OpenAdaptAI/openadapt-ml](https://github.com/OpenAdaptAI/openadapt-ml)

## Installation

```bash
pip install openadapt[ml]
# or
pip install openadapt-ml
```

## Overview

The ML package provides:

- Model adapters for various LMMs (Qwen-VL, LLaVA, etc.)
- Policy learning infrastructure from demonstration trajectories
- Inference engine for action prediction
- Agent policies for autonomous execution

## CLI Commands

### Start Policy Learning

```bash
openadapt train start --capture my-task --model qwen3vl-2b
```

Options:

- `--capture` - Name of the demonstration to learn from (required)
- `--model` - Model architecture (required)
- `--epochs` - Number of training epochs (default: 10)
- `--batch-size` - Batch size (default: 4)
- `--output` - Output directory (default: training_output/)

### Check Policy Learning Status

```bash
openadapt train status
```

### Stop Policy Learning

```bash
openadapt train stop
```

### List Available Models

```bash
openadapt train models
```

## Supported Models

| Model | Size | Description |
|-------|------|-------------|
| `qwen3vl-2b` | 2B | Qwen3-VL 2B parameters |
| `qwen3vl-7b` | 7B | Qwen3-VL 7B parameters |
| `llava-1.6-7b` | 7B | LLaVA 1.6 7B parameters |
| `custom` | - | Custom model configuration |

## Python API

```python
from openadapt_ml import QwenVLAdapter, Trainer, AgentPolicy

# Load a pre-trained model
adapter = QwenVLAdapter.from_pretrained("qwen3vl-2b")

# Create trainer for policy learning
trainer = Trainer(
    model=adapter,
    demonstration="my-task",  # demonstration name
    epochs=10
)

# Learn policy from demonstration trajectory
checkpoint_path = trainer.train()

# Load trained policy for execution
policy = AgentPolicy.from_checkpoint(checkpoint_path)

# Predict next action from observation
observation = load_screenshot()
action = policy.predict(observation)
```

## Policy Learning Pipeline

```mermaid
flowchart LR
    subgraph Input
        DEMO[Demonstration]
        OBS[Observations]
        ACT[Actions]
    end

    subgraph Processing
        DL[DataLoader]
        AUG[Augmentation]
        TOK[Tokenization]
    end

    subgraph Learning
        FWD[Forward Pass]
        LOSS[Loss Calculation]
        OPT[Optimization]
    end

    subgraph Output
        CKPT[Trained Policy]
        LOG[Training Logs]
    end

    DEMO --> DL
    OBS --> DL
    ACT --> DL
    DL --> AUG
    AUG --> TOK
    TOK --> FWD
    FWD --> LOSS
    LOSS --> OPT
    OPT --> CKPT
    OPT --> LOG
```

## Key Exports

| Export | Description |
|--------|-------------|
| `QwenVLAdapter` | Qwen-VL model adapter |
| `LLaVAAdapter` | LLaVA model adapter |
| `Trainer` | Policy learning infrastructure |
| `AgentPolicy` | Trained policy for execution |
| `learn_from_demonstrations` | Policy learning function |

## Hardware Requirements

| Model | VRAM | Recommended GPU |
|-------|------|-----------------|
| qwen3vl-2b | 8GB | RTX 3070+ |
| qwen3vl-7b | 24GB | RTX 4090 / A100 |
| llava-1.6-7b | 24GB | RTX 4090 / A100 |

## Related Packages

- [openadapt-capture](capture.md) - Collect demonstrations
- [openadapt-evals](evals.md) - Evaluate trained policies
- [openadapt-retrieval](retrieval.md) - Trajectory retrieval for few-shot policy learning
