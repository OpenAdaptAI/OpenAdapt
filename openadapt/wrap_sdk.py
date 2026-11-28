"""Two-line Python SDK wrapper for action functions, tools, and agents.

``openadapt.wrap`` adds identity pinning, uncertain-write retry protection,
post-action verification (defaulting to ``same_screen`` with no reload or API
requirement, or delegating to an ``openadapt.verify.Contract`` when supplied),
and SHA-256 or Ed25519 receipts to any Python callable or agent object without
requiring YAML configuration files.

Example::

    import openadapt

    safe_save = openadapt.wrap(
        save_chart_note,
        check=lambda out, *a, **kw: out.get("saved") is True,
        pinned={"patient_id": "PT-104"},
    )
"""

from __future__ import annotations

import asyncio
import base64
import enum
import functools
import hashlib
import inspect
import json
import os
import tempfile
import threading
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Literal, TypeVar

__all__ = [
    "VALID_CHECKED_HOW",
    "WrapBlockedError",
    "WrapError",
    "WrapLedger",
    "WrapOutcome",
    "WrapReceipt",
    "WrapResult",
    "WrapVerificationError",
    "WrappedAgentProxy",
    "verify_wrap_receipt",
    "wrap",
]

F = TypeVar("F", bound=Callable[..., Any])

RECEIPT_SCHEMA = "openadapt.wrap_receipt/v1"
SIGNING_DOMAIN = b"openadapt.wrap_receipt/v1\n"

VALID_CHECKED_HOW: tuple[str, ...] = (
    "same_screen",
    "reopened",
    "separate_session",
    "system_of_record",
    "unchecked",
)


class WrapOutcome(str, enum.Enum):
    """Outcome recorded on a ``WrapReceipt``."""

    CONFIRMED = "confirmed"
    REFUTED = "refuted"
    UNCERTAIN = "uncertain"
    BLOCKED = "blocked"
    UNCHECKED = "unchecked"


class _BlockMode(str, enum.Enum):
    RAISE = "raise"
    RETURN = "return"


class _LedgerState(str, enum.Enum):
    IN_FLIGHT = "in_flight"
    CONFIRMED = "confirmed"
    UNCHECKED = "unchecked"
    UNCERTAIN = "uncertain"
    REFUTED = "refuted"


_OUTCOME_TOKENS: dict[str, WrapOutcome] = {
    "confirmed": WrapOutcome.CONFIRMED,
    "ok": WrapOutcome.CONFIRMED,
    "true": WrapOutcome.CONFIRMED,
    "refuted": WrapOutcome.REFUTED,
    "false": WrapOutcome.REFUTED,
    "uncertain": WrapOutcome.UNCERTAIN,
    "unknown": WrapOutcome.UNCERTAIN,
    "blocked": WrapOutcome.BLOCKED,
    "unchecked": WrapOutcome.UNCHECKED,
}

_MODE_TOKENS: dict[str, _BlockMode] = {
    "raise": _BlockMode.RAISE,
    "return": _BlockMode.RETURN,
}

_CONTRACT_CHECKED_HOW: dict[str, str] = {
    "sql": "system_of_record",
    "database": "system_of_record",
    "rest": "system_of_record",
    "graphql": "system_of_record",
    "fhir": "system_of_record",
    "file": "system_of_record",
    "fs": "system_of_record",
    "document": "system_of_record",
    "email": "system_of_record",
    "ui_readback": "separate_session",
    "fresh_session": "separate_session",
    "onscreen": "same_screen",
    "desktop_readback": "same_screen",
}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _to_json_safe(value: Any) -> tuple[Any, bool]:
    """Convert *value* into deterministic JSON-compatible data.

    Returns ``(converted_value, exact)`` where ``exact`` is ``True`` only when
    primitive JSON types were preserved losslessly for cross-process replay.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value, True
    if isinstance(value, (list, tuple)):
        items = []
        exact = isinstance(value, list)
        for item in value:
            conv, item_exact = _to_json_safe(item)
            items.append(conv)
            exact = exact and item_exact
        return items, exact
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        exact = type(value) is dict
        for k, v in value.items():
            if not isinstance(k, str):
                exact = False
            conv, item_exact = _to_json_safe(v)
            out[str(k)] = conv
            exact = exact and item_exact
        return out, exact
    if isinstance(value, Path):
        return value.as_posix(), False
    if isinstance(value, enum.Enum):
        conv, _ = _to_json_safe(value.value)
        return conv, False
    return repr(value), False


def _canonical_bytes(payload: Any) -> bytes:
    safe, _ = _to_json_safe(payload)
    return json.dumps(
        safe,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _sha256_hex(payload: Any) -> str:
    if isinstance(payload, bytes):
        return hashlib.sha256(payload).hexdigest()
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _values_equal(left: Any, right: Any) -> bool:
    """Compare values without treating ``bool`` as ``int`` (``True != 1``)."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        if len(left) != len(right):
            return False
        return all(_values_equal(a, b) for a, b in zip(left, right))
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        if set(left.keys()) != set(right.keys()):
            return False
        return all(_values_equal(left[k], right[k]) for k in left)
    return bool(left == right)


def _load_signing_key(signing_key: Any) -> Any:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if isinstance(signing_key, Ed25519PrivateKey):
        return signing_key
    if isinstance(signing_key, (bytes, bytearray)):
        raw = bytes(signing_key)
        if len(raw) == 32:
            return Ed25519PrivateKey.from_private_bytes(raw)
        return serialization.load_pem_private_key(raw, password=None)
    if isinstance(signing_key, Path) or (
        isinstance(signing_key, str) and Path(signing_key).is_file()
    ):
        pem = Path(signing_key).read_bytes()
        return serialization.load_pem_private_key(pem, password=None)
    if isinstance(signing_key, str):
        stripped = signing_key.strip()
        if len(stripped) == 64:
            try:
                return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(stripped))
            except ValueError:
                pass
        return serialization.load_pem_private_key(
            stripped.encode("utf-8"), password=None
        )
    raise TypeError(
        "signing_key must be an Ed25519PrivateKey, 32-byte seed, 64-char hex seed, or PEM path"
    )


def _load_public_key(public_key: Any) -> Any:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if isinstance(public_key, Ed25519PublicKey):
        return public_key
    if isinstance(public_key, (bytes, bytearray)):
        raw = bytes(public_key)
        if len(raw) == 32:
            return Ed25519PublicKey.from_public_bytes(raw)
        return serialization.load_pem_public_key(raw)
    if isinstance(public_key, str):
        stripped = public_key.strip()
        if len(stripped) == 64:
            try:
                return Ed25519PublicKey.from_public_bytes(bytes.fromhex(stripped))
            except ValueError:
                pass
        try:
            decoded = base64.b64decode(stripped, validate=True)
            if len(decoded) == 32:
                return Ed25519PublicKey.from_public_bytes(decoded)
        except ValueError:
            pass
        return serialization.load_pem_public_key(stripped.encode("utf-8"))
    raise TypeError(
        "public_key must be an Ed25519PublicKey, 32-byte key, hex/base64 string, or PEM"
    )


@dataclass(frozen=True)
class WrapReceipt(Mapping[str, Any]):
    """Tamper-evident receipt attached as ``wrapped.last_receipt``.

    Supports both attribute access (``receipt.outcome``, ``receipt.sha256``) and
    mapping lookup (``receipt["outcome"]``, ``receipt.get("checked_how")``).
    """

    schema: str
    outcome: str
    checked_how: str
    timestamp: str
    action: str
    request_key: str
    arguments_sha256: str
    dispatched: bool
    replayed: bool
    identity: Mapping[str, Any]
    declared_by: Mapping[str, str]
    reason: str
    contract_receipt_sha256: str | None
    sha256: str
    signature: str | None = None
    public_key: str | None = None

    @property
    def ok(self) -> bool:
        """True when the action succeeded and was not blocked, refuted, or uncertain."""
        return self.outcome in (
            WrapOutcome.CONFIRMED.value,
            WrapOutcome.UNCHECKED.value,
        )

    @property
    def verified(self) -> bool:
        """True only when a post-action check or contract confirmed the outcome."""
        return (
            self.outcome == WrapOutcome.CONFIRMED.value
            and self.checked_how != "unchecked"
        )

    def payload_for_digest(self) -> dict[str, Any]:
        """Return the canonical dictionary covered by ``sha256`` and ``signature``."""
        return {
            "schema": self.schema,
            "outcome": self.outcome,
            "checked_how": self.checked_how,
            "timestamp": self.timestamp,
            "action": self.action,
            "request_key": self.request_key,
            "arguments_sha256": self.arguments_sha256,
            "dispatched": self.dispatched,
            "replayed": self.replayed,
            "identity": dict(self.identity),
            "declared_by": dict(self.declared_by),
            "reason": self.reason,
            "contract_receipt_sha256": self.contract_receipt_sha256,
        }

    def to_dict(self) -> dict[str, Any]:
        """Serialize the full receipt including digest and optional signature."""
        body = self.payload_for_digest()
        body["sha256"] = self.sha256
        body["signature"] = self.signature
        body["public_key"] = self.public_key
        return body

    def verify(self, public_key: Any = None) -> bool:
        """Verify ``sha256`` integrity and optional Ed25519 ``signature``."""
        return verify_wrap_receipt(self, public_key=public_key)

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.to_dict())

    def __len__(self) -> int:
        return len(self.to_dict())


