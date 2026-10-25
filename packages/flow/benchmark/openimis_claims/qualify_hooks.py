"""Sign-in, reset and the claims oracle role for the local openIMIS fixture.

``benchmark/openimis_claims/verify.claims.yaml`` names these functions. They
are environment setup for a synthetic test system, not workflow logic:

- ``open_session`` signs the demo actor in and opens a blank claim form for
  the fixed health facility and claim administrator (the demo's unmeasured
  setup, ``scripts/openimis_claims_demo.py``);
- ``reset`` voids the synthetic claims the cases and the soak entered (codes
  starting ``OA`` or ``SI``); voided claims are invisible to every read;
- ``ensure_claims_oracle`` creates the read-only role the record check
  connects as and exports its password for the SQL verifier.

Environment (all optional): ``OPENIMIS_STATE_DIR``, ``OPENIMIS_DB_PORT``.
"""

from __future__ import annotations

import atexit
import os
from typing import Any, Callable

ORACLE_ROLE = "oa_claims_oracle"
ORACLE_TABLES = ("tblClaim", "tblInsuree", "tblHF")
#: Claim-code prefixes the benchmark writes (the demo's OA..., the soak's SI...).
SYNTHETIC_PREFIXES = ("OA", "SI")

_fixture: Any = None
_browser: Any = None


def fixture() -> Any:
    global _fixture
    if _fixture is None:
        from benchmark.openimis_claims.fixture import OpenIMISFixture

        _fixture = OpenIMISFixture()
    return _fixture


def ensure_claims_oracle() -> str:
    """Create (or reset) the record check's read-only role; export its password."""

    fx = fixture()
    password = fx._secrets().get("claims_oracle_password")
    if not password:
        import json
        import secrets

        path = fx.state_dir / "secrets.json"
        data = json.loads(path.read_text())
        data["claims_oracle_password"] = password = secrets.token_urlsafe(24)
        path.write_text(json.dumps(data, indent=2))
    exists = fx._psql(
        f"SELECT count(*) FROM pg_roles WHERE rolname='{ORACLE_ROLE}';"
    ).strip()
    verb = "ALTER" if exists == "1" else "CREATE"
    fx._psql(f"{verb} ROLE \"{ORACLE_ROLE}\" WITH LOGIN PASSWORD '{password}';")
    fx._psql(
        f'ALTER ROLE "{ORACLE_ROLE}" WITH NOSUPERUSER NOCREATEDB NOCREATEROLE;'
        f' ALTER ROLE "{ORACLE_ROLE}" SET default_transaction_read_only = on;'
    )
    for table in ORACLE_TABLES:
        fx._psql(f'GRANT SELECT ON "{table}" TO "{ORACLE_ROLE}";')
    os.environ["OPENIMIS_CLAIMS_ORACLE_PASSWORD"] = password
    return password


def _shared_browser() -> Any:
    global _browser
    if _browser is None:
        from playwright.sync_api import sync_playwright

        from openadapt_flow._browser_setup import ensure_chromium_installed

        ensure_chromium_installed()
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
        _browser = (pw, browser)

        def _stop() -> None:
            try:
                browser.close()
            finally:
                pw.stop()

        atexit.register(_stop)
    return _browser[1]


def open_session(config: Any) -> tuple[Any, Callable[[], None]]:
    """A fresh signed-in context at a blank claim form."""

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from scripts.openimis_claims_demo import _setup_claim_form

    if not os.environ.get("OPENIMIS_CLAIMS_ORACLE_PASSWORD"):
        ensure_claims_oracle()
    width, height = config.app.viewport
    context = _shared_browser().new_context(
        viewport={"width": width, "height": height}, device_scale_factor=1
    )
    page = context.new_page()
    _setup_claim_form(page, fixture())
    return PlaywrightBackend(page), context.close


def void_synthetic_claims(prefixes: tuple[str, ...] = SYNTHETIC_PREFIXES) -> int:
    fx = fixture()
    where = " OR ".join(f"\"ClaimCode\" LIKE '{p}%'" for p in prefixes)
    out = fx._psql(
        f'UPDATE "tblClaim" SET "ValidityTo" = now() '
        f'WHERE "ValidityTo" IS NULL AND ({where}) RETURNING "ClaimID";'
    )
    # psql prints the command tag ("UPDATE n") after the returned ids.
    return len([line for line in out.splitlines() if line.strip().isdigit()])


def reset() -> None:
    """Void every synthetic claim the benchmark entered."""

    void_synthetic_claims()
