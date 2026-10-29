"""Verify an effect that something other than Flow replay claims it made.

``openadapt-flow verify-run`` reads the system of record through an existing
:class:`~openadapt_flow.runtime.effects.EffectVerifier` and rules on a declared
effect contract. The executor can be a computer-use agent, a script, a person,
or a Flow replay. Its run record is bound into the receipt by digest and is
never read as evidence: the verdict comes from the read-back alone.

Three inputs, kept apart on purpose:

* ``verify.yaml``: the verifier wiring (the same keys as the ``effects:``
  section of ``deployment.yaml``) and the effect contract (the same list as
  ``effect_contract.yaml``).
* ``--params``: the values the authorized task named (which patient, which
  note). They come from whoever authorized the work, not from the executor,
  so the executor cannot choose what it is checked against.
* ``--run-record``: what the executor says it did. Hashed, never trusted.

The output is an ``openadapt.effect-receipt/v1`` document as RFC-0002
(openadapt-internal ``docs/RFC-0002_OPEN_EFFECT_RECEIPT.md``) specifies:

* The receipt keeps every field of ``openadapt.execute-evidence-receipt/v1``
  and adds ``executor_kind``, ``executor_ref_sha256``, ``effect_verdict`` and
  ``verification_tier``. Two optional fields, ``remediates`` and
  ``correlation_id``, link a receipt to the receipt it remediates and to the
  caller's own trace; they are absent unless given, and when present they
  are inside ``receipt_sha256`` and so inside the signature. ``checked_how``
  (``unchecked``, ``same_screen``, ``reopened``, ``separate_session``,
  ``system_of_record``) names the check level of the weakest read; it is signed the same way,
  and a receipt without it (issued before it existed) still checks.
* Every digest whose input can hold customer data is salted with a per-run
  256-bit salt (``openadapt.run-salt/v1``). The salt lives only in a private
  file (mode 0600): the baseline, or a run file when there is no baseline.
* ``verification_tier`` is ``independent_readback`` only when every effect
  was read from the system of record without the UI (Flow tier 1). A
  separate signed-in UI session (tier 2) is ``screen_readback``. Only
  ``independent_readback`` can yield ``verified``.
* A signature is ``openadapt.effect-seal-signature/v1``, over its own domain,
  so a verifier that predates the RFC refuses an effect receipt instead of
  passing it as an Execute Seal. verify-run never signs; it checks.

Nothing here signs, uploads, or calls a model.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Optional, Sequence, cast
from uuid import uuid4

from openadapt_types.approval import (
    CHECKED_HOW_TIER,
    CHECKED_HOW_VALUES,
    checked_how_for_flow_tier,
)
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from openadapt_flow.deployment import EffectsConfig, build_effect_verifier
from openadapt_flow.fault_gate import (
    AdmissionPolicy,
    CoverageRecord,
    GateError,
    check_digest,
    load_coverage,
)
from openadapt_flow.qualify.result import verify_config_sha256
from openadapt_flow.runtime.effects.adapter import verifier_identity
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectKind,
    EffectState,
    EffectVerdict,
    Verdict,
)
from openadapt_flow.runtime.effects.frame import FrameSpec, FrameVerdict, judge_frame
from openadapt_flow.verification import (
    EffectContractMutationError,
    VerificationTier,
    oracle_tier_from_verification_tier,
    verifier_effect_tier,
    verify_effect_without_mutation,
)

EFFECT_RECEIPT_SCHEMA = "openadapt.effect-receipt/v1"
EFFECT_EVIDENCE_SCHEMA = "openadapt.effect-evidence/v1"
EFFECT_SEAL_SIGNATURE_SCHEMA = "openadapt.effect-seal-signature/v1"
EFFECT_SEAL_ISSUER = "openadapt-cloud:unattested-verifier"
VERIFY_CONFIG_SCHEMA = "openadapt.verify/v1"
BASELINE_SCHEMA = "openadapt.effect-baseline/v1"
RUN_FILE_SCHEMA = "openadapt.effect-run/v1"
SALT_SCHEME = "openadapt.run-salt/v1"

_EFFECT_SEAL_DOMAIN = (EFFECT_SEAL_SIGNATURE_SCHEMA + "\0").encode("utf-8")

#: The baseline file holds exactly these keys. The first five are bound by
#: ``baseline_sha256``; ``salt`` is the HMAC key and never part of a message.
BASELINE_KEYS: tuple[str, ...] = (
    "schema_version",
    "execution_id",
    "baseline_read_at",
    "verifier_identity",
    "state",
    "salt",
)
_BASELINE_BOUND_KEYS = BASELINE_KEYS[:5]

ExecutorKind = Literal["flow_replay", "agent", "script", "human", "unknown"]
EXECUTOR_KINDS: tuple[str, ...] = ("flow_replay", "agent", "script", "human", "unknown")
APPROVER_KINDS: tuple[str, ...] = ("person", "rule", "agent", "system")

EffectVerdictLabel = Literal["CONFIRMED", "REFUTED", "INDETERMINATE"]

#: How the effects were checked (RFC-0002 decision 6 and the tier table).
ReadbackClass = Literal["independent_readback", "screen_readback", "unchecked"]

#: Verifier kinds that never read independently of the acting surface.
_NOT_INDEPENDENT = {"none", "onscreen"}

_OPAQUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SALTED = re.compile(r"^hmac-sha256:[0-9a-f]{64}$")
_ISO_SECONDS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

_STRENGTH_BY_TIER = {
    VerificationTier.INDEPENDENT_SYSTEM: "independent_system_of_record",
    VerificationTier.INDEPENDENT_SESSION: "independent_session",
    VerificationTier.PERSISTED_STATE_REACQUISITION: "persisted_state_reacquisition",
    VerificationTier.IMMEDIATE_SCREEN: "immediate_screen_confirmation",
}
_STRENGTH_RANK = {
    "immediate_screen_confirmation": 1,
    "persisted_state_reacquisition": 2,
    "independent_session": 3,
    "independent_system_of_record": 4,
}

#: Where the baseline keeps the audit log read for frame attribution. Not a
#: surface name: those are plain identifiers.
FRAME_AUDIT_DETAIL = "openadapt:frame_audit"
#: Where the baseline keeps the whole-database snapshot (``frame.database``).
FRAME_DB_DETAIL = "openadapt:frame_database"

#: ``checked`` (``docs/VERIFICATION_COVERAGE.md``): which parts of the claim
#: behind ``verified`` were checked, and how each came out.
CHECKED_PARTS: tuple[str, ...] = ("delta", "frame", "source")
#: ``checked.coverage``, present only under an admission policy.
COVERAGE_KEYS = frozenset({"killed", "applicable", "sha256", "policy"})
#: Also in ``checked.coverage`` since the per-class rule: each claimed fault
#: class and how it came out. Receipts issued before it lack the key.
COVERAGE_CLASSES_KEY = "classes"
CHECKED_VALUES = ("held", "refuted", "not_checked")
SOURCE_VALUES = CHECKED_VALUES + ("instruction",)

#: Receipt fields beyond Execute v1. The last two are optional.
_NEW_RECEIPT_FIELDS = (
    "executor_kind",
    "executor_ref_sha256",
    "effect_verdict",
    "verification_tier",
)
_OPTIONAL_RECEIPT_FIELDS = ("remediates", "correlation_id")
#: ``checked_how`` is optional too, but a label, not an opaque id.
_SIGNATURE_FIELDS = frozenset(
    {
        "schema",
        "issuer",
        "algorithm",
        "key_id",
        "receipt_schema",
        "receipt_sha256",
        "run_id",
        "issued_at",
        "signature",
    }
)


class VerifyRunError(ValueError):
    """verify-run refused its inputs. Nothing was read or written."""


class VerifyConfig(BaseModel):
    """``verify.yaml``: what to read, and what must be true there."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["openadapt.verify/v1"] = "openadapt.verify/v1"
    #: Opaque ids copied into the receipt. Defaults say plainly that nothing
    #: qualified this contract.
    qualification_id: str = Field(
        default="unqualified-verify-run",
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$",
    )
    environment_id: str = Field(
        default="unnamed-environment",
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$",
    )
    minimum_effect_strength: Literal[
        "independent_system_of_record", "independent_session"
    ] = "independent_system_of_record"
    #: Longest time from the baseline read to the read-back. Outside it the
    #: baseline counts as absent, so change claims are INDETERMINATE.
    max_execution_window_s: int = Field(default=3600, ge=1, le=7 * 24 * 3600)
    #: Same shape as ``deployment.yaml`` ``effects:``.
    verifier: EffectsConfig
    #: Same shape as ``effect_contract.yaml`` ``effects:``.
    effects: list[Effect] = Field(min_length=1)
    #: What else in the read set must stay unchanged, and who may change it
    #: (``runtime/effects/frame.py``). Unset: the default frame (the whole
    #: read set, compared by ``id``, no attribution).
    frame: Optional[FrameSpec] = None
    #: The check's admission policy: its fault-gate coverage record and the
    #: minimum it must meet (``openadapt_flow/fault_gate.py``). Unset: no
    #: coverage is required or stated.
    admission: Optional[AdmissionPolicy] = None
    #: Where ``admission.coverage`` resolves (the file's directory).
    _base_dir: Optional[Path] = PrivateAttr(default=None)

    def coverage_path(self) -> Optional[Path]:
        if self.admission is None:
            return None
        path = Path(self.admission.coverage)
        if not path.is_absolute() and self._base_dir is not None:
            path = self._base_dir / path
        return path


