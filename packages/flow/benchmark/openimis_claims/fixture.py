"""Pinned local openIMIS claims-intake reference environment.

This is the reproducible INSURANCE reference environment for OpenAdapt: a real
open-source health-insurance management system (openIMIS, AGPL-3.0, used by
national health-insurance schemes) run locally from digest-pinned images, in
which a health-facility claim is entered through the browser UI and verified
against the database — the same record -> compile -> replay loop the OpenEMR
(healthcare) and Frappe Lending (lending) reference environments demonstrate.

It is a DEMO/reference environment, not a benchmark:

* no timing matrix, no agent arm, no publication protocol;
* the success oracle is a direct SQL read of the claim row (actor self-report
  and pixels never establish success), plus an exact one-row cardinality check;
* every value is synthetic. The upstream openIMIS demo dataset is a fictional
  fixture (invented regions, facilities, insurees, tariffs), and the bootstrap
  adds one more synthetic policyholder with in-force coverage so the recorded
  claim shows an active policy.

Mutable state (generated secrets) is confined to ``state_dir``. All published
ports bind to loopback only.
"""

from __future__ import annotations

import json
import secrets
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

HERE = Path(__file__).resolve().parent

COMPOSE_PROJECT = "openadapt-openimis-claims"
HTTP_PORT = 9401

# openIMIS demo-dataset actor (upstream synthetic fixture credential; the
# stack is loopback-only and contains synthetic data exclusively).
ACTOR_USER = "Admin"
ACTOR_PASSWORD = "admin123"

# Synthetic policyholder created by ``bootstrap()`` (SYNTHETIC DATA ONLY).
POLICYHOLDER_CHF = "999000001"
POLICYHOLDER_NAME = "Avery Doe"

# Demonstrated claim scenario (all values exist in the upstream synthetic
# demo dataset).
HEALTH_FACILITY_CODE = "VIHOS001"  # "Vida District Hospital" (fictional)
CLAIM_ADMIN_CODE = "VHOS0011"
# Diagnosis/service pickers are driven by letters-only search text: replay
# verifies typed input by reading it back from the live field, and a code
# such as "A000" is one OCR 0/O flip away from a spurious mismatch. The
# searched names select codes A389 and A1 from the synthetic tariff.
DIAGNOSIS_QUERY = "Scarlet"
DIAGNOSIS_OPTION = "Scarlet fever, uncomplicated"  # ICD A389
SERVICE_QUERY = "General"
SERVICE_OPTION = "General Consultation"  # service A1
# The openIMIS claim-code input accepts at most 8 characters.
CLAIM_CODE_MAX_LEN = 8
DEFAULT_CLAIM_CODE = "OA000001"


class FixtureError(RuntimeError):
    """A fixture precondition or oracle check failed."""


def _require_pinned(image: str, name: str) -> str:
    if "@sha256:" not in image:
        raise FixtureError(f"lock image for {name!r} is not digest-pinned: {image!r}")
    return image


