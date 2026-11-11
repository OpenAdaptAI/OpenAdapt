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
import sqlite3
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

from openadapt_flow.complete_read import CompleteReadProvider
from openadapt_flow.deployment import EffectsConfig, build_effect_verifier
from openadapt_flow.fault_gate import (
    AdmissionPolicy,
    CoverageRecord,
    GateError,
    check_digest,
    load_coverage,
)
from openadapt_flow.owned_sqlite_insert import OwnedSqliteInsert, OwnedSqliteInsertConfig
from openadapt_flow.qualify.result import verify_config_sha256
from openadapt_flow.request_binding import RequestContext
from openadapt_flow.runtime.effects import late_write
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
_WRITE_ATTRIBUTION = "write_attribution"
_SQLITE_RESERVATION = "owned_sqlite_reservation"
_OWNED_DISPATCH_OWNER = "owned_dispatch_owner"
OwnedBaselineReference = tuple[str, str, str]
VerificationPurpose = Literal["observation", "owned_attempt"]

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
FRAME_IDENTITY_DETAIL = "openadapt:frame_identity_indexes"
#: Why the whole-database snapshot couldn't be read, when it couldn't.
FRAME_DB_ERROR = "openadapt:frame_database_error"
#: Where the baseline keeps the target versions read (``frame.versions``), as
#: keyed MACs.
FRAME_VERSIONS_DETAIL = "openadapt:frame_versions"
#: Where the baseline keeps the read set as keyed MACs (``effects/sealed.py``).
SEALED_DETAIL = "openadapt:sealed_records"

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
#: Optional too: when a verified write's recheck is due (whole-second UTC),
#: present only when the contract sets ``frame.database.recheck_after_s``.
#: It equals ``evidence.frame.recheck.due_at``.
RECHECK_RECEIPT_FIELD = "recheck_due_at"
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


_RECEIPT_VALIDATION_REASONS = frozenset(
    {
        "request_context",
        "write_attribution",
        "schema_version",
        "unknown_field",
        "missing_field",
        "execute_v1_rules",
        "issued_at",
        "executor_kind",
        "executor_ref_sha256",
        "effect_verdict",
        "verification_tier",
        "remediates",
        "correlation_id",
        "recheck_due_at",
        "checked_how",
        "checked",
        "verified_without_every_part",
        "verified_without_independent_readback",
        "delivery_uncertain",
        "evidence_mismatch",
        "unsalted",
        "baseline_window",
        "self_approved",
        "issued_before_read",
    }
)


