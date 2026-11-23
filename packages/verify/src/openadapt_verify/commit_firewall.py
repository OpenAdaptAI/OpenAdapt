"""UI commit check helper for duck-typed sync/async browser pages and screenshots.

Tested against duck-typed sync/async page objects exposing field-level readers
(``read_field``, ``get_by_label``, ``input_value``) and raw screenshot bytes
paired with a visual verifier. This module provides same-screen UI checks (not
an independent system-of-record store verifier like ``openadapt_verify.guard``):

1. **Exact field-level DOM checks (no body-text substring fallback):** Reads
   each expected field directly via ``read_field`` / ``get_by_label`` /
   ``input_value`` with exact Unicode comparison (no NFKC folding). Empty
   ``expected`` maps and field-read errors always fail closed.
2. **Optional visual veto check:** Duck-typed for ``PrePostSubmitVisualVeto``
   or a custom visual verifier callable, enforcing strict ``ABSTAIN -> HALT``
   semantics and passing ``deterministic_passed=False`` whenever no DOM check
   ran.
3. **Explicitly keyed Ed25519 receipts:** Mints a signed ``record/run/v1`` DSSE
   receipt only when the caller supplies an explicit ``signing_key`` (never
   silently generates an ephemeral per-call key) and binds both
   ``expected_values_sha256`` and ``record_id`` into the signed statement.
4. **Pre-commit gate & exception recording:** :class:`UiCommitCheck` supports
   an optional ``pre_expected`` check in ``__enter__`` / ``__aenter__`` that
   halts BEFORE the commit body executes, and records an ``ABSTAIN`` inbox
   entry if the wrapped body raises.
"""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Optional, Union

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from openadapt_types import receipt as receipt_lib

CommitStatus = Literal[
    "VERIFIED", "SAME_SCREEN", "VISUAL_ONLY", "STOPPED", "UNCONFIRMED_WRITE"
]
CommitVerdict = Literal["VERIFIED", "SAME_SCREEN", "VISUAL_ONLY", "VETO", "ABSTAIN"]
ScreenshotInput = Union[bytes, bytearray, str, Path, None]


@dataclass(frozen=True)
class CommitResult:
    """Outcome of a UI commit check."""

    status: CommitStatus
    verdict: CommitVerdict
    intent: str
    reason: str = ""
    receipt_id: Optional[str] = None
    receipt_path: Optional[Path] = None
    receipt: Optional[dict[str, Any]] = None
    public_key: Optional[str] = None
    key_id: Optional[str] = None
    mismatches: tuple[str, ...] = ()
    inbox_item: Optional[dict[str, Any]] = None
    inbox_path: Optional[Path] = None

    @property
    def verified(self) -> bool:
        return self.status == "VERIFIED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "verdict": self.verdict,
            "verified": self.verified,
            "intent": self.intent,
            "reason": self.reason,
            "receipt_id": self.receipt_id,
            "receipt_path": str(self.receipt_path) if self.receipt_path else None,
            "key_id": self.key_id,
            "public_key": self.public_key,
            "mismatches": list(self.mismatches),
            "inbox_item": self.inbox_item,
            "inbox_path": str(self.inbox_path) if self.inbox_path else None,
        }


class CommitBlockedError(RuntimeError):
    """Raised when a UI commit check halts before or after a commit step."""

    def __init__(self, result: CommitResult) -> None:
        self.result = result
        self.reason = result.reason
        self.inbox_item = result.inbox_item
        super().__init__(
            f"Commit blocked ({result.verdict}) for intent {result.intent!r}: "
            f"{result.reason or 'verification failed'}"
        )


def _is_page_like(obj: Any) -> bool:
    if obj is None or isinstance(obj, (bytes, bytearray, str, Path)):
        return False
    return any(
        hasattr(obj, attr)
        for attr in (
            "read_field",
            "get_by_label",
            "input_value",
            "locator",
            "inner_text",
            "text_content",
            "content",
            "screenshot",
        )
    )


def _coerce_screenshot_bytes(source: ScreenshotInput) -> Optional[bytes]:
    if source is None:
        return None
    if isinstance(source, (bytes, bytearray)):
        return bytes(source)
    if isinstance(source, (str, Path)):
        p = Path(source)
        if p.is_file():
            return p.read_bytes()
    return None


