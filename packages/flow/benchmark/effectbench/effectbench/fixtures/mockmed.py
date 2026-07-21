"""MockMed -- the public, synthetic system-of-record fixture (REFERENCE ONLY).

MockMed is a tiny in-memory clinical record store with a transactional
persistence boundary. It is entirely synthetic (no real patient data, no
network, no Docker) and is the reference environment the public EffectBench
sample runs against. It is the ONE synthetic reference fixture the reusable
package ships; results on it are a REFERENCE result about this fixture, not a
general result about any real system of record. A third party scores their own
system of record by implementing a :class:`~effectbench.provider.BenchmarkProvider`.
A "save" mutates an in-process list of encounter records;
the dangerous failures for consequential writes live at that boundary and are
injected by a ``fault`` mode on the write:

- ``ok``         control: the write is persisted normally.
- ``partial``    the row persists but the note field is dropped. Banner: saved.
- ``optimistic`` the server REJECTS the write; the UI already painted success.
- ``timeout``    the row COMMITS, then the client times out. Banner: error.
- ``session``    the write returns 401; nothing persists. Banner: error.
- ``stale``      last-write-wins clobbers a concurrent actor's row (lost update).
- ``duplicate`` / ``double``  the write is accepted every time it arrives; two
                 deliveries write TWO rows.
- ``idempotent`` like duplicate but de-duplicated on an idempotency key (the fix).

The store exposes :meth:`read_records` -- the independent read path the oracle
uses -- and :meth:`write`, which returns the (deceptive) screen banner exactly as
a real optimistic-UI SPA would paint it. All data is fake.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# The canonical target of the recorded triage-save workflow (synthetic).
TARGET_PATIENT = "p1"
TARGET_TYPE = "Triage"

# Faults whose realistic manifestation is a repeated delivery.
DOUBLE_POST_FAULTS = frozenset({"duplicate", "double", "idempotent"})
# Faults that leave the optimistic banner painted "saved" despite the outcome.
_BANNER_SAVED_FAULTS = frozenset(
    {"ok", "partial", "optimistic", "stale", "duplicate", "double", "idempotent"}
)


@dataclass
class ScreenObservation:
    """The untrusted witness an agent perceives -- the rendered banner."""

    banner_saved: bool
    detail: str = ""


class MockMedSoR:
    """An in-memory clinical record store with an injectable fault boundary."""

    def __init__(self) -> None:
        self._records: list[dict[str, Any]] = []
        self._next_id = 1
        self._seen_keys: set[str] = set()

    def reset(self, *, seed_concurrent: bool = False) -> None:
        """Clear the store; optionally plant a concurrent actor's row.

        The concurrent row is a DIFFERENT encounter type for the same patient,
        so it does NOT match the target selector -- its later disappearance is
        collateral loss (the stale / lost-update fault), not the agent's write.
        """
        self._records = []
        self._next_id = 1
        self._seen_keys = set()
        if seed_concurrent:
            self._append(
                patient_id=TARGET_PATIENT,
                enc_type="Urgent",
                note="pre-existing concurrent encounter",
                source="other",
                key=None,
            )

    def seed_decoy(
        self, *, patient_id: str, enc_type: str = TARGET_TYPE, note: str = "decoy"
    ) -> None:
        """Plant a confusable / stale decoy row (its own non-trial note)."""
        self._append(
            patient_id=patient_id,
            enc_type=enc_type,
            note=note,
            source="decoy",
            key=None,
        )

    def read_records(self) -> list[dict[str, Any]]:
        """The independent read path (a copy). Never returns ``None`` here."""
        return [dict(r) for r in self._records]

    def _append(
        self,
        *,
        patient_id: str,
        enc_type: str,
        note: str,
        source: str,
        key: Optional[str],
    ) -> None:
        self._records.append(
            {
                "id": self._next_id,
                "patient_id": patient_id,
                "type": enc_type,
                "note": note,
                "source": source,
                "key": key,
            }
        )
        self._next_id += 1

    def write(
        self,
        *,
        patient_id: str,
        enc_type: str,
        note: str,
        fault: str,
        key: Optional[str] = None,
    ) -> ScreenObservation:
        """Apply one write under ``fault`` and return the rendered banner."""
        banner = fault in _BANNER_SAVED_FAULTS

        if fault in ("optimistic", "session"):
            # Nothing persists (rejected / session-expired).
            return ScreenObservation(banner, f"fault={fault}: not persisted")

        if fault == "partial":
            self._append(
                patient_id=patient_id,
                enc_type=enc_type,
                note="",
                source="replay",
                key=key,
            )
            return ScreenObservation(banner, "partial: note field dropped")

        if fault == "stale":
            # Last-write-wins: our row lands while the concurrent actor's row is
            # silently destroyed.
            self._records = []
            self._append(
                patient_id=patient_id,
                enc_type=enc_type,
                note=note,
                source="replay",
                key=key,
            )
            return ScreenObservation(banner, "stale: concurrent row clobbered")

        if fault == "idempotent":
            if key is not None and key in self._seen_keys:
                return ScreenObservation(banner, "idempotent: de-duplicated on key")
            if key is not None:
                self._seen_keys.add(key)
            self._append(
                patient_id=patient_id,
                enc_type=enc_type,
                note=note,
                source="replay",
                key=key,
            )
            return ScreenObservation(banner, "idempotent: first write persisted")

        # ok / duplicate / double / timeout: persist the row normally.
        self._append(
            patient_id=patient_id,
            enc_type=enc_type,
            note=note,
            source="replay",
            key=key,
        )
        if fault == "timeout":
            # Committed, but the client aborts before the ack -> the UI shows an
            # error even though the write landed.
            return ScreenObservation(False, "timeout: committed but client aborted")
        return ScreenObservation(banner, f"fault={fault or 'ok'}: persisted")


@dataclass
class MockMedEnv:
    """The arm-facing handle to a provisioned MockMed episode.

    An agent (system under test) drives the app through :meth:`attempt_intended_action`
    and reads back ONLY the screen banner -- never the system of record. The
    independent oracle the harness scores with is a SEPARATE object the agent can
    never reach. :meth:`product_effect_verifier` is the agent's OWN optional
    record-readback capability (a distinct verifier instance), the honest way an
    agent can refuse to trust the screen.
    """

    goal: str
    sor: MockMedSoR
    fault: str
    n_posts: int
    params: dict[str, str]
    #: Factory the agent may call to get its OWN independent verifier. Distinct
    #: instance/read path from the harness oracle; ``None`` models "no record API".
    _verifier_factory: Any = field(default=None)
    _acted: bool = field(default=False)

    def attempt_intended_action(self, params: dict[str, str]) -> ScreenObservation:
        """Perform the task's intended write once (twice for double-delivery)."""
        self._acted = True
        obs = ScreenObservation(False, "no write performed")
        key = params.get("record_key") if self.fault == "idempotent" else None
        for _ in range(self.n_posts):
            obs = self.sor.write(
                patient_id=TARGET_PATIENT,
                enc_type=TARGET_TYPE,
                note=params["note"],
                fault=self.fault,
                key=key,
            )
        return obs

    def product_effect_verifier(self) -> Any:
        """The agent's OWN independent verifier, or ``None`` if unavailable.

        REFERENCE CONVENIENCE: this synthetic fixture hands the SUT a working
        verifier so the reference result is reproducible. On a real system of
        record, authoring this verifier is the SUT's own cost -- an external
        :class:`~effectbench.provider.BenchmarkProvider` that does not supply one
        leaves ``_verifier_factory`` unset, so this returns ``None`` and
        :class:`~effectbench.adapter.EffectVerifiedSUT` fails safe.
        """
        if self._verifier_factory is None:
            return None
        return self._verifier_factory()