class VerifyRunError(ValueError):
    """A refusal with an optional closed receipt-validation diagnostic.

    All existing validator codes are listed above. Other refusals and unknown
    future codes stay unclassified. This diagnostic grants no authority and
    does not change the exception's existing arguments or message.
    """

    def __init__(self, *args: object, validation_reason: object = None) -> None:
        super().__init__(*args)
        self._validation_reason = VerifyRunError._closed_reason(validation_reason)

    @staticmethod
    def _closed_reason(value: object) -> str:
        return (
            value
            if type(value) is str and value in _RECEIPT_VALIDATION_REASONS
            else "unclassified"
        )

    @property
    def validation_reason(self) -> str:
        return VerifyRunError._closed_reason(self.__dict__.get("_validation_reason"))


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
    owned_sqlite_insert: Optional[OwnedSqliteInsertConfig] = None
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
QUALIFY_ONLY_SECTIONS: tuple[str, ...] = (
    "app",
    "save_step",
    "inputs",
    "campaign",
    "business_rules",
)


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
    surface = (
        record.get("surface")
        if isinstance(record, dict)
        else getattr(record, "surface", None)
    )
    if surface:
        from openadapt_flow.qualify.config import record_reader

        _, problems = record_reader(cfg.verifier, surface)
        if problems:
            raise VerifyRunError(f"invalid {source}: " + "; ".join(problems))
    key = (
        record.get("duplicate_key")
        if isinstance(record, dict)
        else getattr(record, "duplicate_key", None)
    )
    if isinstance(key, list):
        from openadapt_flow.qualify.config import composite_key_read_problems

        problems = composite_key_read_problems(key, cfg.verifier, surface)
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
    history = cfg.frame.history if cfg.frame is not None else None
    if history is not None:
        sources = cfg.verifier.surfaces or {"": cfg.verifier}
        if set(history.surfaces) != set(sources):
            raise VerifyRunError("frame.history must cover every verifier surface")
        for name, history_source in sources.items():
            feed = history.feeds[history.surfaces[name]]
            read = EffectsConfig.model_validate(feed.read)
            if read.candidates or read.surfaces or read.records_drop_fields:
                raise VerifyRunError(
                    "history feeds cannot omit fields or route candidates"
                )
            if history_source.kind == "fhir" and (
                read.kind != "fhir"
                or read.resource_type != history_source.resource_type
                or read.base_url != history_source.base_url
            ):
                raise VerifyRunError(
                    "history must cover the same FHIR origin and type as the record read"
                )
        for feed in history.feeds.values():
            read = EffectsConfig.model_validate(feed.read)
            if read.candidates or read.surfaces or read.records_drop_fields:
                raise VerifyRunError(
                    "history feeds cannot omit fields or route candidates"
                )
    database = cfg.frame.database if cfg.frame is not None else None
    if database is not None:
        surfaces = set(cfg.verifier.surfaces) or {""}
        unknown = sorted(set(database.views.values()) - surfaces)
        if unknown:
            raise VerifyRunError(
                "frame.database.views names no such surface: " + ", ".join(unknown)
            )
        unknown = sorted(set(database.judged_by.values()) - surfaces)
        if unknown:
            raise VerifyRunError(
                "frame.database.judged_by names no such surface: " + ", ".join(unknown)
            )
        named = list(database.judged_by.values())
        twice = sorted({s for s in named if named.count(s) > 1})
        if twice:
            raise VerifyRunError(
                "frame.database.judged_by names one surface for two tables: "
                + ", ".join(twice)
            )
        attribution_spec = cfg.frame.attribution if cfg.frame is not None else None
        surface_ignore = set(cfg.frame.ignore_fields if cfg.frame is not None else [])
        if attribution_spec is not None and attribution_spec.kind == "record_field":
            surface_ignore |= set(attribution_spec.server_set)
        for table in sorted(database.judged_by):
            loose = sorted(surface_ignore - set(database.ignore_columns_for(table)))
            if loose:
                raise VerifyRunError(
                    f"frame.database.judged_by: the surface for {table} ignores "
                    f"{', '.join(loose)}, which the table doesn't ignore "
                    "(frame.database.ignore_columns)"
                )
        both = sorted(
            set(database.judged_by) & (set(database.views) | set(database.modes()))
        )
        if both:
            raise VerifyRunError(
                "frame.database.judged_by names a table that views or "
                "allow_tables also names: " + ", ".join(both)
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
        for name in effect.absent_is_null_fields():
            # Only a JSON body leaves a null field out. A SQL row or a FHIR
            # record carries every column or path, and the REST reader never
            # resolves a dotted key: there a declared field would read as
            # null on every record, whatever was written.
            if "." in name:
                raise VerifyRunError(
                    f"absent_is_null on {name!r}: a dotted key is never read "
                    "from a REST body, so it can't be declared absent"
                )
            json_kinds = ("rest", "graphql")
            if any((kind or "").strip().lower() not in json_kinds for kind in kinds):
                raise VerifyRunError(
                    f"absent_is_null on {name!r}: only a rest or graphql read "
                    "may leave a null field out; a SQL row or FHIR record "
                    "always carries it"
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


def _read_versions(
    cfg: VerifyConfig, params: Mapping[str, object]
) -> Optional[list[dict[str, Any]]]:
    """The ``frame.versions`` read: one record per version, or None when it
    can't be read."""

    spec = cfg.frame.versions if cfg.frame is not None else None
    if spec is None:
        return None
    try:
        reader = build_effect_verifier(EffectsConfig.model_validate(spec.read), params)
    except ValueError as exc:
        raise VerifyRunError(f"cannot build the versions read: {exc}") from exc
    try:
        state = reader.capture_pre_state() if reader is not None else None
    except Exception:  # noqa: BLE001 - unreadable -> the check stops the run
        return None
    if not isinstance(state, EffectState) or not state.reachable:
        return None
    return [dict(r) for r in state.records]


def _judge_versions(
    cfg: VerifyConfig,
    frame: FrameVerdict,
    before: EffectState,
    effects: Sequence[Effect],
    verdicts: Sequence[EffectVerdict],
    params: Mapping[str, object],
    salt: bytes,
) -> FrameVerdict:
    """Add ``frame.versions`` to the frame: a version of a target in the
    window that isn't the agent's own refutes it; one that can't be read or
    attributed leaves it not checked."""

    from openadapt_flow.runtime.effects.versions import VersionVerdict, judge_versions

    spec = cfg.frame.versions if cfg.frame is not None else None
    if spec is None:
        return frame
    field_name = spec.target_field or cfg.frame.identity_field or "id"  # type: ignore[union-attr]
    # Whether a target existed is told by its identity, which a save doesn't
    # change, never by the target field's value after the write: a target
    # field the write changed would make an old record look created.
    identity = cfg.frame.identity_field or "id"  # type: ignore[union-attr]
    from openadapt_flow.runtime.effects.surfaces import surface_of

    def index(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            if r.get(identity) not in (None, ""):
                out.setdefault(str(r.get(identity)), dict(r))
        return out

    def before_rows(effect: Effect) -> Optional[dict[str, dict[str, Any]]]:
        """The records read before the write where this effect looks: its
        surface's own read, else the verifier's records. None: no read says
        which records were there (then no target is taken as new)."""

        surface = surface_of(effect)
        if surface:
            sub = (before.detail or {}).get(surface)
            if not isinstance(sub, Mapping) or not sub.get("reachable"):
                return None
            rows = sub.get("records")
            return index(rows) if isinstance(rows, list) else None
        return index(before.records) if before.reachable else None

    def writes_a_target(effect: Effect) -> bool:
        """A write effect whose records can be targets. A guard ("nothing
        here", ``record_written`` with ``expected_count`` 0) never is."""

        if effect.kind not in (EffectKind.RECORD_WRITTEN, EffectKind.FIELD_EQUALS):
            return False
        return not (
            effect.kind is EffectKind.RECORD_WRITTEN and effect.expected_count == 0
        )

    ignored = set(cfg.frame.ignore_fields)  # type: ignore[union-attr]
    targets: list[tuple[Optional[str], bool, bool]] = []
    for effect, verdict in zip(effects, verdicts):
        if not writes_a_target(effect):
            continue
        if not verdict.confirmed:
            continue
        if (
            spec.target_surface is not None
            and surface_of(effect) != spec.target_surface
        ):
            continue  # a row beside the target, not the target
        old = before_rows(effect)
        for record in verdict.matched_records:
            value = record.get(field_name)
            text = None if value in (None, "") else str(value)
            key = record.get(identity)
            was = None if key in (None, "") or old is None else old.get(str(key))
            # A record is new only when the read before the write shows it
            # wasn't there. No identity, or no read to look in: never new.
            existed = was is not None or key in (None, "") or old is None
            # Changed in the window: a field read before differs now (a
            # sealed baseline marks a changed field, so it differs too).
            changed = existed and (
                was is None
                or any(record.get(k) != v for k, v in was.items() if k not in ignored)
            )
            targets.append((text, existed, changed))
    stored = (before.detail or {}).get(FRAME_VERSIONS_DETAIL)
    if spec.target_surface is not None and not any(
        writes_a_target(e) and surface_of(e) == spec.target_surface for e in effects
    ):
        # A surface no write target is read from (a typo in a hand-written
        # contract, or a surface only a guard reads) would leave no target,
        # and no target holds.
        result = VersionVerdict(
            "not_checked",
            "unknown_surface",
            reason="frame.versions.target_surface names a surface no write "
            "target is read from, so no target's versions can be judged",
        )
    else:
        result = judge_versions(
            spec,
            stored if isinstance(stored, Mapping) else {},
            _read_versions(cfg, params),
            targets,
            _mac_key(salt),
            params,
        )
    frame.versions = result
    if result.status == "refuted":
        frame.status = "refuted"
        frame.reason = result.reason
    elif result.status == "not_checked" and frame.status == "held":
        frame.status = "not_checked"
        frame.reason = result.reason
    return frame


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


def _same_sql_source(source: Mapping[str, Any], read: Mapping[str, Any]) -> bool:
    """Positive source binding for an attributed physical-row witness."""

    if source.get("kind") != "sql":
        return False
    if source.get("sqlite_database") or read.get("sqlite_database"):
        a, b = source.get("sqlite_database"), read.get("sqlite_database")
        return bool(a and b and Path(str(a)).resolve() == Path(str(b)).resolve())
    return bool(source.get("sql_driver")) and (
        source.get("sql_driver") == read.get("sql_driver")
        and (source.get("sql_connect_args") or {})
        == (read.get("sql_connect_args") or {})
        and source.get("sql_password_env") == read.get("sql_password_env")
    )


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
    cols.update(binding.column for binding in frame.identities.values())
    cols.update(
        cell.column
        for binding in frame.identities.values()
        for cell in (binding.field_bindings or {}).values()
    )
    a = frame.attribution
    if a is not None:
        cols |= {a.record_key} if a.record_key else set()
        cols |= set(a.record_keys.values())
        cols |= {o.column for o in a.owners.values()}
    for side in frame.side_records:
        cols |= {side.link_field, side.of_field}
    if frame.learned is not None:
        # A learned excuse is checked by its actor column and moved columns.
        cols |= set(frame.learned.columns_to_keep())
    profile = frame.database.loaded_profile() if frame.database else None
    if profile is not None:
        cols |= set(profile.ref_columns())
    return sorted(c for c in cols if c)


def _read_database(
    cfg: VerifyConfig,
    salt: bytes,
    before: Optional[Any] = None,
    errors: Optional[list[str]] = None,
    retain_values: Sequence[str] = (),
) -> Optional[Any]:
    """The whole-database snapshot, or None when it can't be read (and then
    why, appended to ``errors``)."""

    from openadapt_flow.runtime.effects.db_snapshot import mac_key, take_snapshot

    database = cfg.frame.database if cfg.frame is not None else None
    if database is None or cfg.frame is None:
        return None
    try:
        return take_snapshot(
            _db_connect(database.read),
            database.dialect,
            key=mac_key(salt),
            modes=database.modes(),  # type: ignore[arg-type]
            max_rows=database.max_rows,
            read_row_budget=database.read_row_budget,
            before=before,
            extra_queries=_audit_sql(cfg),
            ignore=database.all_ignore_columns(),
            ref_columns=_ref_columns(cfg),
            schemas=database.schemas,
            optional_modes=database.profile_modes(),  # type: ignore[arg-type]
            links=database.recheck_after_s is not None,
            retain_values=tuple(
                set(retain_values) | {b.table for b in cfg.frame.identities.values()}
            ),
        )
    except Exception as exc:  # noqa: BLE001 - unreadable -> the frame is not checked
        if errors is not None:
            errors.append(f"{type(exc).__name__}: {exc}")
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
    #: A baseline or post-save read exceeded the row budget.
    too_large: bool = False
    #: The app profile's judgment of one row change (``AppProfile.excuse``).
    excuse: Any = None
    #: Every user's id by sign-in name (in memory only), to resolve an
    #: audit entry's actor to an owner column (owner-scoped attribution).
    users_map: dict[str, str] = field(default_factory=dict)
    #: Append-only tables in both reads whose profile rule excuses the acting
    #: user's insert entries that name no record.
    no_record_inserts: list[str] = field(default_factory=list)
    #: The profile's record-field values that name no record.
    no_record_values: list[str] = field(default_factory=list)
    #: The settled read after the save, and when it was taken: what a later
    #: recheck compares with (``recheck_after_s``).
    post: Any = None
    post_read_at: str = ""
    #: The snapshot the rows are judged against.
    old: Any = None
    #: (table, record) -> the row key MAC of the table's row with that
    #: record's primary key values, or None when they can't be read from it.
    row_key: Any = None


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
        SnapshotTooLarge,
        cell_hash,
        key_text,
        mac_key,
        row_key_hash,
        settled_state,
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
    limit_error = SnapshotTooLarge.__name__ + ":"
    if not isinstance(stored, dict):
        error = (
            before.detail.get(FRAME_DB_ERROR, "")
            if isinstance(before.detail, dict)
            else ""
        )
        unreachable.too_large = str(error).startswith(limit_error)
        return unreachable
    old = Snapshot.from_json(stored)
    errors: list[str] = []
    new = _read_database(cfg, salt, before=old, errors=errors)
    if new is None:
        unreachable.too_large = any(e.startswith(limit_error) for e in errors)
        return unreachable
    new_at = _now()
    # Read again a moment later: two reads in a row must match, or the app
    # is still writing and what the save caused can't be told yet. A live
    # app moves on its own, so up to ``settle_attempts`` more reads are
    # taken, each ``settle_s`` after the last; the earlier read of the first
    # matching pair is judged. Rows added to append-only tables between the
    # two don't count: the frame excuses them anyway.
    attempts = database.settle_attempts
    settled = False
    used = 0
    for used in range(1, attempts + 1):
        time.sleep(database.settle_s)
        again = _read_database(cfg, salt, before=old, errors=errors)
        if again is None:
            unreachable.too_large = any(e.startswith(limit_error) for e in errors)
            return unreachable
        if settled_state(new, again):
            settled = True
            break
        if used < attempts:
            new, new_at = again, _now()
    if not settled:
        moving = _DatabaseFrame({}, {}, [], {}, ("", ""), {}, None)
        moving.still_moving = True
        moving.evidence = {
            "settle_attempts": used,
            "settle_attempts_allowed": attempts,
            "settled": False,
        }
        return moving
    if not (old.code_visible and new.code_visible):
        blind = _DatabaseFrame({}, {}, [], {}, ("", ""), {}, None)
        blind.blind = True
        return blind
    key = mac_key(salt)
    out = _DatabaseFrame({}, {}, [], {}, (old.clock, new.clock), {}, None)
    out.post, out.post_read_at, out.old = new, new_at, old

    def row_key(table: str, record: Mapping[str, Any]) -> Optional[str]:
        t = old.tables.get(table)
        pk = list(t.primary_key) if t is not None else []
        if not pk or any(record.get(c) is None for c in pk):
            return None
        return row_key_hash(key, key_text(pk, record), table)

    out.row_key = row_key
    if "audit" in new.extra:
        out.audit_after = list(new.extra["audit"])
    if old.objects != new.objects:
        out.unread.append(
            ("(database code)", "triggers, views, routines or events changed")
        )
    counts = {"compare": 0, "append_only": 0, "skip": 0}
    from openadapt_flow.app_profiles import table_name

    profile = database.loaded_profile()
    no_record = profile.no_record_insert_tables() if profile is not None else set()
    out.no_record_values = list(profile.no_record_values) if profile is not None else []
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
            a is not None
            and b is not None
            and a.mode == b.mode == "append_only"
            and table_name(name) in no_record
        ):
            out.no_record_inserts.append(name)
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
        **(
            {"schemas_skipped": list(new.schemas_skipped)}
            if new.schemas_skipped
            else {}
        ),
        "allow_list_sha256": database.allow_list_sha256(),
        "settle_attempts": used,
        "settle_attempts_allowed": attempts,
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

        def excuse(name: str, before: Any, after: Any, fields: list[str]) -> Any:
            from openadapt_flow.runtime.effects.db_snapshot import every_cell_hashed

            tables = (old.tables.get(name), new.tables.get(name))
            if any(t is not None and not every_cell_hashed(t) for t in tables):
                # A column no cell MAC shows may have moved: no rule judges it.
                return False
            return profile.excuse(
                name, before, after, fields, hashers[name], names, ids
            )

        out.excuse = excuse
    return out


def _later_status(found: Sequence[Optional[str]]) -> Optional[str]:
    """``clean`` when a later write's window covers a change and none calls
    it disputed; ``disputed`` when one does; None when none covers it."""

    hits = [f for f in found if f is not None]
    if not hits:
        return None
    return "disputed" if "disputed" in hits else "clean"


def _collateral_record(verdict: FrameVerdict, db: Any, salt: bytes) -> dict[str, Any]:
    rows: list[list[str]] = []
    entries: list[Mapping[str, Any]] = []
    unmapped = False
    for change in verdict.collateral:
        if change.audit_only and isinstance(change.entry, Mapping):
            entries.append(change.entry)
        elif change.surface in db.slices and isinstance(change.key, str):
            rows.append([change.surface, change.key])
        else:
            unmapped = True  # a change a recheck can't match by key
    return {
        "rows": sorted(rows),
        "entries": sorted(_audit_ids_each(entries, salt)),
        "unmapped": unmapped or bool(verdict.collateral and not db.slices),
    }


def _read_audit(verifier: Any) -> Optional[list[dict[str, Any]]]:
    if verifier is None:
        return None
    try:
        return _post_records(verifier.capture_pre_state())
    except Exception:  # noqa: BLE001 - an unreadable log attributes nothing
        return None


def _declared_by_surface(
    table: str,
    surface: str,
    state: tuple[EffectState, Optional[list[dict[str, Any]]]],
    effects: Sequence[Effect],
    routes: Mapping[str, Sequence[str]],
    db: _DatabaseFrame,
) -> set[str]:
    """Row key MACs of ``table``'s rows that ``surface`` declares
    (``frame.database.judged_by``): its records, before or after the save,
    that an effect routed to it selects, matched to the table by primary
    key. The table judges those rows itself, every column exactly, with the
    surface's fields as the ones a declared row may change. Empty when a
    declared record's key can't be read, so nothing is declared there and a
    declared change stops the write."""

    from openadapt_flow.runtime.effects.effect import record_matches
    from openadapt_flow.runtime.effects.frame import declared_selectors

    before_state, after_records = state
    if after_records is None or not before_state.reachable:
        return set()
    selectors = [
        s
        for i, effect in enumerate(effects)
        if surface in routes.get(str(i), ())
        for s in declared_selectors(effect)
    ]
    typed = before_state.substrate == "sql"
    keys: set[str] = set()
    for record in [*before_state.records, *after_records]:
        if not any(
            record_matches(dict(record), s, text_numbers=typed) for s in selectors
        ):
            continue
        k = db.row_key(table, record)
        if k is None:
            return set()
        keys.add(k)
    return keys


def _frame_surface_routes(
    cfg: VerifyConfig, effects: Sequence[Effect]
) -> dict[str, list[str]]:
    """Use the same content-twin routes before and after a write."""
    from openadapt_flow.runtime.effects.surfaces import surface_of

    if not cfg.verifier.surfaces:
        return {str(i): [""] for i in range(len(effects))}

    def read_of(name: str) -> str:
        sub = cfg.verifier.surfaces.get(name)
        if sub is None:
            return name
        shape = sub.model_dump(mode="json", exclude={"records_drop_fields"})
        return json.dumps(shape, sort_keys=True, default=str)

    views: dict[str, list[str]] = {}
    for name in cfg.verifier.surfaces:
        views.setdefault(read_of(name), []).append(name)
    routes = {}
    for i, effect in enumerate(effects):
        own = surface_of(effect) or ""
        routes[str(i)] = (
            views.get(read_of(own), [own]) if own in cfg.verifier.surfaces else [own]
        )
    return routes


def _identity_evidence_mac(data: Any, key: bytes) -> str:
    raw = json.dumps(
        data, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    return hmac.new(
        key, b"openadapt.frame-identity-evidence/v1\0" + raw, hashlib.sha256
    ).hexdigest()


def _identity_effect_binding(
    effects: Sequence[Effect], params: Mapping[str, object], key: bytes
) -> str:
    return _identity_evidence_mac(
        {
            "effects": [e.model_dump(mode="json") for e in effects],
            "params": dict(params),
        },
        key,
    )


def _mapped_baseline_evidence(
    cfg: VerifyConfig,
    state: EffectState,
    indexes: Mapping[str, dict[str, str]],
    params: Mapping[str, object],
    salt: bytes,
) -> dict[str, Any]:
    """Authenticate old selectors before sealing removes the original values."""
    from openadapt_flow.runtime.effects.effect import record_matches
    from openadapt_flow.runtime.effects.frame import declared_selectors
    from openadapt_flow.runtime.effects.frame_identity import match_records

    if cfg.frame is None:
        return {}
    effects = [e.resolve(params) for e in cfg.effects]
    routes = _frame_surface_routes(cfg, effects)
    key = _mac_key(salt)
    bound = _identity_effect_binding(effects, params, key)
    result = {}
    for surface, binding in cfg.frame.identities.items():
        records = _surface_records(state, surface)
        row_keys = match_records(binding, records, indexes[surface], key)
        selectors = [
            s
            for i, e in enumerate(effects)
            if surface in routes.get(str(i), ())
            for s in declared_selectors(e)
        ]
        typed = _surface_config(cfg, surface).kind == "sql"
        selected = [
            row_key
            for record, row_key in zip(records, row_keys)
            if any(
                record_matches(dict(record), sel, text_numbers=typed)
                for sel in selectors
            )
        ]
        data = {
            "index": indexes[surface],
            "declared": sorted(selected),
            "effects": bound,
            "read_config": binding.read_config_sha256,
            "binding": binding.model_dump(mode="json"),
        }
        result[surface] = {"data": data, "mac": _identity_evidence_mac(data, key)}
    return result


def _map_frame_rows(
    cfg: VerifyConfig,
    before: EffectState,
    after: Any,
    db: _DatabaseFrame,
    slices: dict[str, tuple[EffectState, Optional[list[dict[str, Any]]]]],
    effects: Sequence[Effect],
    routes: Mapping[str, Sequence[str]],
    salt: bytes,
    declared: dict[str, set[str]],
    params: Mapping[str, object],
    column_values: dict[str, dict[str, dict[str, Any]]],
) -> None:
    from openadapt_flow.runtime.effects.effect import record_matches
    from openadapt_flow.runtime.effects.frame import declared_selectors
    from openadapt_flow.runtime.effects.frame_identity import match_records

    if cfg.frame is None or not isinstance(after, EffectState):
        raise VerifyRunError("no current mapped surface read")
    previous = before.detail.get(FRAME_IDENTITY_DETAIL)
    if not isinstance(previous, dict):
        raise VerifyRunError("baseline has no bound identity index")
    if set(previous) != set(cfg.frame.identities):
        raise VerifyRunError(
            "baseline identity evidence belongs to another surface mapping"
        )
    key = _mac_key(salt)
    bound = _identity_effect_binding(effects, params, key)
    authenticated = {}
    for surface, binding in cfg.frame.identities.items():
        envelope = previous.get(surface)
        if (
            not isinstance(envelope, dict)
            or set(envelope) != {"data", "mac"}
            or type(envelope["mac"]) is not str
        ):
            raise VerifyRunError("baseline identity evidence is malformed")
        data = envelope["data"]
        if not isinstance(data, dict) or not hmac.compare_digest(
            envelope["mac"], _identity_evidence_mac(data, key)
        ):
            raise VerifyRunError("baseline identity evidence authentication failed")
        if (
            set(data) != {"index", "declared", "effects", "read_config", "binding"}
            or data["effects"] != bound
            or data["read_config"] != binding.read_config_sha256
            or data["binding"] != binding.model_dump(mode="json")
        ):
            raise VerifyRunError(
                "baseline identity evidence belongs to another read or effect contract"
            )
        authenticated[surface] = data
    current = _frame_identity_indexes(cfg, after, db.post, salt)
    for surface, binding in cfg.frame.identities.items():
        data = authenticated[surface]
        prior, selected = data["index"], data["declared"]
        if not isinstance(prior, dict) or any(
            type(k) is not str or type(v) is not str for k, v in prior.items()
        ):
            raise VerifyRunError("baseline identity index is malformed")
        old_table = db.old.tables.get(binding.table) if db.old is not None else None
        if (
            old_table is None
            or set(prior.values()) != set(old_table.rows)
            or len(prior) != len(old_table.rows)
        ):
            raise VerifyRunError("baseline identity index does not cover its table")
        if (
            not isinstance(selected, list)
            or any(type(k) is not str or k not in prior.values() for k in selected)
            or len(selected) != len(set(selected))
        ):
            raise VerifyRunError("baseline selected row keys are malformed")
        old_state, post_records = slices[surface]
        if post_records is None or not old_state.reachable:
            raise VerifyRunError("mapped surface is unreadable")
        # The old selection came from actual pre-write records and is bound
        # to these exact effects/parameters. Sealed REMOVED/CHANGED markers
        # are never interpreted as actual application identities or values.
        keys = set(selected)
        selectors = [
            s
            for i, e in enumerate(effects)
            if surface in routes.get(str(i), ())
            for s in declared_selectors(e)
        ]
        row_keys = match_records(binding, post_records, current[surface], key)
        for record, row_key in zip(post_records, row_keys):
            if any(
                record_matches(
                    dict(record), sel, text_numbers=old_state.substrate == "sql"
                )
                for sel in selectors
            ):
                keys.add(row_key)
        declared[binding.table] = keys
        if binding.field_bindings is not None:
            # Pair actual current reader values to the frame's own row keys.
            # Intent is checked by the ordinary effect verdict; the frame
            # independently checks the bound stored cells against this read.
            column_values[binding.table] = {
                row_key: dict(record)
                for record, row_key in zip(post_records, row_keys)
                if row_key in keys
            }
            # The database diff ordinarily carries changed rows only. Cell
            # verification also needs an unchanged target: a misleading read
            # must not certify a requested update that never reached storage.
            from openadapt_flow.runtime.effects.db_snapshot import KEY, ROW, unpack

            table = db.post.tables[binding.table]
            baseline, now = slices[binding.table]
            if now is None:
                raise VerifyRunError("mapped table has no current row slice")
            present = {record[KEY] for record in now}
            previous_rows = list(baseline.records)
            current_rows = list(now)
            for row_key in column_values[binding.table]:
                if row_key in present:
                    continue
                current_rows.append(
                    {KEY: row_key, ROW: table.rows[row_key][0], **table.values[row_key]}
                )
                if row_key in old_table.rows:
                    previous_rows.append(
                        {
                            KEY: row_key,
                            ROW: old_table.rows[row_key][0],
                            **unpack(
                                old_table.cell_columns, old_table.rows[row_key][1]
                            ),
                        }
                    )
            slices[binding.table] = (
                baseline.model_copy(update={"records": previous_rows}),
                current_rows,
            )


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
    database_only: bool = False,
    later: Sequence[Any] = (),
) -> FrameVerdict:
    """Read the slice after the save and judge the frame against ``before``.

    ``database_only`` (a recheck): judge ``frame.database`` and the audit
    log only; the verifier's own read set is not read, and is used only to
    route effects to their tables. ``later``: the windows of later writes
    (``late_write.LaterWindow``) whose own frames already judged the rows
    and audit entries in them.
    """

    spec = cfg.frame or FrameSpec()
    if FRAME_IDENTITY_DETAIL in before.detail and (
        cfg.frame is None or cfg.frame.database is None or not cfg.frame.identities
    ):
        return FrameVerdict(
            status="not_checked",
            reason="baseline identity evidence belongs to another surface mapping",
        )
    # The database first, then the surfaces: a row a surface judges
    # (``database.judged_by``) is read by it no earlier than the settled
    # read a recheck compares with, so no change falls between the two.
    db = _database_frame(cfg, before, salt)
    if db is not None and db.too_large:
        from openadapt_flow.runtime.effects.db_snapshot import SnapshotTooLarge

        assert spec.database is not None
        return FrameVerdict(
            status="not_checked",
            reason=str(SnapshotTooLarge(spec.database.read_row_budget)),
            database={
                "read_status": "too_large",
                "read_row_budget": spec.database.read_row_budget,
            },
            attribution=spec.attribution.kind if spec.attribution else "none",
        )
    after: Any = None
    if not database_only:
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
        routes = _frame_surface_routes(cfg, bound_effects)
        for name in [] if database_only else routed:
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
    elif not database_only:
        slices[""] = (before, _post_records(after))
    identities: dict[str, str] = {
        name: b.surface_field for name, b in spec.identities.items()
    }
    #: Table -> row key MACs its ``judged_by`` surface declares.
    declared_rows: dict[str, set[str]] = {}
    column_values: dict[str, dict[str, dict[str, Any]]] = {}
    if db is not None and db.still_moving:
        tries = db.evidence.get("settle_attempts", 1)
        return FrameVerdict(
            status="not_checked",
            reason=(
                "the database was still changing after the save (no two reads "
                f"in a row matched in {tries} attempt(s)); what the save "
                "caused can't be told yet"
            ),
            attribution=spec.attribution.kind if spec.attribution else "none",
            database=dict(db.evidence),
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
        if spec.database is not None and db.row_key is not None:
            for table, surface in spec.database.judged_by.items():
                if table in db.slices and surface in slices:
                    declared_rows[table] = _declared_by_surface(
                        table, surface, slices[surface], bound_effects, routes, db
                    )
        identities.update({name: KEY for name in db.slices})
        if (
            spec.identities or FRAME_IDENTITY_DETAIL in before.detail
        ) and not database_only:
            try:
                _map_frame_rows(
                    cfg,
                    before,
                    after,
                    db,
                    slices,
                    bound_effects,
                    routes,
                    salt,
                    declared_rows,
                    params,
                    column_values,
                )
            except (ValueError, TypeError) as exc:
                return FrameVerdict(
                    status="not_checked",
                    reason=f"frame identity mapping refused: {exc}",
                )
    audit_before: Optional[list[dict[str, Any]]] = None
    audit_after: Optional[list[dict[str, Any]]] = None
    stored = (
        before.detail.get(FRAME_AUDIT_DETAIL)
        if isinstance(before.detail, dict)
        else None
    )
    entries_now: Optional[list[dict[str, Any]]] = None
    #: A change a later write's window covers that may be what refuted it.
    disputed: list[bool] = []
    if isinstance(stored, dict):
        entries_now = (
            db.audit_after
            if db is not None and db.audit_after is not None
            else _read_audit(audit_verifier)
        )
    if isinstance(stored, dict) and stored.get("reachable"):
        if entries_now is not None:
            if "ids" in stored:
                # The window: entries now that the baseline didn't hold.
                seen = set(stored["ids"])
                audit_before = []
                audit_after = []
                for e, h in zip(entries_now, _audit_ids_each(entries_now, salt)):
                    if h in seen:
                        continue
                    status = _later_status([w.entry(e) for w in later])
                    if status == "disputed":
                        disputed.append(True)
                    if status is None:
                        audit_after.append(e)
            else:
                audit_before = list(stored.get("records", []))
                audit_after = entries_now
    excuse = None if db is None else db.excuse
    if db is not None and later and spec.database is not None:
        from openadapt_flow.runtime.effects.db_snapshot import KEY

        db_spec = spec.database
        profile_excuse = db.excuse

        old_tables = db.old.tables if db.old is not None else {}

        def excuse(name: str, before: Any, after: Any, fields: list[str]) -> Any:
            skip = db_spec.ignore_columns_for(name)
            table = old_tables.get(name)
            links = table.links if table is not None else {}
            status = _later_status(
                [w.row(name, before, after, skip, links) for w in later]
            )
            if status == "disputed":
                disputed.append(True)
            if status is not None:
                return [late_write.LATER_WRITE_RULE]
            if profile_excuse is None:
                return None
            return profile_excuse(name, before, after, fields)

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
        no_record_inserts=() if db is None else db.no_record_inserts,
        no_record_values=() if db is None else db.no_record_values,
        hashers=None if db is None else db.hashers,
        clock=("", "") if db is None else db.clock,
        database_evidence=None if db is None else db.evidence,
        excuse=excuse,
        user_ids=None if db is None else db.users_map,
        declared_rows=declared_rows,
        column_values=column_values,
    )
    profile = db.evidence.get("profile") if db is not None else None
    if profile is not None:
        # The rules that actually excused something: unconditional ones seen
        # in the snapshots, conditional ones as the judge applied them.
        profile["rules_used"] = sorted(
            (set(profile["rules_used"]) | verdict.excused_by)
            - {late_write.LATER_WRITE_RULE}
        )
    if disputed:
        verdict.excused_by.add(late_write.LATER_WRITE_DISPUTED)
    _witness_attributed_additions(cfg, verifier, verdict, db)
    database = spec.database
    if (
        db is not None
        and db.post is not None
        and database is not None
        and database.recheck_after_s is not None
    ):
        # What a later recheck compares with (and, for a later write, what
        # its own window covered): MACs and keyed hashes only.
        verdict.post_state = {
            "snapshot": db.post.to_json(),
            "read_at": db.post_read_at,
            # What this run's frame reported, as row key MACs and keyed
            # entry hashes: a later recheck of an earlier write can tell a
            # change this run was refuted for from one it explained.
            "collateral": _collateral_record(verdict, db, salt),
            "audit": None
            if not isinstance(stored, dict)
            else {
                "reachable": entries_now is not None,
                "ids": _audit_ids(entries_now or [], salt),
            },
        }
    return verdict


def _sql_reader(verifier: Any, surface: str) -> Any:
    from openadapt_flow.runtime.effects.sql import SqlRecordVerifier

    routed = getattr(verifier, "verifiers", None)
    reader = (
        routed.get(surface)
        if isinstance(routed, dict)
        else (verifier if not surface else None)
    )
    return reader if isinstance(reader, SqlRecordVerifier) else None


def _witness_attributed_additions(
    cfg: VerifyConfig, verifier: Any, frame: FrameVerdict, db: Optional[_DatabaseFrame]
) -> None:
    """Bind another actor's addition to one physical row in this read source.

    Audit identities need to be unique in the entire settled table, including
    unchanged rows and rows a surface query hides. Counts of visible records
    or audit entries cannot establish which physical row an actor inserted.
    """

    from openadapt_flow.runtime.effects.db_snapshot import unpack
    from openadapt_flow.runtime.effects.frame import AddedRowWitness

    spec = cfg.frame
    database = spec.database if spec is not None else None
    attribution = spec.attribution if spec is not None else None
    if (
        not frame.held
        or not any(effect.kind is EffectKind.EXACT_NEW_SET for effect in cfg.effects)
        or spec is None
        or database is None
        or attribution is None
        or attribution.kind != "audit_read"
        or not attribution.insert_events
        or not attribution.table_field
        or not _same_sql_source(attribution.read or {}, database.read)
        or db is None
        or db.post is None
        or db.old is None
    ):
        return
    counts: dict[str, dict[str, int]] = {}
    for change in frame.concurrent:
        name = change.surface
        surface = database.views.get(name)
        record_key = attribution.record_keys.get(name, attribution.record_key)
        if (
            change.change != "added"
            or change.attribution != "other"
            or surface is None
            or not record_key
            or any(side.surface == name for side in spec.side_records)
            or [t for t, v in database.views.items() if v == surface] != [name]
        ):
            continue
        source = (
            cfg.verifier.surfaces.get(surface)
            if cfg.verifier.surfaces
            else cfg.verifier
            if surface == ""
            else None
        )
        if source is None or not _same_sql_source(source.model_dump(), database.read):
            continue
        table = db.post.tables.get(name)
        old = db.old.tables.get(name)
        reader = _sql_reader(verifier, surface)
        if (
            table is None
            or old is None
            or not table.primary_key
            or table.non_transactional
            or record_key not in table.cell_columns
            or change.key in old.rows
            or reader is None
            or reader.source_relation is None
        ):
            continue
        relation = (
            ("main", name, 0)
            if database.dialect == "sqlite"
            else table.source_relation
            if database.dialect == "postgresql"
            else None
        )
        if relation is None or reader.source_relation != relation:
            continue
        record = table.values.get(change.key)
        if record is None or record.get(record_key) is None:
            continue
        if name not in counts:
            counts[name] = {}
            for _, cells in table.rows.values():
                identity = unpack(table.cell_columns, cells)[record_key]
                counts[name][identity] = counts[name].get(identity, 0) + 1
        identity = db.hashers[name](record_key, record[record_key])
        if counts[name].get(identity) != 1:
            continue
        frame.added_row_witnesses.setdefault(surface, []).append(
            AddedRowWitness(tuple(table.primary_key), dict(record), relation)
        )


def _excuse_attributed_additions(
    cfg: VerifyConfig,
    verifier: Any,
    before: EffectState,
    effects: Sequence[Effect],
    verdicts: list[EffectVerdict],
    rows: list[dict[str, Any]],
    frame: FrameVerdict,
) -> None:
    """Re-judge a refuted ``exact_new_set`` without records another user added.

    ``exact_new_set`` says the save added these records and nothing else. A
    record the frame's attribution names another actor for (a colleague's
    new patient in the same window) is not this save's addition. Only such
    attributed additions are set aside; an unattributed one still refutes.

    Only an insert event into the record's own table proves who created it:
    the frame must diff the database (``frame.database``) and read the log
    with ``insert_events`` and a ``table_field``. A colleague's later entry or last-modifier
    stamp on a record says who touched it, not who added it, so under any
    other attribution nothing is set aside.

    The settled snapshot must also witness the surface's physical row in
    the same SQL source, with an audit identity unique in the full table.
    """

    from openadapt_flow.runtime.effects._common import judge_records
    from openadapt_flow.runtime.effects.db_snapshot import canonical
    from openadapt_flow.runtime.effects.surfaces import surface_of

    if not frame.added_row_witnesses:
        return

    for i, (effect, verdict) in enumerate(zip(effects, verdicts)):
        if (
            effect.kind is not EffectKind.EXACT_NEW_SET
            or verdict.verdict is not Verdict.REFUTED
        ):
            continue
        try:
            state = verifier.capture_pre_state()
        except Exception:  # noqa: BLE001 - unreadable: the refutation stands
            continue
        surface = surface_of(effect) or ""
        routed = isinstance(getattr(verifier, "verifiers", None), dict)
        if routed and surface:
            post = (state.detail or {}).get(surface, {})
            pre = (before.detail or {}).get(surface, {})
            records = list(post.get("records", [])) if post.get("reachable") else None
            base = EffectState(
                substrate=str(pre.get("substrate", "")),
                reachable=bool(pre.get("reachable", False)),
                records=list(pre.get("records", [])),
            )
            name = surface
        else:
            records = list(state.records) if state.reachable else None
            base, name = before, ""
        if records is None:
            continue
        kept = list(records)
        for witness in frame.added_row_witnesses.get(name, ()):
            reader = _sql_reader(verifier, name)
            if reader is None or reader.source_relation != witness.source_relation:
                continue
            # The fresh surface read must still name this physical key and
            # match every returned column. Missing keys or changed
            # contents cannot borrow an earlier row's attribution.
            matches = []
            for j, record in enumerate(kept):
                if any(record.get(k) is None for k in witness.primary_key):
                    continue
                try:
                    if all(
                        k in witness.record
                        and canonical(value) == canonical(witness.record[k])
                        for k, value in record.items()
                    ):
                        matches.append(j)
                except (TypeError, ValueError):
                    continue
            if len(matches) == 1:
                kept.pop(matches[0])
        if len(kept) == len(records):
            continue
        again = judge_records(effect, base, kept, substrate=verdict.substrate)
        if again.verdict is Verdict.CONFIRMED:
            verdicts[i] = again
            rows[i]["verdict"] = _label(again.verdict)
            rows[i]["observed_effect"] = again.observed_effect
            rows[i]["observed_count"] = again.observed_count


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


def _build_verifier(
    cfg: VerifyConfig,
    params: Mapping[str, object],
    read_provider: Optional[CompleteReadProvider] = None,
) -> Any:
    try:
        verifier = (
            build_effect_verifier(cfg.verifier, params)
            if read_provider is None
            else read_provider.resolve(cfg.verifier, params)
        )
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


def _sqlite_insert(
    cfg: VerifyConfig,
    verifier: Any,
    params: Mapping[str, object],
    read_provider: Optional[CompleteReadProvider],
) -> Optional[OwnedSqliteInsert]:
    if cfg.owned_sqlite_insert is None or read_provider is not None:
        return None
    database = cfg.frame.database if cfg.frame is not None else None
    if database is not None and (
        database.dialect != "sqlite"
        or database.read.get("sqlite_database") != cfg.verifier.sqlite_database
    ):
        return None
    try:
        return OwnedSqliteInsert.bind(
            cfg.owned_sqlite_insert, cfg.verifier, verifier, cfg.effects, params
        )
    except (OSError, ValueError):
        return None


# -- baseline -----------------------------------------------------------------


def _surface_config(cfg: VerifyConfig, surface: str) -> EffectsConfig:
    if not surface and not cfg.verifier.surfaces:
        return cfg.verifier
    if surface not in cfg.verifier.surfaces:
        raise VerifyRunError("frame identity names an unavailable surface")
    return cfg.verifier.surfaces[surface]


def _available_surface_records(
    state: EffectState, surface: str
) -> Optional[list[dict[str, Any]]]:
    """Actual readable records, without asserting their scope is complete."""
    if not state.reachable:
        return None
    if not surface:
        return list(state.records)
    sub = state.detail.get(surface)
    if not isinstance(sub, dict) or sub.get("reachable") is not True:
        return None
    records = sub.get("records")
    if not isinstance(records, list) or any(not isinstance(r, dict) for r in records):
        return None
    return records


def _surface_records(state: EffectState, surface: str) -> list[dict[str, Any]]:
    if not surface:
        if not state.reachable or state.readback_complete is not True:
            raise VerifyRunError("frame identity needs a complete reachable read")
        return list(state.records)
    sub = state.detail.get(surface)
    if (
        not state.reachable
        or not isinstance(sub, dict)
        or sub.get("reachable") is not True
        or sub.get("readback_complete") is not True
    ):
        raise VerifyRunError("frame identity needs a complete reachable surface")
    records = sub.get("records")
    if not isinstance(records, list) or any(not isinstance(r, dict) for r in records):
        raise VerifyRunError("frame identity surface has no record list")
    return records


def learn_frame_identities(
    cfg: VerifyConfig,
    params: Mapping[str, object],
    *,
    read_provider: Optional[CompleteReadProvider] = None,
) -> VerifyConfig:
    """Compatibility API: learn row identity, retaining strict legacy columns."""
    return _learn_frame_correspondence(cfg, params, read_provider, fields=False)


def learn_frame_correspondence(
    cfg: VerifyConfig,
    params: Mapping[str, object],
    *,
    read_provider: Optional[CompleteReadProvider] = None,
) -> VerifyConfig:
    """Setup: learn row and field/column correspondence from the same reads."""
    return _learn_frame_correspondence(cfg, params, read_provider, fields=True)


def _learn_frame_correspondence(
    cfg: VerifyConfig,
    params: Mapping[str, object],
    read_provider: Optional[CompleteReadProvider],
    *,
    fields: bool,
) -> VerifyConfig:
    """Learn row correspondence during setup, before the contract is frozen.

    Uses the frame's own complete snapshot, not a supplied column descriptor.
    The same read-only client/provider used for selection can be retained.
    Returned data contains column names and witness counts, never raw rows.
    """
    from openadapt_flow.runtime.effects.db_snapshot import settled_state
    from openadapt_flow.runtime.effects.frame_identity import (
        FrameIdentityError,
        learn_field_bindings,
        learn_identity,
    )

    if cfg.frame is None or cfg.frame.database is None:
        raise VerifyRunError("identity learning needs a database frame")
    database = cfg.frame.database
    targets = {**database.views, **database.judged_by}
    if not targets or len(set(targets.values())) != len(targets):
        raise VerifyRunError("identity learning needs one target table per surface")
    salt = secrets.token_bytes(32)
    old = _read_database(cfg, salt, retain_values=tuple(targets))
    verifier = _build_verifier(cfg, params, read_provider)
    state = verifier.capture_pre_state()
    current = _read_database(cfg, salt, retain_values=tuple(targets))
    if old is None or current is None or not settled_state(old, current):
        raise VerifyRunError(
            "frame snapshot changed or was unavailable during identity setup"
        )
    if not isinstance(state, EffectState):
        raise VerifyRunError("identity setup returned no readable state")
    bindings = {}
    try:
        for table_name, surface in targets.items():
            table = current.tables.get(table_name)
            if (
                table is None
                or table.mode != "compare"
                or set(table.values) != set(table.rows)
            ):
                raise FrameIdentityError(
                    "identity learning needs every target-table row"
                )
            read = _surface_config(cfg, surface)
            # FHIR has a resource identity; other readers keep their declared
            # shared identity (or the ordinary default id).
            records = _surface_records(state, surface)
            identity = cfg.frame.identity_field or "id"
            if read.kind == "fhir" or any(identity not in record for record in records):
                identity = "id"  # The ordinary record reader's stable-id field.
            bindings[surface] = learn_identity(
                records,
                list(table.values.values()),
                surface_field=identity,
                table=table_name,
                read_config=read.model_dump(mode="json"),
            )
            if fields:
                binding = bindings[surface]
                bindings[surface] = binding.model_copy(
                    update={
                        "field_bindings": learn_field_bindings(
                            binding, records, list(table.values.values())
                        ),
                        "field_columns": sorted(table.columns),
                    }
                )
    except FrameIdentityError as exc:
        raise VerifyRunError(f"frame identity setup refused: {exc}") from exc
    frame = cfg.frame.model_copy(update={"identities": bindings})
    return cfg.model_copy(update={"frame": frame})


def _frame_identity_indexes(
    cfg: VerifyConfig, state: EffectState, snapshot: Any, salt: bytes
) -> dict[str, dict[str, str]]:
    from openadapt_flow.runtime.effects.frame_identity import (
        FrameIdentityError,
        match_records,
        read_config_digest,
        snapshot_index,
    )

    if cfg.frame is None:
        return {}
    indexes = {}
    try:
        for surface, binding in cfg.frame.identities.items():
            read = _surface_config(cfg, surface)
            database = cfg.frame.database
            if (
                database is None
                or {**database.views, **database.judged_by}.get(binding.table)
                != surface
            ):
                raise FrameIdentityError(
                    "identity binding does not match its frame route"
                )
            if binding.read_config_sha256 != read_config_digest(
                read.model_dump(mode="json")
            ):
                raise FrameIdentityError(
                    "identity binding belongs to another read configuration"
                )
            table = snapshot.tables.get(binding.table) if snapshot is not None else None
            if binding.field_bindings is not None and (
                table is None or sorted(table.columns) != binding.field_columns
            ):
                raise FrameIdentityError("stored-column inventory no longer matches")
            index = snapshot_index(binding, table, _mac_key(salt))
            match_records(
                binding, _surface_records(state, surface), index, _mac_key(salt)
            )
            indexes[surface] = index
        # A nonempty selected read must expose its declared legacy identity.
        # Empty create baselines do not invent correspondence evidence.
        reads = cfg.verifier.surfaces or {"": cfg.verifier}
        for surface, read in reads.items():
            identity = cfg.frame.identity_field
            if (
                read.kind in {"fhir", "rest", "sql"}
                and surface not in cfg.frame.identities
                and identity
            ):
                records = _available_surface_records(state, surface)
                if records is None:
                    # Preserve the existing unreadable-baseline/budget path.
                    # Available identity is not a new completeness assertion.
                    continue
                if any(identity not in record for record in records):
                    raise FrameIdentityError(
                        "frame identity is unavailable on the selected read; learn its row mapping at setup"
                    )
                from openadapt_flow.runtime.effects.frame_identity import (
                    canonical_identity,
                )

                values = [
                    canonical_identity(record.get(identity), "exact")
                    for record in records
                ]
                if len(set(values)) != len(values):
                    raise FrameIdentityError(
                        "frame identity is not unique on the selected read"
                    )
    except FrameIdentityError as exc:
        raise VerifyRunError(f"frame identity setup refused: {exc}") from exc
    return indexes


def capture_baseline(
    cfg: VerifyConfig,
    params: Mapping[str, object],
    out: Path,
    *,
    read_provider: Optional[CompleteReadProvider] = None,
    purpose: VerificationPurpose = "observation",
    _owned_dispatch_owner: Optional[str] = None,
) -> dict[str, Any]:
    """Read the system of record before the executor starts.

    The baseline is what lets ``count_new_only`` and ``exact_new_set`` tell a
    record the executor added from one that was already there. It holds raw
    records and the run salt, so it stays private on this machine: mode 0600,
    never inside a receipt. Only its salted digest is bound. It also starts
    the run: ``execution_id`` is generated here and the read step reuses it.
    ``owned_attempt`` also reserves any configured native commit witness.
    """

    purpose_fields: dict[str, str] = {"purpose": purpose}
    if _owned_dispatch_owner is not None:
        purpose_fields[_OWNED_DISPATCH_OWNER] = _owned_dispatch_owner
    _baseline_purpose(purpose_fields)
    verifier = _build_verifier(cfg, params, read_provider)
    identity = verifier_identity(verifier)
    execution_id = _new_execution_id()
    _tag_reads(verifier, execution_id)
    salt = secrets.token_bytes(32)
    insert = None
    if purpose == "owned_attempt" and cfg.owned_sqlite_insert is not None:
        insert = _sqlite_insert(cfg, verifier, params, read_provider)
        if insert is None:
            raise VerifyRunError("the configured owned SQLite insert is unsupported")
    history_before = None
    history_error = None
    if cfg.frame is not None and cfg.frame.history is not None:
        try:
            history_before, _history_rows = _read_history(cfg, params, _now(), salt)
        except Exception as exc:
            history_error = type(exc).__name__
    # The versions first: a save between this read and the record read is
    # then in the record baseline too, never hidden in the versions one.
    versions_rows = (
        _read_versions(cfg, params)
        if cfg.frame is not None and cfg.frame.versions is not None
        else None
    )
    state = verifier.capture_pre_state()
    if not isinstance(state, EffectState):
        raise VerifyRunError("verifier returned no readable baseline")
    if insert is not None:
        if (
            not state.reachable
            or state.readback_complete is not True
            or verifier.source_relation != ("main", insert.table, 0)
        ):
            raise VerifyRunError("the owned SQLite reader did not observe the declared table")
        try:
            insert.check_baseline(state.records)
        except (OSError, ValueError, sqlite3.Error) as exc:
            raise VerifyRunError("the owned SQLite insert baseline could not be reserved") from exc
    detail: dict[str, Any] = {}
    if cfg.frame is not None and cfg.frame.history is not None:
        from openadapt_flow.runtime.effects.history import DETAIL

        detail[DETAIL] = (
            history_before if history_before is not None else {"error": history_error}
        )
    entries: Optional[list[dict[str, Any]]] = None
    snapshot = None
    if cfg.frame is not None and cfg.frame.database is not None:
        why: list[str] = []
        snapshot = _read_database(cfg, salt, errors=why)
        if snapshot is not None:
            detail[FRAME_DB_DETAIL] = snapshot.to_json()
            entries = snapshot.extra.get("audit")
        else:
            detail[FRAME_DB_ERROR] = why[0] if why else "not read"
    audit_verifier = _audit_verifier(cfg, params)
    if audit_verifier is not None:
        if entries is None and (
            cfg.frame is None or cfg.frame.database is None or snapshot is not None
        ):
            # A failed database read must not retry its audit query outside
            # the snapshot's budget and consistent transaction.
            entries = _read_audit(audit_verifier)
        # Which entries existed, as keyed hashes: never what they say.
        detail[FRAME_AUDIT_DETAIL] = {
            "reachable": entries is not None,
            "ids": _audit_ids(entries or [], salt),
        }
    if cfg.frame is not None and cfg.frame.versions is not None:
        from openadapt_flow.runtime.effects.versions import seal_versions

        detail[FRAME_VERSIONS_DETAIL] = seal_versions(
            cfg.frame.versions, versions_rows, _mac_key(salt)
        )
    if detail:
        state = state.model_copy(update={"detail": {**state.detail, **detail}})
    indexes = _frame_identity_indexes(cfg, state, snapshot, salt)
    if indexes:
        state = state.model_copy(
            update={
                "detail": {
                    **state.detail,
                    FRAME_IDENTITY_DETAIL: _mapped_baseline_evidence(
                        cfg, state, indexes, params, salt
                    ),
                }
            }
        )
    state = _seal_state(cfg, state, salt)
    payload = {
        "schema_version": BASELINE_SCHEMA,
        "execution_id": execution_id,
        "baseline_read_at": _now(),
        "verifier_identity": identity,
        "state": state.model_dump(mode="json"),
        "salt": salt.hex(),
    }
    if purpose == "owned_attempt":
        payload.update(purpose_fields)
    if insert is not None:
        if not state.reachable or _sqlite_insert(cfg, verifier, params, read_provider) != insert:
            raise VerifyRunError("the owned SQLite baseline could not be bound")
        payload[_SQLITE_RESERVATION] = insert.reservation(
            execution_id, salt, _owned_dispatch_owner
        )
    _write_private(Path(out), payload)
    return payload


def _seal_state(cfg: VerifyConfig, state: EffectState, salt: bytes) -> EffectState:
    """The read set as keyed MACs: the baseline holds no record values."""

    from openadapt_flow.runtime.effects.sealed import seal

    identity = cfg.frame.identity_field if cfg.frame is not None else None
    key = _mac_key(salt)
    identities = cfg.frame.identities if cfg.frame is not None else {}
    own_identity = identities[""].surface_field if "" in identities else identity
    sealed = {"": seal(state.records, key, own_identity)}
    detail = dict(state.detail or {})
    for name, sub in (state.detail or {}).items():
        if name.startswith("openadapt:") or not isinstance(sub, dict):
            continue
        if isinstance(sub.get("records"), list):
            field = identities[name].surface_field if name in identities else identity
            sealed[name] = seal(sub["records"], key, field, name)
            detail[name] = {**sub, "records": []}
    detail[SEALED_DETAIL] = sealed
    return state.model_copy(update={"records": [], "detail": detail})


def _unseal_state(
    cfg: VerifyConfig, before: EffectState, verifier: Any, salt: bytes
) -> EffectState:
    """Rebuild a sealed baseline's records from one read now."""

    from openadapt_flow.runtime.effects.sealed import unseal

    sealed = (before.detail or {}).get(SEALED_DETAIL)
    if not isinstance(sealed, dict):
        return before  # an older baseline that kept the records
    try:
        now = verifier.capture_pre_state()
    except Exception:  # noqa: BLE001 - unreadable: nothing can be rebuilt
        now = None
    if not isinstance(now, EffectState) or not now.reachable:
        return before.model_copy(update={"reachable": False})
    identity = cfg.frame.identity_field if cfg.frame is not None else None
    key = _mac_key(salt)
    detail = dict(before.detail)
    identities = cfg.frame.identities if cfg.frame is not None else {}
    for name, entries in sealed.items():
        if not name:
            continue
        current = (now.detail or {}).get(name, {})
        if not (isinstance(current, dict) and current.get("reachable")):
            detail[name] = {**detail.get(name, {}), "reachable": False}
            continue
        detail[name] = {
            **detail.get(name, {}),
            "records": unseal(
                entries,
                list(current.get("records", [])),
                key,
                identities[name].surface_field if name in identities else identity,
                name,
            ),
        }
    return before.model_copy(
        update={
            "records": unseal(
                sealed.get("", []),
                list(now.records),
                key,
                identities[""].surface_field if "" in identities else identity,
            ),
            "detail": detail,
        }
    )


def _baseline_purpose(baseline: Mapping[str, Any]) -> VerificationPurpose:
    purpose = baseline.get("purpose", "observation")
    if type(purpose) is not str or purpose not in ("observation", "owned_attempt"):
        raise VerifyRunError("invalid verification purpose")
    if _OWNED_DISPATCH_OWNER in baseline:
        owner = baseline[_OWNED_DISPATCH_OWNER]
        if purpose != "owned_attempt" or type(owner) is not str or not _OPAQUE.fullmatch(owner):
            raise VerifyRunError("invalid owned dispatch owner")
    if _SQLITE_RESERVATION in baseline:
        reservation = baseline[_SQLITE_RESERVATION]
        if (
            purpose != "owned_attempt"
            or type(reservation) is not str
            or re.fullmatch(r"[0-9a-f]{64}", reservation) is None
        ):
            raise VerifyRunError("invalid owned SQLite reservation")
    return cast(VerificationPurpose, purpose)


def _bound_baseline(baseline: Mapping[str, Any]) -> dict[str, Any]:
    _baseline_purpose(baseline)
    keys = _BASELINE_BOUND_KEYS + tuple(
        name for name in ("purpose", _SQLITE_RESERVATION, _OWNED_DISPATCH_OWNER)
        if name in baseline
    )
    return {key: baseline[key] for key in keys}


def baseline_digest(baseline: Mapping[str, Any], salt: bytes) -> str:
    """Salted digest of the baseline, including any recorded write purpose."""

    return salted_digest(salt, "baseline", _bound_baseline(baseline))


def _used_marker(path: Path) -> Path:
    return path.with_name(path.name + ".used")


def _load_baseline(path: Path, identity: str) -> tuple[dict[str, Any], bytes]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise VerifyRunError(f"cannot read baseline {path}: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != BASELINE_SCHEMA:
        raise VerifyRunError(f"{path} is not an {BASELINE_SCHEMA} file")
    required = set(BASELINE_KEYS)
    if not required <= set(raw) <= required | {
        "purpose", _SQLITE_RESERVATION, _OWNED_DISPATCH_OWNER
    }:
        # Every field must be one the digest names; nothing rides along unbound.
        raise VerifyRunError("baseline file has missing or unknown fields")
    _baseline_purpose(raw)
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


def _owned_baseline_reference(
    baseline: Mapping[str, Any], salt: bytes
) -> Optional[OwnedBaselineReference]:
    owner = baseline.get(_OWNED_DISPATCH_OWNER)
    if owner is None:
        return None
    return owner, baseline["execution_id"], baseline_digest(baseline, salt)


def _claim_owned_dispatch(path: Path) -> bool:
    # One atomic claim closes this baseline to all later native dispatches.
    # If the writer won, a concurrent read still cannot assert no dispatch.
    original = path.resolve(strict=True)
    claim = original.with_name(original.name + ".dispatched")
    try:
        fd = os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    os.close(fd)
    return True


def commit_owned_sqlite_insert(
    cfg: VerifyConfig, params: Mapping[str, object], baseline_path: Path
) -> None:
    """Actuate the exact insert reserved by an owned SQLite baseline.

    The operator controls the writer and the private baseline. This helper
    executes one native transaction. It accepts no SQL, row or attempt ID from
    the actuator, and never records a witness for an already present row.
    Ledger-owned baselines require the verifier's live pending-write route.
    """

    _commit_owned_sqlite_insert(cfg, params, baseline_path, None)


def _commit_owned_sqlite_insert(
    cfg: VerifyConfig,
    params: Mapping[str, object],
    baseline_path: Path,
    owner: Optional[OwnedBaselineReference],
) -> None:
    verifier = _build_verifier(cfg, params)
    path = Path(baseline_path)
    baseline, salt = _load_baseline(path, verifier_identity(verifier))
    if _owned_baseline_reference(baseline, salt) != owner:
        raise VerifyRunError("the owned SQLite baseline does not belong to this dispatch")
    insert = _reserved_sqlite_insert(cfg, verifier, params, None, baseline, salt)
    reservation = baseline.get(_SQLITE_RESERVATION)
    execution_id = baseline["execution_id"]
    elapsed = (_parse_time(_now()) - _parse_time(baseline["baseline_read_at"])).total_seconds()
    if (
        _baseline_purpose(baseline) != "owned_attempt"
        or insert is None
        or not isinstance(reservation, str)
        or _used_marker(path).exists()
        or not 0 <= elapsed <= cfg.max_execution_window_s
    ):
        raise VerifyRunError("the owned SQLite insert does not match an active reservation")
    try:
        if not _claim_owned_dispatch(path):
            raise VerifyRunError("the owned SQLite insert was already claimed")
        if _used_marker(path).exists():
            raise VerifyRunError("the owned SQLite reservation was already checked")
        insert.commit(execution_id, reservation, salt)
    except (OSError, ValueError, sqlite3.Error) as exc:
        raise VerifyRunError("the owned SQLite insert could not be committed") from exc


def _reserved_sqlite_insert(
    cfg: VerifyConfig,
    verifier: Any,
    params: Mapping[str, object],
    read_provider: Optional[CompleteReadProvider],
    baseline: Mapping[str, Any],
    salt: bytes,
) -> Optional[OwnedSqliteInsert]:
    insert = _sqlite_insert(cfg, verifier, params, read_provider)
    reservation = baseline.get(_SQLITE_RESERVATION)
    if insert is None or not isinstance(reservation, str) or not hmac.compare_digest(
        reservation,
        insert.reservation(baseline["execution_id"], salt, baseline.get(_OWNED_DISPATCH_OWNER)),
    ):
        return None
    return insert


def _observed_owned_insert(
    cfg: VerifyConfig,
    params: Mapping[str, object],
    baseline: Optional[Mapping[str, Any]],
    salt: bytes,
    verifier: Any,
    read_provider: Optional[CompleteReadProvider],
    verdicts: Sequence[EffectVerdict],
) -> bool:
    if baseline is None or _SQLITE_RESERVATION not in baseline:
        return False
    insert = _reserved_sqlite_insert(cfg, verifier, params, read_provider, baseline, salt)
    if insert is None or len(verdicts) != 1:
        return False
    matches = verdicts[0].matched_records
    reservation = baseline[_SQLITE_RESERVATION]
    attempt = baseline["execution_id"]
    if len(matches) != 1 or not insert.matches(matches[0]):
        return False
    try:
        return insert.independently_observed(attempt, reservation, salt)
    except (OSError, ValueError, sqlite3.Error):
        return False


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
    post_state_out: Optional[dict[str, Any]] = None,
    read_provider: Optional[CompleteReadProvider] = None,
    request_context: Optional[RequestContext] = None,
    purpose: Optional[VerificationPurpose] = None,
    _owned_reference: Optional[OwnedBaselineReference] = None,
) -> dict[str, Any]:
    """Read the system of record and return ``{"receipt", "evidence"}``.

    The run record contributes ``executor_kind``, a digest of
    ``executor_id``, a digest of ``executor_build``, and a salted digest of
    itself. No other field of it is read. ``verified`` requires every effect
    CONFIRMED by a system-of-record read at or above
    ``minimum_effect_strength``; anything weaker is never upgraded.

    ``request_context`` is an explicit producer correlation context, separate
    from ``execution_id``. It does not authenticate a caller. An ambient
    wrapper scope alone does not bind direct verifier or canary receipts.

    With ``baseline_path`` the run continues the one the baseline started
    (its ``execution_id`` and salt) and the baseline is consumed. Without one,
    a new salt is drawn and written to ``private_dir/effect-run.private.json``
    (mode 0600), so ``private_dir`` is required.

    ``write_attempted`` says the caller knows the save was dispatched (the
    guarded save sets it after its commit step). Then a store that shows
    nothing is ``reconciliation_required``, not ``halted_before_effect``: the
    write may still land, so it must not be retried.

    ``purpose`` inherits the captured baseline's purpose when omitted.
    Observation reports the state the reader saw. An owned attempt needs
    independent commit evidence to credit that state to this attempt.

    With ``frame.database.recheck_after_s``, a verified run's evidence says a
    recheck is due (``evidence.frame.recheck``), and ``post_state_out`` (a
    dict the caller passes) receives what the recheck compares with: the
    settled post-save snapshot as keyed MACs. It is private, like the
    baseline (``runtime/effects/late_write.py``, :func:`recheck_run`).
    """

    from openadapt_types.execute import (
        EffectStrengthV1,
        ExecuteEvidenceContractV1,
        ExecuteEvidenceReceiptV1,
        ExecuteTerminalOutcomeV1,
    )

    if not isinstance(run_record, Mapping):
        raise VerifyRunError("run record must be a JSON object")
    if request_context is not None:
        from openadapt_flow.request_binding import BindingError

        try:
            if type(request_context) is not RequestContext:
                raise BindingError("request_context_type")
            request_context = RequestContext.parse(request_context.to_dict())
        except BindingError:
            raise VerifyRunError(
                "invalid request context", validation_reason="request_context"
            ) from None
    if not isinstance(authorization_ref, str) or not authorization_ref:
        raise VerifyRunError("an authorization reference is required")
    remediates = _opaque_or_none("remediates", remediates)
    correlation_id = _opaque_or_none("correlation_id", correlation_id)
    approver_doc = _approver(approver)
    executor_kind, executor_ref, build_digest = _executor(run_record)

    verifier = _build_verifier(cfg, params, read_provider)
    identity = verifier_identity(verifier)

    baseline: Optional[dict[str, Any]] = None
    if baseline_path is not None:
        baseline, salt = _load_baseline(Path(baseline_path), identity)
        recorded_purpose = _baseline_purpose(baseline)
        if purpose is not None and _baseline_purpose({"purpose": purpose}) != recorded_purpose:
            raise VerifyRunError("verification purpose differs from the baseline")
        purpose = recorded_purpose
        if _owned_baseline_reference(baseline, salt) != _owned_reference:
            raise VerifyRunError("the baseline does not match the original owned attempt")
        execution_id = str(baseline["execution_id"])
        if purpose == "owned_attempt":
            try:
                _claim_owned_dispatch(Path(baseline_path))
            except OSError as exc:
                raise VerifyRunError("the owned dispatch claim could not be checked") from exc
        _consume_baseline(Path(baseline_path), execution_id)
    else:
        if _owned_reference is not None:
            raise VerifyRunError("an owned ledger reference requires its baseline")
        purpose = _baseline_purpose({"purpose": "observation" if purpose is None else purpose})
        if private_dir is None:
            raise VerifyRunError(
                "without a baseline, verify-run needs a private directory for "
                "the run salt"
            )
        salt = secrets.token_bytes(32)
        execution_id = _new_execution_id()
        run_file = {
            "schema_version": RUN_FILE_SCHEMA,
            "execution_id": execution_id,
            "salt": salt.hex(),
        }
        if purpose == "owned_attempt":
            run_file["purpose"] = purpose
        _write_private(Path(private_dir) / "effect-run.private.json", run_file)
    _tag_reads(verifier, execution_id)

    history_checkpoint = None
    history_problem = None
    history_spec = cfg.frame.history if cfg.frame is not None else None
    if history_spec is not None:
        try:
            if baseline is None:
                raise ValueError("history requires a baseline")
            from openadapt_flow.runtime.effects.history import (
                DETAIL,
                checkpoint_problem,
            )

            stored_history = baseline["state"]["detail"].get(DETAIL)
            if checkpoint_problem(stored_history, history_spec) is not None:
                raise ValueError("history baseline is unavailable")
            history_checkpoint, _history_rows = _read_history(
                cfg, params, stored_history["since"], salt
            )
        except Exception as exc:  # unreadable or incomplete history never confirms
            history_problem = (
                f"history checkpoint could not be read ({type(exc).__name__})"
            )

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
            before = _unseal_state(cfg, before, verifier, salt)
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
    judged_effects: list[Effect] = []
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
        judged_effects.append(judged)
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
        frame = _judge_versions(
            cfg, frame, before, bound_effects, verdicts, params, salt
        )
    else:
        frame = FrameVerdict(
            status="not_checked",
            reason="no baseline, so what else changed can't be known",
        )
    if frame.concurrent and any(c.change == "added" for c in frame.concurrent):
        _excuse_attributed_additions(
            cfg, verifier, before, judged_effects, verdicts, rows, frame
        )
        delta_verdict = _aggregate(verdicts)
        observed_effect = _observed(verdicts)
    if history_spec is not None:
        if history_problem is not None and frame.status != "refuted":
            frame.status = "not_checked"
            frame.reason = history_problem
        elif history_checkpoint is not None:
            from openadapt_flow.runtime.effects.history import judge_initial_history
            from openadapt_flow.runtime.effects.surfaces import surface_of

            targets: set[tuple[str, str]] = set()
            for effect, verdict in zip(bound_effects, verdicts):
                if effect.kind not in (
                    EffectKind.RECORD_WRITTEN,
                    EffectKind.FIELD_EQUALS,
                    EffectKind.EXACT_NEW_SET,
                ):
                    continue
                scope = history_spec.surfaces[surface_of(effect) or ""]
                for target_record in verdict.matched_records:
                    target_identity = target_record.get(
                        frame_spec.identity_field or "id"
                    )
                    if target_identity not in (None, ""):
                        targets.add((scope, str(target_identity)))
            history_frame = judge_initial_history(
                history_spec,
                stored_history,
                history_checkpoint,
                _history_rows,
                params,
                str(run_record.get("executor_id") or ""),
                targets,
            )
            frame.collateral.extend(history_frame.collateral)
            frame.concurrent.extend(history_frame.concurrent)
            if history_frame.status == "refuted" or (
                history_frame.status == "not_checked" and frame.status == "held"
            ):
                frame.status = history_frame.status
                frame.reason = history_frame.reason
            frame.post_state = {"read_at": _now(), "history": history_checkpoint}
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
    read_proves_delta = (
        delta_verdict == "CONFIRMED"
        and verification_tier == "independent_readback"
        and strong_enough
        and oracle_tier in (2, 3)
    )
    attributed = (
        purpose == "owned_attempt"
        and read_proves_delta
        and _observed_owned_insert(cfg, params, baseline, salt, verifier, read_provider, verdicts)
    )
    status = "attributed" if attributed else "unattributed"
    if purpose == "observation":
        status = "observed"
    write_attribution = {"purpose": purpose, "status": status}
    read_proves_write = read_proves_delta and (purpose == "observation" or attributed)
    if purpose == "owned_attempt" and not attributed and (
        effect_verdict == "CONFIRMED"
        or (observed_effect == "absent" and frame.status != "refuted")
    ):
        # An absent read cannot settle an owned attempt whose dispatch may land.
        effect_verdict = "INDETERMINATE"
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
        read_proves_delta,
    )
    if purpose == "owned_attempt" and source_status == "refuted":
        effect_verdict = "REFUTED"
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
        and purpose == "observation"
        and frame.status != "refuted"
    ):
        # The store shows nothing landed, whatever the executor said.
        outcome = ExecuteTerminalOutcomeV1.HALTED_BEFORE_EFFECT
    else:
        # A wrong effect, an unreadable store, or a read too weak to certify.
        outcome = ExecuteTerminalOutcomeV1.RECONCILIATION_REQUIRED
        delivery_uncertain = True
    verified = outcome is ExecuteTerminalOutcomeV1.VERIFIED
    landed = read_proves_write and (purpose == "observation" or effect_verdict != "REFUTED")
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
        _WRITE_ATTRIBUTION: write_attribution,
    }
    database_spec = frame_spec.database
    recheck_after_s = (
        frame_spec.history.quiet_s
        if frame_spec.history is not None
        else database_spec.recheck_after_s
        if database_spec is not None
        else None
    )
    recheck_due_at: Optional[str] = None
    if frame.post_state is not None and recheck_after_s is not None:
        # A write that lands after the settled read lands after this verdict:
        # say when the database is read again (a verified write only), and
        # bind what it compares with. A write of any outcome binds its
        # post-save state, so a recheck of an earlier write can tell what
        # this one's own window covered.
        schedule = late_write.schedule(
            frame.post_state,
            recheck_after_s,
            salted_digest(salt, late_write.POST_STATE_LABEL, frame.post_state),
            due=verified,
        )
        cast(dict[str, Any], evidence["frame"])[late_write.EVIDENCE_KEY] = schedule
        recheck_due_at = schedule["due_at"]
        if post_state_out is not None:
            post_state_out.update(frame.post_state)
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
    if request_context is not None:
        evidence["request_context"] = request_context.to_dict()
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
    receipt[_WRITE_ATTRIBUTION] = dict(write_attribution)
    if remediates is not None:
        receipt["remediates"] = remediates
    if correlation_id is not None:
        receipt["correlation_id"] = correlation_id
    if recheck_due_at is not None:
        receipt[RECHECK_RECEIPT_FIELD] = recheck_due_at
    if request_context is not None:
        receipt["request_context"] = request_context.to_dict()
    problem = receipt_problem(receipt) or evidence_problem(evidence, receipt)
    if problem is not None:  # pragma: no cover - a producer bug, never input
        raise VerifyRunError(
            f"verify-run built an invalid receipt: {problem}",
            validation_reason=problem,
        )
    return {
        "schema_version": EFFECT_RECEIPT_SCHEMA,
        "receipt": receipt,
        "evidence": evidence,
    }


