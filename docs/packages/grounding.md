# openadapt-grounding

UI element grounding for improved action accuracy.

**Repository**: [OpenAdaptAI/openadapt-grounding](https://github.com/OpenAdaptAI/openadapt-grounding)

## Installation

```bash
pip install openadapt[grounding]
# or
pip install openadapt-grounding
```

## Overview

The grounding package provides UI element detection and grounding to improve:

- Click accuracy by targeting element centers
- Robustness to UI changes
- Visual understanding of interfaces

## Features

### Element Detection

Detect UI elements in screenshots:

- Buttons
- Text fields
- Links
- Icons
- Menus

### Bounding Box Extraction

Get precise coordinates for UI elements.

### Set-of-Mark (SoM) Prompting

Overlay numbered markers on detected elements for LMM prompting.

## Python API

```python
from openadapt_grounding import ElementDetector, SoMPrompt

# Detect elements in a screenshot
detector = ElementDetector()
elements = detector.detect(screenshot_path)

for element in elements:
    print(f"{element.label}: {element.bbox}")

# Create Set-of-Mark prompt
som = SoMPrompt(screenshot_path)
marked_image, element_map = som.create()

# element_map: {1: "Submit button", 2: "Email field", ...}
```

## Integration with Policy Execution

```python
from openadapt_ml import AgentPolicy
from openadapt_grounding import ElementDetector

# Create policy with grounding
policy = AgentPolicy.from_checkpoint(
    "model.pt",
    grounding=ElementDetector()
)

# Actions will use grounded coordinates
observation = load_screenshot()
action = policy.predict(observation)
```

## CLI Commands

### Detect Elements

```bash
openadapt ground detect screenshot.png
```

Output:

```
Found 12 elements:
  1. Button: "Submit" at (450, 320, 520, 350)
  2. TextField: "Email" at (200, 200, 400, 230)
  ...
```

### Create SoM Image

```bash
openadapt ground som screenshot.png --output marked.png
```

## Key Exports

| Export | Description |
|--------|-------------|
| `ElementDetector` | Detects UI elements |
| `SoMPrompt` | Creates Set-of-Mark prompts |
| `BoundingBox` | Element coordinates |
| `Element` | Detected element data |

## Models

| Model | Size | Accuracy | Speed |
|-------|------|----------|-------|
| `omniparser` | 1.2GB | High | Medium |
| `som-base` | 500MB | Medium | Fast |
| `custom` | - | - | - |

## Related Resources

- [Set-of-Mark Paper](https://arxiv.org/abs/2310.11441)
- [OpenAdaptAI/SoM](https://github.com/OpenAdaptAI/SoM) - SoM implementation

## Related Packages

- [openadapt-ml](ml.md) - Use grounding in policy learning and execution
- [openadapt-capture](capture.md) - Apply grounding to demonstrations