def load_verify_config(path: str | Path) -> VerifyConfig:
    import yaml

    source = Path(path)
    if not source.is_file():
        raise VerifyRunError(f"verify config {source} is not a file")
    try:
        data = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise VerifyRunError(f"cannot parse {source}: {exc}") from exc
    cfg = parse_verify_config(data, source=f"verify config {source}")
    cfg._base_dir = source.resolve().parent
    return cfg


#: ``openadapt.verify/v1`` sections only qualify reads (RFC-0002, Qualify-only
#: sections). verify-run accepts and ignores them, so one file serves both.
QUALIFY_ONLY_SECTIONS: tuple[str, ...] = ("app", "save_step", "inputs", "campaign")


def _record_effects(record: Any) -> list[Effect]:
    """Expand qualify's ``record`` shorthand exactly as qualify does."""

    from types import SimpleNamespace

    from openadapt_flow.qualify.campaign import record_effects
    from openadapt_flow.qualify.config import RecordConfig

    try:
        parsed = RecordConfig.model_validate(record)
    except Exception as exc:
        raise VerifyRunError(f"invalid record section: {exc}") from exc
    if not parsed.fields or not parsed.match:
        raise VerifyRunError("record must name fields and match")
    return list(record_effects(cast(Any, SimpleNamespace(record=parsed))))


def parse_verify_config(data: Any, *, source: str = "verify config") -> VerifyConfig:
    """Validate a parsed ``verify.yaml`` the way verify-run reads it.

    The same file qualify reads: ``${NAME}`` environment references are
    expanded the same way, qualify-only sections and ``verifier.session`` are
    ignored, and ``record`` expands to the same effect list qualify uses, so
    ``workflow_digest`` equals qualify's ``verify_config_sha256``.
    """

    from openadapt_flow.qualify.config import _expand

    if not isinstance(data, dict):
        raise VerifyRunError(f"{source} must be a YAML mapping")
    missing: list[str] = []
    data = _expand(dict(data), missing)
    if missing:
        raise VerifyRunError(
            "environment variable(s) not set: " + ", ".join(sorted(set(missing)))
        )
    for name in QUALIFY_ONLY_SECTIONS:
        data.pop(name, None)
    verifier = data.get("verifier")
    if isinstance(verifier, dict) and "session" in verifier:
        # A qualify hook for an authenticated client; not part of the
        # verifier's identity, and verify-run does not import hooks.
        data["verifier"] = {k: v for k, v in verifier.items() if k != "session"}
    record = data.pop("record", None)
    if record is not None:
        if data.get("effects") is not None:
            raise VerifyRunError(
                "give the save's checks as either effects or record, not both"
            )
        # Only the fields record_effects set, so an effect's model_fields_set
        # says what the file chose (``_audit_effect`` reads it); the values,
        # and so the digests, are the same as qualify's.
        data["effects"] = [
            e.model_dump(mode="json", exclude_unset=True)
            for e in _record_effects(record)
        ]
    try:
        cfg = VerifyConfig.model_validate(data)
    except Exception as exc:
        raise VerifyRunError(f"invalid {source}: {exc}") from exc
    key = record.get("duplicate_key") if isinstance(record, dict) else None
    if isinstance(key, list):
        from openadapt_flow.qualify.config import composite_key_read_problems

        problems = composite_key_read_problems(key, cfg.verifier)
        if problems:
            raise VerifyRunError(f"invalid {source}: " + "; ".join(problems))
    kinds = [cfg.verifier.kind] + [s.kind for s in cfg.verifier.surfaces.values()]
    for kind in kinds:
        if kind in _NOT_INDEPENDENT:
            raise VerifyRunError(
                f"verifier kind {kind!r} does not read independently of "
                "the executor; verify-run needs a system-of-record or "
                "separate-session verifier"
            )
    if any(
        (kind or "").strip().lower() in ("deferred_report", "deferred-report")
        for kind in kinds
    ):
        raise VerifyRunError(
            "a deferred report check is settled later by 'openadapt-flow deferred "
            "settle', not by verify-run"
        )
    if cfg.verifier.candidates:
        raise VerifyRunError("verify-run does not support verifier candidates yet")
    attribution = cfg.frame.attribution if cfg.frame is not None else None
    if attribution is not None and attribution.read is not None:
        try:
            audit = EffectsConfig.model_validate(attribution.read)
        except Exception as exc:
            raise VerifyRunError(f"invalid frame.attribution.read: {exc}") from exc
        if audit.kind in _NOT_INDEPENDENT or audit.surfaces or audit.candidates:
            raise VerifyRunError(
                "frame.attribution.read must be one read-only system-of-record "
                "read (rest, sql, fhir, graphql, ...)"
            )
    database = cfg.frame.database if cfg.frame is not None else None
    if database is not None:
        surfaces = set(cfg.verifier.surfaces) or {""}
        unknown = sorted(set(database.views.values()) - surfaces)
        if unknown:
            raise VerifyRunError(
                "frame.database.views names no such surface: " + ", ".join(unknown)
            )
    for effect in cfg.effects:
        if effect.needs_operator_confirmation:
            raise VerifyRunError(
                "the effect contract still holds an unconfirmed placeholder"
            )
        if effect.readback is not None:
            raise VerifyRunError(
                "an on-screen read-back effect cannot be verified after the fact"
            )
    return cfg


def _audit_effect(effect: Effect, bound: Effect) -> Effect:
    """The effect as verify-run judges it: an audit after the fact.

    A ``record_written`` check also refutes when a record outside the target
    changed (``Effect.forbid_collateral_change``), not only when one vanished,
    unless the file sets ``forbid_collateral_change: false`` on that effect.
    The contract itself is left as the file states it, so its digest stays
    the one qualify reports; the receipt's evidence says the check ran.
    """

    if (
        bound.kind is not EffectKind.RECORD_WRITTEN
        or not bound.forbid_collateral_loss
        or "forbid_collateral_change" in effect.model_fields_set
    ):
        return bound
    return bound.model_copy(update={"forbid_collateral_change": True})


def _frame_effect(effect: Effect, bound: Effect) -> Effect:
    """The effect as verify-run judges it when the frame is judged.

    The frame judge covers every record outside the declared delta, once for
    the whole contract, so the per-effect collateral rules would only repeat
    it -- and, judged one effect at a time, flag another effect's target. They
    stay on only where the file sets them explicitly.
    """

    if "forbid_collateral_loss" in effect.model_fields_set:
        return bound
    return bound.model_copy(update={"forbid_collateral_loss": False})


def _post_records(state: Any) -> Optional[list[dict[str, Any]]]:
    if not isinstance(state, EffectState) or not state.reachable:
        return None
    return list(state.records)


def _audit_verifier(cfg: VerifyConfig, params: Mapping[str, object]) -> Any:
    attribution = cfg.frame.attribution if cfg.frame is not None else None
    if attribution is None or attribution.read is None:
        return None
    try:
        return build_effect_verifier(
            EffectsConfig.model_validate(attribution.read), params
        )
    except ValueError as exc:
        raise VerifyRunError(f"cannot build the audit read: {exc}") from exc


def _db_connect(read: Mapping[str, Any]) -> Any:
    """A connect function for ``frame.database.read`` (read-only)."""

    if read.get("sqlite_database"):
        import sqlite3

        uri = Path(str(read["sqlite_database"])).resolve().as_uri() + "?mode=ro"
        return lambda: sqlite3.connect(uri, uri=True)
    import importlib

    from openadapt_flow.deployment import _require_env

    module = importlib.import_module(str(read["sql_driver"]))
    args = dict(read.get("sql_connect_args") or {})
    if read.get("sql_password_env"):
        args["password"] = _require_env(
            str(read["sql_password_env"]), "frame.database.read.sql_password_env"
        )
    return lambda: module.connect(**args)


def _same_connection(
    audit: Optional[Mapping[str, Any]], read: Mapping[str, Any]
) -> bool:
    """Does the audit read use the database read's own connection?"""

    if not audit or audit.get("kind") != "sql":
        return False
    keys = ("sqlite_database", "sql_driver", "sql_connect_args")
    return all(audit.get(k) == read.get(k) for k in keys)


