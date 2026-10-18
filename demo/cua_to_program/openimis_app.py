"""openIMIS claim entry, wired for the JIT wrapper.

Uses the pinned openIMIS reference stack in
``packages/flow/benchmark/openimis_claims`` (the upstream compose file is
fetched at run time and checked against pinned hashes; nothing AGPL is stored
in this repository). All data is synthetic: the upstream demo dataset plus
the policyholders created here.

What this module supplies:

* ``setup``: unmeasured sign-in with the demo actor and opening the blank
  Health Facility Claim form. Never recorded or replayed.
* ``read_claims``: a snapshot of the claim table, taken by the recorder after
  each step so the compiler can mine the effect contract.
* ``claims_verifier``: the independent record check. A read-only SELECT run
  as a dedicated Postgres role that can only read four tables.
* ``ui_change``: the deliberate UI change at record #50 (a script injected
  into the app's own index.html that renames one on-screen label).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parents[2]
FLOW = REPO / "packages" / "flow"
sys.path.insert(0, str(FLOW / "benchmark"))

from openimis_claims.fixture import (  # noqa: E402
    ACTOR_PASSWORD,
    ACTOR_USER,
    CLAIM_ADMIN_CODE,
    HEALTH_FACILITY_CODE,
    FixtureError,
    OpenIMISFixture,
)

CLAIM_FORM_PATH = "/front/claim/healthFacilities/claim"
READER_ROLE = "oa_claims_reader"
READER_TABLES = ("tblClaim", "tblInsuree", "tblHF", "tblICDCodes")

# Synthetic policyholders for the batch. Invented names and numbers.
POLICYHOLDERS = [
    ("999000101", "Rivera", "Jordan", "1984-03-02", "M"),
    ("999000102", "Okafor", "Amara", "1991-07-19", "F"),
    ("999000103", "Lindqvist", "Sam", "1977-11-05", "M"),
    ("999000104", "Tanaka", "Mei", "1969-01-28", "F"),
    ("999000105", "Haddad", "Omar", "2001-09-14", "M"),
    ("999000106", "Novak", "Petra", "1988-05-30", "F"),
    ("999000107", "Mensah", "Kofi", "1995-12-08", "M"),
    ("999000108", "Silva", "Lucia", "1973-04-22", "F"),
    ("999000109", "Brennan", "Cal", "1982-08-11", "M"),
    ("999000110", "Kaur", "Asha", "1999-02-17", "F"),
]

CLAIMS_SQL = """
SELECT c."ClaimCode" AS claim_no,
       i."CHFID" AS insurance_no,
       c."Explanation" AS explanation,
       c."ClaimStatus" AS claim_status,
       hf."HFCode" AS hf_code,
       d."ICDCode" AS diagnosis,
       CAST(c."Claimed" AS TEXT) AS claimed