def _read_field_sync(page: Any, key: str) -> tuple[bool, Optional[str], Optional[str]]:
    """Read a field directly from a page object.

    Returns ``(found, value, error_detail)``. Never falls back to body-text
    substring matching.
    """
    read_field = getattr(page, "read_field", None)
    if callable(read_field):
        try:
            val = read_field(key)
            return True, None if val is None else str(val), None
        except KeyError:
            return False, None, f"field {key!r} not found via read_field"
        except Exception as exc:  # noqa: BLE001
            return False, None, f"read_field({key!r}) raised {type(exc).__name__}"

    get_by_label = getattr(page, "get_by_label", None)
    if callable(get_by_label):
        try:
            loc = get_by_label(key)
            for attr in ("input_value", "inner_text", "text_content"):
                getter = getattr(loc, attr, None)
                if callable(getter):
                    val = getter()
                    if isinstance(val, str):
                        return True, val, None
        except KeyError:
            return False, None, f"field {key!r} not found via get_by_label"
        except Exception as exc:  # noqa: BLE001
            return False, None, f"get_by_label({key!r}) raised {type(exc).__name__}"

    input_value = getattr(page, "input_value", None)
    if callable(input_value):
        try:
            val = input_value(key)
            if isinstance(val, str):
                return True, val, None
        except KeyError:
            return False, None, f"field {key!r} not found via input_value"
        except Exception as exc:  # noqa: BLE001
            return False, None, f"input_value({key!r}) raised {type(exc).__name__}"

    return False, None, f"page exposes no field-level reader for {key!r}"


async def _read_field_async(
    page: Any, key: str
) -> tuple[bool, Optional[str], Optional[str]]:
    read_field = getattr(page, "read_field", None)
    if callable(read_field):
        try:
            val = read_field(key)
            if inspect.isawaitable(val):
                val = await val
            return True, None if val is None else str(val), None
        except KeyError:
            return False, None, f"field {key!r} not found via read_field"
        except Exception as exc:  # noqa: BLE001
            return False, None, f"read_field({key!r}) raised {type(exc).__name__}"

    get_by_label = getattr(page, "get_by_label", None)
    if callable(get_by_label):
        try:
            loc = get_by_label(key)
            for attr in ("input_value", "inner_text", "text_content"):
                getter = getattr(loc, attr, None)
                if callable(getter):
                    val = getter()
                    if inspect.isawaitable(val):
                        val = await val
                    if isinstance(val, str):
                        return True, val, None
        except KeyError:
            return False, None, f"field {key!r} not found via get_by_label"
        except Exception as exc:  # noqa: BLE001
            return False, None, f"get_by_label({key!r}) raised {type(exc).__name__}"

    input_value = getattr(page, "input_value", None)
    if callable(input_value):
        try:
            val = input_value(key)
            if inspect.isawaitable(val):
                val = await val
            if isinstance(val, str):
                return True, val, None
        except KeyError:
            return False, None, f"field {key!r} not found via input_value"
        except Exception as exc:  # noqa: BLE001
            return False, None, f"input_value({key!r}) raised {type(exc).__name__}"

    return False, None, f"page exposes no field-level reader for {key!r}"


def _check_dom_sync(
    page: Any, expected: Mapping[str, str]
) -> tuple[bool, tuple[str, ...]]:
    """Run exact field-level Unicode checks (no NFKC folding, no body-text fallback)."""
    mismatches: list[str] = []
    for field_key, expected_val in expected.items():
        expected_str = str(expected_val)
        found, actual, err = _read_field_sync(page, field_key)
        if not found:
            mismatches.append(err or f"field {field_key!r}: unreadable")
            continue
        if actual != expected_str:
            mismatches.append(
                f"field {field_key!r}: expected {expected_str!r}, observed {actual!r}"
            )
    return len(mismatches) == 0, tuple(mismatches)


async def _check_dom_async(
    page: Any, expected: Mapping[str, str]
) -> tuple[bool, tuple[str, ...]]:
    mismatches: list[str] = []
    for field_key, expected_val in expected.items():
        expected_str = str(expected_val)
        found, actual, err = await _read_field_async(page, field_key)
        if not found:
            mismatches.append(err or f"field {field_key!r}: unreadable")
            continue
        if actual != expected_str:
            mismatches.append(
                f"field {field_key!r}: expected {expected_str!r}, observed {actual!r}"
            )
    return len(mismatches) == 0, tuple(mismatches)