def _audit_sql(cfg: VerifyConfig) -> dict[str, str]:
    """Reads that run inside the database snapshot: the audit query (when it
    shares the connection), and the acting user's ids (for a profile's
    own-row rules)."""

    frame = cfg.frame
    if frame is None or frame.database is None:
        return {}
    out: dict[str, str] = {}
    profile = frame.database.loaded_profile()
    if profile is not None:
        query = profile.actor_ids_query(_self_names(cfg), frame.database.dialect)
        if query:
            out["actor_ids"] = query
        users = profile.users_query(frame.database.dialect)
        if users:
            out["users_map"] = users
    if frame.attribution is None:
        return out
    audit = frame.attribution.read
    if (
        _same_connection(audit, frame.database.read)
        and audit
        and audit.get("sql_query")
    ):
        out["audit"] = str(audit["sql_query"])
    return out


def _self_names(cfg: VerifyConfig) -> list[str]:
    """The bot's own sign-in names the frame lists (literal ``self``)."""

    a = cfg.frame.attribution if cfg.frame is not None else None
    if a is None:
        return []
    return sorted({v.literal for v in a.self_actors if v.literal})


def _ref_columns(cfg: VerifyConfig) -> list[str]:
    """Columns attribution and side records read: kept as cell MACs in
    every table, however large."""

    frame = cfg.frame
    if frame is None:
        return []
    cols = {frame.identity_field} if frame.identity_field else set()
    a = frame.attribution
    if a is not None:
        cols |= {a.record_key} if a.record_key else set()
        cols |= set(a.record_keys.values())
    for side in frame.side_records:
        cols |= {side.link_field, side.of_field}
    profile = frame.database.loaded_profile() if frame.database else None
    if profile is not None:
        cols |= set(profile.ref_columns())
    return sorted(c for c in cols if c)


def _read_database(
    cfg: VerifyConfig, salt: bytes, before: Optional[Any] = None
) -> Optional[Any]:
    """The whole-database snapshot, or None when it can't be read."""

    from openadapt_flow.runtime.effects.db_snapshot import mac_key, take_snapshot

    database = cfg.frame.database if cfg.frame is not None else None
    if database is None:
        return None
    try:
        return take_snapshot(
            _db_connect(database.read),
            database.dialect,
            key=mac_key(salt),
            modes=database.modes(),  # type: ignore[arg-type]
            max_rows=database.max_rows,
            before=before,
            extra_queries=_audit_sql(cfg),
            ignore=database.all_ignore_columns(),
            ref_columns=_ref_columns(cfg),
            schemas=database.schemas,
            optional_modes=database.profile_modes(),  # type: ignore[arg-type]
        )
    except Exception:  # noqa: BLE001 - unreadable -> the frame is not checked
        return None


def _mac_key(salt: bytes) -> bytes:
    from openadapt_flow.runtime.effects.db_snapshot import mac_key

    return mac_key(salt)


def _audit_ids_each(entries: Sequence[Mapping[str, Any]], salt: bytes) -> list[str]:
    from openadapt_flow.runtime.effects.effect import stable_id

    return [
        hmac.new(
            _mac_key(salt), repr(stable_id(dict(e))).encode(), hashlib.sha256
        ).hexdigest()
        for e in entries
    ]


def _audit_ids(entries: Sequence[Mapping[str, Any]], salt: bytes) -> list[str]:
    """Keyed hashes of audit entries: the baseline keeps which entries
    existed, never what they say."""

    from openadapt_flow.runtime.effects.effect import stable_id

    return sorted(
        hmac.new(
            _mac_key(salt), repr(stable_id(dict(e))).encode(), hashlib.sha256
        ).hexdigest()
        for e in entries
    )


@dataclass
class _DatabaseFrame:
    slices: dict[str, tuple[EffectState, Optional[list[dict[str, Any]]]]]
    defaults: dict[str, dict[str, Optional[str]]]
    unread: list[tuple[str, str]]
    hashers: dict[str, Any]
    clock: tuple[str, str]
    evidence: dict[str, Any]
    audit_after: Optional[list[dict[str, Any]]]
    #: The read account can't see triggers: what the save caused can't be
    #: ruled out, so the frame is not checked.
    blind: bool = False
    #: The two reads after the save differed: the database was still moving.
    still_moving: bool = False
    #: The app profile's judgment of one row change (``AppProfile.excuse``).
    excuse: Any = None
    #: Every user's id by sign-in name (in memory only), and the names and
    #: ids of other users who wrote in the window.
    users_map: dict[str, str] = field(default_factory=dict)
    colleagues: tuple[set[str], set[str]] = (set(), set())


def _rules_used(profile: Any, old: Any, new: Any) -> list[str]:
    """The profile's unconditional rules that applied in this run: an
    append-only table that gained rows, or a row of a compared table whose
    only changes were in a column rule's columns. (Conditional rules are
    reported by the judge when they excuse a row.)"""

    from openadapt_flow.app_profiles import table_name
    from openadapt_flow.runtime.effects.db_snapshot import unpack

    used = []
    for rule in profile.rules:
        if rule.conditional:
            continue
        tables = [n for n in new.tables if table_name(n) == table_name(rule.table)]
        for name in tables:
            a, b = old.tables.get(name), new.tables.get(name)
            if a is None or b is None:
                continue
            if rule.mode == "append_only" and len(b.rows.keys() - a.rows.keys()):
                used.append(rule.id)
                break
            cols = [c for c in rule.columns if c in a.cell_columns]
            if (
                cols
                and a.cell_columns == b.cell_columns
                and any(
                    unpack(a.cell_columns, a.rows[k][1])[c]
                    != unpack(b.cell_columns, b.rows[k][1])[c]
                    for k in a.rows.keys() & b.rows.keys()
                    for c in cols
                )
            ):
                used.append(rule.id)
                break
    return used


def _database_frame(
    cfg: VerifyConfig, before: EffectState, salt: bytes
) -> Optional[_DatabaseFrame]:
    """The rows that moved, as slices, from the baseline and one read now.

    Only rows whose contents MAC differs (changed), and rows on one side only
    (added or removed), reach the judge: a baseline row as its MACs, a row now
    as read. None when there is no ``frame.database``. A baseline without a
    readable snapshot, or an unreadable read now, gives one unreachable
    slice, so the frame is not checked.
    """

    from openadapt_flow.runtime.effects.db_snapshot import (
        KEY,
        ROW,
        Snapshot,
        SnapshotError,
        cell_hash,
        mac_key,
        same_state,
        unpack,
        unread_reasons,
    )

    database = cfg.frame.database if cfg.frame is not None else None
    if database is None:
        return None
    unreachable = _DatabaseFrame(
        {"database": (EffectState(substrate="database", reachable=False), None)},
        {},
        [],
        {},
        ("", ""),
        {},
        None,
    )
    stored = (
        before.detail.get(FRAME_DB_DETAIL) if isinstance(before.detail, dict) else None
    )
    if not isinstance(stored, dict):
        return unreachable
    old = Snapshot.from_json(stored)
    new = _read_database(cfg, salt, before=old)
    if new is None:
        return unreachable
    # Read again a moment later: the two reads must match, or the app is
    # still writing and what the save caused can't be told yet.
    time.sleep(database.settle_s)
    again = _read_database(cfg, salt, before=old)
    if again is None:
        return unreachable
    if not same_state(new, again):
        moving = _DatabaseFrame({}, {}, [], {}, ("", ""), {}, None)
        moving.still_moving = True
        return moving
    if not (old.code_visible and new.code_visible):
        blind = _DatabaseFrame({}, {}, [], {}, ("", ""), {}, None)
        blind.blind = True
        return blind
    key = mac_key(salt)
    out = _DatabaseFrame({}, {}, [], {}, (old.clock, new.clock), {}, None)
    if "audit" in new.extra:
        out.audit_after = list(new.extra["audit"])
    if old.objects != new.objects:
        out.unread.append(
            ("(database code)", "triggers, views, routines or events changed")
        )
    counts = {"compare": 0, "append_only": 0, "skip": 0}
    for name in sorted(set(old.tables) | set(new.tables)):
        a, b = old.tables.get(name), new.tables.get(name)
        try:
            reasons = unread_reasons(a, b, new.dialect)
        except SnapshotError as exc:
            reasons = [str(exc)]
        out.unread.extend((name, why) for why in reasons)
        if b is not None:
            counts[b.mode] = counts.get(b.mode, 0) + 1
        if (
            a is None
            or b is None
            or a.mode != "compare"
            or b.mode != "compare"
            or a.cell_columns != b.cell_columns
            or (reasons and reasons[0] in ("definition changed", "created or dropped"))
        ):
            continue
        gone = [k for k in a.rows if k not in b.rows or b.rows[k][0] != a.rows[k][0]]
        now = [k for k in b.rows if k not in a.rows or a.rows[k][0] != b.rows[k][0]]
        before_rows = [
            {KEY: k, ROW: a.rows[k][0], **unpack(a.cell_columns, a.rows[k][1])}
            for k in gone
        ]
        after_rows = [{KEY: k, ROW: b.rows[k][0], **b.values.get(k, {})} for k in now]
        out.slices[name] = (
            EffectState(substrate="database", reachable=True, records=before_rows),
            after_rows,
        )
        out.defaults[name] = dict(b.defaults)
        out.hashers[name] = (
            lambda table: lambda column, value: cell_hash(key, value, table, column)
        )(name)
    out.evidence = {
        "tables_compared": counts.get("compare", 0),
        "tables_append_only": counts.get("append_only", 0),
        "tables_skipped": counts.get("skip", 0),
        "tables_without_snapshot_isolation": sum(
            1 for t in new.tables.values() if t.non_transactional
        ),
        "schemas": list(new.schemas),
        "allow_list_sha256": database.allow_list_sha256(),
    }
    profile = database.loaded_profile()
    if profile is not None:
        out.evidence["profile"] = {
            "version": profile.version,
            "sha256": profile.sha256(),
            "rules_used": _rules_used(profile, old, new),
        }
        names = set(_self_names(cfg))
        ids = {str(r.get("id")) for r in new.extra.get("actor_ids", [])}
        hashers = out.hashers
        out.users_map = {
            str(r.get("name")): str(r.get("id")) for r in new.extra.get("users_map", [])
        }
        frame_out = out

        def excuse(name: str, before: Any, after: Any, fields: list[str]) -> Any:
            return profile.excuse(
                name,
                before,
                after,
                fields,
                hashers[name],
                names,
                ids,
                frame_out.colleagues,
            )

        out.excuse = excuse
    return out