# -- late writes ----------------------------------------------------------------


def recheck_state(
    baseline: Mapping[str, Any],
    result: Mapping[str, Any],
    post_state: Mapping[str, Any],
) -> Optional[dict[str, Any]]:
    """The private recheck state of a run, or None when its evidence binds
    no post-save state (the contract sets no ``recheck_after_s``, or the
    database didn't settle after the save). ``baseline`` is the payload
    :func:`capture_baseline` wrote, ``result`` what :func:`verify_run`
    returned, ``post_state`` its ``post_state_out``.

    A verified run's state is what its recheck compares with. Any run's
    state can excuse its own window in an earlier write's recheck."""

    evidence = dict(result["evidence"])
    if not post_state or late_write.EVIDENCE_KEY not in (evidence.get("frame") or {}):
        return None
    return {
        "schema_version": late_write.RECHECK_SCHEMA,
        "execution_id": str(baseline["execution_id"]),
        "salt": str(baseline["salt"]),
        "verifier_identity": str(baseline["verifier_identity"]),
        "baseline": _bound_baseline(baseline),
        "receipt": dict(result["receipt"]),
        "evidence": evidence,
        "post_state": dict(post_state),
    }


def recheck_problem(
    cfg: VerifyConfig,
    state: Any,
    *,
    params: Mapping[str, object],
    run_record: Mapping[str, Any],
    verified: bool = True,
    read_provider: Optional[CompleteReadProvider] = None,
) -> Optional[str]:
    """A plain sentence when ``state`` can't be used as this contract's
    write by this executor with these params; None when it can.
    ``verified``: the write must also have been verified (the write being
    rechecked; a later write that excuses its window may have any outcome).

    Everything the recheck relies on is bound to the receipt: the receipt
    and its evidence agree, the salt is the one the evidence commits to, the
    baseline and the post-save state are the ones their digests name, and
    the contract, the read, the executor and each bound parameter are the
    ones the receipt was issued for.
    """

    problem = late_write.state_problem(state)
    if problem is not None:
        return problem
    history_spec = cfg.frame.history if cfg.frame is not None else None
    if history_spec is not None:
        from openadapt_flow.runtime.effects.history import (
            DETAIL,
            checkpoint_pair_problem,
        )

        baseline_history = (
            (state["baseline"].get("state") or {}).get("detail") or {}
        ).get(DETAIL)
        post_history = state["post_state"].get("history")
        problem = checkpoint_pair_problem(baseline_history, post_history, history_spec)
        if problem is not None:
            return problem
    receipt, evidence = dict(state["receipt"]), dict(state["evidence"])
    if receipt_problem(receipt) or evidence_problem(evidence, receipt):
        return "the recheck state's receipt and evidence don't match"
    if verified and receipt.get("outcome") != "verified":
        return "the write was not verified, so there is nothing to recheck"
    if receipt.get("execution_id") != state["execution_id"]:
        return "the recheck state belongs to another run"
    salt = bytes.fromhex(str(state["salt"]))
    if (evidence.get("salting") or {}).get("salt_commitment") != salt_commitment(salt):
        return "the recheck state's salt is not the one its receipt commits to"
    readback = evidence.get("readback") or {}
    baseline = dict(state["baseline"])
    try:
        baseline_matches = (
            set(baseline) == set(_bound_baseline(baseline))
            and readback.get("baseline_sha256") == baseline_digest(baseline, salt)
        )
        purpose_matches = _baseline_purpose(baseline) == (
            receipt.get(_WRITE_ATTRIBUTION, {}).get("purpose", "observation")
        )
    except (KeyError, VerifyRunError):
        baseline_matches = purpose_matches = False
    if not baseline_matches or not purpose_matches:
        return "the recheck state's baseline is not the one its receipt names"
    frame = evidence.get("frame") or {}
    schedule = frame.get(late_write.EVIDENCE_KEY)
    database = cfg.frame.database if cfg.frame is not None else None
    history = cfg.frame.history if cfg.frame is not None else None
    after_s = (
        history.quiet_s
        if history is not None
        else database.recheck_after_s
        if database is not None
        else None
    )
    if not isinstance(schedule, Mapping) or after_s is None:
        return "the receipt binds no post-save state"
    post = dict(state["post_state"])
    digest = salted_digest(salt, late_write.POST_STATE_LABEL, post)
    if schedule.get("post_state_sha256") != digest:
        return "the recheck state is not the post-save state its receipt names"
    is_verified = receipt.get("outcome") == "verified"
    if dict(schedule) != late_write.schedule(post, after_s, digest, due=is_verified):
        return (
            "the contract's recheck_after_s is not the one the receipt was issued under"
        )
    try:
        verifier = _build_verifier(cfg, params, read_provider)
        identity = verifier_identity(verifier)
        executor_kind, executor_ref, _build = _executor(run_record)
    except (VerifyRunError, KeyError, ValueError) as exc:
        return f"the contract can't be bound for the recheck: {exc}"
    attributed = receipt.get(_WRITE_ATTRIBUTION) == {
        "purpose": "owned_attempt", "status": "attributed"
    }
    if attributed and _reserved_sqlite_insert(
        cfg, verifier, params, read_provider, baseline, salt
    ) is None:
        return "the current store is not the one reserved for this owned write"
    if (
        identity != state["verifier_identity"]
        or identity != readback.get("verifier_identity")
        or identity != baseline.get("verifier_identity")
    ):
        return "the recheck state was taken by a different verifier"
    contract = evidence.get("contract") or {}
    if (
        contract.get("contract_sha256")
        != verify_config_sha256(identity, list(cfg.effects))
        or frame.get("frame_sha256") != (cfg.frame or FrameSpec()).digest()
    ):
        return "the recheck state was taken under a different contract"
    if (
        receipt.get("executor_kind") != executor_kind
        or receipt.get("executor_ref_sha256") != executor_ref
    ):
        return "the recheck state's write was made by a different actor"
    rows = readback.get("effects") or []
    if len(rows) != len(cfg.effects):
        return "the recheck state was taken under a different contract"
    for effect, row in zip(cfg.effects, rows):
        try:
            salted = {
                name: _salted_hex(salt, f"effect_param:{name}", params[name])
                for name in sorted(effect.referenced_params())
            }
            effect_digest = effect.resolved_contract_hash(
                {}, opaque_param_sha256=salted
            )
        except (KeyError, ValueError):
            return "the params don't bind the contract"
        if (
            not isinstance(row, Mapping)
            or row.get("effect_contract_hash") != effect_digest
        ):
            return "the params are not the ones the write was verified with"
    return None


