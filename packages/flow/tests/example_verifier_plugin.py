"""A worked example CUSTOMER verifier plugin (the adapter SDK's reference).

This module is what a customer package ships: one adapter class implementing
the :class:`~openadapt_flow.runtime.effects.adapter.VerifierAdapter` surface
(here by subclassing ``VerifierAdapterBase``) plus one factory with the
:data:`~openadapt_flow.runtime.effects.adapter.VerifierFactory` signature. In
a real package the factory is registered declaratively in ``pyproject.toml``::

    [project.entry-points."openadapt_flow.effect_verifiers"]
    csv-ledger = "acme_verifiers.csv_ledger:build_csv_ledger_verifier"

after which ``effects: {kind: csv-ledger, ...}`` in a deployment YAML builds
it. The test suite exercises both registration paths (programmatic and a
simulated entry point) in ``tests/test_verifier_adapter_platform.py``.

The substrate itself is deliberately small but REAL: a CSV ledger file is a
common last-mile system of record (an interface export, a reconciliation
extract), and the adapter demonstrates every platform obligation -- fail-safe
unreadable handling, ``ValueExpr`` entity binding through the config, the
shared judge (cardinality / duplicates / collateral), settlement polling, and
evidence redaction.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Mapping, Optional

from openadapt_flow.runtime.effects.adapter import (
    RedactionPolicy,
    VerifierAdapterBase,
    poll_until_settled,
    redact_verdict,
)
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectState,
    EffectVerdict,
)
from openadapt_flow.verification import VerificationTier


class CsvLedgerVerifier(VerifierAdapterBase):
    """Verify effects against a CSV ledger file (one record per row)."""

    substrate = "csv-ledger"
    verification_tier = VerificationTier.INDEPENDENT_SYSTEM

    def __init__(
        self,
        path: str,
        *,
        poll_interval_s: float = 0.05,
        redaction: Optional[RedactionPolicy] = None,
    ) -> None:
        self.path = Path(path)
        self.poll_interval_s = poll_interval_s
        self.redaction = redaction

    def _fetch_records(self) -> Optional[list[dict[str, Any]]]:
        """Every ledger row as a dict; ``None`` (unreadable) on any failure."""
        try:
            with self.path.open(newline="", encoding="utf-8") as handle:
                return [dict(row) for row in csv.DictReader(handle)]
        except OSError:
            return None

    def capture_pre_state(self, context: Any = None) -> EffectState:
        records = self._fetch_records()
        return EffectState(
            substrate=self.substrate,
            reachable=records is not None,
            records=records or [],
            detail={"path": str(self.path)},
        )

    def verify(
        self, expected: Effect, before: EffectState, context: Any = None
    ) -> EffectVerdict:
        verdict = poll_until_settled(
            self._fetch_records,
            expected,
            before,
            substrate=self.substrate,
            poll_interval_s=self.poll_interval_s,
        )
        return redact_verdict(verdict, self.redaction, field=expected.field)


def build_csv_ledger_verifier(
    cfg: Any, params: Optional[Mapping[str, str]] = None
) -> CsvLedgerVerifier:
    """The plugin factory (``VerifierFactory`` signature).

    Reads its substrate location from the shared ``EffectsConfig.root`` field
    (a plugin may also define and parse its own config file section); fails
    LOUD on missing config, exactly as the built-in factories do.
    """
    root = getattr(cfg, "root", None)
    if not root:
        raise ValueError("effects.kind 'csv-ledger' requires effects.root")
    return CsvLedgerVerifier(root)