def _read_audit(verifier: Any) -> Optional[list[dict[str, Any]]]:
    if verifier is None:
        return None
    try:
        return _post_records(verifier.capture_pre_state())
    except Exception:  # noqa: BLE001 - an unreadable log attributes nothing
        return None


def judge_run_frame(
    cfg: VerifyConfig,
    verifier: Any,
    before: EffectState,
    bound_effects: Sequence[Effect],
    params: Mapping[str, object],
    *,
    audit_verifier: Any = None,
    run_actors: Sequence[str] = (),
    salt: bytes = b"",
) -> FrameVerdict:
    """Read the slice after the save and judge the frame against ``before``."""

    from openadapt_flow.runtime.effects.surfaces import surface_of

    spec = cfg.frame or FrameSpec()
    try:
        capture = getattr(verifier, "capture_post_state", None) or (
            verifier.capture_pre_state
        )
        after = capture()
    except Exception:  # noqa: BLE001 - unreadable -> not checked
        after = None
    routed = getattr(verifier, "verifiers", None)
    slices: dict[str, tuple[EffectState, Optional[list[dict[str, Any]]]]] = {}
    routes: Optional[dict[str, list[str]]] = None
    if isinstance(routed, dict) and routed:
        # Surfaces that differ only in dropped fields (a content twin) are
        # views of one read: a record an effect declares in one is declared
        # in each.
        def read_of(name: str) -> str:
            sub = cfg.verifier.surfaces.get(name)
            if sub is None:
                return name
            shape = sub.model_dump(mode="json", exclude={"records_drop_fields"})
            return json.dumps(shape, sort_keys=True, default=str)

        views: dict[str, list[str]] = {}
        for name in routed:
            views.setdefault(read_of(name), []).append(name)
        routes = {}
        for i, effect in enumerate(bound_effects):
            own = surface_of(effect) or ""
            routes[str(i)] = views.get(read_of(own), [own]) if own in routed else [own]
        for name in routed:
            sub = before.detail.get(name, {}) if isinstance(before.detail, dict) else {}
            post = (
                after.detail.get(name, {})
                if isinstance(after, EffectState) and isinstance(after.detail, dict)
                else {}
            )
            slices[name] = (
                EffectState(
                    substrate=str(sub.get("substrate", "")),
                    reachable=bool(before.reachable and sub.get("reachable", False)),
                    records=list(sub.get("records", [])),
                ),
                list(post.get("records", [])) if post.get("reachable") else None,
            )
    else:
        slices[""] = (before, _post_records(after))
    identities: Optional[dict[str, str]] = None
    db = _database_frame(cfg, before, salt)
    if db is not None and db.still_moving:
        return FrameVerdict(
            status="not_checked",
            reason=(
                "the database was still changing after the save (two reads "
                "differed); what the save caused can't be told yet"
            ),
            attribution=spec.attribution.kind if spec.attribution else "none",
        )
    if db is not None and db.blind:
        return FrameVerdict(
            status="not_checked",
            reason=(
                "the database read account can't see triggers (on MySQL it "
                "needs TRIGGER), so a trigger planted in the window could pass"
            ),
            attribution=spec.attribution.kind if spec.attribution else "none",
        )
    if db is not None:
        from openadapt_flow.runtime.effects.db_snapshot import KEY

        clash = sorted(set(db.slices) & set(slices))
        if clash:
            return FrameVerdict(
                status="not_checked",
                reason="a table has the same name as a surface: " + ", ".join(clash),
            )
        if routes is None:
            routes = {str(i): [""] for i in range(len(bound_effects))}
        db_views = spec.database.views if spec.database is not None else {}
        routes = {k: list(v) for k, v in routes.items()}
        for route in routes.values():
            route.extend(t for t, surface in db_views.items() if surface in route)
        slices.update(db.slices)
        identities = {name: KEY for name in db.slices}
    audit_before: Optional[list[dict[str, Any]]] = None
    audit_after: Optional[list[dict[str, Any]]] = None
    stored = (
        before.detail.get(FRAME_AUDIT_DETAIL)
        if isinstance(before.detail, dict)
        else None
    )
    if isinstance(stored, dict) and stored.get("reachable"):
        entries_now = (
            db.audit_after
            if db is not None and db.audit_after is not None
            else _read_audit(audit_verifier)
        )
        if entries_now is not None:
            if "ids" in stored:
                # The window: entries now that the baseline didn't hold.
                seen = set(stored["ids"])
                audit_before = []
                audit_after = [
                    e
                    for e, h in zip(entries_now, _audit_ids_each(entries_now, salt))
                    if h not in seen
                ]
            else:
                audit_before = list(stored.get("records", []))
                audit_after = entries_now
    if db is not None and audit_after is not None and spec.attribution is not None:
        # Other users who wrote in the window: their own per-user state is
        # their work (the profile's own_rows rules only).
        a = spec.attribution
        writes = set(a.write_events)
        mine = set(_self_names(cfg)) | {str(x) for x in run_actors}
        active = {
            str(e.get(a.actor_field))
            for e in audit_after
            if (not writes or str(e.get(a.event_field or "")) in writes)
            and e.get(a.actor_field) not in (None, "")
        } - mine
        db.colleagues = (active, {db.users_map[n] for n in active if n in db.users_map})
    verdict = judge_frame(
        spec,
        bound_effects,
        slices,
        params=params,
        routes=routes,
        audit_before=audit_before,
        audit_after=audit_after,
        run_actors=run_actors,
        identities=identities,
        new_row_defaults=None if db is None else db.defaults,
        unread_changes=() if db is None else db.unread,
        hashers=None if db is None else db.hashers,
        clock=("", "") if db is None else db.clock,
        database_evidence=None if db is None else db.evidence,
        excuse=None if db is None else db.excuse,
    )
    profile = db.evidence.get("profile") if db is not None else None
    if profile is not None:
        # The rules that actually excused something: unconditional ones seen
        # in the snapshots, conditional ones as the judge applied them.
        profile["rules_used"] = sorted(set(profile["rules_used"]) | verdict.excused_by)
    return verdict


def _target_record(
    effects: Sequence[Effect], verdicts: Sequence[EffectVerdict]
) -> Optional[dict[str, Any]]:
    """The one record every confirmed write/read-back effect matched, if any."""

    from openadapt_flow.runtime.effects.effect import stable_id

    found: Optional[dict[str, Any]] = None
    for effect, verdict in zip(effects, verdicts):
        if effect.kind not in (EffectKind.RECORD_WRITTEN, EffectKind.FIELD_EQUALS):
            continue
        if effect.kind is EffectKind.RECORD_WRITTEN and effect.expected_count == 0:
            continue  # a guard: "nothing here"
        if not verdict.confirmed or len(verdict.matched_records) != 1:
            return None
        record = verdict.matched_records[0]
        if found is not None and stable_id(found) != stable_id(record):
            return None
        found = record
    return found


def _judge_source(
    provenance: Optional[Any],
    documents: Mapping[str, Any],
    second: Optional[Any],
    params: Mapping[str, object],
    effects: Sequence[Effect],
    verdicts: Sequence[EffectVerdict],
    delta_held: bool,
) -> tuple[str, dict[str, Any]]:
    """``checked.source`` and its evidence (counts and names, no values)."""

    from openadapt_flow.provenance import (
        bound_fields,
        bound_maps,
        default_extractor,
        round_trip,
    )

    if provenance is None or not provenance.has_documents():
        return "instruction", {"status": "instruction"}
    differ = provenance.matches(params)
    if differ:
        # The values checked are not the values the source provenance records.
        return "refuted", {
            "status": "refuted",
            "params_differ_from_provenance": differ,
        }
    extractor = second or default_extractor(provenance)
    record = _target_record(effects, verdicts) if delta_held else None
    result = round_trip(
        provenance,
        record,
        bound_fields(effects),
        documents,
        extractor,
        maps=bound_maps(effects),
    )
    evidence = result.summary()
    evidence["first_extractors"] = sorted(
        {p.extractor for p in provenance.params.values()}
    )
    evidence["documents"] = len(provenance.documents())
    return result.status, evidence


