"""The Medplum (FHIR R4) adapter for the lifecycle soak (``run.py --app fhir``).

The referral workload's patients become FHIR Patients. The oracle reads
Medplum's Postgres from inside its container (a channel no arm uses), and
the harness's own writes (seeded patients, other clerks, items the operator
enters by hand) go through the FHIR API with a client of their own.

Injected events:

- ``other_clerk_creates`` / ``other_clerk_edits_unrelated``: FHIR writes by
  the harness client;
- ``slow_save`` / ``failed_save``: a Postgres trigger on the Patient table,
  keyed by the item's email;
- ``force_logout``: Medplum keeps sessions as Login resources the harness
  can't revoke through the API, so this event is recorded as unsupported;
- UI changes: a style or script added to the web app's index page.

Synthetic patients only.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from .oracle import OracleSchema

GENDER = {"Female": "female", "Male": "male"}
STATE_CODE = {"Massachusetts": "MA"}

SCHEMA = OracleSchema(
    fields=(
        "id",
        "fname",
        "lname",
        "DOB",
        "gender",
        "street",
        "city",
        "state",
        "postal_code",
        "phone_home",
        "email",
    ),
    columns={
        "fname": "fname",
        "lname": "lname",
        "DOB": "DOB",
        "street": "street",
        "city": "city",
        "postal_code": "postal_code",
        "phone_home": "phone_home",
        "email": "email",
    },
    value_maps={"sex": ("gender", GENDER), "state_label": ("state", STATE_CODE)},
    name_keys=frozenset({"fname", "lname"}),
    same_keys=("fname", "lname", "DOB"),
    belongs_keys=("email",),
)

#: Drift: a script or style the web app's index page gets, and what it does.
DRIFT = {
    "theme": '<style id="oa-drift-theme">body{background:#f3efe6!important;'
    "font-size:15px!important}</style>",
    "relabel": '<script id="oa-drift-relabel">new MutationObserver(()=>{'
    "document.querySelectorAll('button').forEach(b=>{"
    "if(b.textContent.trim()==='Create'){b.textContent='Save patient'}})})"
    ".observe(document.documentElement,{childList:true,subtree:true});</script>",
    "interstitial": '<script id="oa-drift-interstitial">window.addEventListener('
    "'load',()=>{const d=document.createElement('div');d.id='oa-notice';"
    "d.setAttribute('role','dialog');d.style.cssText='position:fixed;inset:0;"
    "background:rgba(0,0,0,.4);z-index:99999;display:flex;align-items:center;"
    "justify-content:center';d.innerHTML='<div style=\"background:#fff;padding:24px"
    "\"><p>Scheduled maintenance tonight.</p><button>Dismiss</button></div>';"
    "d.querySelector('button').onclick=()=>d.remove();document.body.appendChild(d)"
    "});</script>",
}


def to_patient(fields: dict[str, str]) -> dict[str, Any]:
    telecom = []
    if fields.get("phone_home"):
        telecom.append(
            {"system": "phone", "use": "home", "value": fields["phone_home"]}
        )
    if fields.get("email"):
        telecom.append({"system": "email", "value": fields["email"]})
    patient: dict[str, Any] = {
        "resourceType": "Patient",
        "name": [
            {"family": fields.get("lname", ""), "given": [fields.get("fname", "")]}
        ],
        "address": [
            {
                "line": [fields.get("street", "")],
                "city": fields.get("city", ""),
                "state": STATE_CODE.get(fields.get("state_label", ""), ""),
                "postalCode": fields.get("postal_code", ""),
            }
        ],
        "telecom": telecom,
    }
    if fields.get("DOB"):
        patient["birthDate"] = fields["DOB"]
    if fields.get("sex") in GENDER:
        patient["gender"] = GENDER[fields["sex"]]
    return patient


def flatten(row: dict[str, Any]) -> dict[str, str]:
    from openadapt_flow.runtime.effects.fhir import extract_path

    r = row["resource"]

    def get(path: str) -> str:
        v = extract_path(r, path)
        return "" if v is None else str(v)

    return {
        "id": row["id"],
        "fname": get("name.given"),
        "lname": get("name.family"),
        "DOB": get("birthDate"),
        "gender": get("gender"),
        "street": get("address.line"),
        "city": get("address.city"),
        "state": get("address.state"),
        "postal_code": get("address.postalCode"),
        "phone_home": get("telecom[system=phone].value"),
        "email": get("telecom[system=email].value"),
    }


class FhirApp:
    oracle_schema = SCHEMA

    def __init__(self) -> None:
        from benchmark.fhir_medplum import qualify_hooks

        self.hooks = qualify_hooks
        self.fx = qualify_hooks.fixture()
        self.ui_url = self.fx.ui_url
        self.api_url = self.fx.fhir_url
        self._drift: list[str] = []
        self._triggers: list[str] = []

    # -- setup -------------------------------------------------------------------

    def point_hooks_here(self) -> None:
        os.environ.setdefault("MEDPLUM_UI_URL", self.fx.ui_url)
        os.environ.setdefault("MEDPLUM_FHIR_URL", self.fx.fhir_url)

    def _token(self) -> str:
        return self.fx.client_token("harness")

    def describe(self) -> dict[str, Any]:
        return {
            "app": "fhir",
            "server": "medplum",
            "version": "5.2.8",
            "lock": self.fx.lock["services"],
            "oracle": 'Postgres "Patient" table, read inside the db container',
        }

    def accounts(self) -> list[tuple[str, str]]:
        return [(self.hooks.ACTOR_EMAIL, self.hooks.actor_password())]

    def extra(self) -> dict[str, Any]:
        return {
            "fixture": self.fx,
            "fhir_url": self.fx.fhir_url,
            "fhir_token": lambda: self.fx.client_token("record-check"),
        }

    # -- state -------------------------------------------------------------------

    def reset(self) -> str:
        self.clear_all_events()
        token = self._token()
        ids = self.fx.patient_ids(token=token)
        for pid in ids:
            self.fx.delete_patient(pid, token=token)
        return f"deleted {len(ids)} patients"

    def rows(self) -> list[dict[str, str]]:
        return [flatten(r) for r in self.fx.patient_rows()]

    def create_chart(self, fields: dict[str, str], *, who: str) -> str:
        return self.fx.create_patient(to_patient(fields), token=self._token())

    def seed_existing(self, people: list[dict[str, str]]) -> list[str]:
        return [self.create_chart(p, who="seed") for p in people]

    # -- events ------------------------------------------------------------------

    def _trigger(self, name: str, email: str, body: str) -> None:
        email_sql = email.replace("'", "''")
        self.fx.psql(
            f"CREATE OR REPLACE FUNCTION {name}() RETURNS trigger AS $$ BEGIN "
            f"IF NEW.content LIKE '%{email_sql}%' THEN {body} END IF; "
            "RETURN NEW; END $$ LANGUAGE plpgsql;"
            f'DROP TRIGGER IF EXISTS {name} ON "Patient";'
            f'CREATE TRIGGER {name} BEFORE INSERT ON "Patient" '
            f"FOR EACH ROW EXECUTE FUNCTION {name}();"
        )
        self._triggers.append(name)

    def inject(
        self,
        inj: dict[str, Any],
        truth: Any,
        *,
        harness: set[str],
        stop: Optional[Any] = None,
    ) -> dict[str, Any]:
        event = inj["event"]
        if event == "other_clerk_creates":
            cid = self.create_chart(inj["person"], who="other_clerk")
            harness.add(cid)
            return {"chart_id": cid}
        if event == "other_clerk_edits_unrelated":
            rows = self.rows()
            if not rows:
                return {"skipped": "no patient to edit"}
            target = rows[0]
            token = self._token()
            from benchmark.fhir_medplum.fixture import _http

            res = _http(
                "GET", f"{self.fx.fhir_url}/Patient/{target['id']}", token=token
            )
            res.setdefault("telecom", []).append(
                {"system": "phone", "use": "work", "value": "555-0199"}
            )
            _http(
                "PUT",
                f"{self.fx.fhir_url}/Patient/{target['id']}",
                body=res,
                token=token,
            )
            return {"touched_ids": [target["id"]]}
        email = (truth.person or {}).get("email", "")
        if event == "slow_save":
            self._trigger(
                "oa_slow_save", email, f"PERFORM pg_sleep({int(inj['seconds'])});"
            )
            return {"trigger": "oa_slow_save"}
        if event == "failed_save":
            self.fx.psql(
                "CREATE TABLE IF NOT EXISTS oa_fail_once (n int); "
                "TRUNCATE oa_fail_once; INSERT INTO oa_fail_once VALUES (0);"
            )
            self._trigger(
                "oa_failed_save",
                email,
                "UPDATE oa_fail_once SET n = n + 1; "
                "IF (SELECT n FROM oa_fail_once) <= 1 THEN "
                "RAISE EXCEPTION 'injected save failure'; END IF;",
            )
            return {"trigger": "oa_failed_save"}
        if event == "force_logout":
            return {
                "unsupported": "Medplum sessions are Login resources the harness "
                "can't revoke through the API"
            }
        raise ValueError(f"unknown event {event}")

    def clear_item_events(self) -> list[str]:
        cleared = []
        for name in self._triggers:
            self.fx.psql(
                f'DROP TRIGGER IF EXISTS {name} ON "Patient"; '
                f"DROP FUNCTION IF EXISTS {name}();"
            )
            cleared.append(name)
        self._triggers = []
        return cleared

    # -- UI changes ----------------------------------------------------------------

    def _index_edit(self, script: str) -> str:
        return self.fx.compose("exec", "-T", "medplum-app", "sh", "-c", script)

    def apply_drift(self, kind: str) -> dict[str, Any]:
        snippet = DRIFT.get(kind)
        if snippet is None:
            return {"unsupported": kind}
        # Keep a pristine copy once, then add the snippet before </head>.
        escaped = snippet.replace("'", "'\\''").replace("/", "\\/").replace("&", "\\&")
        self._index_edit(
            "f=$(grep -rl '<div id=\"root\"' /usr/share/nginx/html/index.html "
            "2>/dev/null || echo /usr/share/nginx/html/index.html); "
            '[ -f "$f.oa-orig" ] || cp -p "$f" "$f.oa-orig"; '
            f"sed -i 's/<\\/head>/{escaped}<\\/head>/' \"$f\""
        )
        self._drift.append(kind)
        return {"kind": kind, "injected": snippet[:80]}

    def clear_all_events(self) -> None:
        self.clear_item_events()
        try:
            self._index_edit(
                "f=/usr/share/nginx/html/index.html; "
                '[ -f "$f.oa-orig" ] && cp -p "$f.oa-orig" "$f"; true'
            )
        except Exception:  # noqa: BLE001 - a stopped app has nothing to undo
            pass
        self._drift = []
