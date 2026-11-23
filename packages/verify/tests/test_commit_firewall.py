"""Tests for openadapt_verify.commit_firewall (UI commit check helper)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from openadapt_types import receipt as receipt_lib

from openadapt_verify import (
    CommitBlockedError,
    CommitResult,
    async_verify_commit,
    ui_commit_check,
    verify_commit,
)


class _SyncLocator:
    def __init__(self, value: str) -> None:
        self._value = value

    def input_value(self) -> str:
        return self._value


class _SyncPlaywrightPage:
    """Duck-typed sync page exposing get_by_label() and screenshot()."""

    def __init__(self, fields: dict[str, str], *, body_text: str = "") -> None:
        self.fields = dict(fields)
        self.body_text = body_text
        self.screenshots_taken = 0

    def get_by_label(self, label: str) -> _SyncLocator:
        if label not in self.fields:
            raise KeyError(label)
        return _SyncLocator(self.fields[label])

    def inner_text(self, selector: str) -> str:
        return self.body_text

    def screenshot(self) -> bytes:
        self.screenshots_taken += 1
        return f"\x89PNG-frame-{self.screenshots_taken}".encode("utf-8")


class _AsyncLocator:
    def __init__(self, value: str) -> None:
        self._value = value

    async def input_value(self) -> str:
        return self._value


class _AsyncPlaywrightPage:
    """Duck-typed async page exposing get_by_label() and screenshot()."""

    def __init__(self, fields: dict[str, str]) -> None:
        self.fields = dict(fields)
        self.screenshots_taken = 0

    def get_by_label(self, label: str) -> _AsyncLocator:
        if label not in self.fields:
            raise KeyError(label)
        return _AsyncLocator(self.fields[label])

    async def screenshot(self) -> bytes:
        self.screenshots_taken += 1
        return f"\x89PNG-async-{self.screenshots_taken}".encode("utf-8")


class _BodyOnlyPage:
    """Page exposing only body text (no field-level reader)."""

    def __init__(self, body_text: str) -> None:
        self._body = body_text

    def inner_text(self, selector: str) -> str:
        return self._body


class _RaisingReadFieldPage:
    """Page whose read_field raises RuntimeError while body text contains 'Yes'."""

    def read_field(self, key: str) -> str:
        raise RuntimeError(f"DOM detached while reading {key}")

    def inner_text(self, selector: str) -> str:
        return "Status: Yes"


class _FakeVetoVerdict(str, Enum):
    PASS = "pass"
    VETO = "veto"
    ABSTAIN = "abstain"


@dataclass(frozen=True)
class _FakeVetoDecision:
    verdict: _FakeVetoVerdict
    reason: str = ""


@dataclass(frozen=True)
class _FakeSubmitVetoResult:
    decision: _FakeVetoDecision
    allowed: bool


class _DuckPrePostVisualVeto:
    """Duck-typed PrePostSubmitVisualVeto without importing Flow's vision stack."""

    def __init__(self, verdict: _FakeVetoVerdict, reason: str = "") -> None:
        self._verdict = verdict
        self._reason = reason
        self.calls: list[dict[str, Any]] = []

    def check_post_submit(
        self,
        *,
        screenshot: bytes,
        expected_state: str,
        deterministic_passed: bool,
    ) -> _FakeSubmitVetoResult:
        self.calls.append(
            {
                "screenshot": screenshot,
                "expected_state": expected_state,
                "deterministic_passed": deterministic_passed,
            }
        )
        return _FakeSubmitVetoResult(
            decision=_FakeVetoDecision(verdict=self._verdict, reason=self._reason),
            allowed=(self._verdict is _FakeVetoVerdict.PASS and deterministic_passed),
        )


