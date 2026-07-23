"""Pinned local openIMIS claims-intake reference environment.

This is the reproducible INSURANCE reference environment for OpenAdapt: a real
open-source health-insurance management system (openIMIS, AGPL-3.0, used by
national health-insurance schemes) run locally from digest-pinned images, in
which a health-facility claim is entered through the browser UI and verified
against the database — the same record -> compile -> replay loop the OpenEMR
(healthcare) and Frappe Lending (lending) reference environments demonstrate.

It is a DEMO/reference environment, not a benchmark:

* no matched timing matrix or publication protocol; a separate 3-trial paid
  agent run is reported only as small-N aggregate engineering evidence;
* the success oracle is a direct SQL read of the claim row (actor self-report
  and pixels never establish success), plus an exact one-row cardinality check;
* every value is synthetic. The upstream openIMIS demo dataset is a fictional
  fixture (invented regions, facilities, insurees, tariffs), and the bootstrap
  adds synthetic policyholders with active, expired, and future-dated policies
  so both confirmed and refused outcomes can be exercised.

Mutable state (generated secrets) is confined to ``state_dir``. All published
ports bind to loopback only.
"""

from __future__ import annotations

import datetime as dt
import json
import os
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

# Synthetic policyholders created by ``bootstrap_eligibility()`` for the
# coverage / eligibility-check reference workflow (SYNTHETIC DATA ONLY):
# one policyholder whose coverage LAPSED (the halt-on-anomaly scenario), a
# second active policyholder (so replay can parameterize onto a policyholder
# the demonstration never saw), and one not-yet-effective policyholder.
LAPSED_CHF = "999000002"
LAPSED_NAME = "Jordan Roe"
LAPSED_EXPIRY = "2026-05-31"  # in the past -> coverage lapsed
SECOND_ACTIVE_CHF = "999000003"
SECOND_ACTIVE_NAME = "Sam Poe"
FUTURE_CHF = "999000004"
FUTURE_NAME = "Taylor Foe"

# The reference run asks one explicit, reproducible eligibility question:
# whether General Consultation (A1) is covered on 2026-07-21.  Keeping the
# as-of date in the governed run parameters (rather than consulting the host
# clock) prevents the committed fixture from changing meaning as time passes.
ELIGIBILITY_SERVICE_CODE = "A1"
ELIGIBILITY_AS_OF_DATE = "2026-07-21"

# openIMIS tblPolicy.PolicyStatus codes (upstream schema).
POLICY_STATUS_ACTIVE = 2
POLICY_STATUS_EXPIRED = 8

# Read-only PostgreSQL role the SQL outcome verifier connects as
# (deployment.eligibility.yaml). Created by ``bootstrap_eligibility()`` with
# SELECT on exactly the five policy/product/service lookup tables and
# default_transaction_read_only=on; its generated password lives in the
# ignored ``out/state/secrets.json`` and is surfaced to the verifier ONLY via
# the OPENIMIS_ORACLE_PASSWORD environment variable (kit convention: secrets
# are references, never literals).
ORACLE_ROLE = "oa_eligibility_oracle"
ORACLE_PASSWORD_ENV = "OPENIMIS_ORACLE_PASSWORD"
DB_PORT = 9402

