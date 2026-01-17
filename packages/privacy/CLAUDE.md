# Claude Code Instructions for openadapt-privacy

## Overview

**openadapt-privacy** provides PII/PHI detection and redaction for GUI automation data. It protects sensitive information (emails, phone numbers, SSNs, credit cards, dates) in text, images, and nested dictionaries.

Key responsibilities:
- Detect personally identifiable information using NER models (Presidio)
- Redact PII from text, images, and structured data
- Support custom entity detection and scrubbing rules
- Integrate with openadapt-capture recording pipelines

**Always use PRs, never push directly to main**

## Quick Commands

```bash
# Install with Presidio (recommended)
uv add "openadapt-privacy[presidio]"

# Download spaCy model for NER
python -m spacy download en_core_web_trf

# Run tests
uv run pytest tests/ -v

# Scrub text
uv run python -c "
from openadapt_privacy import PresidioScrubber
scrubber = PresidioScrubber()
text = 'Contact John Smith at john@example.com or 555-123-4567'
print(scrubber.scrub_text(text))
"

# Scrub dictionary
uv run python -c "
from openadapt_privacy import scrub_dict, PresidioScrubber
scrubber = PresidioScrubber()
data = {'name': 'John Smith', 'email': 'john@example.com'}
print(scrub_dict(data, scrubber))
"
```

## Architecture

```
openadapt_privacy/
  base.py              # Base classes: Scrubber, ScrubbingProvider
  config.py            # PrivacyConfig dataclass
  loaders.py           # Recording/Action/Screenshot loaders
  pipelines/dicts.py   # Dictionary scrubbing utilities
  providers/presidio.py # PresidioScrubber (primary implementation)
```

## Key Components

### PresidioScrubber
Primary scrubber using Presidio NER and spaCy:
- Detects: PERSON, EMAIL_ADDRESS, PHONE_NUMBER, US_SSN, CREDIT_CARD, DATE_TIME, LOCATION
- `scrub_text(text)` - Redact PII from text
- `scrub_image(image)` - OCR + redact PII regions

### Text Scrubbing
```python
from openadapt_privacy import PresidioScrubber
scrubber = PresidioScrubber()
text = "Contact John Smith at john@example.com"
# Output: "Contact <PERSON> at <EMAIL_ADDRESS>"
print(scrubber.scrub_text(text))
```

### Dictionary Scrubbing
```python
from openadapt_privacy import scrub_dict, PresidioScrubber
scrubber = PresidioScrubber()
action = {"text": "Email john@example.com", "title": "User John Smith"}
scrubbed = scrub_dict(action, scrubber)
```

## Supported Entity Types

| Entity | Input | Output |
|--------|-------|--------|
| PERSON | John Smith | <PERSON> |
| EMAIL_ADDRESS | john@example.com | <EMAIL_ADDRESS> |
| PHONE_NUMBER | 555-123-4567 | <PHONE_NUMBER> |
| US_SSN | 923-45-6789 | <US_SSN> |
| CREDIT_CARD | 4532-1234-5678-9012 | <CREDIT_CARD> |
| DATE_TIME | 01/15/1985 | <DATE_TIME> |
| LOCATION | Toronto, ON | <LOCATION> |

## Testing

```bash
uv run pytest tests/ -v
```

## Related Projects

- [openadapt-capture](https://github.com/OpenAdaptAI/openadapt-capture) - GUI interaction capture
- [openadapt-ml](https://github.com/OpenAdaptAI/openadapt-ml) - Train models on captures
- [openadapt-viewer](https://github.com/OpenAdaptAI/openadapt-viewer) - Visualization components
- [Presidio](https://github.com/microsoft/presidio) - PII detection and redaction
