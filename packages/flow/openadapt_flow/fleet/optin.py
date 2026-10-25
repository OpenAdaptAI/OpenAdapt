"""Per-app-family opt-in. Export and import are off until an admin allows them.

The opt-in file is local to each tenant (``<fleet dir>/opt-in.json``; see
:func:`default_fleet_dir`). A missing file means nothing is allowed. An admin
adds an entry with ``openadapt-flow fleet allow``. An entry names either:

* an **app family** (``availity/payer-portal``), matched exactly; or
* a **domain** (``availity.com``), matched against the host of the
  application this tenant runs, as this tenant recorded it. The host is read
  locally and never leaves the tenant.

Export and import have separate lists. Allowing import does not allow
export.
"""

from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openadapt_flow.fleet.fingerprint import APP_FAMILY_PATTERN

FLEET_DIR_ENV = "OPENADAPT_FLEET_DIR"
OPT_IN_FILE = "opt-in.json"
_DOMAIN = re.compile(
    r"^(?=.{3,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)

Direction = Literal["export", "import"]


def default_fleet_dir() -> Path:
    configured = os.environ.get(FLEET_DIR_ENV)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".openadapt" / "fleet"


class FleetOptInError(ValueError):
    """This tenant has not opted in to fleet patches for this application."""


class AllowEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    app_family: Optional[str] = Field(default=None, pattern=APP_FAMILY_PATTERN)
    domain: Optional[str] = None
    added_by: str = Field(min_length=1, max_length=200)
    added_at: str = Field(min_length=10, max_length=64)

    @model_validator(mode="after")
    def _one(self) -> "AllowEntry":
        if (self.app_family is None) == (self.domain is None):
            raise ValueError("an entry names exactly one app family or one domain")
        if self.domain is not None and not _DOMAIN.match(self.domain):
            raise ValueError("domain must be a lowercase host name like example.com")
        return self

    @property
    def key(self) -> str:
        return (
            f"family:{self.app_family}" if self.app_family else f"domain:{self.domain}"
        )

    def matches(self, app_family: str, origin_host: Optional[str]) -> bool:
        if self.app_family is not None:
            return self.app_family == app_family
        if not origin_host:
            return False
        host = origin_host.lower().rstrip(".")
        return host == self.domain or host.endswith("." + str(self.domain))


class FleetOptIn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_version: Literal["openadapt.fleet.opt-in/v1"] = "openadapt.fleet.opt-in/v1"
    export_allow: tuple[AllowEntry, ...] = ()
    import_allow: tuple[AllowEntry, ...] = ()

    @classmethod
    def off(cls) -> "FleetOptIn":
        return cls()

    def entries(self, direction: Direction) -> tuple[AllowEntry, ...]:
        return self.export_allow if direction == "export" else self.import_allow

    def allows(
        self, direction: Direction, app_family: str, origin_host: Optional[str] = None
    ) -> bool:
        return any(e.matches(app_family, origin_host) for e in self.entries(direction))

    def require(
        self, direction: Direction, app_family: str, origin_host: Optional[str] = None
    ) -> None:
        if not self.allows(direction, app_family, origin_host):
            raise FleetOptInError(
                f"fleet {direction} is off for {app_family}: an admin must add "
                f"the app family or its domain with `openadapt-flow fleet allow "
                f"--{direction}`"
            )

    def allow(
        self,
        direction: Direction,
        *,
        added_by: str,
        app_family: Optional[str] = None,
        domain: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> "FleetOptIn":
        entry = AllowEntry(
            app_family=app_family,
            domain=domain.lower() if domain else None,
            added_by=added_by,
            added_at=(now or datetime.now(timezone.utc)).isoformat(),
        )
        kept = tuple(e for e in self.entries(direction) if e.key != entry.key)
        updated = tuple(sorted(kept + (entry,), key=lambda e: e.key))
        field = "export_allow" if direction == "export" else "import_allow"
        return self.model_copy(update={field: updated})

    def disallow(
        self,
        direction: Direction,
        *,
        app_family: Optional[str] = None,
        domain: Optional[str] = None,
    ) -> "FleetOptIn":
        key = (
            f"family:{app_family}" if app_family else f"domain:{(domain or '').lower()}"
        )
        kept = tuple(e for e in self.entries(direction) if e.key != key)
        field = "export_allow" if direction == "export" else "import_allow"
        return self.model_copy(update={field: kept})

    @classmethod
    def load(cls, path: Path | str) -> "FleetOptIn":
        """Read the opt-in file. A missing file means everything is off."""
        file = Path(path)
        if not file.is_file():
            return cls.off()
        return cls.model_validate_json(file.read_text(encoding="utf-8"))

    def save(self, path: Path | str) -> None:
        file = Path(path)
        file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=file.parent, prefix=".opt-in-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(self.model_dump_json(indent=2) + "\n")
            os.replace(tmp, file)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


__all__ = [
    "AllowEntry",
    "FLEET_DIR_ENV",
    "FleetOptIn",
    "FleetOptInError",
    "OPT_IN_FILE",
    "default_fleet_dir",
]
