"""Read-scope DATA for the owned, unpaged SQLite fixture endpoints.

The fixture returns every row in the selected snapshot without a LIMIT.
The disbursements scope excludes fees; it does not cover every app surface.
"""

from typing import Any

MOCKLOAN_ALL_READ_CONTRACT: dict[str, Any] = {
    "records_path": "/api/db",
    "records_key": "records",
    "records_complete": True,
}

MOCKLOAN_DISBURSEMENTS_READ_CONTRACT: dict[str, Any] = {
    "records_path": "/api/disbursements",
    "records_key": "records",
    "records_complete": True,
}
