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

__all__ = ["EncounterWrites", "encounter_authorization_spec", "encounter_scope"]


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


def encounter_authorization_spec(
    base_url: str,
    *,
    patient_id: str,
    note: Optional[str] = None,
    note_sha256: Optional[str] = None,
    encounter_type: str = "Triage",
    purposes: tuple[str, ...] = ("teach", "repair"),
    forbidden_controls: tuple[str, ...] = ("Delete encounter",),
) -> dict[str, Any]:
    """The teach authorization a person signs for MockMed's add-a-note task.

    Give the note as ``note`` (spelled out in the statement) or as
    ``note_sha256`` (the operator supplies the value at teach time).
    """

    from openadapt_flow.teach_scope import origin_of

    if (note is None) == (note_sha256 is None):
        raise ValueError("give note or note_sha256")
    return {
        "task": {
            "task_id": "mockmed.add_note",
            "summary": "Add one triage encounter note to the named patient",
        },
        "purposes": list(purposes),
        "origins": [origin_of(base_url)],
        "inputs": {
            "note": {"value": note} if note is not None else {"sha256": note_sha256}
        },
        "writes": {
            "record_kind": "encounter",
            "identity": {"patient_id": {"value": patient_id}},
            "fields": {"type": {"value": encounter_type}, "note": {"input": "note"}},
            "expected_rows": 1,
            "write_adapter": "openadapt_flow.mockmed.teach_scope:EncounterWrites",
        },
        "forbidden": {
            "actions": ["drag", "delete_request", "new_window", "download", "file_upload"],
            "controls": list(forbidden_controls),
        },
    }
