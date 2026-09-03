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
uses. ``json_file`` and ``screen_dump`` read a JSON document on disk, which
is what the synthetic tier-0 fixture needs.

The channel belongs to the adapter, not to the recipe kind. Two recipe
kinds that build the same adapter over the same bytes read through the same
channel and earn the same tier. ``json_file`` and ``screen_dump`` are that
pair: both hand :class:`JsonDocumentOracle` a JSON document on the worker's
own disk, and nothing in the bytes tells a system-of-record dump from a
screen scrape. So both read through ``ocr`` and both stay at tier 0.
Claiming tier 2 needs a channel whose mechanism the worker can check: a
SQLite database file it opens read-only, a REST or FHIR endpoint it calls
over the network, or a directory it lists.
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
    """Read a JSON document of records from disk. Always channel ``ocr``.

    The channel is a class attribute and no caller may set it. Both the
    ``json_file`` and the ``screen_dump`` recipe kinds build this adapter, so
    a bundle that could pick the channel could hand the same bytes two
    different tiers: point ``screen_dump`` at a document and the receipt says
    tier 0 and ``development_only``; point ``json_file`` at the same document
    and it says tier 2 and ``certified``.

    Tier 0 is the honest floor for this reader. A JSON document on the
    worker's own disk carries nothing that separates a system-of-record dump
    from a screen scrape, and whoever writes the file writes the answer. Use
    ``sqlite``, ``rest``, ``fhir``, or ``file_arrival`` when the read really
    is a record channel.
    """

    #: Fixed. See the class docstring for why this is not a constructor
    #: argument.
    channel: OracleChannel = OracleChannel.OCR

    def __init__(
        self,
        path: Path | str,
        *,
        records_key: Optional[str] = "records",
    ) -> None:
        self.path = Path(path)
        self.records_key = records_key

    def read(self, identity: Mapping[str, str]) -> OracleObservation:
        # ``identity`` is stamped, not applied: this returns the whole
        # document. See the note on ``VerifierOracle.read`` for what binds a
        # judgement to a subject and for the guard that enforces it.
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
        # ``identity`` is stamped onto the observation; it does NOT scope the
        # read. The verifier returns every record in the collection, so the
        # judgement is bound to the subject only by the required effect's own
        # selector (``worker._bind`` resolves ``{"param": ...}`` references
        # against this identity). ``RewardBundle.load`` compensates: it refuses
        # a bundle whose required effects select no record by a declared
        # identity key. Scoping the read itself is the broader fix, tracked in
        # OpenAdaptAI/openadapt-flow#455.
        try:
            state = self.verifier.capture_post_state(None)
        except Exception:  # noqa: BLE001 - an unreadable store is not a guess
            return observation(self.channel, identity, None)
        if not getattr(state, "reachable", False):
            return observation(self.channel, identity, None)
        return observation(self.channel, identity, list(state.records))


class OracleMechanismError(ValueError):
    """The recipe cannot read through the channel it claims."""


#: The first sixteen bytes of every SQLite database file, from the file
#: format specification. A JSON screen dump renamed ``store.db`` does not
#: carry them, so the ``db`` channel cannot be claimed by renaming a file.
_SQLITE_MAGIC = b"SQLite format 3\x00"


def assert_sqlite_database(path: Path) -> Path:
    """Refuse a ``sqlite`` recipe path that is not a SQLite database file.

    The ``db`` channel is tier 2. What earns that tier is the mechanism: the
    worker opens a real database read-only and runs one SELECT through the
    engine. Checking the file header is what stops the recipe kind from being
    the whole claim.
    """

    try:
        with path.open("rb") as handle:
            header = handle.read(len(_SQLITE_MAGIC))
    except OSError as exc:
        raise OracleMechanismError(
            f"oracle recipe sqlite cannot open {path}: {exc}"
        ) from exc
    if header != _SQLITE_MAGIC:
        raise OracleMechanismError(
            f"{path} is not a SQLite database file, so this recipe cannot "
            "read through the db channel. A JSON document reads through the "
            "ocr channel at tier 0; use the screen_dump or json_file kind "
            "for one."
        )
    return path


def _adapter_for(recipe: OracleRecipeV1, base_dir: Path) -> OracleAdapter:
    """Construct the adapter a recipe names. Secrets come from the environment."""

    if recipe.kind in {"json_file", "screen_dump"}:
        # One adapter, one channel. Neither kind may claim the other's tier;
        # see the JsonDocumentOracle docstring.
        return JsonDocumentOracle(
            recipe.resolve_path(base_dir),
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

        database = assert_sqlite_database(recipe.resolve_path(base_dir))

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


def build_oracle(recipe: OracleRecipeV1, base_dir: Path) -> OracleAdapter:
    """Build the adapter and refuse it when its channel is not the recipe's.

    ``OracleRecipeV1.channel`` is a table keyed on the recipe kind, and the
    contract's declared channel is checked against that table when the bundle
    loads. Neither of those reads the adapter. This function closes the loop:
    the adapter the worker actually holds decides, and a table that drifts
    away from it stops the worker instead of shipping a tier the read cannot
    support.
    """

    adapter = _adapter_for(recipe, base_dir)
    if adapter.channel is not recipe.channel:
        raise OracleMechanismError(
            f"oracle recipe {recipe.kind} declares channel "
            f"{recipe.channel.value} but its adapter reads through "
            f"{adapter.channel.value}"
        )
    return adapter
