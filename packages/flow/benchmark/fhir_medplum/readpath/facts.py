"""Translate this probe's output into the generic server facts.

``openadapt_flow.generate.ensure_contract`` reads only the generic names in
``FACTS`` from a teach directory's ``facts.json``; what each Medplum probe
measured, and under which name, stays here with the probe.

    PYTHONPATH=. python -m benchmark.fhir_medplum.readpath.facts probe.json > facts.json
"""

from __future__ import annotations

import json
import sys
from typing import Any, Mapping

from openadapt_flow.generate.ensure_contract import FACTS


def generic_facts(probe: Mapping[str, Any]) -> dict[str, Any]:
    """The generic facts this probe's output supports, by their generic names."""

    if all(k in probe for k in FACTS):
        return {k: probe[k] for k in FACTS}
    out: dict[str, Any] = {}
    logged = probe.get("bundle_and_graphql_writes_logged")
    if isinstance(logged, list):
        out["audit_logs_writes"] = len(logged) > 0
    for name, key in (
        ("audit_names_actor", "audit_names_the_user_profile"),
        ("agent_cannot_write_audit", "restricted_bot_audit_read_only"),
        ("history_lists_versions", "delete_in_history"),
        ("meta_author_returned", "meta_author_returned"),
    ):
        if key in probe:
            out[name] = probe[key]
    lags = probe.get("audit_lag_s")
    if isinstance(lags, Mapping) and lags:
        out["audit_lag_max_s"] = max(
            float(v.get("max", 0)) for v in lags.values() if isinstance(v, Mapping)
        )
    return out


if __name__ == "__main__":
    with open(sys.argv[1], encoding="utf-8") as f:
        json.dump(generic_facts(json.load(f)), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
