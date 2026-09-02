"""Oracle adapters for the reward worker.

Every adapter here satisfies ``openadapt_types.oracle.OracleAdapter``: a
``channel`` and a read-only ``read(identity)`` that returns one
``OracleObservation``. The channel sets the tier. The observation's value
carries the system-of-record records the shared judge consumes, plus a
``reachable`` flag; an unreachable read is a value with ``reachable``
false, never a guessed empty list.

The REST, SQL, FHIR, and file-arrival adapters wrap the effect-verifier kit
(``docs/EFFECT_KIT.md``) so the read logic, the read-only SQL whitelist, and
the "unreadable means INDETERMINATE" rule are the same code the runtime
uses. ``json_file`` and ``screen_dump`` are the synthetic MockMed fixtures.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping, Optional

from openadapt_types.oracle import OracleAdapter, OracleChannel, OracleObservation

from openadapt_flow.reward.models import OracleRecipeV1
from openadapt_flow.runtime.effects.effect import EffectState


def observation(
    channel: OracleChannel,
    identity: Mapping[str, str],
    records: Optional[list[dict[str, Any]]],
) -> OracleObservation:
    """Wrap a raw read as one observation; ``None`` records mean unreachable."""

    return OracleObservation(
        channel=channel,
        identity=dict(identity),
        value={
            "reachable": records is not None,
            "records": list(records or []),
        },
    )


def records_of(observed: OracleObservation) -> Optional[list[dict[str, Any]]]:
    """Return the records an observation carries, or ``None`` if unreachable."""

    if not observed.value.get("reachable"):
        return None
    raw = observed.value.get("records")
    if not isinstance(raw, list):
        return None
    return [dict(item) for item in raw if isinstance(item, dict)]


def effect_state_of(observed: OracleObservation, substrate: str) -> EffectState:
    """Project an observation onto the judge's pre-state shape."""

    records = records_of(observed)
    return EffectState(
        substrate=substrate,
        reachable=records is not None,
        records=records or [],
    )


class JsonDocumentOracle:
    """Read a JSON document of records from disk.

    ``channel`` is ``file`` for a system-of-record dump and ``ocr`` for the
    synthetic screen dump. The same reader, two tiers, on purpose: the tier
    comes from what the document is, not from how it is parsed.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        channel: OracleChannel = OracleChannel.FILE,
        records_key: Optional[str] = "records",
    ) -> None:
        self.path = Path(path)
        self.channel = channel
        self.records_key = records_key

    def read(self, identity: Mapping[str, str]) -> OracleObservation:
        try:
            body = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return observation(self.channel, identity, None)
        if self.records_key is None:
            records = body
        elif isinstance(body, dict):
            records = body.get(self.records_key)
        else:
            records = None
        if not isinstance(records, list):
            return observation(self.channel, identity, None)
        return observation(
            self.channel,
            identity,
            [item for item in records if isinstance(item, dict)],
        )


class VerifierOracle:
    """Adapt an effect-kit verifier's fresh read into an oracle observation.

    The kit verifier owns the transport (REST GET, read-only SELECT, FHIR
    search, directory listing). This adapter only asks it for a fresh
    snapshot and forwards the records.
    """

    def __init__(self, verifier: Any, *, channel: OracleChannel) -> None:
        self.verifier = verifier
        self.channel = channel

    def read(self, identity: Mapping[str, str]) -> OracleObservation:
        try:
            state = self.verifier.capture_post_state(None)
        except Exception:  # noqa: BLE001 - an unreadable store is not a guess
            return observation(self.channel, identity, None)
        if not getattr(state, "reachable", False):
            return observation(self.channel, identity, None)
        return observation(self.channel, identity, list(state.records))


def build_oracle(recipe: OracleRecipeV1, base_dir: Path) -> OracleAdapter:
    """Construct the adapter a recipe names. Secrets come from the environment."""

    if recipe.kind == "json_file":
        return JsonDocumentOracle(
            recipe.resolve_path(base_dir),
            channel=OracleChannel.FILE,
            records_key=recipe.records_key,
        )
    if recipe.kind == "screen_dump":
        return JsonDocumentOracle(
            recipe.resolve_path(base_dir),
            channel=OracleChannel.OCR,
            records_key=recipe.records_key,
        )
    if recipe.kind == "rest":
        from openadapt_flow.runtime.effects.rest import RestRecordVerifier

        return VerifierOracle(
            RestRecordVerifier(
                str(recipe.base_url),
                records_path=recipe.records_path,
                records_key=recipe.records_key,
                headers=recipe.headers(),
                timeout_s=recipe.timeout_s,
            ),
            channel=OracleChannel.API,
        )
    if recipe.kind == "sqlite":
        from openadapt_flow.runtime.effects.sql import SqlRecordVerifier

        database = recipe.resolve_path(base_dir)

        def connect() -> sqlite3.Connection:
            uri = f"file:{database}?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            conn.row_factory = sqlite3.Row
            return conn

        return VerifierOracle(
            SqlRecordVerifier(connect, str(recipe.query), timeout_s=recipe.timeout_s),
            channel=OracleChannel.DB,
        )
    if recipe.kind == "fhir":
        from openadapt_flow.runtime.effects.fhir import FhirEffectVerifier

        return VerifierOracle(
            FhirEffectVerifier(
                str(recipe.base_url),
                resource_type=recipe.resource_type,
                search_params=dict(recipe.search_params),
                field_paths=recipe.field_paths,
                access_token=recipe.token(),
                timeout_s=recipe.timeout_s,
            ),
            channel=OracleChannel.API,
        )
    if recipe.kind == "file_arrival":
        from openadapt_flow.runtime.effects.file_arrival import FileArrivalVerifier

        return VerifierOracle(
            FileArrivalVerifier(recipe.resolve_path(base_dir), pattern=recipe.pattern),
            channel=OracleChannel.FILE,
        )
    raise ValueError(f"unknown oracle recipe kind {recipe.kind!r}")