def _normalize_visual_outcome(raw: Any) -> tuple[CommitVerdict, str]:
    """Normalize a visual verifier reply into (VERIFIED | VETO | ABSTAIN, reason).

    Strict fail-closed mapping: any unknown, occluded, uncertain, or abstained
    reply maps to ``ABSTAIN`` (which halts the commit).
    """
    if raw is None:
        return "ABSTAIN", "visual verifier returned no decision"

    decision_obj = getattr(raw, "decision", None)
    if decision_obj is not None:
        raw = decision_obj

    reason = ""
    token: Any = raw
    if isinstance(raw, Mapping):
        token = raw.get("verdict") or raw.get("status") or raw.get("decision")
        reason = str(raw.get("reason") or raw.get("detail") or "")
    elif not isinstance(raw, str):
        verdict_attr = (
            getattr(raw, "verdict", None)
            or getattr(raw, "status", None)
            or getattr(raw, "decision", None)
        )
        if verdict_attr is not None:
            token = getattr(verdict_attr, "value", verdict_attr)
        reason = str(getattr(raw, "reason", "") or getattr(raw, "detail", "") or "")

    norm = str(getattr(token, "value", token) or "").strip().upper()
    if norm in ("PASS", "VERIFIED", "OK", "CONFIRMED"):
        return "VERIFIED", reason
    if norm in ("VETO", "REFUTED", "FAIL", "FAILED", "REJECT", "BLOCKED"):
        return "VETO", reason or "visual verifier vetoed commit"
    if norm in ("ABSTAIN", "OCCLUDED", "UNCERTAIN", "UNKNOWN", "UNREADABLE"):
        return "ABSTAIN", reason or "visual verifier abstained (possible occlusion)"
    return "ABSTAIN", reason or f"unrecognized visual verdict {norm!r} (fail-closed)"


def _run_visual_verifier_sync(
    verifier: Any,
    *,
    before_png: Optional[bytes],
    after_png: Optional[bytes],
    intent: str,
    expected: Mapping[str, str],
    deterministic_passed: bool,
) -> tuple[CommitVerdict, str]:
    try:
        check_post = getattr(verifier, "check_post_submit", None)
        if callable(check_post) and after_png is not None:
            res = check_post(
                screenshot=after_png,
                expected_state=intent,
                deterministic_passed=deterministic_passed,
            )
            return _normalize_visual_outcome(res)

        check_pre = getattr(verifier, "check_pre_submit", None)
        if callable(check_pre) and before_png is not None and after_png is not None:
            res = check_pre(
                recorded_png=before_png,
                live_png=after_png,
                deterministic_passed=deterministic_passed,
                expected_state=intent,
            )
            return _normalize_visual_outcome(res)

        fn = getattr(verifier, "verify", None) or getattr(verifier, "check", None)
        if callable(fn):
            res = fn(
                before_screenshot=before_png,
                after_screenshot=after_png,
                intent=intent,
                expected=dict(expected),
            )
            return _normalize_visual_outcome(res)

        if callable(verifier):
            res = verifier(
                before_screenshot=before_png,
                after_screenshot=after_png,
                intent=intent,
                expected=dict(expected),
            )
            return _normalize_visual_outcome(res)
    except Exception as exc:  # noqa: BLE001
        return "ABSTAIN", f"visual verifier error (fail-closed): {exc}"

    return "ABSTAIN", "unsupported visual verifier interface (fail-closed)"


async def _run_visual_verifier_async(
    verifier: Any,
    *,
    before_png: Optional[bytes],
    after_png: Optional[bytes],
    intent: str,
    expected: Mapping[str, str],
    deterministic_passed: bool,
) -> tuple[CommitVerdict, str]:
    try:
        check_post = getattr(verifier, "check_post_submit", None)
        if callable(check_post) and after_png is not None:
            res = check_post(
                screenshot=after_png,
                expected_state=intent,
                deterministic_passed=deterministic_passed,
            )
            if inspect.isawaitable(res):
                res = await res
            return _normalize_visual_outcome(res)

        check_pre = getattr(verifier, "check_pre_submit", None)
        if callable(check_pre) and before_png is not None and after_png is not None:
            res = check_pre(
                recorded_png=before_png,
                live_png=after_png,
                deterministic_passed=deterministic_passed,
                expected_state=intent,
            )
            if inspect.isawaitable(res):
                res = await res
            return _normalize_visual_outcome(res)

        fn = getattr(verifier, "verify", None) or getattr(verifier, "check", None)
        if callable(fn):
            res = fn(
                before_screenshot=before_png,
                after_screenshot=after_png,
                intent=intent,
                expected=dict(expected),
            )
            if inspect.isawaitable(res):
                res = await res
            return _normalize_visual_outcome(res)

        if callable(verifier):
            res = verifier(
                before_screenshot=before_png,
                after_screenshot=after_png,
                intent=intent,
                expected=dict(expected),
            )
            if inspect.isawaitable(res):
                res = await res
            return _normalize_visual_outcome(res)
    except Exception as exc:  # noqa: BLE001
        return "ABSTAIN", f"visual verifier error (fail-closed): {exc}"

    return "ABSTAIN", "unsupported visual verifier interface (fail-closed)"


