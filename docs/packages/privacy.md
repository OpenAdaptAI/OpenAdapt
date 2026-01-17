# openadapt-privacy

PII/PHI scrubbing for captured demonstrations.

**Repository**: [OpenAdaptAI/openadapt-privacy](https://github.com/OpenAdaptAI/openadapt-privacy)

## Installation

```bash
pip install openadapt[privacy]
# or
pip install openadapt-privacy
```

## Overview

The privacy package provides:

- Detection of personally identifiable information (PII)
- Detection of protected health information (PHI)
- Redaction and anonymization
- Compliance with privacy regulations

## Detected Information Types

### PII (Personally Identifiable Information)

- Names
- Email addresses
- Phone numbers
- Social Security numbers
- Credit card numbers
- Addresses
- Dates of birth

### PHI (Protected Health Information)

- Medical record numbers
- Health plan IDs
- Account numbers
- Certificate/license numbers
- Medical conditions
- Treatment information

## CLI Commands

### Scrub a Demonstration

```bash
openadapt privacy scrub my-task
```

Options:

- `--output` - Output directory (default: scrubbed/)
- `--mode` - Redaction mode (blur, mask, replace)
- `--types` - Information types to scrub (default: all)

### Preview Detection

```bash
openadapt privacy detect my-task
```

Shows detected PII/PHI without modifying files.

### Scrub a Single Image

```bash
openadapt privacy scrub-image screenshot.png --output clean.png
```

## Python API

```python
from openadapt_privacy import Scrubber, PIIDetector

# Create a scrubber
scrubber = Scrubber(mode="blur")

# Scrub a demonstration
scrubber.scrub_demonstration("my-task", output_dir="scrubbed/")

# Or scrub individual images
scrubbed_image = scrubber.scrub_image(screenshot_path)

# Just detect without scrubbing
detector = PIIDetector()
detections = detector.detect(screenshot_path)

for detection in detections:
    print(f"{detection.type}: {detection.text} at {detection.bbox}")
```

### Integration with Capture

```python
from openadapt_capture import CaptureSession, Recorder
from openadapt_privacy import Scrubber

# Record with automatic scrubbing
session = CaptureSession(
    name="my-task",
    scrubber=Scrubber(mode="blur")
)

recorder = Recorder(session)
recorder.start()
# ... demonstration collection ...
recorder.stop()

# Demonstrations are automatically scrubbed
```

## Redaction Modes

| Mode | Description | Use Case |
|------|-------------|----------|
| `blur` | Gaussian blur over sensitive areas | Visual redaction |
| `mask` | Black box over sensitive areas | Complete hiding |
| `replace` | Replace with placeholder text | Maintaining layout |

## Key Exports

| Export | Description |
|--------|-------------|
| `Scrubber` | Main scrubbing class |
| `PIIDetector` | PII detection |
| `PHIDetector` | PHI detection |
| `Detection` | Detection result |
| `RedactionMode` | Redaction options |

## Detection Models

| Model | Types | Accuracy |
|-------|-------|----------|
| `presidio` | PII | High |
| `philter` | PHI | High |
| `regex` | Common patterns | Medium |
| `custom` | User-defined | - |

## Compliance

This package helps with compliance for:

- GDPR (General Data Protection Regulation)
- HIPAA (Health Insurance Portability and Accountability Act)
- CCPA (California Consumer Privacy Act)

!!! warning "Disclaimer"
    This tool assists with privacy protection but does not guarantee compliance. Always consult with legal and compliance experts for your specific use case.

## Related Packages

- [openadapt-capture](capture.md) - Collect demonstrations to scrub
- [openadapt-viewer](viewer.md) - View scrubbed demonstrations
