"""Soak-only adjustments to the shared OpenEMR fixture, applied on request.

Two things a fresh fixture stack needs for the record-pinned duplicate check
over FHIR (``LIFECYCLE_FHIR_DUPLICATES=1``):

- **OAuth keys.** The baseline snapshot carries the OAuth ``keys`` rows of the
  install it was taken on. On a fresh stack those rows don't match the key
  files, and every token request fails with HTTP 500. The fresh install's
  ``keys`` table is saved once (:func:`save_fresh_keys`) and put back after
  every reset to the baseline.
- **FHIR read scope.** The duplicate check searches FHIR ``Patient`` with the
  record check's own read token, so the read (oracle) client is registered
  for ``api:fhir user/Patient.read`` too, after every reset, and requests it.

The shared fixture's defaults are unchanged for every other benchmark.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

FHIR_ORACLE_SCOPE = "openid api:oemr api:fhir user/patient.rs user/Patient.read"
KEYS_SQL = "fresh-oauth-keys.sql"


def _db_exec(container: str, script: str, stdin: bytes = b"") -> bytes:
    proc = subprocess.run(
        ["podman", "exec", "-i", container, "sh", "-c", script],
        input=stdin,
        capture_output=True,
        timeout=300,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"podman exec failed: {proc.stderr.decode()[-300:]}")
    return proc.stdout


def save_fresh_keys(state_dir: Path, db_container: str) -> Path:
    """Dump a freshly installed stack's OAuth ``keys`` table, once."""

    path = Path(state_dir) / KEYS_SQL
    data = _db_exec(
        db_container,
        'MYSQL_PWD="$MARIADB_ROOT_PASSWORD" exec mariadb-dump -uroot openemr keys',
    )
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def apply(db_container: str) -> bool:
    """Patch the fixture class when ``LIFECYCLE_FHIR_DUPLICATES`` is set."""

    if os.environ.get("LIFECYCLE_FHIR_DUPLICATES") != "1":
        return False
    import benchmark.openemr_local.fixture as fx

    fx.ORACLE_SCOPE = FHIR_ORACLE_SCOPE
    base = fx.OpenEMRFixture
    if getattr(base.reset, "_lifecycle_patched", False):
        return True
    original = base.reset

    def reset(self: Any) -> str:
        digest = original(self)
        keys = Path(self.state_dir) / KEYS_SQL
        if keys.is_file():
            _db_exec(
                db_container,
                'MYSQL_PWD="$MARIADB_ROOT_PASSWORD" exec mariadb -uroot openemr',
                stdin=keys.read_bytes(),
            )
        clients = json.loads(Path(self.clients_path).read_text())
        oracle = clients["oracle_client_id"]
        self._db(
            "UPDATE openemr.oauth_clients SET "
            f"scope={self._sql_quote(FHIR_ORACLE_SCOPE)} WHERE client_id="
            f"{self._sql_quote(oracle)};"
        )
        return digest

    reset._lifecycle_patched = True  # type: ignore[attr-defined]
    base.reset = reset  # type: ignore[method-assign]
    return True
