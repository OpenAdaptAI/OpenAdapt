"""In-memory systems of record for ExtraDup.

MockMed matches the Flow fault-server encounter store (synthetic, no HTTP).
OpenEMR-shaped matches the local ``patient_data`` CREATE the pinned
``openemr_local`` fixture writes. Neither talks to the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class Screen:
    """Untrusted witness: banner, filled form, or the agent's own claim."""

    banner_saved: bool = False
    form_looks_complete: bool = False
    claimed_success: bool = False

    def looks_done(self) -> bool:
        return self.banner_saved or self.form_looks_complete or self.claimed_success


class Store(Protocol):
    env: str

    def reset(self) -> None: ...

    def snapshot(self) -> list[dict[str, Any]]: ...

    def write(self, fields: dict[str, str]) -> Screen: ...


@dataclass
class _ListStore:
    """Shared auto-id list store. ``write`` persists every supplied field."""

    env: str
    identity_field: str = "id"
    _records: list[dict[str, Any]] = field(default_factory=list)
    _seq: int = 0

    def reset(self) -> None:
        self._records = []
        self._seq = 0

    def snapshot(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._records]

    def write(self, fields: dict[str, str]) -> Screen:
        self._seq += 1
        row: dict[str, Any] = {
            "id": self._seq,
            **fields,
            "source": "replay",
        }
        if self.identity_field != "id":
            row[self.identity_field] = self._seq
        self._records.append(row)
        return Screen(banner_saved=True)


class MockMedStore(_ListStore):
    """Synthetic encounter store. Independent read is ``snapshot()``."""

    def __init__(self) -> None:
        super().__init__(env="mockmed")


class OpenEMRStore(_ListStore):
    """Synthetic patient_data store shaped like local OpenEMR."""

    def __init__(self) -> None:
        super().__init__(env="openemr_local", identity_field="pid")


def store_for(env: str) -> Store:
    if env == "mockmed":
        return MockMedStore()
    if env == "openemr_local":
        return OpenEMRStore()
    raise KeyError(f"unknown ExtraDup env {env!r}")
