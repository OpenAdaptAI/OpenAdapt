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


def test_verify_commit_clean_with_explicit_key_binds_expected_values(
    tmp_path: Path,
) -> None:
    signing_key = Ed25519PrivateKey.generate()
    page_500 = _SyncPlaywrightPage({"Amount": "$500"})
    page_9999 = _SyncPlaywrightPage({"Amount": "$9999"})
    receipt_dir = tmp_path / "receipts"

    res_500 = verify_commit(
        page_500,
        intent="Bind Policy",
        expected={"Amount": "$500"},
        record_id="rec-1",
        receipt_dir=receipt_dir,
        signing_key=signing_key,
    )
    res_9999 = verify_commit(
        page_9999,
        intent="Bind Policy",
        expected={"Amount": "$9999"},
        record_id="rec-2",
        receipt_dir=receipt_dir,
        signing_key=signing_key,
    )

    assert isinstance(res_500, CommitResult)
    assert res_500.status == "VERIFIED"
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

    # Without an explicit signing_key, no ephemeral key or receipt is silently minted.
    unsigned = verify_commit(
        page_500, intent="Bind Policy", expected={"Amount": "$500"}
    )
    assert unsigned.status == "VERIFIED"
    assert unsigned.receipt is None
    assert unsigned.receipt_id is None


def test_empty_expected_body_substring_and_read_errors_fail_closed(
    tmp_path: Path,
) -> None:
    # 1. Empty expected ALWAYS fails closed (ABSTAIN -> STOPPED).
    empty_res = verify_commit(None, intent="x", expected={})
    assert empty_res.status == "STOPPED"
    assert empty_res.verdict == "ABSTAIN"
    assert empty_res.receipt is None

    # 2. Body text "Amount due: $5000" must NEVER match expected={"Amount": "$500"}!
    body_only = _BodyOnlyPage("Amount due: $5000")
    sub_res = verify_commit(body_only, intent="Pay", expected={"Amount": "$500"})
    assert sub_res.status == "STOPPED"
    assert sub_res.verdict == "VETO"
    assert sub_res.receipt is None

    # 3. read_field raising RuntimeError must NEVER fall back to body text "Yes"!
    err_page = _RaisingReadFieldPage()
    err_res = verify_commit(err_page, intent="Confirm", expected={"Approved": "Yes"})
    assert err_res.status == "STOPPED"
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

    assert result.status == "STOPPED"
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
    assert entry["verdict"] == "VETO"


def test_abstain_halts_and_raw_screenshot_passes_deterministic_passed_false(
    tmp_path: Path,
) -> None:
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
    assert result.status == "STOPPED"
    assert result.verdict == "ABSTAIN"

    # When no DOM check ran (raw PNG), visual verifier MUST receive deterministic_passed=False!
    raw_veto = _DuckPrePostVisualVeto(_FakeVetoVerdict.PASS)
    raw_res = verify_commit(
        b"\x89PNG-raw",
        intent="Approve Invoice",
        expected={"Status": "Approved"},
        verifier=raw_veto,
    )
    assert len(raw_veto.calls) == 1
    assert raw_veto.calls[0]["deterministic_passed"] is False
    assert raw_res.status == "VERIFIED"
    # No signing_key supplied -> no receipt minted.
    assert raw_res.receipt is None


def test_ui_commit_check_pre_commit_gate_body_exception_and_async(
    tmp_path: Path,
) -> None:
    signing_key = Ed25519PrivateKey.generate()

    # 1. Pre-commit gate blocks BEFORE body executes when pre_expected fails.
    pre_page = _SyncPlaywrightPage({"Amount": "$9999"})
    body_ran = False
    with pytest.raises(CommitBlockedError):
        with ui_commit_check(
            pre_page,
            intent="Submit Payment",
            pre_expected={"Amount": "$500"},
            expected={"Status": "Paid"},
        ):
            body_ran = True
    assert body_ran is False

    # 2. Exception inside body records ABSTAIN inbox entry before re-raising.
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
    assert check.result.verdict == "ABSTAIN"
    assert exc_inbox.is_file()

    # 3. Async context manager succeeds with explicit signing key.
    async def _run_async() -> CommitResult:
        async_page = _AsyncPlaywrightPage({"Status": "Draft"})
        async with ui_commit_check(
            async_page,
            intent="Approve Async",
            expected={"Status": "Approved"},
            receipt_dir=tmp_path / "async_receipts",
            signing_key=signing_key,
        ) as acheck:
            async_page.fields["Status"] = "Approved"
        assert acheck.before_screenshot is not None
        assert acheck.result is not None
        return acheck.result

    async_res = asyncio.run(_run_async())
    assert async_res.status == "VERIFIED"
    assert async_res.receipt_path is not None and async_res.receipt_path.is_file()

    # 4. Async verify_commit with raising verifier -> ABSTAIN.
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
    assert res_err.status == "STOPPED"
    assert res_err.verdict == "ABSTAIN"
