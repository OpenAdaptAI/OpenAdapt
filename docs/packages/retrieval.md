# openadapt-retrieval

Multimodal trajectory retrieval for few-shot policy learning.

**Repository**: [OpenAdaptAI/openadapt-retrieval](https://github.com/OpenAdaptAI/openadapt-retrieval)

## Installation

```bash
pip install openadapt[retrieval]
# or
pip install openadapt-retrieval
```

## Overview

The retrieval package enables:

- Semantic search over demonstration trajectories
- Few-shot example selection for policy learning
- Multimodal similarity (text + image)
- Demonstration library management

## Use Cases

### Few-Shot Policy Learning

Find similar demonstrations to use as examples when learning agent policies.

### Trajectory Transfer

Retrieve relevant demonstration trajectories for new tasks.

### Demonstration Discovery

Search your library of demonstration trajectories.

## Python API

```python
from openadapt_retrieval import DemoIndex, retrieve_similar

# Build an index over your demonstrations
index = DemoIndex()
index.add_demonstrations(["task-1", "task-2", "task-3"])

# Retrieve similar demonstration trajectories
observation = load_screenshot()
similar = index.search(
    query_image=observation,
    query_text="click the submit button",
    top_k=3
)

for result in similar:
    print(f"{result.demonstration_name}: {result.similarity:.2f}")
```

### Integration with Policy Learning

```python
from openadapt_ml import AgentPolicy
from openadapt_retrieval import DemoIndex

# Create policy with retrieval augmentation
index = DemoIndex.load("demo_index.pkl")
policy = AgentPolicy.from_checkpoint(
    "model.pt",
    retrieval_index=index
)

# Policy uses similar trajectory examples for few-shot learning
observation = load_screenshot()
action = policy.predict(observation, use_retrieval=True)
```

## CLI Commands

### Build Index

```bash
openadapt retrieval index --captures task-1 task-2 task-3
```

### Search

```bash
openadapt retrieval search --image screenshot.png --text "click submit"
```

### List Indexed Demonstrations

```bash
openadapt retrieval list
```

## Key Exports

| Export | Description |
|--------|-------------|
| `DemoIndex` | Demonstration trajectory index |
| `retrieve_similar` | Similarity search |
| `Embedding` | Vector embedding |
| `SearchResult` | Search result data |

## Embedding Models

| Model | Dimensions | Modality |
|-------|------------|----------|
| `clip-vit-l` | 768 | Image + Text |
| `siglip-so400m` | 1152 | Image + Text |
| `custom` | - | - |

## Index Storage

Indexes are stored as pickle files:

```
indexes/
  demo_index.pkl      # Main index
  embeddings.npy      # Vector embeddings
  metadata.json       # Demonstration metadata
```

## Performance

| Index Size | Search Time | Memory |
|------------|-------------|--------|
| 100 demos | <10ms | 50MB |
| 1,000 demos | <50ms | 500MB |
| 10,000 demos | <200ms | 5GB |

## Related Packages

- [openadapt-capture](capture.md) - Collect demonstrations to index
- [openadapt-ml](ml.md) - Use retrieval in policy learning