def _entry_id(entry: Mapping[str, Any], key: bytes) -> str:
    from openadapt_flow.runtime.effects.effect import stable_id

    return hmac.new(
        key, repr(stable_id(dict(entry))).encode(), hashlib.sha256
    ).hexdigest()


def _later_window(state: Mapping[str, Any]) -> Optional[Any]:
    """A later write's own window, when its frame judged it (held or
    refuted); None when its frame wasn't checked, so nothing in its window
    was reported and nothing there is excused."""

    evidence = state["evidence"]
    if evidence.get(_WRITE_ATTRIBUTION) == {"purpose": "owned_attempt", "status": "unattributed"}:
        return None
    frame = (evidence.get("frame") or {}).get("status")
    if frame not in ("held", "refuted"):
        return None
    record = state["post_state"].get("collateral")
    if not isinstance(record, Mapping):
        return None
    detail = (state["baseline"].get("state") or {}).get("detail") or {}
    snapshot = detail.get(FRAME_DB_DETAIL)
    if not isinstance(snapshot, dict):
        return None
    post = state["post_state"]
    before_audit = detail.get(FRAME_AUDIT_DETAIL)
    after_audit = post.get("audit")
    readable = bool(
        isinstance(before_audit, Mapping)
        and before_audit.get("reachable")
        and isinstance(after_audit, Mapping)
        and after_audit.get("reachable")
    )
    ids_before = set(before_audit["ids"]) if readable and before_audit else None
    ids_after = set(after_audit["ids"]) if readable and after_audit else None
    try:
        return late_write.LaterWindow(
            _mac_key(bytes.fromhex(str(state["salt"]))),
            snapshot,
            post["snapshot"],
            verified=state["receipt"].get("outcome") == "verified",
            delta_held=(evidence.get("checked") or {}).get("delta") == "held",
            collateral_rows={(str(t), str(k)) for t, k in record.get("rows") or []},
            collateral_entries={str(h) for h in record.get("entries") or []},
            unmapped=bool(record.get("unmapped", True)),
            audit_before=ids_before,
            audit_after=ids_after,
            entry_id=_entry_id,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _read_history(
    cfg: VerifyConfig, params: Mapping[str, object], since: str, salt: bytes
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    from openadapt_flow.runtime.effects.history import read_history

    if cfg.frame is None or cfg.frame.history is None:
        raise VerifyRunError("no history feeds are configured")

    def build(read: dict[str, Any]) -> Any:
        verifier = build_effect_verifier(EffectsConfig.model_validate(read), params)
        if verifier is None:
            raise VerifyRunError("history built no read-only verifier")
        return verifier

    return read_history(cfg.frame.history, since, build, _mac_key(salt))


def recheck_run(
    cfg: VerifyConfig,
    state: Mapping[str, Any],
    *,
    params: Mapping[str, object],
    run_record: Mapping[str, Any],
    excused: Sequence[tuple[Mapping[str, Any], Mapping[str, object]]] = (),
    read_provider: Optional[CompleteReadProvider] = None,
) -> tuple[FrameVerdict, str]:
    """Judge every change since a verified write's post-save read.

    ``state`` must have passed :func:`recheck_problem`. ``excused``: the
    recheck states and params of later writes under this contract by this
    executor, of any outcome, each begun after ``state``'s post-save read
    (each is checked here as ``state`` was). Where a later write's frame
    was judged (held or refuted), each row and audit entry that changed in
    its own window, between its baseline and its post-save read, and still
    holds what that read saw, is accounted for by it
    (``late_write.LaterWindow``): its declared effects and side rows as they
    landed then, never a row matching its selector that lands later. A
    change that may be what refuted that later write is ``disputed``: the
    verdict's ``excused_by`` then holds ``LATER_WRITE_DISPUTED``. A later
    write whose frame wasn't judged excuses nothing; when the verdict is
    refuted with one in ``excused``, ``excused_by`` holds
    ``LATER_WRITE_UNJUDGED``, since the change may be that write's. The
    database is read twice, ``settle_s`` apart, as after the save. Returns
    the frame verdict and a conservative lower bound on when its reads began.

    Raises :class:`VerifyRunError` for an excuse that doesn't qualify.
    """

    baseline = state.get("baseline")
    receipt = state.get("receipt")
    attribution = receipt.get(_WRITE_ATTRIBUTION) if isinstance(receipt, Mapping) else None
    owned = (
        isinstance(baseline, Mapping) and baseline.get("purpose") == "owned_attempt"
    ) or (
        isinstance(attribution, Mapping) and attribution.get("purpose") == "owned_attempt"
    )
    if owned:
        problem = recheck_problem(
            cfg, state, params=params, run_record=run_record, read_provider=read_provider
        )
        if problem is not None:
            raise VerifyRunError(problem)
    def checked_result(verdict: FrameVerdict, read_at: str) -> tuple[FrameVerdict, str]:
        if owned:
            problem = recheck_problem(
                cfg, state, params=params, run_record=run_record, read_provider=read_provider
            )
            if problem is not None:
                return FrameVerdict(status="not_checked", reason=problem), read_at
        return verdict, read_at

    post = dict(state["post_state"])
    if cfg.frame is not None and cfg.frame.history is not None:
        from openadapt_flow.runtime.effects.history import instant, judge_history

        if excused:
            raise VerifyRunError(
                "history rechecks require source provenance; later-write windows cannot excuse events"
            )
        from openadapt_flow.runtime.effects.history import (
            DETAIL,
            checkpoint_pair_problem,
        )

        checkpoint = post.get("history")
        baseline_history = (
            (state.get("baseline") or {}).get("state", {}).get("detail") or {}
        ).get(DETAIL)
        problem = checkpoint_pair_problem(
            baseline_history, checkpoint, cfg.frame.history
        )
        if problem is not None:
            raise VerifyRunError(problem)
        if not isinstance(checkpoint, dict):
            raise VerifyRunError("the history checkpoint is malformed")
        now, rows = _read_history(
            cfg, params, checkpoint["since"], bytes.fromhex(str(state["salt"]))
        )
        schedule = state["evidence"]["frame"][late_write.EVIDENCE_KEY]
        verdict = judge_history(
            cfg.frame.history,
            checkpoint,
            now,
            rows,
            params,
            schedule["due_at"],
            str(run_record.get("executor_id") or ""),
        )
        horizon = min(instant(feed["through"]) for feed in now["feeds"].values())
        return checked_result(verdict, late_write.format_time(horizon))
    since = late_write.parse_time(str(post["read_at"]))
    windows: list[Any] = []
    unjudged = 0
    for i, (other, other_params) in enumerate(excused):
        problem = recheck_problem(
            cfg, other, params=other_params, run_record=run_record, verified=False
        )
        if problem is not None:
            raise VerifyRunError(f"excused_by[{i}] is not a later write: {problem}")
        if other["execution_id"] == state["execution_id"]:
            raise VerifyRunError(f"excused_by[{i}] is the write being rechecked")
        started = other["baseline"].get("baseline_read_at")
        try:
            later = late_write.parse_time(str(started)) >= since
        except ValueError:
            later = False
        if not later:
            raise VerifyRunError(
                f"excused_by[{i}] started before this write's post-save read"
            )
        window = _later_window(other)
        if window is not None:
            windows.append(window)
        else:
            unjudged += 1
    salt = bytes.fromhex(str(state["salt"]))
    detail: dict[str, Any] = {FRAME_DB_DETAIL: dict(post["snapshot"])}
    if post.get("audit") is not None:
        detail[FRAME_AUDIT_DETAIL] = dict(post["audit"])
    before = EffectState(substrate="database", reachable=True, detail=detail)
    verifier = _build_verifier(cfg, params, read_provider)
    # A lower bound on every observation in this pass. Completion may be
    # delayed after the database snapshots were taken, even past due_at.
    read_started_at = _now()
    verdict = judge_run_frame(
        cfg,
        verifier,
        before,
        [],  # no declared delta: only the later writes' windows excuse rows
        params,
        audit_verifier=_audit_verifier(cfg, params),
        run_actors=[str(run_record.get("executor_id") or "")],
        salt=salt,
        database_only=True,
        later=windows,
    )
    if unjudged and verdict.status == "refuted":
        # A later write whose frame wasn't judged excuses nothing, so what
        # refuted this write may be that write's own change.
        verdict.excused_by.add(late_write.LATER_WRITE_UNJUDGED)
    return checked_result(verdict, read_started_at)


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
        | {"checked_how", "checked", RECHECK_RECEIPT_FIELD, "request_context", _WRITE_ATTRIBUTION}
    )
    if set(receipt) - allowed:
        return "unknown_field"
    if any(name not in receipt for name in _NEW_RECEIPT_FIELDS):
        return "missing_field"
    if "request_context" in receipt:
        from openadapt_flow.request_binding import BindingError, RequestContext

        try:
            RequestContext.parse(receipt["request_context"])
        except BindingError:
            return "request_context"
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
    if RECHECK_RECEIPT_FIELD in receipt and (
        receipt.get("outcome") != "verified"
        or not _ISO_SECONDS.match(str(receipt[RECHECK_RECEIPT_FIELD]))
    ):
        return RECHECK_RECEIPT_FIELD
    # Optional: receipts issued before RFC-0002 open question 5 lack it.
    if "checked_how" in receipt and (
        receipt["checked_how"] not in CHECKED_HOW_VALUES
        or CHECKED_HOW_TIER[receipt["checked_how"]] != receipt["verification_tier"]
    ):
        return "checked_how"
    outcome = receipt.get("outcome")
    if _WRITE_ATTRIBUTION in receipt:
        attribution = receipt[_WRITE_ATTRIBUTION]
        if not isinstance(attribution, Mapping) or set(attribution) != {"purpose", "status"}:
            return "write_attribution"
        pair = (attribution["purpose"], attribution["status"])
        if any(type(part) is not str for part in pair) or pair not in (
            ("observation", "observed"),
            ("owned_attempt", "attributed"),
            ("owned_attempt", "unattributed"),
        ):
            return "write_attribution"
        if pair[0] == "owned_attempt":
            attributed = pair[1] == "attributed"
            contracts = receipt["contracts"]
            landed = attributed and receipt["effect_verdict"] != "REFUTED"
            if not isinstance(contracts, Mapping) or contracts.get("effect_passed") is not landed:
                return "write_attribution"
            if not attributed and (
                outcome in ("verified", "rolled_back_verified") or receipt["effect_verdict"] == "CONFIRMED"
            ):
                return "write_attribution"
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
    if (_WRITE_ATTRIBUTION in evidence) != (_WRITE_ATTRIBUTION in receipt) or (
        evidence.get(_WRITE_ATTRIBUTION) != receipt.get(_WRITE_ATTRIBUTION)
    ):
        return "write_attribution"
    if "request_context" in receipt or "request_context" in evidence:
        from openadapt_flow.request_binding import BindingError, RequestContext

        try:
            if RequestContext.parse(
                receipt.get("request_context")
            ) != RequestContext.parse(evidence.get("request_context")):
                return "request_context"
        except BindingError:
            return "request_context"
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
    schedule = (
        frame.get(late_write.EVIDENCE_KEY) if isinstance(frame, Mapping) else None
    )
    due = schedule.get("due_at") if isinstance(schedule, Mapping) else None
    if receipt.get(RECHECK_RECEIPT_FIELD) != due:
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
