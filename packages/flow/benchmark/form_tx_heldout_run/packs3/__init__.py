"""Form packs for the third held-out portal set (data only).

Each portal's pack lives in its own module, written from that portal's dev
intake and the dev-case demonstration in ``../../form_tx_heldout3_run/demos``,
without reading portal source, scored intakes or the answer key. A module
exports ``PACK``, ``PROFILE`` and optionally ``derived_facts(raw) -> dict``,
facts restated from the intake in the form the portal shows them (never new
information).
"""

from __future__ import annotations

import importlib
from typing import Any

from benchmark.form_tx_heldout_run.packs import _intake_value
from openadapt_flow.form_tx.types import DossierFact, InputDossier, ProvenanceKind

PORTAL_IDS = ("corvane", "pellbrook")
_MODULES: dict[str, Any] = {}
for _pid in PORTAL_IDS:
    try:
        _MODULES[_pid] = importlib.import_module(f"{__name__}.{_pid}")
    except ModuleNotFoundError as _exc:  # a pack not written yet
        if _exc.name != f"{__name__}.{_pid}":
            raise
PACKS: dict[str, Any] = {pid: (m.PACK, m.PROFILE) for pid, m in _MODULES.items()}


def dossier_for(intake: dict[str, Any], request_id: str) -> InputDossier:
    source = f"intake:{intake['case_id']}"
    raw = intake["facts"]
    values = {k: _intake_value(v) for k, v in raw.items() if v is not None and v != ""}
    module = next(
        (
            mod
            for mod in _MODULES.values()
            if str(intake["case_id"]).startswith(mod.CASE_PREFIX + "-")
        ),
        None,
    )
    if module is not None and hasattr(module, "derived_facts"):
        for key, value in module.derived_facts(raw).items():
            values.setdefault(key, value)
    facts = {
        key: DossierFact(
            key=key,
            value=value,
            provenance=ProvenanceKind.INTAKE_DOCUMENT,
            source_id=source,
            confidence=1.0,
        )
        for key, value in values.items()
    }
    return InputDossier(dossier_id=source, request_id=request_id, facts=facts)