def test_verify_commit_independent_readback_mints_receipt_and_same_screen_does_not(
    tmp_path: Path,
) -> None:
    signing_key = Ed25519PrivateKey.generate()
    page_500 = _SyncPlaywrightPage({"Amount": "$500"})
    indep_500 = _SyncPlaywrightPage({"Amount": "$500"})
    page_9999 = _SyncPlaywrightPage({"Amount": "$9999"})
    indep_9999 = _SyncPlaywrightPage({"Amount": "$9999"})
    receipt_dir = tmp_path / "receipts"

    # Independent read-back -> VERIFIED + signed receipt with distinct subject IDs.
    res_500 = verify_commit(
        page_500,
        intent="Bind Policy",
        expected={"Amount": "$500"},
        record_id="rec-1",
        receipt_dir=receipt_dir,
        signing_key=signing_key,
        independent_page=indep_500,
    )
    res_9999 = verify_commit(
        page_9999,
        intent="Bind Policy",
        expected={"Amount": "$9999"},
        record_id="rec-2",
        receipt_dir=receipt_dir,
        signing_key=signing_key,
        independent_page=indep_9999,
    )

    assert isinstance(res_500, CommitResult)
    assert res_500.status == "VERIFIED"
    assert res_500.verified is True
    assert res_500.receipt_id is not None and res_9999.receipt_id is not None
    # Different expected values must produce different signed receipt subjects/IDs!
    assert res_500.receipt_id != res_9999.receipt_id
    assert res_500.receipt_path is not None and res_500.receipt_path.is_file()
    assert res_500.key_id is not None and res_500.public_key is not None

    on_disk = json.loads(res_500.receipt_path.read_text(encoding="utf-8"))
    registry = [
        receipt_lib.RoleKey(
            key_id=res_500.key_id,
            public_key=res_500.public_key,
            role="this_computer",
        )
    ]
    check = receipt_lib.verify_receipt(on_disk, registry)
    assert check.ok is True, f"Receipt verification failed: {check}"

    # Without an explicit signing_key, independent read-back is VERIFIED with no receipt.
    unsigned = verify_commit(
        page_500,
        intent="Bind Policy",
        expected={"Amount": "$500"},
        independent_page=indep_500,
    )
    assert unsigned.status == "VERIFIED"
    assert unsigned.verified is True
    assert unsigned.receipt is None
    assert unsigned.receipt_id is None

    # Same-screen DOM check (no independent_page) returns SAME_SCREEN and NEVER mints a receipt.
    same_screen = verify_commit(
        page_500,
        intent="Bind Policy",
        expected={"Amount": "$500"},
        record_id="rec-same",
        receipt_dir=receipt_dir,
        signing_key=signing_key,
    )
    assert same_screen.status == "SAME_SCREEN"
    assert same_screen.verdict == "SAME_SCREEN"
    assert same_screen.verified is False
    assert same_screen.receipt is None
    assert same_screen.receipt_id is None

    # signing_key without non-empty record_id must raise ValueError (fail closed).
    with pytest.raises(ValueError, match="record_id"):
        verify_commit(
            page_500,
            intent="Bind Policy",
            expected={"Amount": "$500"},
            record_id="",
            signing_key=signing_key,
            independent_page=indep_500,
        )


def test_empty_expected_body_substring_and_read_errors_fail_closed(
    tmp_path: Path,
) -> None:
    # 1. Empty expected ALWAYS fails closed (ABSTAIN -> UNCONFIRMED_WRITE post-commit).
    empty_res = verify_commit(None, intent="x", expected={})
    assert empty_res.status == "UNCONFIRMED_WRITE"
    assert empty_res.verdict == "ABSTAIN"
    assert empty_res.receipt is None

    # 2. Body text "Amount due: $5000" must NEVER match expected={"Amount": "$500"}!
    body_only = _BodyOnlyPage("Amount due: $5000")
    sub_res = verify_commit(body_only, intent="Pay", expected={"Amount": "$500"})
    assert sub_res.status == "UNCONFIRMED_WRITE"
    assert sub_res.verdict == "VETO"
    assert sub_res.receipt is None

    # 3. read_field raising RuntimeError must NEVER fall back to body text "Yes"!
    err_page = _RaisingReadFieldPage()
    err_res = verify_commit(err_page, intent="Confirm", expected={"Approved": "Yes"})
    assert err_res.status == "UNCONFIRMED_WRITE"
    assert err_res.verdict == "VETO"
    assert "RuntimeError" in err_res.reason


def test_verify_commit_catches_silent_unbound_and_homoglyph_mismatch(
    tmp_path: Path,
) -> None:
    page = _SyncPlaywrightPage(
        {"Policy Holder": "\u0410lice Novak", "Coverage Limit": ""}
    )
    inbox_path = tmp_path / "inbox.jsonl"

    result = verify_commit(
        page,
        intent="Bind Policy",
        expected={"Policy Holder": "Alice Novak", "Coverage Limit": "$250,000"},
        inbox=inbox_path,
    )

    assert result.status == "UNCONFIRMED_WRITE"
    assert result.verdict == "VETO"
    assert result.verified is False
    assert result.receipt is None
    assert len(result.mismatches) == 2
    assert result.inbox_item is not None
    assert result.inbox_path == inbox_path

    lines = inbox_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["schema"] == "openadapt.commit-inbox/v1"
    assert entry["status"] == "UNCONFIRMED_WRITE"
    assert entry["verdict"] == "VETO"