def _build_receipt(
    *,
    outcome: WrapOutcome,
    checked_how: str,
    action: str,
    request_key: str,
    arguments_sha256: str,
    dispatched: bool,
    replayed: bool = False,
    identity: Mapping[str, Any] | None = None,
    declared_by: Mapping[str, str] | None = None,
    reason: str = "",
    contract_receipt_sha256: str | None = None,
    signing_key: Any = None,
) -> WrapReceipt:
    safe_identity, _ = _to_json_safe(dict(identity or {}))
    safe_declared = {str(k): str(v) for k, v in (declared_by or {}).items()}
    timestamp = _utc_now_iso()
    payload = {
        "schema": RECEIPT_SCHEMA,
        "outcome": outcome.value,
        "checked_how": checked_how,
        "timestamp": timestamp,
        "action": action,
        "request_key": request_key,
        "arguments_sha256": arguments_sha256,
        "dispatched": dispatched,
        "replayed": replayed,
        "identity": safe_identity,
        "declared_by": safe_declared,
        "reason": reason,
        "contract_receipt_sha256": contract_receipt_sha256,
    }
    canonical = _canonical_bytes(payload)
    digest = hashlib.sha256(canonical).hexdigest()
    signature_b64: str | None = None
    public_hex: str | None = None
    if signing_key is not None:
        from cryptography.hazmat.primitives import serialization

        priv = _load_signing_key(signing_key)
        sig_bytes = priv.sign(SIGNING_DOMAIN + canonical)
        signature_b64 = base64.b64encode(sig_bytes).decode("ascii")
        pub_bytes = priv.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        public_hex = pub_bytes.hex()
    return WrapReceipt(
        schema=RECEIPT_SCHEMA,
        outcome=outcome.value,
        checked_how=checked_how,
        timestamp=timestamp,
        action=action,
        request_key=request_key,
        arguments_sha256=arguments_sha256,
        dispatched=dispatched,
        replayed=replayed,
        identity=MappingProxyType(dict(safe_identity)),
        declared_by=MappingProxyType(safe_declared),
        reason=reason,
        contract_receipt_sha256=contract_receipt_sha256,
        sha256=digest,
        signature=signature_b64,
        public_key=public_hex,
    )


def verify_wrap_receipt(
    receipt: WrapReceipt | Mapping[str, Any],
    *,
    public_key: Any = None,
) -> bool:
    """Verify the SHA-256 digest (and Ed25519 signature when present or required)."""
    data = receipt.to_dict() if isinstance(receipt, WrapReceipt) else dict(receipt)
    claimed_sha = data.get("sha256")
    if not isinstance(claimed_sha, str) or len(claimed_sha) != 64:
        return False
    payload = {
        "schema": data.get("schema"),
        "outcome": data.get("outcome"),
        "checked_how": data.get("checked_how"),
        "timestamp": data.get("timestamp"),
        "action": data.get("action"),
        "request_key": data.get("request_key"),
        "arguments_sha256": data.get("arguments_sha256"),
        "dispatched": data.get("dispatched"),
        "replayed": data.get("replayed"),
        "identity": data.get("identity") or {},
        "declared_by": data.get("declared_by") or {},
        "reason": data.get("reason") or "",
        "contract_receipt_sha256": data.get("contract_receipt_sha256"),
    }
    canonical = _canonical_bytes(payload)
    if hashlib.sha256(canonical).hexdigest() != claimed_sha:
        return False
    signature = data.get("signature")
    if public_key is None and signature is None:
        return True
    if not isinstance(signature, str) or not signature:
        return False
    key_source = public_key if public_key is not None else data.get("public_key")
    if key_source is None:
        return False
    from cryptography.exceptions import InvalidSignature

    try:
        pub = _load_public_key(key_source)
        sig_bytes = base64.b64decode(signature, validate=True)
        pub.verify(sig_bytes, SIGNING_DOMAIN + canonical)
    except (InvalidSignature, ValueError, TypeError):
        return False
    return True


@dataclass(frozen=True)
class WrapResult:
    """Envelope returned when ``return_receipt=True`` (or ``on_block="return"``)."""

    value: Any
    receipt: WrapReceipt
    blocked: bool = False

    @property
    def ok(self) -> bool:
        return not self.blocked and self.receipt.ok

    @property
    def verified(self) -> bool:
        return not self.blocked and self.receipt.verified

    @property
    def outcome(self) -> str:
        return self.receipt.outcome

    def to_dict(self) -> dict[str, Any]:
        safe_val, _ = _to_json_safe(self.value)
        return {
            "ok": self.ok,
            "verified": self.verified,
            "blocked": self.blocked,
            "outcome": self.outcome,
            "value": safe_val,
            "receipt": self.receipt.to_dict(),
        }