# The ONE read-only SELECT both the SQL outcome verifier
# (deployment.eligibility.yaml) and the fixture's own coverage oracle run:
# resolve the checked policyholder's UNIQUE policy/product/service row and
# derive ``eligibility`` for the explicitly requested service and as-of date.
# A positive result requires an active policy whose effective/expiry dates
# contain the as-of date and whose product includes the requested, current
# service.  Kept here so the committed deployment YAML and the fixture can
# never drift apart (tests assert they match).
ELIGIBILITY_ORACLE_SQL = """\
SELECT i."CHFID" AS chf_id,
       i."LastName" AS last_name,
       i."OtherNames" AS other_names,
       s."ServCode" AS service_code,
       CAST(CAST(%(as_of_date)s AS date) AS text) AS as_of_date,
       CAST(p."PolicyStatus" AS text) AS policy_status,
       CAST(p."EffectiveDate" AS text) AS effective_date,
       CAST(p."ExpiryDate" AS text) AS expiry_date,
       CASE WHEN p."PolicyStatus" = 2
                  AND p."EffectiveDate" IS NOT NULL
                  AND p."EffectiveDate" <= CAST(%(as_of_date)s AS date)
                  AND p."ExpiryDate" IS NOT NULL
                  AND p."ExpiryDate" >= CAST(%(as_of_date)s AS date)
                  AND ip."EffectiveDate" IS NOT NULL
                  AND ip."EffectiveDate" <= CAST(%(as_of_date)s AS date)
                  AND ip."ExpiryDate" IS NOT NULL
                  AND ip."ExpiryDate" >= CAST(%(as_of_date)s AS date)
                  AND CAST(ps."ValidityFrom" AS date)
                      <= CAST(%(as_of_date)s AS date)
                  AND CAST(s."ValidityFrom" AS date)
                      <= CAST(%(as_of_date)s AS date)
            THEN 'Eligible' ELSE 'Ineligible' END AS eligibility
FROM "tblInsuree" i
JOIN "tblInsureePolicy" ip ON ip."InsureeID" = i."InsureeID"
JOIN "tblPolicy" p ON p."PolicyID" = ip."PolicyId"
JOIN "tblProductServices" ps ON ps."ProdID" = p."ProdID"
JOIN "tblServices" s ON s."ServiceID" = ps."ServiceID"
WHERE i."CHFID" = %(insurance_no)s
  AND s."ServCode" = %(service_code)s
  AND i."ValidityTo" IS NULL
  AND ip."ValidityTo" IS NULL
  AND p."ValidityTo" IS NULL
  AND ps."ValidityTo" IS NULL
  AND s."ValidityTo" IS NULL
"""

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

    def __init__(
        self,
        state_dir: Path | None = None,
        http_port: int = HTTP_PORT,
        db_port: int | None = None,
        project_name: str | None = None,
    ):
        self.http_port = http_port
        self.db_port = int(
            db_port
            if db_port is not None
            else os.environ.get("OPENIMIS_DB_PORT", DB_PORT)
        )
        self.project_name = project_name or os.environ.get(
            "OPENIMIS_COMPOSE_PROJECT", COMPOSE_PROJECT
        )
        configured_state = os.environ.get("OPENIMIS_STATE_DIR")
        self.state_dir = state_dir or (
            Path(configured_state) if configured_state else HERE / "out" / "state"
        )
        if not (1 <= self.http_port <= 65535 and 1 <= self.db_port <= 65535):
            raise FixtureError("openIMIS fixture ports must be between 1 and 65535")
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
        if "oracle_password" not in data:
            # Backfill for state dirs created before the eligibility oracle
            # existed; same generated-secret discipline as the stack secrets.
            data["oracle_password"] = secrets.token_urlsafe(24)
            path.write_text(json.dumps(data, indent=2))
        return data

    def oracle_password(self) -> str:
        """The read-only SQL-oracle role's generated fixture secret."""
        return self._secrets()["oracle_password"]

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
            "OPENIMIS_DB_PORT": str(self.db_port),
        }

    def _compose(self, *args: str, input_text: str | None = None) -> str:
        import os

        cmd = [
            "docker",
            "compose",
            "--project-name",
            self.project_name,
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

    # -- eligibility scenario (SYNTHETIC DATA ONLY) --------------------------

    # Same shape as BOOTSTRAP_SQL, parameterized over the policyholder. All
    # values are fixture-controlled synthetic literals validated by
    # ``_bootstrap_policyholder`` (never external input).
    _POLICYHOLDER_SQL = """
BEGIN;
WITH ins AS (
  INSERT INTO "tblInsuree" ("InsureeUUID","CHFID","LastName","OtherNames",
    "DOB","Gender","Marital","IsHead","CardIssued","isOffline","AuditUserID",
    "ValidityFrom","status")
  VALUES (gen_random_uuid()::text,'{chf}','{last}','{other}','{dob}',
    '{gender}','S',true,false,false,-1,now(),'AC')
  RETURNING "InsureeID"
), fam AS (
  INSERT INTO "tblFamilies" ("FamilyUUID","InsureeID","LocationId","Poverty",
    "isOffline","AuditUserID","ValidityFrom","FamilyAddress")
  SELECT gen_random_uuid()::text,"InsureeID",35,false,false,-1,now(),
    '{address}'
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
  SELECT gen_random_uuid()::text,'N',{status},10000,'{enroll}','{enroll}',
    '{enroll}','{expiry}',false,-1,"FamilyID",1,4,now()
  FROM fam
  RETURNING "PolicyID"
)
INSERT INTO "tblInsureePolicy" ("InsureeID","PolicyId","EnrollmentDate",
  "StartDate","EffectiveDate","ExpiryDate","isOffline","AuditUserID",
  "ValidityFrom")
SELECT f."InsureeID", p."PolicyID",'{enroll}','{enroll}','{enroll}',
  '{expiry}',false,-1,now()
FROM fam f, pol p;
COMMIT;
"""

    def _bootstrap_policyholder(
        self,
        *,
        chf: str,
        last: str,
        other: str,
        dob: str,
        gender: str,
        address: str,
        enroll: str,
        expiry: str,
        status: int,
    ) -> None:
        """Idempotently insert one synthetic policyholder + policy."""
        for value in (chf, last, other, dob, gender, address, enroll, expiry):
            if "'" in value or ";" in value:
                raise FixtureError(f"refusing suspicious fixture value {value!r}")
        existing = self._psql(
            'SELECT count(*) FROM "tblInsuree" '
            f'WHERE "CHFID"=\'{chf}\' AND "ValidityTo" IS NULL;'
        ).strip()
        if existing != "0":
            return
        self._psql(
            self._POLICYHOLDER_SQL.format(
                chf=chf,
                last=last,
                other=other,
                dob=dob,
                gender=gender,
                address=address,
                enroll=enroll,
                expiry=expiry,
                status=int(status),
            )
        )

    def _bootstrap_oracle_role(self) -> None:
        """Create/refresh the read-only SQL-oracle role (idempotent).

        The role is the REAL read-only enforcement behind the SQL outcome
        verifier (the kit's statement filter is only defense in depth): LOGIN
        with SELECT on exactly the five policy/product/service tables, plus
        ``default_transaction_read_only=on``.
        """
        password = self.oracle_password().replace("'", "''")
        exists = self._psql(
            f"SELECT count(*) FROM pg_roles WHERE rolname='{ORACLE_ROLE}';"
        ).strip()
        if exists == "0":
            self._psql(f"CREATE ROLE \"{ORACLE_ROLE}\" LOGIN PASSWORD '{password}';")
        else:
            self._psql(
                f"ALTER ROLE \"{ORACLE_ROLE}\" WITH LOGIN PASSWORD '{password}';"
            )
        self._psql(
            f'ALTER ROLE "{ORACLE_ROLE}" WITH LOGIN NOSUPERUSER NOCREATEDB '
            f"NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;\n"
            f'ALTER ROLE "{ORACLE_ROLE}" SET default_transaction_read_only = on;\n'
            # PostgreSQL roles always inherit PUBLIC privileges. Revoking only
            # from the named role does not remove PUBLIC's default schema/temp
            # grants, so close those fixture-wide grants first. IMISuser owns
            # this isolated synthetic database and retains owner privileges.
            f"REVOKE CREATE ON SCHEMA public FROM PUBLIC;\n"
            f'REVOKE CREATE ON DATABASE "IMIS" FROM PUBLIC;\n'
            f'REVOKE TEMPORARY ON DATABASE "IMIS" FROM PUBLIC;\n'
            f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM PUBLIC;\n"
            f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public "
            f'FROM "{ORACLE_ROLE}";\n'
            f'REVOKE CREATE ON SCHEMA public FROM "{ORACLE_ROLE}";\n'
            f'REVOKE CREATE ON DATABASE "IMIS" FROM "{ORACLE_ROLE}";\n'
            f'REVOKE TEMPORARY ON DATABASE "IMIS" FROM "{ORACLE_ROLE}";\n'
            f'GRANT CONNECT ON DATABASE "IMIS" TO "{ORACLE_ROLE}";\n'
            f'GRANT USAGE ON SCHEMA public TO "{ORACLE_ROLE}";\n'
            f'GRANT SELECT ON "tblInsuree", "tblPolicy", "tblInsureePolicy", '
            f'"tblProductServices", "tblServices" '
            f'TO "{ORACLE_ROLE}";'
        )
        audit = self._psql(
            f"""
SELECT CASE WHEN
  has_database_privilege('{ORACLE_ROLE}', 'IMIS', 'CONNECT')
  AND NOT has_database_privilege('{ORACLE_ROLE}', 'IMIS', 'CREATE')
  AND NOT has_database_privilege('{ORACLE_ROLE}', 'IMIS', 'TEMP')
  AND has_schema_privilege('{ORACLE_ROLE}', 'public', 'USAGE')
  AND NOT has_schema_privilege('{ORACLE_ROLE}', 'public', 'CREATE')
  AND has_table_privilege(
    '{ORACLE_ROLE}', 'public."tblInsuree"', 'SELECT'
  )
  AND has_table_privilege(
    '{ORACLE_ROLE}', 'public."tblPolicy"', 'SELECT'
  )
  AND has_table_privilege(
    '{ORACLE_ROLE}', 'public."tblInsureePolicy"', 'SELECT'
  )
  AND has_table_privilege(
    '{ORACLE_ROLE}', 'public."tblProductServices"', 'SELECT'
  )
  AND has_table_privilege(
    '{ORACLE_ROLE}', 'public."tblServices"', 'SELECT'
  )
  AND NOT EXISTS (
    SELECT 1 FROM information_schema.table_privileges
    WHERE grantee IN ('{ORACLE_ROLE}', 'PUBLIC') AND table_schema = 'public'
      AND (
        privilege_type <> 'SELECT'
        OR table_name NOT IN (
          'tblInsuree', 'tblPolicy', 'tblInsureePolicy',
          'tblProductServices', 'tblServices'
        )
      )
  )
  AND 5 = (
    SELECT count(*) FROM information_schema.table_privileges
    WHERE grantee = '{ORACLE_ROLE}' AND table_schema = 'public'
      AND privilege_type = 'SELECT'
  )
  AND EXISTS (
    SELECT 1 FROM pg_roles
    WHERE rolname = '{ORACLE_ROLE}' AND rolcanlogin AND NOT rolsuper
      AND NOT rolcreatedb AND NOT rolcreaterole AND NOT rolinherit
      AND NOT rolreplication AND NOT rolbypassrls
      AND rolconfig @> ARRAY['default_transaction_read_only=on']::text[]
  )
  AND NOT EXISTS (
    SELECT 1 FROM pg_auth_members
    WHERE member = (SELECT oid FROM pg_roles WHERE rolname = '{ORACLE_ROLE}')
  )
  AND NOT EXISTS (
    SELECT 1 FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relowner = (SELECT oid FROM pg_roles WHERE rolname = '{ORACLE_ROLE}')
      AND n.nspname = 'public'
  )
  AND NOT EXISTS (
    SELECT 1 FROM pg_namespace
    WHERE nspowner = (SELECT oid FROM pg_roles WHERE rolname = '{ORACLE_ROLE}')
  )
  AND NOT EXISTS (
    SELECT 1 FROM pg_database
    WHERE datdba = (SELECT oid FROM pg_roles WHERE rolname = '{ORACLE_ROLE}')
  )
THEN 'read-only-exact' ELSE 'unexpected-privileges' END;
"""
        ).strip()
        if audit != "read-only-exact":
            raise FixtureError(
                "eligibility oracle role failed its effective read-only "
                f"privilege audit: {audit!r}"
            )

    def bootstrap_eligibility(self) -> dict[str, dict[str, str]]:
        """Provision the coverage-check scenario (idempotent, synthetic only).

        Ensures four synthetic policyholders exist -- the claims demo's
        A1/date-eligible policyholder (Avery Doe), an expired policyholder
        (Jordan Roe, expired ``LAPSED_EXPIRY``) for the halt-on-anomaly
        scenario, a second A1/date-eligible policyholder (Sam Poe) so replay
        can parameterize onto a policyholder the demonstration never saw, and
        a not-yet-effective policyholder (Taylor Foe) for the negative contract
        -- and creates the read-only SQL-oracle role the verifier connects as.
        """
        self.bootstrap()  # Avery Doe, eligible on the declared date (idempotent)
        self._bootstrap_policyholder(
            chf=LAPSED_CHF,
            last="Roe",
            other="Jordan",
            dob="1985-06-02",
            gender="M",
            address="14 Synthetic Lane",
            enroll="2025-01-01",
            expiry=LAPSED_EXPIRY,
            status=POLICY_STATUS_EXPIRED,
        )
        self._bootstrap_policyholder(
            chf=SECOND_ACTIVE_CHF,
            last="Poe",
            other="Sam",
            dob="1992-03-20",
            gender="F",
            address="16 Synthetic Lane",
            enroll="2026-01-01",
            expiry="2027-06-30",
            status=POLICY_STATUS_ACTIVE,
        )
        self._bootstrap_policyholder(
            chf=FUTURE_CHF,
            last="Foe",
            other="Taylor",
            dob="1988-11-12",
            gender="F",
            address="18 Synthetic Lane",
            enroll="2026-08-01",
            expiry="2027-07-31",
            status=POLICY_STATUS_ACTIVE,
        )
        self._bootstrap_oracle_role()
        holders = {
            chf: self.coverage(chf)
            for chf in (
                POLICYHOLDER_CHF,
                LAPSED_CHF,
                SECOND_ACTIVE_CHF,
                FUTURE_CHF,
            )
        }
        expected = {
            POLICYHOLDER_CHF: "Eligible",
            LAPSED_CHF: "Ineligible",
            SECOND_ACTIVE_CHF: "Eligible",
            FUTURE_CHF: "Ineligible",
        }
        for chf, want in expected.items():
            got = holders[chf]["eligibility"]
            if got != want:
                raise FixtureError(
                    f"eligibility bootstrap: {chf} outcome is {got!r}, "
                    f"expected {want!r}"
                )
        return holders

    def coverage(
        self,
        chf: str,
        *,
        service_code: str = ELIGIBILITY_SERVICE_CODE,
        as_of_date: str = ELIGIBILITY_AS_OF_DATE,
    ) -> dict[str, str]:
        """The eligibility outcome row for one policyholder (read-only).

        Runs the SAME ``ELIGIBILITY_ORACLE_SQL`` the deployed effect verifier
        uses, so the fixture's ground truth and the verifier's probe can never
        diverge.
        """
        if not chf.isdigit():
            raise FixtureError(f"refusing suspicious insuree number {chf!r}")
        if not service_code.replace("-", "").isalnum():
            raise FixtureError(f"refusing suspicious service code {service_code!r}")
        try:
            parsed_date = dt.date.fromisoformat(as_of_date)
        except ValueError as exc:
            raise FixtureError(
                f"invalid eligibility as-of date {as_of_date!r}"
            ) from exc
        if parsed_date.isoformat() != as_of_date:
            raise FixtureError(
                f"eligibility as-of date is not canonical: {as_of_date!r}"
            )
        sql = ELIGIBILITY_ORACLE_SQL
        sql = sql.replace("%(insurance_no)s", f"'{chf}'")
        sql = sql.replace("%(service_code)s", f"'{service_code}'")
        sql = sql.replace("%(as_of_date)s", f"'{as_of_date}'")
        out = self._psql(sql + ";").strip()
        if not out:
            raise FixtureError(
                f"no policy/service row for insuree {chf!r}, service "
                f"{service_code!r}; run bootstrap_eligibility first"
            )
        rows = out.splitlines()
        if len(rows) > 1:
            raise FixtureError(
                f"expected one policy/service row for {chf!r}, got {len(rows)}"
            )
        (
            chf_id,
            last,
            other,
            observed_service,
            observed_as_of,
            status,
            effective,
            expiry,
            eligibility,
        ) = rows[0].split("|")
        return {
            "chf_id": chf_id,
            "name": f"{other} {last}",
            "service_code": observed_service,
            "as_of_date": observed_as_of,
            "policy_status": status,
            "policy_effective": effective,
            "policy_expiry": expiry,
            "eligibility": eligibility,
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
