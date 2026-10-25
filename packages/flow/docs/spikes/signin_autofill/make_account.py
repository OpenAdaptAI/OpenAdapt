"""Generate the random test account into test_account.local.json (gitignored).

Prints the username only. The password stays in the file; the person copies it
from there once, when saving the login in Chrome.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path

HERE = Path(__file__).resolve().parent
PATH = HERE / "test_account.local.json"

if PATH.exists():
    raise SystemExit(f"{PATH.name} already exists; delete it to make a new account")
account = {
    "username": f"spike-{secrets.token_hex(3)}@example.test",
    "password": secrets.token_urlsafe(18),
    "origin": "http://localhost:47811",
}
PATH.write_text(json.dumps(account, indent=2) + "\n")
PATH.chmod(0o600)
print(f"wrote {PATH.name} for {account['username']}")