def _part(verdict: EffectVerdictLabel, proved: bool) -> str:
    if proved:
        return "held"
    return "refuted" if verdict == "REFUTED" else "not_checked"


# -- hashing ------------------------------------------------------------------


def _canonical(value: Any) -> bytes:
    """Sorted-key compact JSON; the Seal's canonical form."""

    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_tagged(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _salted_hex(salt: bytes, label: str, value: Any) -> str:
    message = label.encode("utf-8") + b"\0" + _canonical(value)
    return hmac.new(salt, message, hashlib.sha256).hexdigest()


def salted_digest(salt: bytes, label: str, value: Any) -> str:
    """``openadapt.run-salt/v1``: HMAC-SHA256 keyed with the run salt."""

    return "hmac-sha256:" + _salted_hex(salt, label, value)


def salt_commitment(salt: bytes) -> str:
    return "sha256:" + hashlib.sha256(salt).hexdigest()


def receipt_sha256(receipt: Mapping[str, Any]) -> str:
    """64 hex over the receipt without ``seal_signature``."""

    body = {k: v for k, v in receipt.items() if k != "seal_signature"}
    return hashlib.sha256(_canonical(body)).hexdigest()


def effect_signing_payload(signature: Mapping[str, Any]) -> bytes:
    """Bytes an ``openadapt.effect-seal-signature/v1`` signature covers."""

    unsigned = {k: v for k, v in signature.items() if k != "signature"}
    return _EFFECT_SEAL_DOMAIN + _canonical(unsigned)


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _parse_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _write_private(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _new_execution_id() -> str:
    return f"verify-{uuid4().hex}"


# -- verifier -----------------------------------------------------------------


def _build_verifier(cfg: VerifyConfig, params: Mapping[str, object]) -> Any:
    try:
        verifier = build_effect_verifier(cfg.verifier, params)
    except ValueError as exc:
        raise VerifyRunError(f"cannot build the verifier: {exc}") from exc
    if verifier is None:
        raise VerifyRunError("verify config built no verifier")
    return verifier


def _tag_reads(verifier: Any, execution_id: str) -> None:
    """Add the run's own headers to every REST read.

    Called after ``verifier_identity`` is taken, and on the built verifier,
    never on the config, so the identity and ``workflow_digest`` stay equal
    across runs of one ``verify.yaml``.
    """

    routed = getattr(verifier, "verifiers", None)
    if isinstance(routed, dict):
        for sub in routed.values():
            _tag_reads(sub, execution_id)
        return
    if not hasattr(verifier, "headers"):
        return
    trace = secrets.token_hex(16)
    span = secrets.token_hex(8)
    verifier.headers = {
        **(verifier.headers or {}),
        "X-OpenAdapt-Execution-Id": execution_id,
        "traceparent": f"00-{trace}-{span}-01",
    }


# -- baseline -----------------------------------------------------------------


def capture_baseline(
    cfg: VerifyConfig, params: Mapping[str, object], out: Path
) -> dict[str, Any]:
    """Read the system of record before the executor starts.

    The baseline is what lets ``count_new_only`` and ``exact_new_set`` tell a
    record the executor added from one that was already there. It holds raw
    records and the run salt, so it stays private on this machine: mode 0600,
    never inside a receipt. Only its salted digest is bound. It also starts
    the run: ``execution_id`` is generated here and the read step reuses it.
    """

    verifier = _build_verifier(cfg, params)
    identity = verifier_identity(verifier)
    execution_id = _new_execution_id()
    _tag_reads(verifier, execution_id)
    salt = secrets.token_bytes(32)
    state = verifier.capture_pre_state()
    if not isinstance(state, EffectState):
        raise VerifyRunError("verifier returned no readable baseline")
    detail: dict[str, Any] = {}
    entries: Optional[list[dict[str, Any]]] = None
    snapshot = None
    if cfg.frame is not None and cfg.frame.database is not None:
        snapshot = _read_database(cfg, salt)
        if snapshot is not None:
            detail[FRAME_DB_DETAIL] = snapshot.to_json()
            entries = snapshot.extra.get("audit")
    audit_verifier = _audit_verifier(cfg, params)
    if audit_verifier is not None:
        if entries is None:
            entries = _read_audit(audit_verifier)
        # Which entries existed, as keyed hashes: never what they say.
        detail[FRAME_AUDIT_DETAIL] = {
            "reachable": entries is not None,
            "ids": _audit_ids(entries or [], salt),
        }
    if detail:
        state = state.model_copy(update={"detail": {**state.detail, **detail}})
    payload = {
        "schema_version": BASELINE_SCHEMA,
        "execution_id": execution_id,
        "baseline_read_at": _now(),
        "verifier_identity": identity,
        "state": state.model_dump(mode="json"),
        "salt": salt.hex(),
    }
    _write_private(Path(out), payload)
    return payload


def baseline_digest(baseline: Mapping[str, Any], salt: bytes) -> str:
    """Salted digest (label ``baseline``) of the five bound baseline fields."""

    return salted_digest(
        salt, "baseline", {key: baseline[key] for key in _BASELINE_BOUND_KEYS}
    )


def _used_marker(path: Path) -> Path:
    return path.with_name(path.name + ".used")


def _load_baseline(path: Path, identity: str) -> tuple[dict[str, Any], bytes]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise VerifyRunError(f"cannot read baseline {path}: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != BASELINE_SCHEMA:
        raise VerifyRunError(f"{path} is not an {BASELINE_SCHEMA} file")
    if set(raw) != set(BASELINE_KEYS):
        # Every field must be one the digest names; nothing rides along unbound.
        raise VerifyRunError(
            "baseline file must hold exactly " + ", ".join(BASELINE_KEYS)
        )
    if raw["verifier_identity"] != identity:
        raise VerifyRunError("baseline was captured by a different verifier")
    execution_id = raw["execution_id"]
    if not isinstance(execution_id, str) or not _OPAQUE.match(execution_id):
        raise VerifyRunError("baseline execution_id is not an opaque id")
    read_at = raw["baseline_read_at"]
    if not isinstance(read_at, str) or not _ISO_SECONDS.match(read_at):
        raise VerifyRunError("baseline_read_at is not whole-second UTC")
    try:
        salt = bytes.fromhex(str(raw["salt"]))
    except ValueError as exc:
        raise VerifyRunError("baseline salt is not hex") from exc
    if len(salt) != 32:
        raise VerifyRunError("baseline salt must be 32 bytes")
    return raw, salt


def _consume_baseline(path: Path, execution_id: str) -> None:
    """One use: record the consumed run beside the baseline, refuse a reuse."""

    marker = _used_marker(Path(path))
    try:
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise VerifyRunError(
            f"baseline {path} was already used for {execution_id}; capture a new one"
        ) from exc
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(execution_id + "\n")


# -- the run record and the approver -----------------------------------------


def _executor(run_record: Mapping[str, Any]) -> tuple[str, str, Optional[str]]:
    kind = run_record.get("executor_kind")
    if kind not in EXECUTOR_KINDS:
        raise VerifyRunError(
            "run record must name executor_kind as one of " + ", ".join(EXECUTOR_KINDS)
        )
    executor_id = run_record.get("executor_id")
    ref = (
        executor_id if isinstance(executor_id, str) and executor_id else "unidentified"
    )
    build = run_record.get("executor_build")
    build_digest: Optional[str] = None
    if build is not None:
        if not isinstance(build, Mapping) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in build.items()
        ):
            raise VerifyRunError("executor_build must be an object of strings")
        build_digest = sha256_tagged(dict(build))
    return (
        str(kind),
        "sha256:" + hashlib.sha256(ref.encode("utf-8")).hexdigest(),
        build_digest,
    )


def _approver(approver: Optional[Mapping[str, Any]]) -> Optional[dict[str, str]]:
    """Placeholder shape until the openadapt-types approver model lands."""

    if approver is None:
        return None
    if not isinstance(approver, Mapping) or set(approver) != {
        "kind",
        "id",
        "policy_ref",
    }:
        raise VerifyRunError("approver must be {kind, id, policy_ref}")
    if approver["kind"] not in APPROVER_KINDS:
        raise VerifyRunError(
            "approver kind must be one of " + ", ".join(APPROVER_KINDS)
        )
    if not isinstance(approver["id"], str) or not _OPAQUE.match(approver["id"]):
        raise VerifyRunError("approver id must be an opaque id, never a name or email")
    if not isinstance(approver["policy_ref"], str) or not _DIGEST.match(
        approver["policy_ref"]
    ):
        raise VerifyRunError("approver policy_ref must be sha256:<hex>")
    return {k: str(approver[k]) for k in ("kind", "id", "policy_ref")}


def _opaque_or_none(name: str, value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not _OPAQUE.match(value):
        raise VerifyRunError(f"{name} must be an opaque id (8-128 of A-Za-z0-9._:-)")
    return value


# -- verdicts -----------------------------------------------------------------


def _label(verdict: Verdict) -> EffectVerdictLabel:
    labels: dict[Verdict, EffectVerdictLabel] = {
        Verdict.CONFIRMED: "CONFIRMED",
        Verdict.REFUTED: "REFUTED",
        Verdict.INDETERMINATE: "INDETERMINATE",
    }
    return labels[verdict]


def _aggregate(verdicts: Sequence[EffectVerdict]) -> EffectVerdictLabel:
    if any(v.verdict is Verdict.INDETERMINATE for v in verdicts):
        return "INDETERMINATE"
    if any(v.verdict is Verdict.REFUTED for v in verdicts):
        return "REFUTED"
    return "CONFIRMED"


def _observed(verdicts: Sequence[EffectVerdict]) -> str:
    seen = {v.observed_effect for v in verdicts}
    if "unknown" in seen:
        return "unknown"
    if "conflicting" in seen or seen == {"absent", "present"}:
        # A wrong record, or some effects landed and others did not.
        return "conflicting"
    return "absent" if "absent" in seen else "present"


def readback_class(
    verdicts: Sequence[EffectVerdict], tiers: Sequence[Optional[VerificationTier]]
) -> ReadbackClass:
    """RFC-0002 tier rule, weakest effect wins.

    ``unchecked`` when any effect was not read (no tier, store unreachable,
    stale). ``independent_readback`` only when every effect was read from the
    system of record without the UI (Flow tier 1). Anything else, including a
    separate signed-in UI session (tier 2), is ``screen_readback``.
    """

    if (
        any(t is None for t in tiers)
        or any(v.unavailable for v in verdicts)
        or any(v.stale for v in verdicts)
    ):
        return "unchecked"
    if all(t is not None and t.reads_system_of_record() for t in tiers):
        return "independent_readback"
    return "screen_readback"


def verify_run(
    cfg: VerifyConfig,
    *,
    params: Mapping[str, object],
    run_record: Mapping[str, Any],
    authorization_ref: str,
    baseline_path: Optional[Path] = None,
    private_dir: Optional[Path] = None,
    approver: Optional[Mapping[str, Any]] = None,
    remediates: Optional[str] = None,
    correlation_id: Optional[str] = None,
    write_attempted: bool = False,
    coverage: Optional[CoverageRecord] = None,
    provenance: Optional[Any] = None,
    documents: Optional[Mapping[str, Any]] = None,
    second_extractor: Optional[Any] = None,
) -> dict[str, Any]:
    """Read the system of record and return ``{"receipt", "evidence"}``.

    The run record contributes ``executor_kind``, a digest of
    ``executor_id``, a digest of ``executor_build``, and a salted digest of
    itself. No other field of it is read. ``verified`` requires every effect
    CONFIRMED by a system-of-record read at or above
    ``minimum_effect_strength``; anything weaker is never upgraded.

    With ``baseline_path`` the run continues the one the baseline started
    (its ``execution_id`` and salt) and the baseline is consumed. Without one,
    a new salt is drawn and written to ``private_dir/effect-run.private.json``
    (mode 0600), so ``private_dir`` is required.

    ``write_attempted`` says the caller knows the save was dispatched (the
    guarded save sets it after its commit step). Then a store that shows
    nothing is ``reconciliation_required``, not ``halted_before_effect``: the
    write may still land, so it must not be retried.
    """

    from openadapt_types.execute import (
        EffectStrengthV1,
        ExecuteEvidenceContractV1,
        ExecuteEvidenceReceiptV1,
        ExecuteTerminalOutcomeV1,
    )

    if not isinstance(run_record, Mapping):
        raise VerifyRunError("run record must be a JSON object")
    if not isinstance(authorization_ref, str) or not authorization_ref:
        raise VerifyRunError("an authorization reference is required")
    remediates = _opaque_or_none("remediates", remediates)
    correlation_id = _opaque_or_none("correlation_id", correlation_id)
    approver_doc = _approver(approver)
    executor_kind, executor_ref, build_digest = _executor(run_record)

    verifier = _build_verifier(cfg, params)
    identity = verifier_identity(verifier)

    baseline: Optional[dict[str, Any]] = None
    if baseline_path is not None:
        baseline, salt = _load_baseline(Path(baseline_path), identity)
        execution_id = str(baseline["execution_id"])
        _consume_baseline(Path(baseline_path), execution_id)
    else:
        if private_dir is None:
            raise VerifyRunError(
                "without a baseline, verify-run needs a private directory for "
                "the run salt"
            )
        salt = secrets.token_bytes(32)
        execution_id = _new_execution_id()
        _write_private(
            Path(private_dir) / "effect-run.private.json",
            {
                "schema_version": RUN_FILE_SCHEMA,
                "execution_id": execution_id,
                "salt": salt.hex(),
            },
        )
    _tag_reads(verifier, execution_id)

    read_at = _now()
    before: EffectState
    baseline_sha: Optional[str] = None
    baseline_read_at: Optional[str] = None
    if baseline is not None:
        elapsed = (
            _parse_time(read_at) - _parse_time(str(baseline["baseline_read_at"]))
        ).total_seconds()
        if 0 <= elapsed <= cfg.max_execution_window_s:
            before = EffectState.model_validate(baseline["state"])
            baseline_sha = baseline_digest(baseline, salt)
            baseline_read_at = str(baseline["baseline_read_at"])
        else:
            # Outside the window the baseline proves nothing about this run.
            baseline = None
    if baseline is None:
        # No baseline: the judge treats the pre-state as unknown, so every
        # claim about change is INDETERMINATE, never guessed.
        before = EffectState(
            substrate=str(getattr(verifier, "substrate", "")), reachable=False
        )

    # Same digest qualify reports as verify_config_sha256 for this file.
    contract_digest = verify_config_sha256(identity, list(cfg.effects))
    verdicts: list[EffectVerdict] = []
    rows: list[dict[str, Any]] = []
    bound_effects: list[Effect] = []
    weakest: Optional[VerificationTier] = None
    tiers: list[Optional[VerificationTier]] = []
    for effect in cfg.effects:
        try:
            bound = effect.resolve(params)
            # Raw values withheld; each bound parameter enters as its salted
            # digest in the opaque-parameter slot (RFC-0002, salted digests 5).
            salted_params = {
                name: _salted_hex(salt, f"effect_param:{name}", params[name])
                for name in sorted(effect.referenced_params())
            }
            contract_hash = effect.resolved_contract_hash(
                {}, opaque_param_sha256=salted_params
            )
        except (KeyError, ValueError) as exc:
            raise VerifyRunError(f"effect contract cannot bind params: {exc}") from exc
        judged = (
            _frame_effect(effect, bound)
            if baseline is not None
            else _audit_effect(effect, bound)
        )
        bound_effects.append(bound)
        tier = verifier_effect_tier(verifier, judged)
        try:
            verdict = verify_effect_without_mutation(verifier, judged, before)
        except EffectContractMutationError as exc:
            raise VerifyRunError(str(exc)) from exc
        if tier is None and verdict.verdict is Verdict.CONFIRMED:
            # An untyped verifier cannot certify anything.
            verdict = EffectVerdict(
                verdict=Verdict.INDETERMINATE,
                kind=bound.kind,
                substrate=verdict.substrate,
                reason="verifier declares no evidence tier",
            )
        if tier is not None and (weakest is None or int(tier) > int(weakest)):
            weakest = tier
        verdicts.append(verdict)
        tiers.append(tier)
        # Closed fields only: no reason text, no observed value, no records.
        rows.append(
            {
                "effect_contract_hash": contract_hash,
                "kind": bound.kind.value,
                "substrate": verdict.substrate,
                "flow_verification_tier": int(tier) if tier is not None else None,
                "verdict": _label(verdict.verdict),
                "observed_effect": verdict.observed_effect,
                "observed_count": verdict.observed_count,
                "expected_count": verdict.expected_count,
                "unavailable": verdict.unavailable,
                "stale": verdict.stale,
                # With a baseline the frame judge covers every record outside
                # the delta (evidence.frame); without one nothing can.
                "collateral_change_checked": judged.forbid_collateral_change
                or baseline is not None,
            }
        )

    delta_verdict = _aggregate(verdicts)
    observed_effect = _observed(verdicts)
    # The frame: nothing outside the declared delta moved. Without a baseline
    # it can't be known, so it is not checked and the run is not verified.
    frame_spec = cfg.frame or FrameSpec()
    if baseline is not None:
        frame = judge_run_frame(
            cfg,
            verifier,
            before,
            bound_effects,
            params,
            audit_verifier=_audit_verifier(cfg, params),
            # The executor's own id is this run, never "another user".
            run_actors=[str(run_record.get("executor_id") or "")],
            salt=salt,
        )
    else:
        frame = FrameVerdict(
            status="not_checked",
            reason="no baseline, so what else changed can't be known",
        )
    # A record outside the declared delta moved: the save's effect on the
    # store is not the declared one, whatever the per-effect reads say.
    effect_verdict: EffectVerdictLabel = (
        "REFUTED" if frame.status == "refuted" else delta_verdict
    )
    if frame.status == "refuted" and observed_effect in ("absent", "present"):
        observed_effect = "conflicting"
    verification_tier = readback_class(verdicts, tiers)
    unread = verification_tier == "unchecked"
    # The check level the weakest read reached (RFC-0002 open question 5):
    # how it was checked, whatever the verdict.
    checked_how = (
        "unchecked"
        if unread or weakest is None
        else checked_how_for_flow_tier(int(weakest))
    )
    strength = (
        _STRENGTH_BY_TIER.get(weakest) if weakest is not None and not unread else None
    )
    oracle_tier = (
        oracle_tier_from_verification_tier(weakest)
        if weakest is not None and not unread
        else 0
    )
    strong_enough = strength is not None and (
        _STRENGTH_RANK[strength] >= _STRENGTH_RANK[cfg.minimum_effect_strength]
    )
    self_approved: Optional[bool] = None
    if approver_doc is not None:
        approver_ref = (
            "sha256:" + hashlib.sha256(approver_doc["id"].encode("utf-8")).hexdigest()
        )
        self_approved = approver_ref == executor_ref

    delivery_uncertain = False
    read_proves_write = (
        delta_verdict == "CONFIRMED"
        and verification_tier == "independent_readback"
        and strong_enough
        and oracle_tier in (2, 3)
    )
    # The source part: with provenance, the record as read back must match
    # the source document (a round trip). Without it the inputs are the
    # authorized instruction itself (``--params``).
    source_status, source_evidence = _judge_source(
        provenance,
        documents or {},
        second_extractor,
        params,
        # Unbound: the field each parameter was written to is named by its
        # ``{param: ...}`` reference, which binding replaces with the value.
        list(cfg.effects),
        verdicts,
        read_proves_write,
    )
    checked: dict[str, Any] = {
        "delta": _part(delta_verdict, read_proves_write),
        "frame": frame.status,
        "source": source_status,
    }
    admitted = True
    if cfg.admission is not None:
        # The check must have been shown to catch the faults it claims.
        record = coverage
        problem: Optional[str] = None
        if record is None:
            path = cfg.coverage_path()
            try:
                record = load_coverage(path) if path is not None else None
            except GateError:
                record = None
        if record is None:
            problem = "coverage_missing"
        else:
            problem = cfg.admission.problem(record, check_digest(cfg))
        admitted = problem is None
        checked["coverage"] = {
            "killed": record.killed if record is not None else 0,
            "applicable": record.applicable if record is not None else 0,
            "sha256": record.digest() if record is not None else None,
            "policy": "met" if admitted else str(problem),
            COVERAGE_CLASSES_KEY: record.class_results() if record is not None else {},
        }
    claim_holds = (
        read_proves_write
        and frame.held
        and checked["source"] in ("held", "instruction")
        and admitted
    )
    if claim_holds and self_approved is True:
        # The write landed, but the executor approved its own write: no
        # oversight separate from itself. Never verified.
        outcome = ExecuteTerminalOutcomeV1.REJECTED_POLICY
    elif claim_holds:
        outcome = ExecuteTerminalOutcomeV1.VERIFIED
    elif (
        effect_verdict == "REFUTED"
        and observed_effect == "absent"
        and not write_attempted
        and frame.status != "refuted"
    ):
        # The store shows nothing landed, whatever the executor said.
        outcome = ExecuteTerminalOutcomeV1.HALTED_BEFORE_EFFECT
    else:
        # A wrong effect, an unreadable store, or a read too weak to certify.
        outcome = ExecuteTerminalOutcomeV1.RECONCILIATION_REQUIRED
        delivery_uncertain = True
    verified = outcome is ExecuteTerminalOutcomeV1.VERIFIED
    landed = read_proves_write
    frame_held = frame.held

    evidence = {
        "schema_version": EFFECT_EVIDENCE_SCHEMA,
        "executor": {
            "kind": executor_kind,
            "ref_sha256": executor_ref,
            "build_sha256": build_digest,
            "run_record_sha256": salted_digest(salt, "run_record", dict(run_record)),
            "claims_counted_as_evidence": False,
        },
        "authorization": {
            "reference_sha256": salted_digest(
                salt, "authorization_reference", authorization_ref
            ),
            "evaluated_by_verifier": False,
            # Placeholder shape until the openadapt-types approver identity
            # model ships (RFC-0002, Who authorized).
            "approver": approver_doc,
            "self_approved": self_approved,
        },
        "salting": {
            "scheme": SALT_SCHEME,
            "salt_commitment": salt_commitment(salt),
        },
        "contract": {
            "contract_sha256": contract_digest,
            "minimum_effect_strength": cfg.minimum_effect_strength,
        },
        "readback": {
            "verifier_identity": identity,
            "verification_tier": verification_tier,
            "checked_how": checked_how,
            "baseline_sha256": baseline_sha,
            "baseline_read_at": baseline_read_at,
            "read_at": read_at,
            "max_execution_window_s": cfg.max_execution_window_s,
            "effects": rows,
        },
        "frame": frame.evidence(frame_spec),
        "source": source_evidence,
        "checked": checked,
        "verifier": {
            "name": "openadapt-flow verify-run",
            "model_calls": 0,
        },
        "effect_verdict": effect_verdict,
    }
    contracts = ExecuteEvidenceContractV1(
        # A reference was bound and the approver is not the executor;
        # verify-run did not evaluate a policy (evidence.authorization says so).
        authorization_passed=verified,
        # Identity is the contract's record selector, judged in the store.
        identity_passed=landed,
        # The postcondition includes the frame: nothing else moved.
        postcondition_passed=landed and frame_held,
        effect_passed=landed,
        minimum_effect_strength=EffectStrengthV1(cfg.minimum_effect_strength),
        observed_effect_strength=(
            EffectStrengthV1(strength) if strength is not None else None
        ),
        # Nothing says the executor used no model, so the receipt cannot.
        model_used=executor_kind != "flow_replay",
        external_network_used=executor_kind != "flow_replay",
    )
    contract_hex = contract_digest.split(":", 1)[1]
    issued_at = _now()
    v1 = ExecuteEvidenceReceiptV1(
        receipt_id=f"receipt-{uuid4().hex}",
        execution_id=execution_id,
        workflow_digest=contract_digest,
        workflow_version=f"contract-{contract_hex[:16]}",
        qualification_id=cfg.qualification_id,
        environment_id=cfg.environment_id,
        runner_id=f"executor-{executor_ref.split(':', 1)[1][:16]}",
        nonce=f"nonce-{uuid4().hex}",
        oracle_tier=cast(Literal[0, 1, 2, 3], oracle_tier),
        outcome=outcome,
        contracts=contracts,
        delivery_uncertain=delivery_uncertain,
        evidence_digest=sha256_tagged(evidence),
        issued_at=issued_at,
    )
    receipt = v1.model_dump(mode="json", exclude={"seal_signature"})
    receipt["schema_version"] = EFFECT_RECEIPT_SCHEMA
    receipt["executor_kind"] = executor_kind
    receipt["executor_ref_sha256"] = executor_ref
    receipt["effect_verdict"] = effect_verdict
    receipt["verification_tier"] = verification_tier
    receipt["checked_how"] = checked_how
    receipt["checked"] = checked
    if remediates is not None:
        receipt["remediates"] = remediates
    if correlation_id is not None:
        receipt["correlation_id"] = correlation_id
    problem = receipt_problem(receipt) or evidence_problem(evidence, receipt)
    if problem is not None:  # pragma: no cover - a producer bug, never input
        raise VerifyRunError(f"verify-run built an invalid receipt: {problem}")
    return {
        "schema_version": EFFECT_RECEIPT_SCHEMA,
        "receipt": receipt,
        "evidence": evidence,
    }


def write_result(result: Mapping[str, Any], out_dir: Path) -> Path:
    out = Path(out_dir) / "effect-receipt.json"
    _write_private(out, result)
    return out


# -- offline check ------------------------------------------------------------


def receipt_problem(receipt: Mapping[str, Any]) -> Optional[str]:
    """None when the receipt meets the schema and the cross-field rules."""

    from openadapt_types.execute import (
        EXECUTE_EVIDENCE_RECEIPT_SCHEMA,
        ExecuteEvidenceReceiptV1,
    )

    if receipt.get("schema_version") != EFFECT_RECEIPT_SCHEMA:
        return "schema_version"
    v1_names = set(ExecuteEvidenceReceiptV1.model_fields)
    allowed = (
        v1_names
        | set(_NEW_RECEIPT_FIELDS)
        | set(_OPTIONAL_RECEIPT_FIELDS)
        | {"checked_how", "checked"}
    )
    if set(receipt) - allowed:
        return "unknown_field"
    if any(name not in receipt for name in _NEW_RECEIPT_FIELDS):
        return "missing_field"
    v1 = {k: v for k, v in receipt.items() if k in v1_names and k != "seal_signature"}
    v1["schema_version"] = EXECUTE_EVIDENCE_RECEIPT_SCHEMA
    try:
        ExecuteEvidenceReceiptV1.model_validate(v1)
    except Exception:
        return "execute_v1_rules"
    if not _ISO_SECONDS.match(str(receipt.get("issued_at", ""))):
        return "issued_at"
    if receipt["executor_kind"] not in EXECUTOR_KINDS:
        return "executor_kind"
    if not _DIGEST.match(str(receipt["executor_ref_sha256"])):
        return "executor_ref_sha256"
    if receipt["effect_verdict"] not in ("CONFIRMED", "REFUTED", "INDETERMINATE"):
        return "effect_verdict"
    if receipt["verification_tier"] not in (
        "independent_readback",
        "screen_readback",
        "unchecked",
    ):
        return "verification_tier"
    for name in _OPTIONAL_RECEIPT_FIELDS:
        if name in receipt and not _OPAQUE.match(str(receipt[name])):
            return name
    # Optional: receipts issued before RFC-0002 open question 5 lack it.
    if "checked_how" in receipt and (
        receipt["checked_how"] not in CHECKED_HOW_VALUES
        or CHECKED_HOW_TIER[receipt["checked_how"]] != receipt["verification_tier"]
    ):
        return "checked_how"
    outcome = receipt.get("outcome")
    # Optional: receipts issued before docs/VERIFICATION_COVERAGE.md lack it.
    if "checked" in receipt:
        parts = receipt["checked"]
        cover = parts.get("coverage") if isinstance(parts, Mapping) else None
        if (
            not isinstance(parts, Mapping)
            or set(parts) - {"coverage"} != set(CHECKED_PARTS)
            or (
                "coverage" in parts
                and (
                    not isinstance(cover, Mapping)
                    or set(cover) - {COVERAGE_CLASSES_KEY} != COVERAGE_KEYS
                    or not isinstance(cover.get(COVERAGE_CLASSES_KEY, {}), Mapping)
                    or not isinstance(cover.get("killed"), int)
                    or not isinstance(cover.get("applicable"), int)
                    or not isinstance(cover.get("policy"), str)
                    or (
                        cover.get("sha256") is not None
                        and not _DIGEST.match(str(cover.get("sha256")))
                    )
                )
            )
            or parts.get("delta") not in CHECKED_VALUES
            or parts.get("frame") not in CHECKED_VALUES
            or parts.get("source") not in SOURCE_VALUES
        ):
            return "checked"
        if outcome in ("verified", "rolled_back_verified") and (
            parts["delta"] != "held"
            or parts["frame"] != "held"
            or parts["source"] not in ("held", "instruction")
            or (cover is not None and cover.get("policy") != "met")
        ):
            return "verified_without_every_part"
    if outcome in ("verified", "rolled_back_verified") and (
        receipt["effect_verdict"] != "CONFIRMED"
        or receipt["verification_tier"] != "independent_readback"
        or receipt.get("oracle_tier") not in (2, 3)
    ):
        return "verified_without_independent_readback"
    if (
        outcome == "reconciliation_required"
        and receipt.get("delivery_uncertain") is not True
    ):
        return "delivery_uncertain"
    return None


def evidence_problem(
    evidence: Mapping[str, Any], receipt: Mapping[str, Any]
) -> Optional[str]:
    """None when the manifest is bound to the receipt and meets its rules."""

    if sha256_tagged(dict(evidence)) != receipt.get("evidence_digest"):
        return "evidence_mismatch"
    executor = evidence.get("executor") or {}
    readback = evidence.get("readback") or {}
    authorization = evidence.get("authorization") or {}
    salting = evidence.get("salting") or {}
    if (
        evidence.get("effect_verdict") != receipt.get("effect_verdict")
        or executor.get("kind") != receipt.get("executor_kind")
        or readback.get("verification_tier") != receipt.get("verification_tier")
        or readback.get("checked_how") != receipt.get("checked_how")
        or evidence.get("checked") != receipt.get("checked")
    ):
        return "evidence_mismatch"
    frame = evidence.get("frame")
    if "checked" in receipt and (
        not isinstance(frame, Mapping)
        or frame.get("status") != receipt["checked"].get("frame")
    ):
        return "evidence_mismatch"
    if salting.get("scheme") != SALT_SCHEME or not _DIGEST.match(
        str(salting.get("salt_commitment", ""))
    ):
        return "unsalted"
    salted = [
        executor.get("run_record_sha256"),
        authorization.get("reference_sha256"),
    ]
    if readback.get("baseline_sha256") is not None:
        salted.append(readback.get("baseline_sha256"))
    if not all(isinstance(v, str) and _SALTED.match(v) for v in salted):
        return "unsalted"
    base_sha = readback.get("baseline_sha256")
    base_at = readback.get("baseline_read_at")
    if (base_sha is None) != (base_at is None):
        return "baseline_window"
    if base_at is not None:
        try:
            elapsed = (
                _parse_time(str(readback.get("read_at"))) - _parse_time(str(base_at))
            ).total_seconds()
        except ValueError:
            return "baseline_window"
        window = readback.get("max_execution_window_s")
        if not isinstance(window, int) or not 0 <= elapsed <= window:
            return "baseline_window"
    if authorization.get("self_approved") is True and (
        (receipt.get("contracts") or {}).get("authorization_passed") is not False
    ):
        return "self_approved"
    read_at = readback.get("read_at")
    if isinstance(read_at, str) and str(receipt.get("issued_at", "")) < read_at:
        return "issued_before_read"
    return None


def check_effect_receipt(
    document: Mapping[str, Any],
    registry: Sequence[Mapping[str, Any]],
    *,
    accept_outcomes: Sequence[str] = (),
) -> dict[str, Any]:
    """RFC-0002 offline verification against a pinned key registry.

    Reports the signature (``verified``) and the outcome
    (``verified_outcome``) separately; ``ok`` is true only when both hold, or
    when the outcome is one the caller named in ``accept_outcomes``. A
    missing signature is ``unsigned`` and is never a pass.
    """

    from openadapt_types.seal import parse_key_registry

    receipt = document.get("receipt") if "receipt" in document else document
    result: dict[str, Any] = {"verified": False, "verified_outcome": False, "ok": False}
    if (
        not isinstance(receipt, Mapping)
        or receipt.get("schema_version") != EFFECT_RECEIPT_SCHEMA
    ):
        return {**result, "reason": "no_effect_receipt"}
    outcome = receipt.get("outcome")
    result.update(
        {
            "schema_version": receipt.get("schema_version"),
            "executor_kind": receipt.get("executor_kind"),
            "effect_verdict": receipt.get("effect_verdict"),
            "verification_tier": receipt.get("verification_tier"),
            "outcome": outcome,
            "verified_outcome": outcome in ("verified", "rolled_back_verified"),
        }
    )
    if receipt_problem(receipt) is not None:
        return {**result, "verified_outcome": False, "reason": "malformed"}
    evidence = document.get("evidence")
    result["evidence_bound"] = isinstance(evidence, Mapping)
    signature = receipt.get("seal_signature")
    if signature is None:
        return {**result, "reason": "unsigned"}
    if (
        not isinstance(signature, Mapping)
        or set(signature) != _SIGNATURE_FIELDS
        or signature.get("schema") != EFFECT_SEAL_SIGNATURE_SCHEMA
        or signature.get("issuer") != EFFECT_SEAL_ISSUER
        or signature.get("algorithm") != "ed25519"
        or signature.get("receipt_schema") != receipt.get("schema_version")
        or not _ISO_SECONDS.match(str(signature.get("issued_at", "")))
    ):
        return {**result, "reason": "malformed"}
    result["issuer"] = signature.get("issuer")
    if signature.get("receipt_sha256") != receipt_sha256(receipt) or signature.get(
        "run_id"
    ) != receipt.get("execution_id"):
        return {**result, "reason": "receipt_mismatch"}
    try:
        keys = parse_key_registry(list(registry))
    except ValueError:
        return {**result, "reason": "bad_registry"}
    key = next((k for k in keys if k.key_id == signature.get("key_id")), None)
    if key is None:
        return {**result, "reason": "unknown_key"}
    if key.status == "revoked" and (
        key.revoked_at is None or str(signature["issued_at"]) >= key.revoked_at
    ):
        return {**result, "reason": "revoked_key"}
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:  # pragma: no cover - cryptography is a core dependency
        return {**result, "reason": "verifier_unavailable"}
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(key.public_key)).verify(
            base64.b64decode(str(signature["signature"]), validate=True),
            effect_signing_payload(signature),
        )
    except (InvalidSignature, ValueError):
        return {**result, "reason": "bad_signature"}
    result["key_id"] = key.key_id
    if isinstance(evidence, Mapping):
        problem = evidence_problem(evidence, receipt)
        if problem is not None:
            return {**result, "reason": problem}
    result["verified"] = True
    accepted = result["verified_outcome"] or (
        outcome in set(accept_outcomes) and outcome not in (None, "")
    )
    result["ok"] = bool(accepted)
    result["reason"] = (
        "verified" if result["verified_outcome"] else "outcome_not_verified"
    )
    return result
