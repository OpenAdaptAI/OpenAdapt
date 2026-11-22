"""Universal 1-line Commit Firewall for GUI and browser agents.

Wraps any irreversible UI commit step (Playwright, Browser-Use, Claude CUA,
OpenAI CUA, or raw screenshots) with:

1. Fast deterministic DOM/text verification (duck-typed; zero hard dependency
   on Playwright or any browser/vision library).
2. Optional visual veto check (duck-typed for ``PrePostSubmitVisualVeto`` or
   any custom visual verifier callable) enforcing strict ``ABSTAIN -> HALT``
   semantics so clean DOM never silently overrides visual occlusion.
3. Cryptographically signed Ed25519 ``record/run/v1`` DSSE receipts via
   ``openadapt_types.receipt`` on ``VERIFIED`` outcomes.
4. Structured ``CommitBlockedError`` / ``CommitResult(status="STOPPED")`` and
   optional local Exception Inbox JSONL logging on ``VETO`` / ``ABSTAIN``.
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

CommitStatus = Literal["VERIFIED", "STOPPED"]
CommitVerdict = Literal["VERIFIED", "VETO", "ABSTAIN"]
ScreenshotInput = Union[bytes, bytearray, str, Path, None]


@dataclass(frozen=True)
class CommitResult:
    """Outcome of a commit firewall check."""

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
    """Raised when the commit firewall halts an irreversible commit."""

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


def _extract_page_text_sync(page: Any) -> Optional[str]:
    for method, args in (
        ("inner_text", ("body",)),
        ("text_content", ("body",)),
        ("content", ()),
    ):
        fn = getattr(page, method, None)
        if callable(fn):
            try:
                val = fn(*args)
            except Exception:
                continue
            if isinstance(val, str):
                return val
    return None


async def _extract_page_text_async(page: Any) -> Optional[str]:
    for method, args in (
        ("inner_text", ("body",)),
        ("text_content", ("body",)),
        ("content", ()),
    ):
        fn = getattr(page, method, None)
        if callable(fn):
            try:
                val = fn(*args)
                if inspect.isawaitable(val):
                    val = await val
            except Exception:
                continue
            if isinstance(val, str):
                return val
    return None


def _read_field_sync(page: Any, key: str) -> tuple[bool, Optional[str]]:
    """Try field-level read first; returns (found_field, value)."""
    read_field = getattr(page, "read_field", None)
    if callable(read_field):
        try:
            val = read_field(key)
            return True, None if val is None else str(val)
        except KeyError:
            return False, None
        except Exception:
            return False, None

    get_by_label = getattr(page, "get_by_label", None)
    if callable(get_by_label):
        try:
            loc = get_by_label(key)
            for attr in ("input_value", "inner_text", "text_content"):
                getter = getattr(loc, attr, None)
                if callable(getter):
                    val = getter()
                    if isinstance(val, str):
                        return True, val
        except Exception:
            pass

    input_value = getattr(page, "input_value", None)
    if callable(input_value):
        try:
            val = input_value(key)
            if isinstance(val, str):
                return True, val
        except Exception:
            pass

    return False, None


async def _read_field_async(page: Any, key: str) -> tuple[bool, Optional[str]]:
    read_field = getattr(page, "read_field", None)
    if callable(read_field):
        try:
            val = read_field(key)
            if inspect.isawaitable(val):
                val = await val
            return True, None if val is None else str(val)
        except KeyError:
            return False, None
        except Exception:
            return False, None

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
                        return True, val
        except Exception:
            pass

    input_value = getattr(page, "input_value", None)
    if callable(input_value):
        try:
            val = input_value(key)
            if inspect.isawaitable(val):
                val = await val
            if isinstance(val, str):
                return True, val
        except Exception:
            pass

    return False, None


def _check_dom_sync(
    page: Any, expected: Mapping[str, str]
) -> tuple[bool, tuple[str, ...]]:
    """Run exact Unicode DOM/text checks (no NFKC folding so homoglyphs fail)."""
    mismatches: list[str] = []
    page_text: Optional[str] = None
    page_text_loaded = False

    for field_key, expected_val in expected.items():
        expected_str = str(expected_val)
        found, actual = _read_field_sync(page, field_key)
        if found:
            if actual != expected_str:
                mismatches.append(
                    f"field {field_key!r}: expected {expected_str!r}, observed {actual!r}"
                )
            continue

        if not page_text_loaded:
            page_text = _extract_page_text_sync(page)
            page_text_loaded = True
        if page_text is None:
            mismatches.append(
                f"field {field_key!r}: page exposed no readable field or text"
            )
        elif expected_str not in page_text:
            mismatches.append(
                f"field {field_key!r}: expected value {expected_str!r} not found in page text"
            )

    return len(mismatches) == 0, tuple(mismatches)


async def _check_dom_async(
    page: Any, expected: Mapping[str, str]
) -> tuple[bool, tuple[str, ...]]:
    mismatches: list[str] = []
    page_text: Optional[str] = None
    page_text_loaded = False

    for field_key, expected_val in expected.items():
        expected_str = str(expected_val)
        found, actual = await _read_field_async(page, field_key)
        if found:
            if actual != expected_str:
                mismatches.append(
                    f"field {field_key!r}: expected {expected_str!r}, observed {actual!r}"
                )
            continue

        if not page_text_loaded:
            page_text = await _extract_page_text_async(page)
            page_text_loaded = True
        if page_text is None:
            mismatches.append(
                f"field {field_key!r}: page exposed no readable field or text"
            )
        elif expected_str not in page_text:
            mismatches.append(
                f"field {field_key!r}: expected value {expected_str!r} not found in page text"
            )

    return len(mismatches) == 0, tuple(mismatches)


def _normalize_visual_outcome(raw: Any) -> tuple[CommitVerdict, str]:
    """Normalize a visual verifier reply into (VERIFIED | VETO | ABSTAIN, reason).

    Strict fail-closed mapping: any unknown, occluded, uncertain, or abstained
    reply maps to ``ABSTAIN`` (which halts the commit).
    """
    if raw is None:
        return "ABSTAIN", "visual verifier returned no decision"

    # Unwrap SubmitVetoResult-like objects (has .decision and .allowed)
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
        # Duck-type PrePostSubmitVisualVeto (post-submit or pre-submit)
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
    except Exception as exc:
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
    except Exception as exc:
        return "ABSTAIN", f"visual verifier error (fail-closed): {exc}"

    return "ABSTAIN", "unsupported visual verifier interface (fail-closed)"


def _build_signed_receipt(
    *,
    intent: str,
    expected: Mapping[str, str],
    used_visual_verifier: bool,
    after_png: Optional[bytes],
    receipt_dir: Optional[Union[str, Path]],
    signing_key: Optional[Ed25519PrivateKey],
) -> tuple[str, dict[str, Any], Optional[Path], str, str]:
    """Emit a signed Ed25519 ``record/run/v1`` DSSE receipt."""
    private_key = signing_key or Ed25519PrivateKey.generate()
    pub_raw = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    pub_b64 = base64.b64encode(pub_raw).decode("ascii")
    kid = receipt_lib.key_id(pub_raw)

    intent_sha = hashlib.sha256(intent.encode("utf-8")).hexdigest()
    report_basis = {
        "intent_sha256": intent_sha,
        "expected_keys": sorted(str(k) for k in expected.keys()),
        "outcome": "VERIFIED",
        "visual_checked": used_visual_verifier,
    }
    report_sha = hashlib.sha256(
        receipt_lib.canonical_json(report_basis).encode("utf-8")
    ).hexdigest()

    subjects: list[dict[str, Any]] = [
        {"name": "report.json", "digest": {"sha256": report_sha}}
    ]
    if after_png is not None:
        subjects.append(
            {
                "name": "evidence",
                "digest": {"sha256": hashlib.sha256(after_png).hexdigest()},
            }
        )

    started_hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00:00Z")
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
                    "strength": "same_screen",
                    "not_checked": [],
                }
            ],
            "checks": {
                "identity": {"required": 1, "confirmed": 1},
                "postconditions": {"required": 1, "confirmed": 1},
                "effects": {"required": 0, "confirmed": 0},
                "authorization": {"required": 0, "confirmed": 0},
                "lines_confirmed": 1,
                "lines_refuted": 0,
                "lines_not_checked": 0,
            },
            "model_calls": 1 if used_visual_verifier else 0,
            "disclosure": {
                "version": receipt_lib.PUBLIC_DISCLOSURE_VERSION,
                "commitments": {},
            },
        },
    }

    envelope = receipt_lib.sign_statement(statement, [(kid, private_key.sign)])
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
    verdict: CommitVerdict,
    reason: str,
    mismatches: tuple[str, ...],
    expected: Mapping[str, str],
) -> tuple[dict[str, Any], Optional[Path]]:
    item: dict[str, Any] = {
        "schema": "openadapt.commit-inbox/v1",
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "intent": intent,
        "status": "STOPPED",
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
    dom_checked: bool,
    dom_passed: bool,
    mismatches: tuple[str, ...],
    visual_verdict: Optional[CommitVerdict],
    visual_reason: str,
    after_png: Optional[bytes],
    inbox: Optional[Union[str, Path]],
    receipt_dir: Optional[Union[str, Path]],
    signing_key: Optional[Ed25519PrivateKey],
    raise_on_block: bool,
) -> CommitResult:
    # Strict ABSTAIN -> HALT & fail-closed combination rule:
    # 1. Visual ABSTAIN ALWAYS halts (clean DOM never overrides occlusion).
    # 2. Visual VETO ALWAYS halts.
    # 3. DOM mismatch ALWAYS halts (passing visual never overrides bad DOM).
    # 4. If neither DOM nor visual verifier ran for non-empty expected, ABSTAIN -> HALT.
    final_verdict: CommitVerdict = "VERIFIED"
    reason = ""

    if visual_verdict == "ABSTAIN":
        final_verdict = "ABSTAIN"
        reason = visual_reason or "visual verifier abstained (ABSTAIN -> HALT)"
    elif visual_verdict == "VETO":
        final_verdict = "VETO"
        reason = visual_reason or "visual verifier vetoed commit"
    elif dom_checked and not dom_passed:
        final_verdict = "VETO"
        reason = "; ".join(mismatches) or "DOM verification failed"
    elif not dom_checked and visual_verdict is None and expected:
        final_verdict = "ABSTAIN"
        reason = "no DOM page or visual verifier available to verify expected fields"

    if final_verdict != "VERIFIED":
        inbox_item, inbox_path = _write_inbox_item(
            inbox,
            intent=intent,
            verdict=final_verdict,
            reason=reason,
            mismatches=mismatches,
            expected=expected,
        )
        result = CommitResult(
            status="STOPPED",
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

    rid, envelope, receipt_path, kid, pub_b64 = _build_signed_receipt(
        intent=intent,
        expected=expected,
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
    before_screenshot: ScreenshotInput = None,
    verifier: Any = None,
    inbox: Optional[Union[str, Path]] = None,
    receipt_dir: Optional[Union[str, Path]] = None,
    signing_key: Optional[Ed25519PrivateKey] = None,
    raise_on_block: bool = False,
) -> CommitResult:
    """Verify an irreversible UI commit in 1 line (sync).

    Accepts a duck-typed Playwright-like ``page`` or screenshot bytes/path.
    Enforces strict ``ABSTAIN -> HALT`` semantics and emits a signed Ed25519
    ``record/run/v1`` receipt on ``VERIFIED``.
    """
    dom_checked = False
    dom_passed = True
    mismatches: tuple[str, ...] = ()
    after_png: Optional[bytes] = None
    before_png = _coerce_screenshot_bytes(before_screenshot)

    if _is_page_like(page_or_screenshot):
        dom_checked = True
        dom_passed, mismatches = _check_dom_sync(page_or_screenshot, expected)
        shot_fn = getattr(page_or_screenshot, "screenshot", None)
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
    if verifier is not None:
        visual_verdict, visual_reason = _run_visual_verifier_sync(
            verifier,
            before_png=before_png,
            after_png=after_png,
            intent=intent,
            expected=expected,
            deterministic_passed=dom_passed if dom_checked else True,
        )

    return _finalize_commit(
        intent=intent,
        expected=expected,
        dom_checked=dom_checked,
        dom_passed=dom_passed,
        mismatches=mismatches,
        visual_verdict=visual_verdict,
        visual_reason=visual_reason,
        after_png=after_png,
        inbox=inbox,
        receipt_dir=receipt_dir,
        signing_key=signing_key,
        raise_on_block=raise_on_block,
    )


async def async_verify_commit(
    page_or_screenshot: Any,
    *,
    intent: str,
    expected: Mapping[str, str],
    before_screenshot: ScreenshotInput = None,
    verifier: Any = None,
    inbox: Optional[Union[str, Path]] = None,
    receipt_dir: Optional[Union[str, Path]] = None,
    signing_key: Optional[Ed25519PrivateKey] = None,
    raise_on_block: bool = False,
) -> CommitResult:
    """Async counterpart to :func:`verify_commit` for async Playwright pages."""
    dom_checked = False
    dom_passed = True
    mismatches: tuple[str, ...] = ()
    after_png: Optional[bytes] = None
    before_png = _coerce_screenshot_bytes(before_screenshot)

    if _is_page_like(page_or_screenshot):
        dom_checked = True
        dom_passed, mismatches = await _check_dom_async(page_or_screenshot, expected)
        shot_fn = getattr(page_or_screenshot, "screenshot", None)
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
    if verifier is not None:
        visual_verdict, visual_reason = await _run_visual_verifier_async(
            verifier,
            before_png=before_png,
            after_png=after_png,
            intent=intent,
            expected=expected,
            deterministic_passed=dom_passed if dom_checked else True,
        )

    return _finalize_commit(
        intent=intent,
        expected=expected,
        dom_checked=dom_checked,
        dom_passed=dom_passed,
        mismatches=mismatches,
        visual_verdict=visual_verdict,
        visual_reason=visual_reason,
        after_png=after_png,
        inbox=inbox,
        receipt_dir=receipt_dir,
        signing_key=signing_key,
        raise_on_block=raise_on_block,
    )


class CommitGuard:
    """Dual sync/async context manager wrapping an irreversible UI commit."""

    def __init__(
        self,
        page: Any,
        *,
        intent: str,
        expected: Mapping[str, str],
        verifier: Any = None,
        inbox: Optional[Union[str, Path]] = None,
        receipt_dir: Optional[Union[str, Path]] = None,
        signing_key: Optional[Ed25519PrivateKey] = None,
        raise_on_block: bool = True,
    ) -> None:
        self._page = page
        self._intent = intent
        self._expected = dict(expected)
        self._verifier = verifier
        self._inbox = inbox
        self._receipt_dir = receipt_dir
        self._signing_key = signing_key
        self._raise_on_block = raise_on_block
        self.before_screenshot: Optional[bytes] = None
        self.result: Optional[CommitResult] = None

    def __enter__(self) -> CommitGuard:
        shot_fn = getattr(self._page, "screenshot", None)
        if callable(shot_fn):
            try:
                self.before_screenshot = _coerce_screenshot_bytes(shot_fn())
            except Exception:
                self.before_screenshot = None
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> Literal[False]:
        if exc_type is not None:
            return False
        self.result = verify_commit(
            self._page,
            intent=self._intent,
            expected=self._expected,
            before_screenshot=self.before_screenshot,
            verifier=self._verifier,
            inbox=self._inbox,
            receipt_dir=self._receipt_dir,
            signing_key=self._signing_key,
            raise_on_block=self._raise_on_block,
        )
        return False

    async def __aenter__(self) -> CommitGuard:
        shot_fn = getattr(self._page, "screenshot", None)
        if callable(shot_fn):
            try:
                raw = shot_fn()
                if inspect.isawaitable(raw):
                    raw = await raw
                self.before_screenshot = _coerce_screenshot_bytes(raw)
            except Exception:
                self.before_screenshot = None
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Literal[False]:
        if exc_type is not None:
            return False
        self.result = await async_verify_commit(
            self._page,
            intent=self._intent,
            expected=self._expected,
            before_screenshot=self.before_screenshot,
            verifier=self._verifier,
            inbox=self._inbox,
            receipt_dir=self._receipt_dir,
            signing_key=self._signing_key,
            raise_on_block=self._raise_on_block,
        )
        return False


def commit_guard(
    page: Any,
    *,
    intent: str,
    expected: Mapping[str, str],
    verifier: Any = None,
    inbox: Optional[Union[str, Path]] = None,
    receipt_dir: Optional[Union[str, Path]] = None,
    signing_key: Optional[Ed25519PrivateKey] = None,
    raise_on_block: bool = True,
) -> CommitGuard:
    """Return a sync/async context manager guarding an irreversible UI commit."""
    return CommitGuard(
        page,
        intent=intent,
        expected=expected,
        verifier=verifier,
        inbox=inbox,
        receipt_dir=receipt_dir,
        signing_key=signing_key,
        raise_on_block=raise_on_block,
    )


__all__ = [
    "CommitBlockedError",
    "CommitGuard",
    "CommitResult",
    "async_verify_commit",
    "commit_guard",
    "verify_commit",
]
