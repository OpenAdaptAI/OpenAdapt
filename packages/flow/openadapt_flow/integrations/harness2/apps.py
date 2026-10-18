"""Resettable app targets a GUI rollout runs against.

Each rollout gets its own copy of the app, started fresh and stopped after the
reward worker has read it. A copy is never shared between rollouts, so
concurrent rollouts cannot see each other's writes, and nothing here ever
points at a production system: a target only starts local, synthetic apps.

A target supplies three things: the URL the agent starts at, the oracle
recipe the reward worker reads the system of record through, and the
patient names a read-back uses to say whose record a write landed on.
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Protocol, runtime_checkable
from urllib.parse import urlencode

from openadapt_flow.integrations.harness2.tasks import MOCKMED_PATIENTS, GuiTask


@runtime_checkable
class AppSession(Protocol):
    """One running copy of the app."""

    base_url: str

    def entry_url(self, task: GuiTask) -> str:
        """Where the agent starts."""

    def oracle_recipe(self) -> dict[str, Any]:
        """The reward worker's ``oracle.json`` for this copy."""

    def subject_names(self) -> dict[str, str]:
        """Record subject id -> display name, for read-back narration."""

    def stop(self) -> None:
        """Shut the copy down. Called exactly once."""


@runtime_checkable
class AppTarget(Protocol):
    """Starts copies of one app."""

    name: str

    def start(self) -> AppSession:
        """Start a fresh, empty copy."""


class MockMedSession:
    """MockMed with its transactional backend (``mockmed/fault_server.py``)."""

    def __init__(self, base_url: str, stop: Callable[[], None], db: Any) -> None:
        self.base_url = base_url
        self.db = db
        self._stop: Optional[Callable[[], None]] = stop

    def entry_url(self, task: GuiTask) -> str:
        flags = {k: v for k, v in sorted(task.flags.items()) if v}
        # The fault flag routes Save through the backend whose store the
        # oracle reads. Without it the SPA keeps the write in page memory
        # and there is no system of record to read.
        flags.setdefault("fault", "ok")
        return f"{self.base_url}?{urlencode(flags)}#tasks"

    def oracle_recipe(self) -> dict[str, Any]:
        return {
            "kind": "rest",
            "base_url": self.base_url.rstrip("/"),
            "records_path": "/api/db",
            "records_key": "records",
        }

    def subject_names(self) -> dict[str, str]:
        return dict(MOCKMED_PATIENTS)

    def stop(self) -> None:
        stop, self._stop = self._stop, None
        if stop is not None:
            stop()


class MockMedTarget:
    """Starts one in-process MockMed server per rollout, on an ephemeral port."""

    name = "mockmed"

    def start(self) -> MockMedSession:
        from openadapt_flow.mockmed import fault_server

        url, db, stop = fault_server.serve()
        return MockMedSession(url, stop, db)


def default_targets() -> dict[str, AppTarget]:
    """The targets the adapter knows by name."""

    return {"mockmed": MockMedTarget()}


__all__ = [
    "AppSession",
    "AppTarget",
    "MockMedSession",
    "MockMedTarget",
    "default_targets",
]
