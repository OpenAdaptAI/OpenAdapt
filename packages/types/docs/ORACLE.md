# Oracle adapters

A production `VERIFIED` receipt needs an independent read of the system of
record. The click is the commodity. The read is what a Seal is for.

## Tiers

| Tier | What you read | Production `VERIFIED` |
| --- | --- | --- |
| 0 | Pixels, OCR, same-surface banner | No |
| 1 | Second session / independent UI | No |
| 2 | API, DB, file, ack | Yes, if the other contracts pass |
| 3 | Counterparty artifact (payer status, legal export) | Yes, if the other contracts pass |

Tier 0 is for local development. `issue_production_verified` raises
`ProductionSealRefused` for tier 0 and tier 1. The Execute receipt does
the same: `verified` and `rolled_back_verified` require observed effect
strength that maps to tier 2 or 3.

This numbering is the Seal ladder. `EffectVerificationTier` counts the
other way (1 is strongest there). Use `OracleTier` when you talk about a
Seal.

## Adapter

Implement `channel` and `read`. That's it.

```python
from pathlib import Path
import json
from openadapt_types import OracleChannel, OracleObservation

class FileStatusOracle:
    channel = OracleChannel.FILE

    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self, identity):
        rec = json.loads(self.path.read_text(encoding="utf-8"))[identity["record_id"]]
        return OracleObservation(
            channel=self.channel,
            identity={"record_id": identity["record_id"]},
            value={"status": rec["status"]},
        )
```

API, DB, ack, and second-session adapters keep that shape. The channel
sets the tier. A visual adapter that stuffs a JSON body into `value`
is still tier 0.

Worked file: [`examples/oracle/file_oracle.py`](../examples/oracle/file_oracle.py).

Don't put a per-vendor recipe in this repository. The interface is
public. Productionized connector recipes stay private.

Flow will call this contract on the Execute path. This package holds the
adapter and the Seal gate. It does not run the GUI.
