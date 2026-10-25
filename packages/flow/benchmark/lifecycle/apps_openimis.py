"""The openIMIS adapter for the lifecycle soak (``run.py --app openimis``).

Items are health-facility claims (workload_claims.py). The oracle reads
``tblClaim`` as the database owner from inside the database container, a
channel no arm uses; voided claims (``ValidityTo`` set) are gone to every
read. Only the benchmark's own claim numbers (``SI...``) are in ``rows``.

The harness's own claims (seeded ones, another clerk's, items the operator
enters by hand) are cloned in SQL from a claim openIMIS itself saved, with a
new id, UUID, claim number, insuree and explanation, and its service lines.

Injected events:

- ``other_clerk_creates`` / ``other_clerk_edits_unrelated``: SQL writes;
- ``slow_save`` / ``failed_save``: a trigger on ``tblClaim`` keyed by the
  item's claim number;
- ``force_logout``: openIMIS signs requests with stateless JWTs, so there is
  no server session to end; recorded as unsupported;
- UI changes: a style or script added to the frontend's index page.

Synthetic claims only.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from .oracle import OracleSchema

SCHEMA = OracleSchema(
    fields=(
        "id",
        "claim_no",
        "insurance_no",
        "explanation",
        "hf_code",
        "claim_status",
    ),
    columns={
        "claim_no": "claim_no",
        "insurance_no": "insurance_no",
        "explanation": "explanation",
        "hf_code": "hf_code",
        "claim_status": "claim_status",
    },
    same_keys=("claim_no",),
    belongs_keys=("claim_no",),
)

ROWS_SQL = """
SELECT c."ClaimID", c."ClaimCode", i."CHFID", coalesce(c."Explanation", ''),
       hf."HFCode", c."ClaimStatus"