def _build_signed_receipt(
    *,
    intent: str,
    expected: Mapping[str, str],
    record_id: str,
    dom_checked: bool,
    used_visual_verifier: bool,
    after_png: Optional[bytes],
    receipt_dir: Optional[Union[str, Path]],
    signing_key: Ed25519PrivateKey,
) -> tuple[str, dict[str, Any], Optional[Path], str, str]:
    """Emit a signed Ed25519 ``record/run/v1`` DSSE receipt bound to expected values & record_id."""
    pub_raw = signing_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    pub_b64 = base64.b64encode(pub_raw).decode("ascii")
    kid = receipt_lib.key_id(pub_raw)

    intent_sha = hashlib.sha256(intent.encode("utf-8")).hexdigest()
    canonical_expected = {str(k): str(v) for k, v in sorted(expected.items())}
    expected_values_sha = hashlib.sha256(
        receipt_lib.canonical_json(canonical_expected).encode("utf-8")
    ).hexdigest()

    report_basis = {
        "intent_sha256": intent_sha,
        "record_id": record_id,
        "expected_keys": sorted(canonical_expected.keys()),
        "expected_values_sha256": expected_values_sha,
        "outcome": "VERIFIED",
        "dom_checked": dom_checked,
        "visual_checked": used_visual_verifier,
        "strength": "independent_readback",
    }
    report_sha = hashlib.sha256(
        receipt_lib.canonical_json(report_basis).encode("utf-8")
    ).hexdigest()

    subjects: list[dict[str, Any]] = [
        {"name": "report.json", "digest": {"sha256": report_sha}},
        {
            "name": "payload",
            "media_type": "application/json",
            "digest": {"sha256": expected_values_sha},
        },
    ]
    if after_png is not None:
        subjects.append(
            {
                "name": "evidence",
                "digest": {"sha256": hashlib.sha256(after_png).hexdigest()},
            }
        )

    started_hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00:00Z")
    # Same-screen checks do NOT claim independent store/identity confirmation.
    post_confirmed = 1 if dom_checked else 0
    statement: dict[str, Any] = {
        "_type": receipt_lib.STATEMENT_TYPE,
        "subject": subjects,
        "predicateType": receipt_lib.PREDICATE_TYPE,
        "predicate": {
            "program": {
                "id": receipt_lib.program_id(intent, intent_sha),
                "version_sha256": intent_sha,
            },
            "started_hour": started_hour,
            "outcome": "VERIFIED",
            "steps": {"total": 1, "ok": 1},
            "lines": [
                {
                    "step": 0,
                    "target_kind": "gui",
                    "operation": "irreversible_write",
                    "check": "confirmed",
                    "strength": "read_back",
                    "not_checked": ["effect_not_declared"],
                }
            ],
            "checks": {
                "identity": {"required": 0, "confirmed": 0},
                "postconditions": {"required": 1, "confirmed": post_confirmed},
                "effects": {"required": 0, "confirmed": 0},
                "authorization": {"required": 0, "confirmed": 0},
                "lines_confirmed": post_confirmed,
                "lines_refuted": 0,
                "lines_not_checked": 0 if dom_checked else 1,
            },
            "model_calls": 1 if used_visual_verifier else 0,
            "disclosure": {
                "version": receipt_lib.PUBLIC_DISCLOSURE_VERSION,
                "commitments": {},
            },
        },
    }

    envelope = receipt_lib.sign_statement(statement, [(kid, signing_key.sign)])
    rid = receipt_lib.payload_sha256(envelope)

    written_path: Optional[Path] = None
    if receipt_dir is not None:
        rdir = Path(receipt_dir)
        rdir.mkdir(parents=True, exist_ok=True)
        written_path = rdir / f"receipt-{rid[:12]}.json"
        written_path.write_text(
            json.dumps(envelope, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    return rid, envelope, written_path, kid, pub_b64


def _write_inbox_item(
    inbox: Optional[Union[str, Path]],
    *,
    intent: str,
    status: CommitStatus,
    verdict: CommitVerdict,
    reason: str,
    mismatches: tuple[str, ...],
    expected: Mapping[str, str],
) -> tuple[dict[str, Any], Optional[Path]]:
    item: dict[str, Any] = {
        "schema": "openadapt.commit-inbox/v1",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "intent": intent,
        "status": status,
        "verdict": verdict,
        "reason": reason,
        "mismatches": list(mismatches),
        "expected_keys": sorted(str(k) for k in expected.keys()),
    }
    inbox_path: Optional[Path] = None
    if inbox is not None:
        inbox_path = Path(inbox)
        inbox_path.parent.mkdir(parents=True, exist_ok=True)
        with inbox_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(item, sort_keys=True) + "\n")
    return item, inbox_path


def _finalize_commit(
    *,
    intent: str,
    expected: Mapping[str, str],
    record_id: str,
    dom_checked: bool,
    dom_passed: bool,
    dom_independent: bool,
    mismatches: tuple[str, ...],
    visual_verdict: Optional[CommitVerdict],
    visual_reason: str,
    after_png: Optional[bytes],
    inbox: Optional[Union[str, Path]],
    receipt_dir: Optional[Union[str, Path]],
    signing_key: Optional[Ed25519PrivateKey],
    raise_on_block: bool,
    stage: Literal["pre_commit", "post_commit"] = "post_commit",
) -> CommitResult:
    # Strict fail-closed combination rule:
    # 1. Empty `expected` ALWAYS abstains/halts (never VERIFIED on nothing).
    # 2. Visual ABSTAIN ALWAYS halts (clean DOM never overrides occlusion).
    # 3. Visual VETO ALWAYS halts.
    # 4. DOM mismatch ALWAYS halts (passing visual never overrides bad DOM).
    # 5. Visual-only (no DOM check) NEVER returns VERIFIED -> VISUAL_ONLY (no signed VERIFIED receipt).
    # 6. Same-screen DOM read (no independent_page) NEVER returns VERIFIED -> SAME_SCREEN (no signed VERIFIED receipt).
    # 7. Only an independent read-back (dom_independent=True) returns VERIFIED and may mint a signed receipt.
    # 8. Post-commit failures return UNCONFIRMED_WRITE (not STOPPED) so callers know the write may have landed.
    fail_status: CommitStatus = (
        "STOPPED" if stage == "pre_commit" else "UNCONFIRMED_WRITE"
    )
    final_verdict: CommitVerdict = "VERIFIED"
    reason = ""

    if not expected:
        final_verdict = "ABSTAIN"
        reason = "expected must contain at least one field to verify"
    elif visual_verdict == "ABSTAIN":
        final_verdict = "ABSTAIN"
        reason = visual_reason or "visual verifier abstained (ABSTAIN -> HALT)"
    elif visual_verdict == "VETO":
        final_verdict = "VETO"
        reason = visual_reason or "visual verifier vetoed commit"
    elif dom_checked and not dom_passed:
        final_verdict = "VETO"
        reason = "; ".join(mismatches) or "DOM verification failed"
    elif not dom_checked and visual_verdict is None:
        final_verdict = "ABSTAIN"
        reason = "no DOM page or visual verifier available to verify expected fields"
    elif not dom_checked:
        # Visual-only path: never VERIFIED and never signs an outcome=VERIFIED receipt.
        return CommitResult(
            status="VISUAL_ONLY",
            verdict="VISUAL_ONLY",
            intent=intent,
            reason=visual_reason
            or "visual-only check passed; DOM read-back did not run (not VERIFIED)",
        )
    elif not dom_independent:
        # Same-screen DOM check: never VERIFIED and never signs an outcome=VERIFIED receipt.
        return CommitResult(
            status="SAME_SCREEN",
            verdict="SAME_SCREEN",
            intent=intent,
            reason="same-screen DOM fields matched; independent read-back did not run (not VERIFIED)",
        )

    if final_verdict != "VERIFIED":
        inbox_item, inbox_path = _write_inbox_item(
            inbox,
            intent=intent,
            status=fail_status,
            verdict=final_verdict,
            reason=reason,
            mismatches=mismatches,
            expected=expected,
        )
        result = CommitResult(
            status=fail_status,
            verdict=final_verdict,
            intent=intent,
            reason=reason,
            mismatches=mismatches,
            inbox_item=inbox_item,
            inbox_path=inbox_path,
        )
        if raise_on_block:
            raise CommitBlockedError(result)
        return result

    rid: Optional[str] = None
    envelope: Optional[dict[str, Any]] = None
    receipt_path: Optional[Path] = None
    kid: Optional[str] = None
    pub_b64: Optional[str] = None
    if signing_key is not None:
        if not str(record_id).strip():
            raise ValueError("record_id is required when signing_key is provided")
        rid, envelope, receipt_path, kid, pub_b64 = _build_signed_receipt(
            intent=intent,
            expected=expected,
            record_id=record_id,
            dom_checked=dom_checked,
            used_visual_verifier=visual_verdict is not None,
            after_png=after_png,
            receipt_dir=receipt_dir,
            signing_key=signing_key,
        )

    return CommitResult(
        status="VERIFIED",
        verdict="VERIFIED",
        intent=intent,
        reason="verified",
        receipt_id=rid,
        receipt_path=receipt_path,
        receipt=envelope,
        public_key=pub_b64,
        key_id=kid,
    )


def verify_commit(
    page_or_screenshot: Any,
    *,
    intent: str,
    expected: Mapping[str, str],
    record_id: str = "",
    independent_page: Any = None,
    before_screenshot: ScreenshotInput = None,
    verifier: Any = None,
    inbox: Optional[Union[str, Path]] = None,
    receipt_dir: Optional[Union[str, Path]] = None,
    signing_key: Optional[Ed25519PrivateKey] = None,
    raise_on_block: bool = False,
    stage: Literal["pre_commit", "post_commit"] = "post_commit",
) -> CommitResult:
    """Verify UI commit fields (sync) with strict fail-closed semantics.

    - Requires non-empty ``expected``.
    - Same-screen DOM reads (without ``independent_page``) return at most
      ``SAME_SCREEN`` (``verified=False``) and never sign ``outcome: VERIFIED``.
    - Visual-only checks (no DOM page) return at most ``VISUAL_ONLY``
      (``verified=False``) and never sign ``outcome: VERIFIED``.
    - Supplying an explicit ``independent_page`` (reloaded page, separate
      session, or store reader) enables ``VERIFIED`` and signed Ed25519
      receipts when ``signing_key`` and non-empty ``record_id`` are provided.
    """
    dom_checked = False
    dom_passed = False
    dom_independent = False
    mismatches: tuple[str, ...] = ()
    after_png: Optional[bytes] = None
    before_png = _coerce_screenshot_bytes(before_screenshot)

    read_target = (
        independent_page if independent_page is not None else page_or_screenshot
    )
    if expected and _is_page_like(read_target):
        dom_checked = True
        dom_independent = independent_page is not None
        dom_passed, mismatches = _check_dom_sync(read_target, expected)
        shot_fn = getattr(read_target, "screenshot", None)
        if callable(shot_fn):
            try:
                raw_shot = shot_fn()
                after_png = _coerce_screenshot_bytes(raw_shot)
            except Exception:
                after_png = None
    else:
        after_png = _coerce_screenshot_bytes(page_or_screenshot)

    visual_verdict: Optional[CommitVerdict] = None
    visual_reason = ""
    if expected and verifier is not None:
        visual_verdict, visual_reason = _run_visual_verifier_sync(
            verifier,
            before_png=before_png,
            after_png=after_png,
            intent=intent,
            expected=expected,
            deterministic_passed=dom_passed if dom_checked else False,
        )

    return _finalize_commit(
        intent=intent,
        expected=expected,
        record_id=record_id,
        dom_checked=dom_checked,
        dom_passed=dom_passed,
        dom_independent=dom_independent,
        mismatches=mismatches,
        visual_verdict=visual_verdict,
        visual_reason=visual_reason,
        after_png=after_png,
        inbox=inbox,
        receipt_dir=receipt_dir,
        signing_key=signing_key,
        raise_on_block=raise_on_block,
        stage=stage,
    )


async def async_verify_commit(
    page_or_screenshot: Any,
    *,
    intent: str,
    expected: Mapping[str, str],
    record_id: str = "",
    independent_page: Any = None,
    before_screenshot: ScreenshotInput = None,
    verifier: Any = None,
    inbox: Optional[Union[str, Path]] = None,
    receipt_dir: Optional[Union[str, Path]] = None,
    signing_key: Optional[Ed25519PrivateKey] = None,
    raise_on_block: bool = False,
    stage: Literal["pre_commit", "post_commit"] = "post_commit",
) -> CommitResult:
    """Async counterpart to :func:`verify_commit` for async page objects."""
    dom_checked = False
    dom_passed = False
    dom_independent = False
    mismatches: tuple[str, ...] = ()
    after_png: Optional[bytes] = None
    before_png = _coerce_screenshot_bytes(before_screenshot)

    read_target = (
        independent_page if independent_page is not None else page_or_screenshot
    )
    if expected and _is_page_like(read_target):
        dom_checked = True
        dom_independent = independent_page is not None
        dom_passed, mismatches = await _check_dom_async(read_target, expected)
        shot_fn = getattr(read_target, "screenshot", None)
        if callable(shot_fn):
            try:
                raw_shot = shot_fn()
                if inspect.isawaitable(raw_shot):
                    raw_shot = await raw_shot
                after_png = _coerce_screenshot_bytes(raw_shot)
            except Exception:
                after_png = None
    else:
        after_png = _coerce_screenshot_bytes(page_or_screenshot)

    visual_verdict: Optional[CommitVerdict] = None
    visual_reason = ""
    if expected and verifier is not None:
        visual_verdict, visual_reason = await _run_visual_verifier_async(
            verifier,
            before_png=before_png,
            after_png=after_png,
            intent=intent,
            expected=expected,
            deterministic_passed=dom_passed if dom_checked else False,
        )

    return _finalize_commit(
        intent=intent,
        expected=expected,
        record_id=record_id,
        dom_checked=dom_checked,
        dom_passed=dom_passed,
        dom_independent=dom_independent,
        mismatches=mismatches,
        visual_verdict=visual_verdict,
        visual_reason=visual_reason,
        after_png=after_png,
        inbox=inbox,
        receipt_dir=receipt_dir,
        signing_key=signing_key,
        raise_on_block=raise_on_block,
        stage=stage,
    )


class UiCommitCheck:
    """Dual sync/async context manager for optional pre-commit gating and post-commit UI read-back.

    ``pre_expected`` defaults to ``None`` (nothing gates the write before the
    body runs unless ``pre_expected`` is supplied). When ``pre_expected`` fails
    with ``raise_on_block=False``, ``__exit__`` / ``__aexit__`` preserves the
    ``STOPPED`` pre-check result and skips the post-check.
    """

    def __init__(
        self,
        page: Any,
        *,
        intent: str,
        expected: Mapping[str, str],
        pre_expected: Optional[Mapping[str, str]] = None,
        record_id: str = "",
        independent_page: Any = None,
        verifier: Any = None,
        inbox: Optional[Union[str, Path]] = None,
        receipt_dir: Optional[Union[str, Path]] = None,
        signing_key: Optional[Ed25519PrivateKey] = None,
        raise_on_block: bool = True,
    ) -> None:
        self._page = page
        self._intent = intent
        self._expected = dict(expected)
        self._pre_expected = dict(pre_expected) if pre_expected is not None else None
        self._record_id = record_id
        self._independent_page = independent_page
        self._verifier = verifier
        self._inbox = inbox
        self._receipt_dir = receipt_dir
        self._signing_key = signing_key
        self._raise_on_block = raise_on_block
        self._pre_blocked = False
        self.before_screenshot: Optional[bytes] = None
        self.result: Optional[CommitResult] = None

    def __enter__(self) -> UiCommitCheck:
        shot_fn = getattr(self._page, "screenshot", None)
        if callable(shot_fn):
            try:
                self.before_screenshot = _coerce_screenshot_bytes(shot_fn())
            except Exception:
                self.before_screenshot = None
        if self._pre_expected is not None:
            pre_res = verify_commit(
                self._page,
                intent=f"{self._intent} (pre-commit)",
                expected=self._pre_expected,
                record_id=self._record_id,
                inbox=self._inbox,
                raise_on_block=self._raise_on_block,
                stage="pre_commit",
            )
            if pre_res.status not in ("VERIFIED", "SAME_SCREEN"):
                self._pre_blocked = True
                self.result = pre_res
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Literal[False]:
        if self._pre_blocked:
            return False
        if exc_type is not None:
            reason = f"commit body raised {exc_type.__name__}: {exc}"
            inbox_item, inbox_path = _write_inbox_item(
                self._inbox,
                intent=self._intent,
                status="UNCONFIRMED_WRITE",
                verdict="ABSTAIN",
                reason=reason,
                mismatches=(),
                expected=self._expected,
            )
            self.result = CommitResult(
                status="UNCONFIRMED_WRITE",
                verdict="ABSTAIN",
                intent=self._intent,
                reason=reason,
                inbox_item=inbox_item,
                inbox_path=inbox_path,
            )
            return False
        self.result = verify_commit(
            self._page,
            intent=self._intent,
            expected=self._expected,
            record_id=self._record_id,
            independent_page=self._independent_page,
            before_screenshot=self.before_screenshot,
            verifier=self._verifier,
            inbox=self._inbox,
            receipt_dir=self._receipt_dir,
            signing_key=self._signing_key,
            raise_on_block=self._raise_on_block,
            stage="post_commit",
        )
        return False

    async def __aenter__(self) -> UiCommitCheck:
        shot_fn = getattr(self._page, "screenshot", None)
        if callable(shot_fn):
            try:
                raw = shot_fn()
                if inspect.isawaitable(raw):
                    raw = await raw
                self.before_screenshot = _coerce_screenshot_bytes(raw)
            except Exception:
                self.before_screenshot = None
        if self._pre_expected is not None:
            pre_res = await async_verify_commit(
                self._page,
                intent=f"{self._intent} (pre-commit)",
                expected=self._pre_expected,
                record_id=self._record_id,
                inbox=self._inbox,
                raise_on_block=self._raise_on_block,
                stage="pre_commit",
            )
            if pre_res.status not in ("VERIFIED", "SAME_SCREEN"):
                self._pre_blocked = True
                self.result = pre_res
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Literal[False]:
        if self._pre_blocked:
            return False
        if exc_type is not None:
            reason = f"commit body raised {exc_type.__name__}: {exc}"
            inbox_item, inbox_path = _write_inbox_item(
                self._inbox,
                intent=self._intent,
                status="UNCONFIRMED_WRITE",
                verdict="ABSTAIN",
                reason=reason,
                mismatches=(),
                expected=self._expected,
            )
            self.result = CommitResult(
                status="UNCONFIRMED_WRITE",
                verdict="ABSTAIN",
                intent=self._intent,
                reason=reason,
                inbox_item=inbox_item,
                inbox_path=inbox_path,
            )
            return False
        self.result = await async_verify_commit(
            self._page,
            intent=self._intent,
            expected=self._expected,
            record_id=self._record_id,
            independent_page=self._independent_page,
            before_screenshot=self.before_screenshot,
            verifier=self._verifier,
            inbox=self._inbox,
            receipt_dir=self._receipt_dir,
            signing_key=self._signing_key,
            raise_on_block=self._raise_on_block,
            stage="post_commit",
        )
        return False


def ui_commit_check(
    page: Any,
    *,
    intent: str,
    expected: Mapping[str, str],
    pre_expected: Optional[Mapping[str, str]] = None,
    record_id: str = "",
    independent_page: Any = None,
    verifier: Any = None,
    inbox: Optional[Union[str, Path]] = None,
    receipt_dir: Optional[Union[str, Path]] = None,
    signing_key: Optional[Ed25519PrivateKey] = None,
    raise_on_block: bool = True,
) -> UiCommitCheck:
    """Return a sync/async context manager for optional pre-commit gating and post-commit UI checks."""
    return UiCommitCheck(
        page,
        intent=intent,
        expected=expected,
        pre_expected=pre_expected,
        record_id=record_id,
        independent_page=independent_page,
        verifier=verifier,
        inbox=inbox,
        receipt_dir=receipt_dir,
        signing_key=signing_key,
        raise_on_block=raise_on_block,
    )


__all__ = [
    "CommitBlockedError",
    "CommitResult",
    "UiCommitCheck",
    "async_verify_commit",
    "ui_commit_check",
    "verify_commit",
]
