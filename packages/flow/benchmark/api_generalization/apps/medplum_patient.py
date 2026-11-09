"""Medplum 5.2 web app (FHIR R4): a front-desk clerk registers a patient.

The app is a single-page app on its own origin; Create sends the Patient
resource as JSON to the FHIR server on another origin
(``POST /fhir/R4/Patient``), signed with the session's bearer token. Reuses
the lab's Medplum fixture (``benchmark/fhir_medplum``) on this experiment's
ports. Medplum is Apache-2.0 and runs from its images. Synthetic patients.

The save goes straight from the browser to the FHIR server, so the fault
proxy (which fronts one origin) can't sit in its path: the fault cases and
the session case don't apply here and are covered on the other apps.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from benchmark.api_generalization.common import (
    AppBase,
    Case,
    ExecSqlVerifier,
    dump_database,
    rec_click,
    rec_text,
)
from openadapt_flow.runtime.effects.effect import Effect, EffectKind, ValueExpr

SERVER, UI = 9673, 9674
STATE = Path.home() / "oa/scratch/api-generalization/medplum-state"
CLERK = "clerk@example.invalid"
os.environ.setdefault(
    "DOCKER_HOST", "unix:///var/folders/35/pqjnmmwx24d2mtf4frt5dhcc0000gn/T/podman/openadapt-benchmark-api.sock"
)


def _fixture() -> Any:
    from benchmark.fhir_medplum.fixture import MedplumFixture

    return MedplumFixture(state_dir=STATE, server_port=SERVER, app_port=UI, project="apigen-medplum")


def mp_rows(query: str) -> list[dict[str, Any]]:
    import csv
    import io

    out = _fixture().compose("exec", "-T", "postgres", "psql", "-U", "medplum", "-d", "medplum", "--csv",
                             "-v", "ON_ERROR_STOP=1", "-c", query)
    return list(csv.DictReader(io.StringIO(out)))


def patients(_query: str = "") -> list[dict[str, Any]]:
    out = []
    for row in _fixture().patient_rows():
        res = row["resource"]
        name = (res.get("name") or [{}])[0]
        out.append({"id": row["id"], "family": name.get("family", ""), "given": " ".join(name.get("given") or []),
                    "names": len(res.get("name") or []), "keys": ",".join(sorted(res))})
    return out


class App(AppBase):
    def dump(self) -> dict[str, list[dict[str, Any]]]:
        return dump_database('postgresql', mp_rows)

    name = "medplum_patient"
    workflow = "new-patient"
    upstream_port = UI
    proxy_port = UI + 2  # unused: the page talks to the FHIR server directly
    form_path = "/Patient/new"
    save_path_regex = r"^/fhir/R4/Patient$"
    unique_param = "family"
    api_identity = {"subject_name": "family"}
    normal_values = [dict(given="Ada", family="Okonkwo"), dict(given="Maja", family="Lindqvist"),
                     dict(given="Hiro", family="Tanabe")]
    blank_values = dict(given="", family="Moreau")
    dup_values = dict(given="Quinn", family="Quinn")
    invalid_values = dict(given="Ivo", family="Bad")
    invalid_note = "not applicable: the FHIR server accepts any name"

    @property
    def base(self) -> str:
        return f"http://localhost:{UI}"

    def up(self) -> None:
        f = _fixture()
        f.up()
        f.ensure_user(CLERK, "Clerk", "Synthetic")

    def down(self) -> None:
        _fixture().compose("stop")

    def login(self, page: Any) -> None:
        password = json.loads((STATE / "secrets.json").read_text())[f"user:{CLERK}"]
        page.goto(self.base + "/signin")
        page.wait_for_load_state("networkidle")
        page.fill("input[name=email]", CLERK)
        page.keyboard.press("Enter")
        page.wait_for_selector("input[name=password]", timeout=30000)
        page.fill("input[name=password]", password)
        page.keyboard.press("Enter")
        page.wait_for_url(lambda u: "/signin" not in u, timeout=30000)
        page.wait_for_load_state("networkidle")

    def open_form(self, page: Any) -> None:
        page.goto(self.base + self.form_path)
        page.wait_for_load_state("networkidle")
        page.get_by_role("button", name="Add Name").wait_for(timeout=30000)

    def record(self, rec: Any, page: Any, v: dict[str, str]) -> None:
        rec_click(rec, page, "button:has-text('Add Name')")
        page.wait_for_timeout(500)
        rec_text(rec, page, "input[placeholder=Given]", v["given"], "given")
        rec_text(rec, page, "input[placeholder=Family]", v["family"], "family")
        rec_click(rec, page, "button:has-text('Create')")

    def cases(self) -> list[Case]:
        return [c for c in super().cases() if c.kind in ("normal", "blank", "dup_values")]

    def rows(self) -> list[dict[str, Any]]:
        return patients()

    def verifier(self) -> Any:
        return ExecSqlVerifier(patients, "SELECT patients", timeout_s=10.0)

    def expect_fields(self, v: dict[str, str]) -> dict[str, str]:
        return {"family": v["family"], "given": v["given"]}

    def effects(self) -> list[Effect]:
        match = {"family": ValueExpr(param="family")}
        return [
            Effect(kind=EffectKind.RECORD_WRITTEN, match=match, expected_count=1, risk="reversible",
                   probe="exactly one patient with this family name", timeout_s=8.0),
            Effect(kind=EffectKind.FIELD_EQUALS, match=match, field="given", value=ValueExpr(param="given"),
                   risk="reversible", probe="patient given name read-back", timeout_s=8.0),
        ]
