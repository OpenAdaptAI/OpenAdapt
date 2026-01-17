# openadapt-retrieval

Multimodal demonstration retrieval for few-shot prompting.

**Repository**: [OpenAdaptAI/openadapt-retrieval](https://github.com/OpenAdaptAI/openadapt-retrieval)

## Installation

```bash
pip install openadapt[retrieval]
# or
pip install openadapt-retrieval
```

## Overview

The retrieval package enables:

- Semantic search over captured demonstrations
- Few-shot example selection for prompting
- Multimodal similarity (text + image)
- Demonstration library management

## Use Cases

### Few-Shot Prompting

Find similar demonstrations to use as examples when prompting an LMM.

### Transfer Learning

Retrieve relevant demonstrations for new tasks.

### Demonstration Discovery

Search your library of captured demonstrations.

## Python API

```python
from openadapt_retrieval import DemoIndex, retrieve_similar

# Build an index over your captures
index = DemoIndex()
index.add_captures(["task-1", "task-2", "task-3"])

# Retrieve similar demonstrations
screenshot = load_screenshot()
similar = index.search(
    query_image=screenshot,
    query_text="click the submit button",
    top_k=3
)

for result in similar:
    print(f"{result.capture_name}: {result.similarity:.2f}")
```

### Integration with ML

```python
from openadapt_ml import AgentPolicy
from openadapt_retrieval import DemoIndex

# Create policy with retrieval augmentation
index = DemoIndex.load("demo_index.pkl")
policy = AgentPolicy.from_checkpoint(
    "model.pt",
    retrieval_index=index
)

# Predictions include relevant examples
action = policy.predict(screenshot, use_retrieval=True)
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

### List Indexed Captures

```bash
openadapt retrieval list
```

## Key Exports

| Export | Description |
|--------|-------------|
| `DemoIndex` | Demonstration index |
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
  metadata.json       # Capture metadata
```

## Performance

| Index Size | Search Time | Memory |
|------------|-------------|--------|
| 100 demos | <10ms | 50MB |
| 1,000 demos | <50ms | 500MB |
| 10,000 demos | <200ms | 5GB |

## Related Packages

- [openadapt-capture](capture.md) - Record demonstrations to index
- [openadapt-ml](ml.md) - Use retrieval in training