class WrapError(RuntimeError):
    """Base exception for ``openadapt.wrap`` refusals and verification failures."""

    def __init__(
        self,
        reason: str,
        *,
        code: str,
        key: str = "",
        receipt: WrapReceipt | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code
        self.key = key
        self.receipt = receipt


class WrapBlockedError(WrapError):
    """Raised before executing the wrapped callable when a guard blocks the write."""


class WrapVerificationError(WrapError):
    """Raised after execution when post-action verification is refuted or uncertain."""

    def __init__(
        self,
        reason: str,
        *,
        code: str,
        key: str = "",
        receipt: WrapReceipt | None = None,
        value: Any = None,
    ) -> None:
        super().__init__(reason, code=code, key=key, receipt=receipt)
        self.value = value


@dataclass
class _LedgerEntry:
    key: str
    action: str
    arguments_sha256: str
    state: _LedgerState
    has_live_value: bool = False
    live_value: Any = None
    json_value: Any = None
    json_exact: bool = False
    checked_how: str = "unchecked"
    reason: str = ""
    updated_at: str = ""
    previous: _LedgerEntry | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "action": self.action,
            "arguments_sha256": self.arguments_sha256,
            "state": self.state.value,
            "json_value": self.json_value,
            "json_exact": self.json_exact,
            "checked_how": self.checked_how,
            "reason": self.reason,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> _LedgerEntry:
        raw_state = str(raw.get("state") or _LedgerState.UNCERTAIN.value)
        try:
            state = _LedgerState(raw_state)
        except ValueError:
            state = _LedgerState.UNCERTAIN
        if state is _LedgerState.IN_FLIGHT:
            # An in-flight attempt from a previous process never recorded its end;
            # treat it as uncertain until an operator releases the key.
            state = _LedgerState.UNCERTAIN
        return cls(
            key=str(raw.get("key") or ""),
            action=str(raw.get("action") or ""),
            arguments_sha256=str(raw.get("arguments_sha256") or ""),
            state=state,
            has_live_value=False,
            live_value=None,
            json_value=raw.get("json_value"),
            json_exact=bool(raw.get("json_exact", False)),
            checked_how=str(raw.get("checked_how") or "unchecked"),
            reason=str(raw.get("reason") or ""),
            updated_at=str(raw.get("updated_at") or ""),
        )


class WrapLedger:
    """Thread-safe uncertain-write and idempotent-request ledger.

    Operates in memory by default, or persists entries atomically to a local
    JSON file when ``path`` is supplied.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path).expanduser() if path is not None else None
        self._lock = threading.RLock()
        self._entries: dict[str, _LedgerEntry] = {}
        if self.path is not None and self.path.exists():
            self._load()

    def _load(self) -> None:
        assert self.path is not None
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        entries = raw.get("entries", {}) if isinstance(raw, Mapping) else {}
        if not isinstance(entries, Mapping):
            raise ValueError(f"corrupt wrap ledger at {self.path}")
        for key, item in entries.items():
            if isinstance(item, Mapping):
                self._entries[str(key)] = _LedgerEntry.from_json(item)

    def _persist(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(
            {
                "schema": "openadapt.wrap_ledger/v1",
                "entries": {k: v.to_json() for k, v in sorted(self._entries.items())},
            },
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent),
            prefix=f".{self.path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(tmp_name, 0o600)
            except OSError:
                pass
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def preflight(
        self,
        *,
        key: str,
        action: str,
        arguments_sha256: str,
        deduplicate_completed: bool,
        block_uncertain: bool,
    ) -> tuple[str, _LedgerEntry | None]:
        """Inspect and reserve *key* prior to action dispatch.

        Returns ``(decision, entry)`` where ``decision`` is one of:
        ``"proceed"``, ``"replay"``, ``"conflict"``, ``"in_flight"``,
        ``"uncertain_latched"``, or ``"refuted_latched"``.
        """
        with self._lock:
            existing = self._entries.get(key)
            if existing is not None:
                if (
                    deduplicate_completed
                    and existing.arguments_sha256 != arguments_sha256
                ):
                    return "conflict", existing
                if existing.state is _LedgerState.IN_FLIGHT and deduplicate_completed:
                    return "in_flight", existing
                if existing.state is _LedgerState.UNCERTAIN and block_uncertain:
                    return "uncertain_latched", existing
                if existing.state is _LedgerState.REFUTED and block_uncertain:
                    return "refuted_latched", existing
                if deduplicate_completed and existing.state in (
                    _LedgerState.CONFIRMED,
                    _LedgerState.UNCHECKED,
                ):
                    if existing.has_live_value or existing.json_exact:
                        return "replay", existing
            if deduplicate_completed or self.path is not None:
                self._entries[key] = _LedgerEntry(
                    key=key,
                    action=action,
                    arguments_sha256=arguments_sha256,
                    state=_LedgerState.IN_FLIGHT,
                    updated_at=_utc_now_iso(),
                    previous=existing,
                )
                self._persist()
            return "proceed", None

    def abort_in_flight(self, key: str) -> None:
        """Roll back an IN_FLIGHT reservation when preflight aborts before dispatch."""
        with self._lock:
            current = self._entries.get(key)
            if current is None or current.state is not _LedgerState.IN_FLIGHT:
                return
            if current.previous is not None:
                self._entries[key] = current.previous
            else:
                self._entries.pop(key, None)
            self._persist()

    def record_completed(
        self,
        *,
        key: str,
        action: str,
        arguments_sha256: str,
        outcome: WrapOutcome,
        checked_how: str,
        value: Any,
        keep_completed: bool,
    ) -> None:
        with self._lock:
            if not keep_completed:
                if (
                    key in self._entries
                    and self._entries[key].state is _LedgerState.IN_FLIGHT
                ):
                    self._entries.pop(key, None)
                    self._persist()
                return
            state = (
                _LedgerState.CONFIRMED
                if outcome is WrapOutcome.CONFIRMED
                else _LedgerState.UNCHECKED
            )
            json_val, exact = _to_json_safe(value)
            self._entries[key] = _LedgerEntry(
                key=key,
                action=action,
                arguments_sha256=arguments_sha256,
                state=state,
                has_live_value=True,
                live_value=value,
                json_value=json_val,
                json_exact=exact,
                checked_how=checked_how,
                updated_at=_utc_now_iso(),
            )
            self._persist()

    def record_non_confirmed(
        self,
        *,
        key: str,
        action: str,
        arguments_sha256: str,
        state: _LedgerState,
        checked_how: str,
        reason: str,
        latch: bool,
    ) -> None:
        with self._lock:
            if not latch:
                if (
                    key in self._entries
                    and self._entries[key].state is _LedgerState.IN_FLIGHT
                ):
                    self._entries.pop(key, None)
                    self._persist()
                return
            self._entries[key] = _LedgerEntry(
                key=key,
                action=action,
                arguments_sha256=arguments_sha256,
                state=state,
                checked_how=checked_how,
                reason=reason,
                updated_at=_utc_now_iso(),
            )
            self._persist()

    def release(
        self,
        key: str | None = None,
        *,
        action: str | None = None,
        actions: Collection[str] | None = None,
    ) -> int:
        """Clear latched uncertain or refuted entries so a reconciled retry can run."""
        with self._lock:
            if key is not None:
                removed = 1 if self._entries.pop(key, None) is not None else 0
                if removed:
                    self._persist()
                return removed
            allowed_actions = set(actions) if actions is not None else None
            to_delete = [
                k
                for k, entry in self._entries.items()
                if entry.state
                in (
                    _LedgerState.UNCERTAIN,
                    _LedgerState.REFUTED,
                    _LedgerState.IN_FLIGHT,
                )
                and (action is None or entry.action == action)
                and (allowed_actions is None or entry.action in allowed_actions)
            ]
            for k in to_delete:
                self._entries.pop(k, None)
            if to_delete:
                self._persist()
            return len(to_delete)


def _resolve_ledger(ledger: WrapLedger | str | Path | None) -> WrapLedger:
    if ledger is None:
        return WrapLedger()
    if isinstance(ledger, WrapLedger):
        return ledger
    if isinstance(ledger, (str, Path)):
        return WrapLedger(ledger)
    raise TypeError("ledger must be a WrapLedger, file path, or None")


def _callable_check_arity(
    check_fn: Callable[..., Any],
) -> Literal["zero", "one", "full"]:
    try:
        sig = inspect.signature(check_fn)
    except (TypeError, ValueError):
        return "one"
    pos_count = 0
    for param in sig.parameters.values():
        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            return "full"
        if param.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            pos_count += 1
    if pos_count == 0:
        return "zero"
    if pos_count == 1:
        return "one"
    return "full"


def _interpret_check_output(
    raw: Any,
    default_checked_how: str,
) -> tuple[WrapOutcome, str, str]:
    """Normalize a ``check(...)`` return value into ``(outcome, checked_how, reason)``."""
    if isinstance(raw, bool):
        if raw:
            return WrapOutcome.CONFIRMED, default_checked_how, ""
        return (
            WrapOutcome.REFUTED,
            default_checked_how,
            "post-action check returned False",
        )

    if isinstance(raw, str):
        token = raw.strip().lower()
        mapped = _OUTCOME_TOKENS.get(token)
        if mapped is WrapOutcome.CONFIRMED:
            return WrapOutcome.CONFIRMED, default_checked_how, ""
        if mapped is WrapOutcome.REFUTED:
            return (
                WrapOutcome.REFUTED,
                default_checked_how,
                f"post-action check returned {raw!r}",
            )
        if mapped is WrapOutcome.UNCERTAIN:
            return (
                WrapOutcome.UNCERTAIN,
                default_checked_how,
                "post-action check returned uncertain",
            )
        return (
            WrapOutcome.UNCERTAIN,
            default_checked_how,
            f"unrecognized post-action check status {raw!r}",
        )

    if isinstance(raw, Mapping):
        how = str(raw.get("checked_how") or default_checked_how)
        if how not in VALID_CHECKED_HOW:
            return (
                WrapOutcome.UNCERTAIN,
                default_checked_how,
                f"invalid checked_how {how!r}",
            )
        reason = str(raw.get("reason") or raw.get("detail") or "")
        if "outcome" in raw:
            token = str(raw.get("outcome") or "").strip().lower()
            mapped = _OUTCOME_TOKENS.get(token, WrapOutcome.UNCERTAIN)
            if mapped is WrapOutcome.CONFIRMED and how == "unchecked":
                return (
                    WrapOutcome.UNCERTAIN,
                    "unchecked",
                    reason or "unchecked cannot certify a confirmed write",
                )
            if mapped in (
                WrapOutcome.CONFIRMED,
                WrapOutcome.REFUTED,
                WrapOutcome.UNCERTAIN,
            ):
                if not reason and mapped is not WrapOutcome.CONFIRMED:
                    reason = f"post-action check reported {mapped.value}"
                return mapped, how, reason
            return (
                WrapOutcome.UNCERTAIN,
                how,
                reason or f"unsupported check outcome {raw.get('outcome')!r}",
            )
        if "ok" in raw:
            ok_val = raw.get("ok")
            if ok_val is True:
                if how == "unchecked":
                    return (
                        WrapOutcome.UNCERTAIN,
                        "unchecked",
                        reason or "unchecked cannot certify a confirmed write",
                    )
                return WrapOutcome.CONFIRMED, how, reason
            if ok_val is False:
                return (
                    WrapOutcome.REFUTED,
                    how,
                    reason or "post-action check mapping reported ok=False",
                )
            return (
                WrapOutcome.UNCERTAIN,
                how,
                reason or f"post-action check ok={ok_val!r} is uncertain",
            )
        return (
            WrapOutcome.UNCERTAIN,
            how,
            reason or "post-action check mapping must include 'ok' or 'outcome'",
        )

    return (
        WrapOutcome.UNCERTAIN,
        default_checked_how,
        f"post-action check must return bool, status string, or Mapping, got {type(raw).__name__}",
    )


def _run_sync_check(
    check_fn: Callable[..., Any],
    arity: Literal["zero", "one", "full"],
    value: Any,
    call_args: tuple[Any, ...],
    call_kwargs: Mapping[str, Any],
    default_checked_how: str,
) -> tuple[Any, str, str, Exception | None]:
    try:
        if arity == "zero":
            raw = check_fn()
        elif arity == "one":
            raw = check_fn(value)
        else:
            raw = check_fn(value, *call_args, **call_kwargs)
    except AssertionError as exc:
        return (
            WrapOutcome.REFUTED,
            default_checked_how,
            str(exc) or "post-action check assertion failed",
            exc,
        )
    except Exception as exc:
        return (
            "unknown",
            default_checked_how,
            f"post-action check raised {type(exc).__name__}: {exc}",
            exc,
        )
    if inspect.isawaitable(raw):
        close = getattr(raw, "close", None)
        if callable(close):
            close()
        err = TypeError(
            "async check callable requires wrapping an async coroutine function"
        )
        return (
            "unknown",
            default_checked_how,
            f"post-action check returned awaitable: {err}",
            err,
        )
    outcome, how, reason = _interpret_check_output(raw, default_checked_how)
    return outcome, how, reason, None


async def _run_async_check(
    check_fn: Callable[..., Any],
    arity: Literal["zero", "one", "full"],
    value: Any,
    call_args: tuple[Any, ...],
    call_kwargs: Mapping[str, Any],
    default_checked_how: str,
) -> tuple[Any, str, str, Exception | None]:
    try:
        if arity == "zero":
            raw = check_fn()
        elif arity == "one":
            raw = check_fn(value)
        else:
            raw = check_fn(value, *call_args, **call_kwargs)
        if inspect.isawaitable(raw):
            raw = await raw
    except AssertionError as exc:
        return (
            WrapOutcome.REFUTED,
            default_checked_how,
            str(exc) or "post-action check assertion failed",
            exc,
        )
    except Exception as exc:
        return (
            "unknown",
            default_checked_how,
            f"post-action check raised {type(exc).__name__}: {exc}",
            exc,
        )
    outcome, how, reason = _interpret_check_output(raw, default_checked_how)
    return outcome, how, reason, None


def _normalize_check_tuple(
    raw_tuple: tuple[Any, str, str, Exception | None],
) -> tuple[WrapOutcome, str, str, Exception | None]:
    raw_outcome, how, reason, err = raw_tuple
    if isinstance(raw_outcome, WrapOutcome):
        return raw_outcome, how, reason, err
    return WrapOutcome.UNCERTAIN, how, reason, err


class _ContractBridge:
    """Lazy bridge to ``openadapt_flow.verifier`` when ``contract=`` is supplied."""

    def __init__(
        self,
        contract_spec: Any,
        *,
        agent_id: str,
        workdir: str | Path | None,
    ) -> None:
        from openadapt_flow.verifier import Contract, MemoryLedger

        if isinstance(contract_spec, Contract):
            self.contract = contract_spec
        elif isinstance(contract_spec, (str, Path)):
            self.contract = Contract.load(contract_spec)
        elif isinstance(contract_spec, Mapping):
            self.contract = Contract.from_dict(contract_spec)
        else:
            raise TypeError(
                "contract must be a verify.yaml path, Mapping, or openadapt.verify.Contract"
            )
        self.agent_id = agent_id
        self.workdir = Path(workdir).expanduser() if workdir is not None else None
        self.ledger = MemoryLedger()
        self.params = tuple(self.contract.params)
        kind = str(self.contract.config.verifier.kind).replace("-", "_")
        self.checked_how = _CONTRACT_CHECKED_HOW.get(kind, "system_of_record")

    def begin(
        self,
        flat_args: Mapping[str, Any],
        pinned: Mapping[str, Any],
        explicit_request_id: str | None,
    ) -> Any:
        from openadapt_flow.verifier import (
            Actor,
            VerifierError,
            WriteBlocked,
            begin_write,
        )

        values: dict[str, Any] = {}
        declared_by: dict[str, str] = {}
        for param_name in self.params:
            if param_name in pinned:
                values[param_name] = pinned[param_name]
                declared_by[param_name] = "operator"
            elif param_name in flat_args:
                values[param_name] = flat_args[param_name]
                declared_by[param_name] = "caller"
            else:
                raise WrapBlockedError(
                    f"missing required contract parameter {param_name!r}",
                    code="missing_contract_param",
                )
        try:
            return begin_write(
                self.contract,
                values,
                actor=Actor("agent", self.agent_id),
                ledger=self.ledger,
                write_key=explicit_request_id,
                workdir=self.workdir,
                declared_by=declared_by,
            )
        except (WriteBlocked, VerifierError) as exc:
            raise WrapBlockedError(
                str(exc),
                code="contract_preflight_blocked",
                key=getattr(exc, "key", "") or (explicit_request_id or ""),
            ) from exc

    def verify(
        self, pending: Any, *, attempted: bool
    ) -> tuple[WrapOutcome, str, str | None]:
        from openadapt_flow.verifier import Outcome, verify_write

        write_res = verify_write(pending, write_attempted=attempted)
        receipt_sha = write_res.receipt_sha256 or None
        if write_res.outcome is Outcome.CONFIRMED:
            return WrapOutcome.CONFIRMED, "", receipt_sha
        if write_res.outcome is Outcome.REFUTED:
            return (
                WrapOutcome.REFUTED,
                write_res.reason or "contract read-back refuted the write",
                receipt_sha,
            )
        return (
            WrapOutcome.UNCERTAIN,
            write_res.reason or "contract read-back was uncertain",
            receipt_sha,
        )


def _is_async_callable(fn: Any) -> bool:
    if inspect.iscoroutinefunction(fn):
        return True
    if not inspect.isfunction(fn) and not inspect.ismethod(fn):
        dunder_call = getattr(fn, "__call__", None)
        if dunder_call is not None and inspect.iscoroutinefunction(dunder_call):
            return True
    return False


class _InvocationPlan:
    """Prepared arguments, identity bindings, and ledger keys for one call."""

    __slots__ = (
        "arguments_sha256",
        "call_args",
        "call_kwargs",
        "declared_by",
        "explicit_request_id",
        "flat_args",
        "identity",
        "request_key",
    )

    def __init__(
        self,
        *,
        call_args: tuple[Any, ...],
        call_kwargs: dict[str, Any],
        flat_args: dict[str, Any],
        identity: dict[str, Any],
        declared_by: dict[str, str],
        explicit_request_id: str | None,
        request_key: str,
        arguments_sha256: str,
    ) -> None:
        self.call_args = call_args
        self.call_kwargs = call_kwargs
        self.flat_args = flat_args
        self.identity = identity
        self.declared_by = declared_by
        self.explicit_request_id = explicit_request_id
        self.request_key = request_key
        self.arguments_sha256 = arguments_sha256


class _CallableGuard:
    """Core commit-boundary guard shared by sync and async callable wrappers."""

    def __init__(
        self,
        fn: Callable[..., Any],
        *,
        action_name: str,
        check_fn: Callable[..., Any] | None,
        checked_how: str | None,
        pinned: Mapping[str, Any],
        identity_keys: tuple[str, ...],
        contract_bridge: _ContractBridge | None,
        ledger: WrapLedger,
        idempotent: bool,
        block_uncertain_retries: bool,
        return_receipt: bool,
        block_mode: _BlockMode,
        unverified_mode: _BlockMode,
        signing_key: Any,
        on_receipt: Callable[[WrapReceipt | None], None] | None = None,
    ) -> None:
        self.fn = fn
        self.action_name = action_name
        self.check_fn = check_fn
        self.check_is_async = bool(
            check_fn is not None and _is_async_callable(check_fn)
        )
        self.check_arity = (
            _callable_check_arity(check_fn) if check_fn is not None else "one"
        )
        self.contract_bridge = contract_bridge
        if checked_how is not None:
            self.default_checked_how = checked_how
        elif contract_bridge is not None:
            self.default_checked_how = contract_bridge.checked_how
        elif check_fn is not None:
            self.default_checked_how = "same_screen"
        else:
            self.default_checked_how = "unchecked"
        self.pinned = dict(pinned)
        self.identity_keys = identity_keys
        self.ledger = ledger
        self.idempotent = idempotent
        self.block_uncertain_retries = block_uncertain_retries
        self.return_receipt = return_receipt
        self.block_mode = block_mode
        self.unverified_mode = unverified_mode
        self.signing_key = signing_key
        self.on_receipt = on_receipt
        self.last_receipt: WrapReceipt | None = None

        try:
            self.signature: inspect.Signature | None = inspect.signature(fn)
        except (TypeError, ValueError):
            self.signature = None

        self.has_var_keyword = False
        self.param_names: frozenset[str] = frozenset()
        self.required_missing_pins: dict[str, Any] = {}
        if self.signature is not None:
            names: list[str] = []
            for p_name, param in self.signature.parameters.items():
                if param.kind is inspect.Parameter.VAR_KEYWORD:
                    self.has_var_keyword = True
                else:
                    names.append(p_name)
                    if (
                        p_name in self.pinned
                        and param.default is inspect.Parameter.empty
                        and param.kind
                        in (
                            inspect.Parameter.POSITIONAL_OR_KEYWORD,
                            inspect.Parameter.KEYWORD_ONLY,
                        )
                    ):
                        self.required_missing_pins[p_name] = self.pinned[p_name]
            self.param_names = frozenset(names)

    def _clear_receipt(self) -> None:
        self.last_receipt = None
        if self.on_receipt is not None:
            self.on_receipt(None)

    def _emit_receipt(self, receipt: WrapReceipt) -> WrapReceipt:
        self.last_receipt = receipt
        if self.on_receipt is not None:
            self.on_receipt(receipt)
        return receipt

    def _refuse(
        self,
        *,
        reason: str,
        code: str,
        request_key: str,
        arguments_sha256: str,
        identity: Mapping[str, Any],
        declared_by: Mapping[str, str],
        cause: BaseException | None = None,
    ) -> Any:
        receipt = self._emit_receipt(
            _build_receipt(
                outcome=WrapOutcome.BLOCKED,
                checked_how="unchecked",
                action=self.action_name,
                request_key=request_key,
                arguments_sha256=arguments_sha256,
                dispatched=False,
                identity=identity,
                declared_by=declared_by,
                reason=reason,
                signing_key=self.signing_key,
            )
        )
        if self.block_mode is _BlockMode.RETURN:
            if self.return_receipt:
                return WrapResult(value=None, receipt=receipt, blocked=True)
            return {
                "ok": False,
                "verified": False,
                "blocked": True,
                "code": code,
                "outcome": receipt.outcome,
                "reason": reason,
                "request_key": request_key,
                "receipt": receipt.to_dict(),
            }
        err = WrapBlockedError(reason, code=code, key=request_key, receipt=receipt)
        if cause is not None:
            raise err from cause
        raise err

    def _prepare_call(
        self, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> _InvocationPlan:
        working_kwargs = dict(kwargs)
        explicit_request_id: str | None = None
        if "request_id" in working_kwargs:
            raw_req = working_kwargs["request_id"]
            if raw_req is not None:
                if not isinstance(raw_req, str) or not raw_req.strip():
                    raise ValueError("request_id must be a non-empty string")
                explicit_request_id = raw_req.strip()
            if not self.has_var_keyword and "request_id" not in self.param_names:
                working_kwargs.pop("request_id", None)

        extra_pinned_supplied: dict[str, Any] = {}
        extra_identity_supplied: dict[str, Any] = {}
        if not self.has_var_keyword and self.signature is not None:
            for p_key in list(working_kwargs):
                if p_key not in self.param_names:
                    if p_key in self.pinned:
                        extra_pinned_supplied[p_key] = working_kwargs.pop(p_key)
                    elif p_key in self.identity_keys:
                        extra_identity_supplied[p_key] = working_kwargs.pop(p_key)

        flat_args: dict[str, Any] = {}
        explicit_supplied: dict[str, Any] = {
            **extra_pinned_supplied,
            **extra_identity_supplied,
        }
        call_args = args
        call_kwargs = working_kwargs

        if self.signature is not None:
            partial = self.signature.bind_partial(*call_args, **call_kwargs)
            for b_name, b_val in partial.arguments.items():
                param = self.signature.parameters[b_name]
                if param.kind is inspect.Parameter.VAR_KEYWORD:
                    explicit_supplied.update(b_val)
                elif param.kind is not inspect.Parameter.VAR_POSITIONAL:
                    explicit_supplied[b_name] = b_val

            for req_pin, pin_val in self.required_missing_pins.items():
                if req_pin not in explicit_supplied and req_pin not in call_kwargs:
                    call_kwargs[req_pin] = pin_val

            bound = self.signature.bind(*call_args, **call_kwargs)
            bound.apply_defaults()
            for b_name, b_val in bound.arguments.items():
                param = self.signature.parameters[b_name]
                if param.kind is inspect.Parameter.VAR_KEYWORD:
                    flat_args.update(b_val)
                elif param.kind is inspect.Parameter.VAR_POSITIONAL:
                    flat_args[b_name] = list(b_val)
                else:
                    flat_args[b_name] = b_val
        else:
            if call_args:
                flat_args["_args"] = list(call_args)
            flat_args.update(call_kwargs)
            explicit_supplied.update(call_kwargs)

        identity: dict[str, Any] = {}
        declared_by: dict[str, str] = {}
        for k, pin_val in self.pinned.items():
            identity[k] = pin_val
            declared_by[k] = "operator"
        for id_key in self.identity_keys:
            if id_key not in identity:
                if id_key in flat_args:
                    identity[id_key] = flat_args[id_key]
                    declared_by[id_key] = "caller"
                elif id_key in extra_identity_supplied:
                    identity[id_key] = extra_identity_supplied[id_key]
                    declared_by[id_key] = "caller"

        canonical_arg_payload = {
            "action": self.action_name,
            "arguments": flat_args,
            "pinned": self.pinned,
            "identity": identity,
        }
        arguments_sha256 = _sha256_hex(canonical_arg_payload)
        request_key = explicit_request_id or arguments_sha256

        # 1. Enforce operator-pinned keys before any side effect can execute.
        for pin_key, expected_val in self.pinned.items():
            if pin_key in explicit_supplied and not _values_equal(
                explicit_supplied[pin_key], expected_val
            ):
                observed_identity = dict(identity)
                observed_identity[pin_key] = explicit_supplied[pin_key]
                raise _PreCallRefusal(
                    reason=(
                        f"pinned key {pin_key!r} mismatch: operator pinned "
                        f"{expected_val!r}, caller supplied {explicit_supplied[pin_key]!r}"
                    ),
                    code="pinned_mismatch",
                    request_key=request_key,
                    arguments_sha256=arguments_sha256,
                    identity=observed_identity,
                    declared_by=declared_by,
                )
            if pin_key in flat_args and not _values_equal(
                flat_args[pin_key], expected_val
            ):
                raise _PreCallRefusal(
                    reason=(
                        f"pinned key {pin_key!r} mismatch: operator pinned "
                        f"{expected_val!r}, default resolved to {flat_args[pin_key]!r}"
                    ),
                    code="pinned_mismatch",
                    request_key=request_key,
                    arguments_sha256=arguments_sha256,
                    identity=identity,
                    declared_by=declared_by,
                )

        # 2. Require non-empty values for every configured identity key.
        for id_key in self.identity_keys:
            val = identity.get(id_key, flat_args.get(id_key))
            if val is None or (isinstance(val, str) and not val.strip()):
                raise _PreCallRefusal(
                    reason=f"required identity key {id_key!r} is missing or empty",
                    code="missing_identity",
                    request_key=request_key,
                    arguments_sha256=arguments_sha256,
                    identity=identity,
                    declared_by=declared_by,
                )

        return _InvocationPlan(
            call_args=call_args,
            call_kwargs=call_kwargs,
            flat_args=flat_args,
            identity=identity,
            declared_by=declared_by,
            explicit_request_id=explicit_request_id,
            request_key=request_key,
            arguments_sha256=arguments_sha256,
        )

    def _preflight_ledger(self, plan: _InvocationPlan) -> tuple[bool, Any]:
        deduplicate = self.idempotent or (plan.explicit_request_id is not None)
        decision, entry = self.ledger.preflight(
            key=plan.request_key,
            action=self.action_name,
            arguments_sha256=plan.arguments_sha256,
            deduplicate_completed=deduplicate,
            block_uncertain=self.block_uncertain_retries,
        )
        if decision == "proceed":
            return False, None
        if decision == "replay" and entry is not None:
            cached_val = entry.live_value if entry.has_live_value else entry.json_value
            replay_outcome = (
                WrapOutcome.CONFIRMED
                if entry.state is _LedgerState.CONFIRMED
                else WrapOutcome.UNCHECKED
            )
            receipt = self._emit_receipt(
                _build_receipt(
                    outcome=replay_outcome,
                    checked_how=entry.checked_how,
                    action=self.action_name,
                    request_key=plan.request_key,
                    arguments_sha256=plan.arguments_sha256,
                    dispatched=False,
                    replayed=True,
                    identity=plan.identity,
                    declared_by=plan.declared_by,
                    signing_key=self.signing_key,
                )
            )
            if self.return_receipt:
                return True, WrapResult(value=cached_val, receipt=receipt)
            return True, cached_val

        reasons = {
            "conflict": (
                "request_id_conflict",
                f"request_id {plan.request_key!r} was already used with different arguments",
            ),
            "in_flight": (
                "in_flight_duplicate",
                f"write {plan.request_key!r} is already in progress; refusing duplicate dispatch",
            ),
            "uncertain_latched": (
                "uncertain_write_blocked",
                f"earlier write {plan.request_key!r} had an uncertain outcome; "
                "inspect the target record and call .release(key) before retrying",
            ),
            "refuted_latched": (
                "refuted_write_blocked",
                f"earlier write {plan.request_key!r} failed post-action verification; "
                "inspect the target record and call .release(key) before retrying",
            ),
        }
        code, reason = reasons.get(
            decision,
            ("ledger_blocked", f"write {plan.request_key!r} blocked by ledger"),
        )
        return True, self._refuse(
            reason=reason,
            code=code,
            request_key=plan.request_key,
            arguments_sha256=plan.arguments_sha256,
            identity=plan.identity,
            declared_by=plan.declared_by,
        )

    def _record_midflight_exception(
        self,
        plan: _InvocationPlan,
        exc: BaseException,
        pending_contract: Any,
    ) -> None:
        contract_sha: str | None = None
        if self.contract_bridge is not None and pending_contract is not None:
            c_outcome, c_reason, contract_sha = self.contract_bridge.verify(
                pending_contract, attempted=True
            )
            reason = f"action raised {type(exc).__name__}: {exc}" + (
                f" ({c_reason})" if c_reason else ""
            )
            latch_state = (
                _LedgerState.REFUTED
                if c_outcome is WrapOutcome.REFUTED
                else _LedgerState.UNCERTAIN
            )
            rec_outcome = (
                WrapOutcome.REFUTED
                if c_outcome is WrapOutcome.REFUTED
                else WrapOutcome.UNCERTAIN
            )
            how = self.contract_bridge.checked_how
        else:
            reason = f"action raised {type(exc).__name__}: {exc}"
            latch_state = _LedgerState.UNCERTAIN
            rec_outcome = WrapOutcome.UNCERTAIN
            how = "unchecked"

        self.ledger.record_non_confirmed(
            key=plan.request_key,
            action=self.action_name,
            arguments_sha256=plan.arguments_sha256,
            state=latch_state,
            checked_how=how,
            reason=reason,
            latch=self.block_uncertain_retries
            or self.idempotent
            or (plan.explicit_request_id is not None),
        )
        self._emit_receipt(
            _build_receipt(
                outcome=rec_outcome,
                checked_how=how,
                action=self.action_name,
                request_key=plan.request_key,
                arguments_sha256=plan.arguments_sha256,
                dispatched=True,
                identity=plan.identity,
                declared_by=plan.declared_by,
                reason=reason,
                contract_receipt_sha256=contract_sha,
                signing_key=self.signing_key,
            )
        )

    def _finalize_post_action(
        self,
        *,
        plan: _InvocationPlan,
        value: Any,
        outcome: WrapOutcome,
        checked_how: str,
        reason: str,
        contract_sha: str | None,
        check_err: Exception | None,
    ) -> Any:
        deduplicate = self.idempotent or (plan.explicit_request_id is not None)
        if outcome in (WrapOutcome.CONFIRMED, WrapOutcome.UNCHECKED):
            self.ledger.record_completed(
                key=plan.request_key,
                action=self.action_name,
                arguments_sha256=plan.arguments_sha256,
                outcome=outcome,
                checked_how=checked_how,
                value=value,
                keep_completed=deduplicate,
            )
            receipt = self._emit_receipt(
                _build_receipt(
                    outcome=outcome,
                    checked_how=checked_how,
                    action=self.action_name,
                    request_key=plan.request_key,
                    arguments_sha256=plan.arguments_sha256,
                    dispatched=True,
                    identity=plan.identity,
                    declared_by=plan.declared_by,
                    reason=reason,
                    contract_receipt_sha256=contract_sha,
                    signing_key=self.signing_key,
                )
            )
            if self.return_receipt:
                return WrapResult(value=value, receipt=receipt)
            return value

        latch_state = (
            _LedgerState.REFUTED
            if outcome is WrapOutcome.REFUTED
            else _LedgerState.UNCERTAIN
        )
        self.ledger.record_non_confirmed(
            key=plan.request_key,
            action=self.action_name,
            arguments_sha256=plan.arguments_sha256,
            state=latch_state,
            checked_how=checked_how,
            reason=reason,
            latch=self.block_uncertain_retries or deduplicate,
        )
        receipt = self._emit_receipt(
            _build_receipt(
                outcome=outcome,
                checked_how=checked_how,
                action=self.action_name,
                request_key=plan.request_key,
                arguments_sha256=plan.arguments_sha256,
                dispatched=True,
                identity=plan.identity,
                declared_by=plan.declared_by,
                reason=reason,
                contract_receipt_sha256=contract_sha,
                signing_key=self.signing_key,
            )
        )
        if self.unverified_mode is _BlockMode.RETURN:
            if self.return_receipt:
                return WrapResult(value=value, receipt=receipt, blocked=False)
            return value

        code = (
            "verification_refuted"
            if outcome is WrapOutcome.REFUTED
            else "verification_uncertain"
        )
        err = WrapVerificationError(
            reason or f"post-action verification {outcome.value}",
            code=code,
            key=plan.request_key,
            receipt=receipt,
            value=value,
        )
        if check_err is not None:
            raise err from check_err
        raise err

    def call_sync(self, *args: Any, **kwargs: Any) -> Any:
        self._clear_receipt()
        if self.check_is_async:
            raise TypeError(
                "async check callable requires wrapping an async coroutine function"
            )
        try:
            plan = self._prepare_call(args, kwargs)
        except _PreCallRefusal as stop:
            return self._refuse(
                reason=stop.reason,
                code=stop.code,
                request_key=stop.request_key,
                arguments_sha256=stop.arguments_sha256,
                identity=stop.identity,
                declared_by=stop.declared_by,
            )

        short_circuit, early_return = self._preflight_ledger(plan)
        if short_circuit:
            return early_return

        pending_contract: Any = None
        if self.contract_bridge is not None:
            try:
                pending_contract = self.contract_bridge.begin(
                    plan.flat_args, self.pinned, plan.explicit_request_id
                )
            except WrapBlockedError as exc:
                self.ledger.abort_in_flight(plan.request_key)
                return self._refuse(
                    reason=exc.reason,
                    code=exc.code,
                    request_key=plan.request_key,
                    arguments_sha256=plan.arguments_sha256,
                    identity=plan.identity,
                    declared_by=plan.declared_by,
                    cause=exc.__cause__,
                )

        try:
            value = self.fn(*plan.call_args, **plan.call_kwargs)
        except BaseException as exc:
            self._record_midflight_exception(plan, exc, pending_contract)
            raise

        if inspect.iscoroutine(value):
            value.close()
            self.ledger.abort_in_flight(plan.request_key)
            if self.contract_bridge is not None and pending_contract is not None:
                self.contract_bridge.verify(pending_contract, attempted=False)
            raise TypeError(
                f"{self.action_name} returned an unawaited coroutine; define it with async def"
            )
        if inspect.isawaitable(value):
            close = getattr(value, "close", None)
            if callable(close):
                close()
            awaitable_err = TypeError(
                f"{self.action_name} returned an awaitable; define it with async def"
            )
            self._record_midflight_exception(plan, awaitable_err, pending_contract)
            raise awaitable_err

        outcome = WrapOutcome.UNCHECKED
        how = self.default_checked_how
        reason = ""
        contract_sha: str | None = None
        check_err: Exception | None = None

        if self.contract_bridge is not None and pending_contract is not None:
            outcome, reason, contract_sha = self.contract_bridge.verify(
                pending_contract, attempted=True
            )
            how = self.default_checked_how

        if self.check_fn is not None and (
            self.contract_bridge is None or outcome is WrapOutcome.CONFIRMED
        ):
            c_outcome, c_how, c_reason, check_err = _normalize_check_tuple(
                _run_sync_check(
                    self.check_fn,
                    self.check_arity,
                    value,
                    plan.call_args,
                    plan.call_kwargs,
                    "same_screen"
                    if self.default_checked_how == "unchecked"
                    else self.default_checked_how,
                )
            )
            outcome = c_outcome
            if self.contract_bridge is None or c_outcome is not WrapOutcome.CONFIRMED:
                how = c_how
            if c_reason:
                reason = c_reason

        return self._finalize_post_action(
            plan=plan,
            value=value,
            outcome=outcome,
            checked_how=how,
            reason=reason,
            contract_sha=contract_sha,
            check_err=check_err,
        )

    async def call_async(self, *args: Any, **kwargs: Any) -> Any:
        self._clear_receipt()
        try:
            plan = self._prepare_call(args, kwargs)
        except _PreCallRefusal as stop:
            return self._refuse(
                reason=stop.reason,
                code=stop.code,
                request_key=stop.request_key,
                arguments_sha256=stop.arguments_sha256,
                identity=stop.identity,
                declared_by=stop.declared_by,
            )

        short_circuit, early_return = self._preflight_ledger(plan)
        if short_circuit:
            return early_return

        pending_contract: Any = None
        if self.contract_bridge is not None:
            try:
                pending_contract = await asyncio.to_thread(
                    self.contract_bridge.begin,
                    plan.flat_args,
                    self.pinned,
                    plan.explicit_request_id,
                )
            except WrapBlockedError as exc:
                self.ledger.abort_in_flight(plan.request_key)
                return self._refuse(
                    reason=exc.reason,
                    code=exc.code,
                    request_key=plan.request_key,
                    arguments_sha256=plan.arguments_sha256,
                    identity=plan.identity,
                    declared_by=plan.declared_by,
                    cause=exc.__cause__,
                )

        try:
            value = await self.fn(*plan.call_args, **plan.call_kwargs)
        except BaseException as exc:
            if self.contract_bridge is not None and pending_contract is not None:
                await asyncio.to_thread(
                    self._record_midflight_exception, plan, exc, pending_contract
                )
            else:
                self._record_midflight_exception(plan, exc, None)
            raise

        outcome = WrapOutcome.UNCHECKED
        how = self.default_checked_how
        reason = ""
        contract_sha: str | None = None
        check_err: Exception | None = None

        if self.contract_bridge is not None and pending_contract is not None:
            outcome, reason, contract_sha = await asyncio.to_thread(
                self.contract_bridge.verify, pending_contract, attempted=True
            )
            how = self.default_checked_how

        if self.check_fn is not None and (
            self.contract_bridge is None or outcome is WrapOutcome.CONFIRMED
        ):
            c_outcome, c_how, c_reason, check_err = _normalize_check_tuple(
                await _run_async_check(
                    self.check_fn,
                    self.check_arity,
                    value,
                    plan.call_args,
                    plan.call_kwargs,
                    "same_screen"
                    if self.default_checked_how == "unchecked"
                    else self.default_checked_how,
                )
            )
            outcome = c_outcome
            if self.contract_bridge is None or c_outcome is not WrapOutcome.CONFIRMED:
                how = c_how
            if c_reason:
                reason = c_reason

        return self._finalize_post_action(
            plan=plan,
            value=value,
            outcome=outcome,
            checked_how=how,
            reason=reason,
            contract_sha=contract_sha,
            check_err=check_err,
        )


class _PreCallRefusal(Exception):
    def __init__(
        self,
        *,
        reason: str,
        code: str,
        request_key: str,
        arguments_sha256: str,
        identity: Mapping[str, Any],
        declared_by: Mapping[str, str],
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code
        self.request_key = request_key
        self.arguments_sha256 = arguments_sha256
        self.identity = identity
        self.declared_by = declared_by


def _build_wrapped_callable(
    fn: Callable[..., Any],
    *,
    action_name: str,
    check_fn: Callable[..., Any] | None,
    checked_how: str | None,
    pinned: Mapping[str, Any],
    identity_keys: tuple[str, ...],
    contract_bridge: _ContractBridge | None,
    ledger: WrapLedger,
    idempotent: bool,
    block_uncertain_retries: bool,
    return_receipt: bool,
    block_mode: _BlockMode,
    unverified_mode: _BlockMode,
    signing_key: Any,
    on_receipt: Callable[[WrapReceipt | None], None] | None = None,
    allow_sync_with_async_check: bool = False,
) -> Callable[..., Any]:
    is_async_fn = _is_async_callable(fn)
    if (
        not is_async_fn
        and check_fn is not None
        and _is_async_callable(check_fn)
        and not allow_sync_with_async_check
    ):
        raise TypeError(
            "async check callable requires wrapping an async coroutine function"
        )
    assigned = tuple(a for a in functools.WRAPPER_ASSIGNMENTS if hasattr(fn, a))

    def _attach_wrapper_attrs(wrapper: Any, guard: _CallableGuard) -> Any:
        if not hasattr(fn, "__name__"):
            wrapper.__name__ = action_name.rsplit(".", 1)[-1]
        if not hasattr(fn, "__qualname__"):
            wrapper.__qualname__ = action_name
        if guard.signature is not None:
            wrapper.__signature__ = guard.signature
        wrapper.__wrapped__ = fn
        wrapper.ledger = ledger
        wrapper.pinned = MappingProxyType(dict(pinned))
        wrapper.identity_keys = identity_keys
        wrapper.last_receipt = None
        wrapper.release = lambda key=None: ledger.release(
            key, action=None if key is not None else action_name
        )
        return wrapper

    if is_async_fn:

        @functools.wraps(fn, assigned=assigned)
        async def _async_wrapped(*args: Any, **kwargs: Any) -> Any:
            return await guard.call_async(*args, **kwargs)

        def _update_async_receipt(receipt: WrapReceipt | None) -> None:
            _async_wrapped.last_receipt = receipt  # type: ignore[attr-defined]
            if on_receipt is not None:
                on_receipt(receipt)

        guard = _CallableGuard(
            fn,
            action_name=action_name,
            check_fn=check_fn,
            checked_how=checked_how,
            pinned=pinned,
            identity_keys=identity_keys,
            contract_bridge=contract_bridge,
            ledger=ledger,
            idempotent=idempotent,
            block_uncertain_retries=block_uncertain_retries,
            return_receipt=return_receipt,
            block_mode=block_mode,
            unverified_mode=unverified_mode,
            signing_key=signing_key,
            on_receipt=_update_async_receipt,
        )
        return _attach_wrapper_attrs(_async_wrapped, guard)

    @functools.wraps(fn, assigned=assigned)
    def _sync_wrapped(*args: Any, **kwargs: Any) -> Any:
        return guard.call_sync(*args, **kwargs)

    def _update_sync_receipt(receipt: WrapReceipt | None) -> None:
        _sync_wrapped.last_receipt = receipt  # type: ignore[attr-defined]
        if on_receipt is not None:
            on_receipt(receipt)

    guard = _CallableGuard(
        fn,
        action_name=action_name,
        check_fn=check_fn,
        checked_how=checked_how,
        pinned=pinned,
        identity_keys=identity_keys,
        contract_bridge=contract_bridge,
        ledger=ledger,
        idempotent=idempotent,
        block_uncertain_retries=block_uncertain_retries,
        return_receipt=return_receipt,
        block_mode=block_mode,
        unverified_mode=unverified_mode,
        signing_key=signing_key,
        on_receipt=_update_sync_receipt,
    )
    return _attach_wrapper_attrs(_sync_wrapped, guard)


class WrappedAgentProxy:
    """Non-mutating proxy wrapping an agent or runner object's ``.run`` / ``.step``."""

    def __init__(
        self,
        target: Any,
        *,
        base_name: str,
        check_fn: Callable[..., Any] | None,
        checked_how: str | None,
        pinned: Mapping[str, Any],
        identity_keys: tuple[str, ...],
        contract_bridge: _ContractBridge | None,
        ledger: WrapLedger,
        idempotent: bool,
        block_uncertain_retries: bool,
        return_receipt: bool,
        block_mode: _BlockMode,
        unverified_mode: _BlockMode,
        signing_key: Any,
    ) -> None:
        object.__setattr__(self, "__wrapped__", target)
        object.__setattr__(self, "ledger", ledger)
        object.__setattr__(self, "pinned", MappingProxyType(dict(pinned)))
        object.__setattr__(self, "identity_keys", identity_keys)
        object.__setattr__(self, "last_receipt", None)

        candidate_methods: list[tuple[str, Callable[..., Any]]] = []
        for method_name in ("run", "arun", "step", "astep"):
            raw_method = getattr(target, method_name, None)
            if callable(raw_method):
                candidate_methods.append((method_name, raw_method))

        if not candidate_methods:
            raise TypeError(
                "openadapt.wrap target must be callable or expose a callable .run or .step method"
            )

        has_async_method = any(_is_async_callable(m) for _, m in candidate_methods)
        if (
            check_fn is not None
            and _is_async_callable(check_fn)
            and not has_async_method
        ):
            raise TypeError(
                "async check callable requires wrapping an async coroutine function"
            )

        wrapped_methods: list[str] = []
        action_names: list[str] = []

        def _record_agent_receipt(receipt: WrapReceipt | None) -> None:
            object.__setattr__(self, "last_receipt", receipt)

        for method_name, raw_method in candidate_methods:
            wrapped_methods.append(method_name)
            action_name = f"{base_name}.{method_name}"
            action_names.append(action_name)
            wrapped_fn = _build_wrapped_callable(
                raw_method,
                action_name=action_name,
                check_fn=check_fn,
                checked_how=checked_how,
                pinned=pinned,
                identity_keys=identity_keys,
                contract_bridge=contract_bridge,
                ledger=ledger,
                idempotent=idempotent,
                block_uncertain_retries=block_uncertain_retries,
                return_receipt=return_receipt,
                block_mode=block_mode,
                unverified_mode=unverified_mode,
                signing_key=signing_key,
                on_receipt=_record_agent_receipt,
                allow_sync_with_async_check=has_async_method,
            )
            object.__setattr__(self, method_name, wrapped_fn)

        object.__setattr__(self, "_wrapped_methods", tuple(wrapped_methods))
        object.__setattr__(self, "_action_names", frozenset(action_names))

    def release(self, key: str | None = None) -> int:
        """Release a latched uncertain or refuted write key on this wrapped agent."""
        action_names = object.__getattribute__(self, "_action_names")
        return self.ledger.release(
            key,
            actions=None if key is not None else action_names,
        )

    def __getattr__(self, item: str) -> Any:
        return getattr(object.__getattribute__(self, "__wrapped__"), item)

    def __repr__(self) -> str:
        inner = object.__getattribute__(self, "__wrapped__")
        return f"WrappedAgentProxy({inner!r})"


def wrap(
    target: Any = None,
    *,
    check: Callable[..., Any] | None = None,
    expect: Callable[..., Any] | None = None,
    checked_how: str | None = None,
    pinned: Mapping[str, Any] | None = None,
    identity_keys: Sequence[str] = (),
    contract: Any = None,
    ledger: WrapLedger | str | Path | None = None,
    idempotent: bool = False,
    block_uncertain_retries: bool = True,
    return_receipt: bool = False,
    on_block: Literal["raise", "return"] = "raise",
    on_unverified: Literal["raise", "return"] | None = None,
    signing_key: Any = None,
    agent_id: str = "agent",
    workdir: str | Path | None = None,
    name: str | None = None,
    supervisor_config: Any = None,
    decision_resolver: Callable[..., Any] | None = None,
    vault: Any = None,
    wal: Any = None,
    cartridge_store: Any = None,
    cartridge: Any = None,
    rung3_verifier: Callable[..., Any] | None = None,
    rung2_verifier: Callable[..., Any] | None = None,
    reopen_executor: Callable[..., Any] | None = None,
    macro_store: Any = None,
    regression_test_runner: Callable[[], tuple[bool, str]] | None = None,
    protected_oracle_paths: Collection[str] = (),
    workspace_root: str = "",
    item_auditor: Callable[[str, str], bool] | None = None,
    on_stall_escalate: Callable[..., None] | None = None,
    micro_handoff_resolver: Callable[..., Any] | None = None,
    completeness_auditor: Callable[[str, str], Mapping[str, Any]] | None = None,
    container_tx: Any = None,
    container_exec_fn: Callable[[str], tuple[int, str, str]] | None = None,
    sop_contract: Any = None,
    dual_egress: Any = None,
    compounding_handoff: Any = None,
) -> Any:
    """Wrap a Python function, coroutine, tool, or agent with commit-boundary checks.

    Supports direct calls (``safe_fn = openadapt.wrap(fn, ...)``), decorator
    syntax (``@openadapt.wrap`` or ``@openadapt.wrap(pinned=...)``),
    ``.run``/``.step`` agent objects, and ``BenchmarkAgent`` objects exposing
    ``.act(observation, task, history)`` (backed by ``SimplexTransducer``).
    """
    if check is not None and expect is not None:
        raise ValueError("pass either check= or expect=, not both")
    resolved_check = check if check is not None else expect
    if resolved_check is not None and not callable(resolved_check):
        raise TypeError("check must be callable or None")

    if checked_how is not None:
        if checked_how not in VALID_CHECKED_HOW:
            raise ValueError(
                f"checked_how must be one of {VALID_CHECKED_HOW}, got {checked_how!r}"
            )
        if resolved_check is not None and checked_how == "unchecked":
            raise ValueError(
                "checked_how cannot be 'unchecked' when a post-action check is supplied"
            )

    if isinstance(identity_keys, (str, bytes)):
        raise TypeError(
            "identity_keys must be a sequence of parameter names, not a string"
        )
    resolved_identity_keys = tuple(dict.fromkeys(str(k) for k in identity_keys))
    resolved_pinned = dict(pinned or {})

    block_mode = _MODE_TOKENS.get(str(on_block).strip().lower())
    if block_mode is None:
        raise ValueError("on_block must be 'raise' or 'return'")

    if on_unverified is None:
        unverified_mode = (
            _BlockMode.RETURN
            if (return_receipt or block_mode is _BlockMode.RETURN)
            else _BlockMode.RAISE
        )
    else:
        maybe_mode = _MODE_TOKENS.get(str(on_unverified).strip().lower())
        if maybe_mode is None:
            raise ValueError("on_unverified must be 'raise' or 'return'")
        unverified_mode = maybe_mode

    if signing_key is not None:
        signing_key = _load_signing_key(signing_key)

    resolved_ledger = _resolve_ledger(ledger)
    contract_bridge = (
        _ContractBridge(contract, agent_id=agent_id, workdir=workdir)
        if contract is not None
        else None
    )

    def _apply(obj: Any) -> Any:
        if (
            not inspect.isclass(obj)
            and not inspect.isroutine(obj)
            and callable(getattr(obj, "act", None))
        ):
            from openadapt.simplex_transducer import SimplexTransducer

            return SimplexTransducer(
                obj,
                config=supervisor_config,
                decision_resolver=decision_resolver,
                vault=vault,
                wal=wal,
                cartridge_store=cartridge_store,
                cartridge=cartridge,
                rung3_verifier=rung3_verifier,
                rung2_verifier=rung2_verifier,
                reopen_executor=reopen_executor,
                pinned=resolved_pinned,
                macro_store=macro_store,
                regression_test_runner=regression_test_runner,
                protected_oracle_paths=protected_oracle_paths,
                workspace_root=workspace_root,
                item_auditor=item_auditor,
                on_stall_escalate=on_stall_escalate,
                micro_handoff_resolver=micro_handoff_resolver,
                completeness_auditor=completeness_auditor,
                container_tx=container_tx,
                container_exec_fn=container_exec_fn,
                sop_contract=sop_contract,
                dual_egress=dual_egress,
                compounding_handoff=compounding_handoff,
            )
        if callable(obj) and not inspect.isclass(obj):
            action_name = (
                name
                or getattr(obj, "__qualname__", None)
                or getattr(obj, "__name__", None)
                or type(obj).__qualname__
            )
            return _build_wrapped_callable(
                obj,
                action_name=action_name,
                check_fn=resolved_check,
                checked_how=checked_how,
                pinned=resolved_pinned,
                identity_keys=resolved_identity_keys,
                contract_bridge=contract_bridge,
                ledger=resolved_ledger,
                idempotent=idempotent,
                block_uncertain_retries=block_uncertain_retries,
                return_receipt=return_receipt,
                block_mode=block_mode,
                unverified_mode=unverified_mode,
                signing_key=signing_key,
            )
        base_name = name or type(obj).__qualname__
        return WrappedAgentProxy(
            obj,
            base_name=base_name,
            check_fn=resolved_check,
            checked_how=checked_how,
            pinned=resolved_pinned,
            identity_keys=resolved_identity_keys,
            contract_bridge=contract_bridge,
            ledger=resolved_ledger,
            idempotent=idempotent,
            block_uncertain_retries=block_uncertain_retries,
            return_receipt=return_receipt,
            block_mode=block_mode,
            unverified_mode=unverified_mode,
            signing_key=signing_key,
        )

    if target is not None:
        return _apply(target)
    return _apply