def test_abstain_halts_and_raw_screenshot_returns_visual_only_never_verified(
    tmp_path: Path,
) -> None:
    signing_key = Ed25519PrivateKey.generate()
    clean_page = _SyncPlaywrightPage({"Status": "Approved"})
    occluded_veto = _DuckPrePostVisualVeto(
        _FakeVetoVerdict.ABSTAIN, reason="modal dialog occludes submit button"
    )
    inbox_path = tmp_path / "abstain_inbox.jsonl"

    result = verify_commit(
        clean_page,
        intent="Approve Invoice",
        expected={"Status": "Approved"},
        verifier=occluded_veto,
        inbox=inbox_path,
    )

    assert len(occluded_veto.calls) == 1
    assert occluded_veto.calls[0]["deterministic_passed"] is True
    assert result.status == "UNCONFIRMED_WRITE"
    assert result.verdict == "ABSTAIN"

    # When no DOM check ran (raw PNG), visual verifier MUST receive deterministic_passed=False,
    # return VISUAL_ONLY (never VERIFIED), and NEVER mint a signed receipt!
    raw_veto = _DuckPrePostVisualVeto(_FakeVetoVerdict.PASS)
    raw_res = verify_commit(
        b"\x89PNG-raw",
        intent="Approve Invoice",
        expected={"Status": "Approved"},
        verifier=raw_veto,
        record_id="rec-visual",
        receipt_dir=tmp_path / "visual_receipts",
        signing_key=signing_key,
    )
    assert len(raw_veto.calls) == 1
    assert raw_veto.calls[0]["deterministic_passed"] is False
    assert raw_res.status == "VISUAL_ONLY"
    assert raw_res.verdict == "VISUAL_ONLY"
    assert raw_res.verified is False
    assert raw_res.receipt is None
    assert raw_res.receipt_id is None


def test_ui_commit_check_pre_commit_gate_body_exception_and_async(
    tmp_path: Path,
) -> None:
    signing_key = Ed25519PrivateKey.generate()

    # 1a. Pre-commit gate blocks BEFORE body executes when pre_expected fails (status=STOPPED).
    pre_page = _SyncPlaywrightPage({"Amount": "$9999"})
    body_ran = False
    with pytest.raises(CommitBlockedError) as exc_info:
        with ui_commit_check(
            pre_page,
            intent="Submit Payment",
            pre_expected={"Amount": "$500"},
            expected={"Status": "Paid"},
        ):
            body_ran = True
    assert body_ran is False
    assert exc_info.value.result.status == "STOPPED"

    # 1b. Pre-commit gate with raise_on_block=False preserves STOPPED result and skips post-check.
    pre_check = ui_commit_check(
        pre_page,
        intent="Submit Payment",
        pre_expected={"Amount": "$500"},
        expected={"Status": "Paid"},
        raise_on_block=False,
    )
    with pre_check:
        pass
    assert pre_check.result is not None
    assert pre_check.result.status == "STOPPED"
    assert pre_check.result.verdict == "VETO"

    # 2. Exception inside body records UNCONFIRMED_WRITE / ABSTAIN inbox entry before re-raising.
    exc_inbox = tmp_path / "exc_inbox.jsonl"
    exc_page = _SyncPlaywrightPage({"Status": "Draft"})
    check = ui_commit_check(
        exc_page,
        intent="Submit Form",
        expected={"Status": "Submitted"},
        inbox=exc_inbox,
    )
    with pytest.raises(RuntimeError, match="click crashed"):
        with check:
            raise RuntimeError("click crashed")
    assert check.result is not None
    assert check.result.status == "UNCONFIRMED_WRITE"
    assert check.result.verdict == "ABSTAIN"
    assert exc_inbox.is_file()

    # 3. Async context manager with independent_page + signing_key -> VERIFIED + signed receipt.
    async def _run_async() -> CommitResult:
        async_page = _AsyncPlaywrightPage({"Status": "Draft"})
        indep_async = _AsyncPlaywrightPage({"Status": "Draft"})
        async with ui_commit_check(
            async_page,
            intent="Approve Async",
            expected={"Status": "Approved"},
            record_id="rec-async-1",
            receipt_dir=tmp_path / "async_receipts",
            signing_key=signing_key,
            independent_page=indep_async,
        ) as acheck:
            async_page.fields["Status"] = "Approved"
            indep_async.fields["Status"] = "Approved"
        assert acheck.before_screenshot is not None
        assert acheck.result is not None
        return acheck.result

    async_res = asyncio.run(_run_async())
    assert async_res.status == "VERIFIED"
    assert async_res.receipt_path is not None and async_res.receipt_path.is_file()

    # 4. Async verify_commit with raising verifier -> UNCONFIRMED_WRITE / ABSTAIN.
    async def _raising_verifier(**_: Any) -> dict[str, Any]:
        raise RuntimeError("VLM timeout")

    res_err = asyncio.run(
        async_verify_commit(
            b"\x89PNG-raw",
            intent="Submit",
            expected={"Status": "Saved"},
            verifier=_raising_verifier,
        )
    )
    assert res_err.status == "UNCONFIRMED_WRITE"
    assert res_err.verdict == "ABSTAIN"