FROM "tblClaim" c
JOIN "tblInsuree" i ON i."InsureeID" = c."InsureeID"
JOIN "tblHF" hf ON hf."HfID" = c."HFID"
WHERE c."ValidityTo" IS NULL AND c."ClaimCode" LIKE 'SI%'
ORDER BY c."ClaimID";
"""

DRIFT = {
    "theme": '<style id="oa-drift-theme">body{background:#f3efe6!important}'
    ".MuiPaper-root{background:#fffaf0!important}</style>",
    "interstitial": '<script id="oa-drift-interstitial">window.addEventListener('
    "'load',()=>{const d=document.createElement('div');d.id='oa-notice';"
    "d.setAttribute('role','dialog');d.style.cssText='position:fixed;inset:0;"
    "background:rgba(0,0,0,.4);z-index:99999;display:flex;align-items:center;"
    "justify-content:center';d.innerHTML='<div style=\"background:#fff;padding:24px"
    "\"><p>Scheduled maintenance tonight.</p><button>Dismiss</button></div>';"
    "d.querySelector('button').onclick=()=>d.remove();document.body.appendChild(d)"
    "});</script>",
}


def _q(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


class OpenIMISApp:
    oracle_schema = SCHEMA

    def __init__(self) -> None:
        from benchmark.openimis_claims import qualify_hooks

        self.hooks = qualify_hooks
        self.fx = qualify_hooks.fixture()
        self.ui_url = self.fx.front_url
        self.api_url = f"{self.fx.base_url}/api/graphql"
        self._triggers: list[str] = []

    def point_hooks_here(self) -> None:
        os.environ.setdefault("OPENIMIS_FRONT_URL", self.fx.front_url)
        os.environ.setdefault("OPENIMIS_DB_PORT", str(self.fx.db_port))

    def describe(self) -> dict[str, Any]:
        return {
            "app": "openimis",
            "version": "25.10",
            "lock": self.fx.lock["services"],
            "oracle": "tblClaim as the database owner, read inside the db container",
        }

    def accounts(self) -> list[tuple[str, str]]:
        from benchmark.openimis_claims.fixture import ACTOR_PASSWORD, ACTOR_USER

        return [(ACTOR_USER, ACTOR_PASSWORD)]

    def extra(self) -> dict[str, Any]:
        return {"fixture": self.fx}

    def sql(self, statement: str) -> str:
        return self.fx._psql(statement)

    # -- state -------------------------------------------------------------------

    def reset(self) -> str:
        self.clear_all_events()
        return f"voided {self.hooks.void_synthetic_claims(('SI',))} claims"

    def rows(self) -> list[dict[str, str]]:
        out = []
        for line in self.sql(ROWS_SQL).splitlines():
            parts = line.split("|")
            if len(parts) != 6 or not parts[0].isdigit():
                continue
            out.append(dict(zip(SCHEMA.fields, parts)))
        return out

    def _columns(self, table: str, skip: tuple[str, ...]) -> list[tuple[str, str]]:
        out = self.sql(
            "SELECT column_name, data_type FROM information_schema.columns "
            f"WHERE table_name = {_q(table)} ORDER BY ordinal_position;"
        )
        cols = []
        for line in out.splitlines():
            if "|" in line:
                name, dtype = line.split("|", 1)
                if name not in skip:
                    cols.append((name, dtype))
        return cols

    def _template(self) -> str:
        out = self.sql(
            'SELECT c."ClaimID" FROM "tblClaim" c JOIN "tblHF" hf ON hf."HfID" = c."HFID" '
            'WHERE hf."HFCode" = \'VIHOS001\' AND c."ClaimStatus" = 2 '
            'ORDER BY c."ValidityTo" IS NULL DESC, c."ClaimID" DESC LIMIT 1;'
        ).strip()
        if not out.isdigit():
            raise RuntimeError("no claim openIMIS saved to clone from")
        return out

    def create_chart(self, fields: dict[str, str], *, who: str) -> str:
        """Enter one claim as another clerk would: cloned from a saved claim."""

        tmpl = self._template()
        override = {
            "ClaimCode": _q(fields["claim_no"]),
            "Explanation": _q(fields.get("explanation", "")),
            "InsureeID": '(SELECT "InsureeID" FROM "tblInsuree" WHERE "CHFID" = '
            f'{_q(fields["insurance_no"])} AND "ValidityTo" IS NULL LIMIT 1)',
            "ClaimStatus": "2",
            "ValidityFrom": "now()",
            "ValidityTo": "NULL",
            "LegacyID": "NULL",
            "DateClaimed": "current_date",
        }
        cols = self._columns("tblClaim", ("ClaimID",))
        exprs = []
        for name, dtype in cols:
            if name in override:
                exprs.append(override[name])
            elif name.lower().endswith("uuid"):
                exprs.append(f"CAST(gen_random_uuid() AS {dtype})")
            else:
                exprs.append(f'"{name}"')
        names = ", ".join(f'"{n}"' for n, _ in cols)
        new_id = self.sql(
            f'INSERT INTO "tblClaim" ({names}) SELECT {", ".join(exprs)} '
            f'FROM "tblClaim" WHERE "ClaimID" = {tmpl} RETURNING "ClaimID";'
        )
        claim_id = next(line for line in new_id.splitlines() if line.strip().isdigit())
        scols = self._columns("tblClaimServices", ("ClaimServiceID",))
        sexprs = []
        for name, dtype in scols:
            if name == "ClaimID":
                sexprs.append(claim_id)
            elif name in ("ValidityFrom",):
                sexprs.append("now()")
            elif name in ("ValidityTo", "LegacyID"):
                sexprs.append("NULL")
            elif name.lower().endswith("uuid"):
                sexprs.append(f"CAST(gen_random_uuid() AS {dtype})")
            else:
                sexprs.append(f'"{name}"')
        snames = ", ".join(f'"{n}"' for n, _ in scols)
        self.sql(
            f'INSERT INTO "tblClaimServices" ({snames}) SELECT {", ".join(sexprs)} '
            f'FROM "tblClaimServices" WHERE "ClaimID" = {tmpl} AND "ValidityTo" IS NULL;'
        )
        return claim_id.strip()

    def seed_existing(self, claims: list[dict[str, str]]) -> list[str]:
        return [self.create_chart(c, who="seed") for c in claims]

    # -- events ------------------------------------------------------------------

    def _trigger(self, name: str, claim_no: str, body: str) -> None:
        self.sql(
            f"CREATE OR REPLACE FUNCTION {name}() RETURNS trigger AS $$ BEGIN "
            f'IF NEW."ClaimCode" = {_q(claim_no)} THEN {body} END IF; '
            "RETURN NEW; END $$ LANGUAGE plpgsql;"
            f'DROP TRIGGER IF EXISTS {name} ON "tblClaim";'
            f'CREATE TRIGGER {name} BEFORE INSERT ON "tblClaim" '
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
                return {"skipped": "no claim to edit"}
            target = rows[0]["id"]
            self.sql(
                'UPDATE "tblClaim" SET "Explanation" = coalesce("Explanation", \'\') '
                f"|| ' (checked)' WHERE \"ClaimID\" = {int(target)};"
            )
            return {"touched_ids": [target]}
        claim_no = (truth.person or {}).get("claim_no", "")
        if event == "slow_save":
            self._trigger(
                "oa_slow_save", claim_no, f"PERFORM pg_sleep({int(inj['seconds'])});"
            )
            return {"trigger": "oa_slow_save"}
        if event == "failed_save":
            self.sql(
                "CREATE TABLE IF NOT EXISTS oa_fail_once (n int); "
                "TRUNCATE oa_fail_once; INSERT INTO oa_fail_once VALUES (0);"
            )
            self._trigger(
                "oa_failed_save",
                claim_no,
                "UPDATE oa_fail_once SET n = n + 1; "
                "IF (SELECT n FROM oa_fail_once) <= 1 THEN "
                "RAISE EXCEPTION 'injected save failure'; END IF;",
            )
            return {"trigger": "oa_failed_save"}
        if event == "force_logout":
            return {
                "unsupported": "openIMIS uses stateless JWTs; there is no "
                "server session to end"
            }
        raise ValueError(f"unknown event {event}")

    def clear_item_events(self) -> list[str]:
        cleared = []
        for name in self._triggers:
            self.sql(
                f'DROP TRIGGER IF EXISTS {name} ON "tblClaim"; '
                f"DROP FUNCTION IF EXISTS {name}();"
            )
            cleared.append(name)
        self._triggers = []
        return cleared

    # -- UI changes ----------------------------------------------------------------

    def _frontend(self, script: str) -> str:
        return self.fx._compose("exec", "-T", "frontend", "sh", "-c", script)

    def apply_drift(self, kind: str) -> dict[str, Any]:
        snippet = DRIFT.get(kind)
        if snippet is None:
            return {"unsupported": kind}
        escaped = snippet.replace("'", "'\\''").replace("/", "\\/").replace("&", "\\&")
        self._frontend(
            "f=$(find / -name index.html -path '*front*' 2>/dev/null | head -1); "
            '[ -f "$f.oa-orig" ] || cp -p "$f" "$f.oa-orig"; '
            f"sed -i 's/<\\/head>/{escaped}<\\/head>/' \"$f\""
        )
        return {"kind": kind, "injected": snippet[:80]}

    def clear_all_events(self) -> None:
        self.clear_item_events()
        try:
            self._frontend(
                "f=$(find / -name index.html.oa-orig -path '*front*' 2>/dev/null "
                '| head -1); [ -n "$f" ] && cp -p "$f" "${f%.oa-orig}"; true'
            )
        except Exception:  # noqa: BLE001 - a stopped app has nothing to undo
            pass
