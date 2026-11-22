# openadapt-eligibility

Optional domain connector for API-first 270/271 healthcare payer eligibility
checks (including Stedi route bindings, qualifier-aware response parsing,
waterfall fallback routing, and atomic practice-local artifact storage).

This package is intentionally separated from `openadapt-flow` so that
domain-specific payer and vendor integrations live outside the general-purpose
verification engine.

## Usage

```python
from openadapt_eligibility import (
    EligibilityRequest,
    StediAccountBoundary,
    StediEligibilityClient,
    load_payer_routes,
    run_waterfall,
    write_and_verify,
)
```
