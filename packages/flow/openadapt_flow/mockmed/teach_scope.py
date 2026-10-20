"""MockMed's write adapter for a teach scope (:mod:`openadapt_flow.teach_scope`).

MockMed's fault server (:mod:`openadapt_flow.mockmed.fault_server`) takes one
kind of record write: ``POST /api/encounter`` with a JSON body naming the
patient, the encounter type and the note. :class:`EncounterWrites` reads that
request before the browser sends it, and reads the rows ``GET /api/db``
returns for the read-back. Every other write to MockMed is unattributed, so a
teach scope blocks it.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

from openadapt_flow.teach_scope import TeachScope, WriteClaim

__all__ = ["EncounterWrites", "encounter_scope"]


class EncounterWrites:
    """Reads MockMed encounter saves and ``/api/db`` rows."""

    def claim(
        self, method: str, url: str, body: Optional[bytes]
    ) -> Optional[WriteClaim]:
        if method != "POST" or urlsplit(url).path != "/api/encounter":
            return None
        try:
            payload = json.loads((body or b"").decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        return WriteClaim(
            record={"patient_id": str(payload.get("patient_id", ""))},
            fields={
                # The app sends no type when none was picked; the server
                # stores Triage then.
                "type": str(payload.get("type") or "Triage"),
                "note": str(payload.get("note", "")),
            },
        )

    def claim_row(self, row: Mapping[str, Any]) -> Optional[WriteClaim]:
        if "patient_id" not in row:
            return None
        return WriteClaim(
            record={"patient_id": str(row["patient_id"])},
            fields={"type": str(row.get("type", "")), "note": str(row.get("note", ""))},
        )


def encounter_scope(
    base_url: str,
    *,
    patient_id: str,
    note: str,
    encounter_type: str = "Triage",
    constants: tuple[str, ...] = (),
) -> TeachScope:
    """The scope of MockMed's add-a-note task: one encounter, on one patient."""

    return TeachScope(
        task_id="mockmed.add_note",
        origins=(base_url,),
        record={"patient_id": patient_id},
        fields={"type": encounter_type, "note": note},
        constants=constants,
        write_adapter=EncounterWrites(),
    )