FROM "tblClaim" c
JOIN "tblInsuree" i ON i."InsureeID" = c."InsureeID"
JOIN "tblHF" hf ON hf."HfID" = c."HFID"
LEFT JOIN "tblICDCodes" d ON d."ICDID" = c."ICDID"
WHERE c."ValidityTo" IS NULL
ORDER BY c."ClaimID"
"""

# The UI change: the field the program types the claim's explanation into is
# relabelled, as a vendor UI update would. The script only rewrites label
# text in the page; it does not touch data, layout or the API.
UI_CHANGE_JS = r"""
(function () {
  var FROM = "Explanation", TO = "Claim notes";
  function apply() {
    document.querySelectorAll("label").forEach(function (l) {
      if (l.textContent.trim().indexOf(FROM) === 0) {
        l.textContent = l.textContent.replace(FROM, TO);
      }
    });
  }
  new MutationObserver(apply).observe(document.documentElement,
    {childList: true, subtree: true});
  apply();
})();
"""
UI_CHANGE = {"from": "Explanation", "to": "Claim notes"}


def fixture() -> OpenIMISFixture:
    return OpenIMISFixture()


def claim_form_url(fx: Optional[OpenIMISFixture] = None) -> str:
    return (fx or fixture()).base_url + CLAIM_FORM_PATH


def _control_input(page: Any, label_text: str) -> Any:
    return page.locator(
        f'xpath=//label[contains(normalize-space(.), "{label_text}")]'
        '/ancestor::*[contains(@class,"MuiFormControl-root")][1]//input'
    ).first


def _wait_network_quiet(page: Any, open_requests: set, quiet_s: float = 0.5) -> None:
    """Wait until no request the page started is still open, for ``quiet_s``."""

    deadline = time.monotonic() + 60
    quiet_since = None
    while time.monotonic() < deadline:
        if open_requests:
            quiet_since = None
        elif quiet_since is None:
            quiet_since = time.monotonic()
        elif time.monotonic() - quiet_since >= quiet_s:
            return
        page.wait_for_timeout(100)
    raise FixtureError(f"{len(open_requests)} request(s) still open after 60 s")


def setup(page: Any) -> None:
    """Sign in with the synthetic demo actor and open a blank claim form.

    Leaves the keyboard focus in the Insurance No. field. Unmeasured: the
    timing boundary for both the agent and the program starts after this.
    """

    fx = fixture()
    open_requests: set = set()
    page.on("request", lambda r: open_requests.add(id(r)))
    page.on("requestfinished", lambda r: open_requests.discard(id(r)))
    page.on("requestfailed", lambda r: open_requests.discard(id(r)))
    page.goto(fx.front_url, wait_until="networkidle", timeout=60_000)
    page.locator("input[type=text]").fill(ACTOR_USER)
    page.locator("input[type=password]").fill(ACTOR_PASSWORD)
    page.get_by_role("button", name="LOG IN").click()
    page.wait_for_url("**/home", timeout=60_000)
    # Let the home page's requests finish before navigating. A fetch the
    # navigation cuts off gets no finished or failed event in Playwright, and
    # the run's readiness gate would wait on it forever.
    _wait_network_quiet(page, open_requests)
    page.goto(f"{fx.base_url}/front/claim/healthFacilities", wait_until="networkidle")
    hf = page.get_by_placeholder("Search a Health Facility")
    hf.click()
    hf.fill(HEALTH_FACILITY_CODE)
    option = page.locator("[role=option]", has_text=HEALTH_FACILITY_CODE).first
    option.wait_for(state="visible", timeout=30_000)
    option.click()
    _wait_network_quiet(page, open_requests)
    admin = page.get_by_placeholder("Search a Claim")
    for attempt in range(3):
        admin.click()
        option = page.locator("[role=option]", has_text=CLAIM_ADMIN_CODE).first
        try:
            option.wait_for(state="visible", timeout=10_000)
            break
        except Exception:
            if attempt == 2:
                raise
            page.keyboard.press("Escape")
            _wait_network_quiet(page, open_requests)
    option.click()
    _wait_network_quiet(page, open_requests)
    fab = page.locator("button.MuiFab-root:enabled").last
    fab.wait_for(state="visible", timeout=30_000)
    fab.click()
    page.wait_for_url("**" + CLAIM_FORM_PATH, timeout=30_000)
    ins = _control_input(page, "Insurance No")
    ins.wait_for(state="visible", timeout=30_000)
    _wait_network_quiet(page, open_requests)
    ins.click()


# -- system of record ---------------------------------------------------------


def _reader_password(fx: OpenIMISFixture) -> str:
    # The same generated fixture secret as the eligibility oracle role, kept in
    # the ignored out/state/secrets.json.
    return fx.oracle_password()


def bootstrap(fx: Optional[OpenIMISFixture] = None) -> dict[str, Any]:
    """Create the policyholders and the read-only reader role. Idempotent."""

    fx = fx or fixture()
    holder = fx.bootstrap()
    for chf, last, other, dob, gender in POLICYHOLDERS:
        fx._bootstrap_policyholder(
            chf=chf,
            last=last,
            other=other,
            dob=dob,
            gender=gender,
            address="Synthetic address",
            enroll="2026-01-01",
            expiry="2027-12-31",
            status=2,
        )
    password = _reader_password(fx).replace("'", "''")
    exists = fx._psql(
        f"SELECT count(*) FROM pg_roles WHERE rolname='{READER_ROLE}';"
    ).strip()
    verb = "CREATE" if exists == "0" else "ALTER"
    fx._psql(f"{verb} ROLE \"{READER_ROLE}\" LOGIN PASSWORD '{password}';")
    fx._psql(
        f'ALTER ROLE "{READER_ROLE}" SET default_transaction_read_only = on;\n'
        f'REVOKE ALL ON ALL TABLES IN SCHEMA public FROM "{READER_ROLE}";\n'
        + "".join(f'GRANT SELECT ON "{t}" TO "{READER_ROLE}";\n' for t in READER_TABLES)
    )
    return {"policyholder": holder, "batch_policyholders": len(POLICYHOLDERS)}


def _connect() -> Any:
    import pg8000.dbapi

    fx = fixture()
    return pg8000.dbapi.connect(
        host="127.0.0.1",
        port=fx.db_port,
        user=READER_ROLE,
        password=_reader_password(fx),
        database="IMIS",
    )


def read_claims() -> list[dict[str, Any]]:
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(CLAIMS_SQL)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def claim_rows(claim_no: str) -> list[dict[str, Any]]:
    return [r for r in read_claims() if r["claim_no"] == claim_no]


def claims_verifier() -> Any:
    from openadapt_flow.runtime.effects.sql import SqlRecordVerifier

    return SqlRecordVerifier(_connect, CLAIMS_SQL, timeout_s=15.0)


# -- the deliberate UI change --------------------------------------------------

_HTML = "/usr/share/nginx/html/index.html"
_JS = "/usr/share/nginx/html/oa-ui-change.js"
_TAG = '<script src="/front/oa-ui-change.js"></script>'


def _frontend(fx: OpenIMISFixture, *args: str, input_text: Optional[str] = None) -> str:
    return fx._compose("exec", "-T", "frontend", *args, input_text=input_text)


def ui_change(on: bool, fx: Optional[OpenIMISFixture] = None) -> None:
    """Turn the label rename on or off in the running app."""

    fx = fx or fixture()
    if on:
        _frontend(fx, "sh", "-c", f"cat > {_JS}", input_text=UI_CHANGE_JS)
        _frontend(
            fx,
            "sh",
            "-c",
            f"grep -q oa-ui-change {_HTML} || "
            f"sed -i 's#</head>#{_TAG}</head>#' {_HTML}",
        )
    else:
        _frontend(
            fx, "sh", "-c", f"sed -i 's#{_TAG}##' {_HTML}; rm -f {_JS}"
        )


def ui_change_is_on(fx: Optional[OpenIMISFixture] = None) -> bool:
    fx = fx or fixture()
    out = _frontend(fx, "sh", "-c", f"grep -c oa-ui-change {_HTML} || true")
    return out.strip() not in ("", "0")


__all__ = [
    "FixtureError",
    "POLICYHOLDERS",
    "UI_CHANGE",
    "bootstrap",
    "claim_form_url",
    "claim_rows",
    "claims_verifier",
    "read_claims",
    "setup",
    "ui_change",
]

if os.environ.get("OPENADAPT_DEMO_DEBUG"):
    print("openimis_app loaded from", __file__, file=sys.stderr)