class OpenIMISFixture:
    """Compose orchestration + synthetic bootstrap + SQL claim oracle."""

    def __init__(self, state_dir: Path | None = None, http_port: int = HTTP_PORT):
        self.http_port = http_port
        self.state_dir = state_dir or (HERE / "out" / "state")
        self.lock = json.loads((HERE / "environment.lock.json").read_text())
        self.images = {
            name: _require_pinned(image, name)
            for name, image in self.lock["services"].items()
        }

    # -- environment ---------------------------------------------------------

    @property
    def base_url(self) -> str:
        return f"http://localhost:{self.http_port}"

    @property
    def front_url(self) -> str:
        return f"{self.base_url}/front/"

    def _secrets(self) -> dict[str, str]:
        path = self.state_dir / "secrets.json"
        if path.exists():
            data = json.loads(path.read_text())
        else:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            data = {
                "db_password": secrets.token_urlsafe(24),
                "redis_password": secrets.token_urlsafe(24),
                "secret_key": secrets.token_hex(32),
            }
            path.touch(mode=0o600)
            path.write_text(json.dumps(data, indent=2))
        return data

    def _compose_env(self) -> dict[str, str]:
        creds = self._secrets()
        return {
            "OPENIMIS_BE_IMAGE": self.images["backend"],
            "OPENIMIS_FE_IMAGE": self.images["frontend"],
            "OPENIMIS_PGSQL_IMAGE": self.images["pgsql"],
            "OPENIMIS_REDIS_IMAGE": self.images["redis"],
            "OPENIMIS_RABBITMQ_IMAGE": self.images["rabbitmq"],
            "OPENIMIS_DB_PASSWORD": creds["db_password"],
            "OPENIMIS_REDIS_PASSWORD": creds["redis_password"],
            "OPENIMIS_SECRET_KEY": creds["secret_key"],
            "OPENIMIS_HTTP_PORT": str(self.http_port),
        }

    def _compose(self, *args: str, input_text: str | None = None) -> str:
        import os

        cmd = [
            "docker",
            "compose",
            "--project-name",
            COMPOSE_PROJECT,
            "-f",
            str(HERE / "compose.yml"),
            *args,
        ]
        proc = subprocess.run(
            cmd,
            input=input_text,
            capture_output=True,
            text=True,
            env={**os.environ, **self._compose_env()},
        )
        if proc.returncode != 0:
            raise FixtureError(
                f"compose {' '.join(args[:2])} failed "
                f"(exit {proc.returncode}):\n{proc.stderr[-2000:]}"
            )
        return proc.stdout

    # -- lifecycle -----------------------------------------------------------

    def up(self, *, wait_s: float = 900.0) -> None:
        """Pull pinned images, start the stack, and wait until it serves."""
        self._compose("up", "-d")
        self.wait_ready(wait_s=wait_s)

    def wait_ready(self, *, wait_s: float = 900.0) -> None:
        """Wait for the frontend (200) AND the backend GraphQL endpoint.

        First bring-up includes Django migrations plus the synthetic demo
        dataset load, which can take several minutes (longer under Rosetta
        emulation on Apple Silicon).
        """
        deadline = time.monotonic() + wait_s
        last = "no response yet"

        def _probe(url: str, ok) -> bool:
            nonlocal last
            try:
                with urlopen(url, timeout=5) as resp:  # noqa: S310
                    last = f"{url} -> HTTP {resp.status}"
                    return ok(resp.status)
            except HTTPError as exc:
                # Any routed HTTP response proves the service is up; GET on
                # the GraphQL endpoint legitimately returns 4xx.
                last = f"{url} -> HTTP {exc.code}"
                return ok(exc.code)
            except (URLError, OSError) as exc:
                last = f"{url} -> {exc}"
                return False

        while time.monotonic() < deadline:
            if _probe(self.front_url, lambda s: s == 200) and _probe(
                f"{self.base_url}/api/graphql", lambda s: s < 500
            ):
                return
            time.sleep(5)
        raise FixtureError(f"openIMIS stack not ready after {wait_s}s: {last}")

    def down(self, *, volumes: bool = False) -> None:
        args = ["down"]
        if volumes:
            args.append("--volumes")
        self._compose(*args)

    # -- SQL -----------------------------------------------------------------

    def _psql(self, sql: str) -> str:
        return self._compose(
            "exec",
            "-T",
            "db",
            "psql",
            "-U",
            "IMISuser",
            "-d",
            "IMIS",
            "-v",
            "ON_ERROR_STOP=1",
            "-t",
            "-A",
            input_text=sql,
        )

    # -- bootstrap (SYNTHETIC DATA ONLY) -------------------------------------

    BOOTSTRAP_SQL = """
BEGIN;
WITH ins AS (
  INSERT INTO "tblInsuree" ("InsureeUUID","CHFID","LastName","OtherNames",
    "DOB","Gender","Marital","IsHead","CardIssued","isOffline","AuditUserID",
    "ValidityFrom","status")
  VALUES (gen_random_uuid()::text,'999000001','Doe','Avery','1990-01-15',
    'F','S',true,false,false,-1,now(),'AC')
  RETURNING "InsureeID"
), fam AS (
  INSERT INTO "tblFamilies" ("FamilyUUID","InsureeID","LocationId","Poverty",
    "isOffline","AuditUserID","ValidityFrom","FamilyAddress")
  SELECT gen_random_uuid()::text,"InsureeID",35,false,false,-1,now(),
    '12 Synthetic Lane'
  FROM ins
  RETURNING "FamilyID","InsureeID"
), upd AS (
  UPDATE "tblInsuree" i SET "FamilyID"=f."FamilyID"
  FROM fam f WHERE i."InsureeID"=f."InsureeID"
  RETURNING i."InsureeID"
), pol AS (
  INSERT INTO "tblPolicy" ("PolicyUUID","PolicyStage","PolicyStatus",
    "PolicyValue","EnrollDate","StartDate","EffectiveDate","ExpiryDate",
    "isOffline","AuditUserID","FamilyID","OfficerID","ProdID","ValidityFrom")
  SELECT gen_random_uuid()::text,'N',2,10000,'2026-01-01','2026-01-01',
    '2026-01-01','2027-12-31',false,-1,"FamilyID",1,4,now()
  FROM fam
  RETURNING "PolicyID"
)
INSERT INTO "tblInsureePolicy" ("InsureeID","PolicyId","EnrollmentDate",
  "StartDate","EffectiveDate","ExpiryDate","isOffline","AuditUserID",
  "ValidityFrom")
SELECT f."InsureeID", p."PolicyID",'2026-01-01','2026-01-01','2026-01-01',
  '2027-12-31',false,-1,now()
FROM fam f, pol p;
COMMIT;
"""

    def bootstrap(self) -> dict[str, str]:
        """Create the synthetic policyholder with in-force coverage.

        Idempotent: refuses to duplicate the policyholder. The policyholder
        (Avery Doe, CHF 999000001, active policy through 2027-12-31) is
        synthetic — as is the entire upstream demo dataset this stack loads.
        """
        existing = self._psql(
            'SELECT count(*) FROM "tblInsuree" '
            f'WHERE "CHFID"=\'{POLICYHOLDER_CHF}\' AND "ValidityTo" IS NULL;'
        ).strip()
        if existing != "0":
            return self.policyholder()
        self._psql(self.BOOTSTRAP_SQL)
        holder = self.policyholder()
        if holder.get("policy_status") != "2":
            raise FixtureError(f"bootstrap did not yield an active policy: {holder}")
        return holder

    def policyholder(self) -> dict[str, str]:
        row = self._psql(
            'SELECT i."CHFID", i."OtherNames", i."LastName", p."PolicyStatus",'
            ' p."ExpiryDate"'
            ' FROM "tblInsuree" i'
            ' JOIN "tblInsureePolicy" ip ON ip."InsureeID"=i."InsureeID"'
            ' JOIN "tblPolicy" p ON p."PolicyID"=ip."PolicyId"'
            f" WHERE i.\"CHFID\"='{POLICYHOLDER_CHF}'"
            ' AND i."ValidityTo" IS NULL AND p."ValidityTo" IS NULL;'
        ).strip()
        if not row:
            raise FixtureError(
                f"synthetic policyholder {POLICYHOLDER_CHF} not found; "
                "run bootstrap first"
            )
        chf, other, last, status, expiry = row.splitlines()[0].split("|")
        return {
            "chf_id": chf,
            "name": f"{other} {last}",
            "policy_status": status,
            "policy_expiry": expiry,
        }

    # -- claim oracle --------------------------------------------------------

    def claim_rows(self, claim_code: str) -> list[dict[str, Any]]:
        if not claim_code.replace("-", "").isalnum():
            raise FixtureError(f"refusing suspicious claim code {claim_code!r}")
        out = self._psql(
            'SELECT c."ClaimID", c."ClaimCode", c."ClaimStatus", c."Claimed",'
            ' i."CHFID", hf."HFCode"'
            ' FROM "tblClaim" c'
            ' JOIN "tblInsuree" i ON i."InsureeID"=c."InsureeID"'
            ' JOIN "tblHF" hf ON hf."HfID"=c."HFID"'
            f" WHERE c.\"ClaimCode\"='{claim_code}'"
            ' AND c."ValidityTo" IS NULL;'
        ).strip()
        rows = []
        for line in out.splitlines():
            claim_id, code, status, claimed, chf, hf = line.split("|")
            rows.append(
                {
                    "claim_id": int(claim_id),
                    "claim_code": code,
                    "claim_status": int(status),
                    "claimed": claimed,
                    "chf_id": chf,
                    "hf_code": hf,
                }
            )
        return rows

    def verify_claim(self, claim_code: str, *, wait_s: float = 30.0) -> dict[str, Any]:
        """SQL oracle: exactly one 'Entered' claim row for ``claim_code``.

        Polls because the claim mutation is processed asynchronously. Fails
        loud on zero rows (nothing written) and on >1 rows (duplicate write —
        a silent wrong-action, the failure mode OpenAdapt exists to catch).
        """
        deadline = time.monotonic() + wait_s
        rows: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            rows = self.claim_rows(claim_code)
            if rows:
                break
            time.sleep(1.0)
        if not rows:
            raise FixtureError(
                f"oracle: no claim row with code {claim_code!r} after {wait_s}s"
            )
        if len(rows) > 1:
            raise FixtureError(
                f"oracle: {len(rows)} claim rows with code {claim_code!r}; "
                "expected exactly one"
            )
        row = rows[0]
        expected = {
            "claim_status": 2,  # Entered
            "chf_id": POLICYHOLDER_CHF,
            "hf_code": HEALTH_FACILITY_CODE,
        }
        mismatches = {k: row[k] for k, v in expected.items() if row[k] != v}
        if mismatches:
            raise FixtureError(
                f"oracle: claim {claim_code!r} field mismatch: {mismatches} "
                f"(expected {expected})"
            )
        return row
