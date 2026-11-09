# OpenEMR showcase bundle — PHI-free

This bundle is compiled from `../recording/` and is committed as a mechanism
demonstration. It uses the **OpenEMR public-demo patient "Belford, Phil"** —
synthetic, published demo data, **not real PHI**.

## PHI posture (PHI audit REM-1 / REM-2)

- The identity band (patient name / DOB / MRN) is **NOT stored in plaintext**.
  Each armed anchor carries a salted-hash `identity_template`
  (`context_text` / `structured_identity` are `null`). No readable identifier
  appears in `workflow.json`, and `workflow.py` no longer reprints the band.
- The manifest fields `contains_phi` / `phi_scrubbed` / `encrypted` classify the
  bundle for a compliance inventory (see `openadapt_flow.ir.Workflow`).
- Residual UI text (a typed search value, a target label `ocr_text`, and a few
  `TEXT_PRESENT` postconditions) reflects the **fake** demo patient. On a
  **real** EMR, compile with the Presidio scrub active
  (`pip install 'openadapt-flow[privacy]'` + `OPENADAPT_FLOW_SCRUB=on`) so
  identifier-bearing postconditions are dropped, and treat the bundle as a HIPAA
  record per [docs/phi_at_rest.md](../../phi_at_rest.md).

## Regenerate

```bash
python - <<'PY'
from pathlib import Path
from openadapt_flow.compiler import compile_recording
import json
rec = Path("docs/showcase-openemr/recording")
meta = json.loads((rec / "meta.json").read_text())
compile_recording(rec, Path("docs/showcase-openemr/bundle"),
                  name=meta.get("name") or "openemr-showcase")
PY
```

The pre-commit / CI guard (`scripts/check_bundle_phi.py`) blocks committing any
bundle whose steps still carry a plaintext identity band.
