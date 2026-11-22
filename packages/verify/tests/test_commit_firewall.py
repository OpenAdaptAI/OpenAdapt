"""Tests for the 1-line Universal Commit Firewall (openadapt_verify.commit_firewall)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import pytest
from openadapt_types import receipt as receipt_lib
from openadapt_verify import (
    CommitBlockedError,
    CommitResult,
    async_verify_commit,
    commit_guard,
    verify_commit,
)


class _SyncLocator:
    def __init__(self, value: str) -> None:
        self._value = value

    def input_value(self) -> str:
        return self._value


class _SyncPlaywrightPage:
    """Duck-typed sync Playwright Page (no playwright import)."""

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
    """Duck-typed async Playwright Page (no playwright import)."""

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


def test_verify_commit_clean_emits_verifiable_ed25519_receipt(tmp_path: Path) -> None:
    page = _SyncPlaywrightPage(
        {"Policy Holder": "Alice Novak", "Coverage Limit": "$250,000"}
    )
    receipt_dir = tmp_path / "receipts"

    result = verify_commit(
        page,
        intent="Bind Policy",
        expected={"Policy Holder": "Alice Novak", "Coverage Limit": "$250,000"},
        receipt_dir=receipt_dir,
    )

    assert isinstance(result, CommitResult)
    assert result.status == "VERIFIED"
    assert result.verdict == "VERIFIED"
    assert result.verified is True
    assert result.receipt_id is not None
    assert result.receipt_path is not None and result.receipt_path.is_file()
    assert result.receipt is not None
    assert result.key_id is not None and result.public_key is not None

    # Cryptographically verify the emitted record/run/v1 DSSE envelope with openadapt-types!
    on_disk = json.loads(result.receipt_path.read_text(encoding="utf-8"))
    registry = [
        receipt_lib.RoleKey(
            key_id=result.key_id,
            public_key=result.public_key,
            role="this_computer",
        )
    ]
    check = receipt_lib.verify_receipt(on_disk, registry)
    assert check.ok is True, f"Receipt verification failed: {check}"
    assert check.outcome == "VERIFIED"


def test_verify_commit_catches_silent_unbound_and_homoglyph_mismatch(
    tmp_path: Path,
) -> None:
    # Cyrillic 'А' (U+0410) instead of Latin 'A' + empty unbound Coverage Limit control
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
    assert entry["status"] == "STOPPED"
    assert entry["verdict"] == "VETO"
    assert entry["expected_keys"] == ["Coverage Limit", "Policy Holder"]


def test_abstain_halts_even_when_dom_is_clean(tmp_path: Path) -> None:
    """Strict ABSTAIN -> HALT: clean DOM must NEVER override visual occlusion."""
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
    assert result.verified is False
    assert result.receipt is None
    assert "occludes" in result.reason
    assert inbox_path.is_file()


def test_visual_pass_never_overrides_dom_mismatch() -> None:
    bad_page = _SyncPlaywrightPage({"Amount": "$0.00"})
    passing_veto = _DuckPrePostVisualVeto(_FakeVetoVerdict.PASS)

    result = verify_commit(
        bad_page,
        intent="Submit Payment",
        expected={"Amount": "$500.00"},
        verifier=passing_veto,
    )

    assert result.status == "STOPPED"
    assert result.verdict == "VETO"
    assert result.verified is False


def test_sync_and_async_commit_guard_context_managers(tmp_path: Path) -> None:
    # Sync context manager: captures before & after screenshots and verifies
    sync_page = _SyncPlaywrightPage({"Status": "Draft"})
    with commit_guard(
        sync_page,
        intent="Submit Form",
        expected={"Status": "Submitted"},
        receipt_dir=tmp_path / "sync_receipts",
    ) as guard:
        sync_page.fields["Status"] = "Submitted"

    assert guard.before_screenshot is not None
    assert guard.result is not None and guard.result.status == "VERIFIED"
    assert sync_page.screenshots_taken == 2

    # Sync context manager raises CommitBlockedError on mismatch
    broken_page = _SyncPlaywrightPage({"Status": "Draft"})
    with pytest.raises(CommitBlockedError) as exc_info:
        with commit_guard(
            broken_page,
            intent="Submit Form",
            expected={"Status": "Submitted"},
            inbox=tmp_path / "blocked.jsonl",
        ):
            pass  # click failed silently
    assert exc_info.value.result.status == "STOPPED"
    assert exc_info.value.inbox_item is not None

    # Async context manager with duck-typed async Playwright page
    async def _run_async() -> CommitResult:
        async_page = _AsyncPlaywrightPage({"Status": "Draft"})
        async with commit_guard(
            async_page,
            intent="Approve Async",
            expected={"Status": "Approved"},
            receipt_dir=tmp_path / "async_receipts",
        ) as aguard:
            async_page.fields["Status"] = "Approved"
        assert aguard.before_screenshot is not None
        assert aguard.result is not None
        return aguard.result

    async_res = asyncio.run(_run_async())
    assert async_res.status == "VERIFIED"
    assert async_res.receipt_path is not None and async_res.receipt_path.is_file()


def test_raw_screenshot_requires_verifier_fail_closed() -> None:
    # Raw screenshot without a visual verifier must ABSTAIN -> HALT when expected is set
    res_no_verifier = verify_commit(
        b"\x89PNG-raw",
        intent="Submit",
        expected={"Status": "Saved"},
    )
    assert res_no_verifier.status == "STOPPED"
    assert res_no_verifier.verdict == "ABSTAIN"

    # Raw screenshot with a passing visual verifier succeeds
    res_with_verifier = verify_commit(
        b"\x89PNG-raw",
        intent="Submit",
        expected={"Status": "Saved"},
        verifier=lambda **_: {"verdict": "PASS", "reason": "confirmed visually"},
    )
    assert res_with_verifier.status == "VERIFIED"

    # Async verify_commit with verifier raising exception -> ABSTAIN -> HALT
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
