"""Unit tests for the lightweight ``openadapt.wrap`` Python SDK."""

from __future__ import annotations

import asyncio
import inspect
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import openadapt
from openadapt import (
    WrapBlockedError,
    WrapLedger,
    WrapReceipt,
    WrapResult,
    WrapVerificationError,
    verify_wrap_receipt,
)


def test_lazy_export_and_stdlib_only_import() -> None:
    """openadapt.wrap works with openadapt_flow blocked at import time."""
    code = (
        "import sys\n"
        "sys.modules['openadapt_flow'] = None\n"
        "import openadapt\n"
        "assert 'openadapt.wrap_sdk' not in sys.modules\n"
        "fn = openadapt.wrap(lambda x: {'x': x}, check=lambda r: r['x'] == 7)\n"
        "assert fn(7) == {'x': 7}\n"
        "assert fn.last_receipt.outcome == 'confirmed'\n"
        "assert fn.last_receipt.checked_how == 'same_screen'\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_signature_and_metadata_preserved_for_sync_and_async() -> None:
    """functools.wraps and __signature__ preserve exact tool inspection metadata."""

    def save_chart_note(
        patient_id: str,
        note: str,
        *,
        urgent: bool = False,
    ) -> dict[str, Any]:
        """Save a clinical note to the active chart."""
        return {"saved": True, "patient_id": patient_id, "urgent": urgent}

    @openadapt.wrap
    def bare_decorated(a: int, b: str = "x") -> str:
        """Bare decorator form."""
        return f"{a}:{b}"

    @openadapt.wrap(
        pinned={"patient_id": "PT-104"},
        check=lambda out, *a, **kw: out.get("saved") is True,
    )
    async def async_save_chart(patient_id: str, note: str) -> dict[str, Any]:
        """Async decorator form."""
        await asyncio.sleep(0)
        return {"saved": True, "patient_id": patient_id, "note": note}

    wrapped = openadapt.wrap(
        save_chart_note,
        pinned={"patient_id": "PT-104"},
        check=lambda out: out["saved"] is True,
    )

    assert wrapped.__name__ == "save_chart_note"
    assert wrapped.__doc__ == "Save a clinical note to the active chart."
    assert inspect.signature(wrapped) == inspect.signature(save_chart_note)
    assert bare_decorated.__name__ == "bare_decorated"
    assert inspect.signature(bare_decorated) == inspect.signature(
        bare_decorated.__wrapped__
    )
    assert inspect.iscoroutinefunction(async_save_chart)
    assert inspect.signature(async_save_chart) == inspect.signature(
        async_save_chart.__wrapped__
    )

    out = wrapped("PT-104", "Follow-up in 2 weeks", urgent=True)
    assert out == {"saved": True, "patient_id": "PT-104", "urgent": True}
    assert isinstance(wrapped.last_receipt, WrapReceipt)
    assert wrapped.last_receipt.outcome == "confirmed"
    assert wrapped.last_receipt["checked_how"] == "same_screen"
    assert wrapped.last_receipt.verified is True

    assert bare_decorated(3) == "3:x"
    assert bare_decorated.last_receipt.outcome == "unchecked"
    assert bare_decorated.last_receipt.checked_how == "unchecked"
    assert bare_decorated.last_receipt.verified is False
    assert bare_decorated.last_receipt.ok is True

    async_out = asyncio.run(async_save_chart("PT-104", "Vitals stable"))
    assert async_out["saved"] is True
    assert async_save_chart.last_receipt.outcome == "confirmed"
    assert async_save_chart.last_receipt.checked_how == "same_screen"


def test_pinned_identity_mismatch_blocks_before_side_effect() -> None:
    """Conflicting pinned values refuse before executing the wrapped function."""
    calls: list[tuple[str, str]] = []

    def submit_order(patient_id: str, medication: str) -> dict[str, Any]:
        calls.append((patient_id, medication))
        return {"submitted": True}

    safe_submit = openadapt.wrap(
        submit_order,
        pinned={"patient_id": "PT-104"},
        check=lambda r: r["submitted"] is True,
    )

    # Positional mismatch
    with pytest.raises(WrapBlockedError, match="patient_id") as exc_info:
        safe_submit("PT-999", "amoxicillin")
    assert calls == []
    assert exc_info.value.code == "pinned_mismatch"
    assert safe_submit.last_receipt is not None
    assert safe_submit.last_receipt.outcome == "blocked"
    assert safe_submit.last_receipt.dispatched is False

    # Keyword mismatch
    with pytest.raises(WrapBlockedError, match="patient_id"):
        safe_submit(patient_id="PT-205", medication="amoxicillin")
    assert calls == []

    # Omitted required pinned arg is filled from operator pin
    result = safe_submit(medication="amoxicillin")
    assert result == {"submitted": True}
    assert calls == [("PT-104", "amoxicillin")]
    assert safe_submit.last_receipt.declared_by["patient_id"] == "operator"


def test_bool_vs_int_pinned_strictness_and_on_block_return() -> None:
    """True != 1 in pinned checks, and on_block='return' avoids raising."""
    executed = False

    def toggle_flag(enabled: bool) -> dict[str, Any]:
        nonlocal executed
        executed = True
        return {"enabled": enabled}

    guarded = openadapt.wrap(
        toggle_flag,
        pinned={"enabled": True},
        on_block="return",
    )
    refusal = guarded(1)  # type: ignore[arg-type]
    assert executed is False
    assert refusal["ok"] is False
    assert refusal["blocked"] is True
    assert refusal["code"] == "pinned_mismatch"


def test_identity_keys_reject_blank_values() -> None:
    """Configured identity_keys must be non-empty before dispatch."""
    side_effects: list[str] = []

    def post_claim(claim_id: str, amount: int) -> dict[str, Any]:
        side_effects.append(claim_id)
        return {"posted": True}

    safe_post = openadapt.wrap(
        post_claim,
        identity_keys=("claim_id",),
        check=lambda r: r["posted"],
    )

    with pytest.raises(WrapBlockedError) as exc_info:
        safe_post("   ", 150)
    assert exc_info.value.code == "missing_identity"
    assert side_effects == []

    ok = safe_post("CLM-88", 150)
    assert ok == {"posted": True}
    assert safe_post.last_receipt.identity["claim_id"] == "CLM-88"
    assert safe_post.last_receipt.declared_by["claim_id"] == "caller"


def test_uncertain_midflight_exception_latches_until_release() -> None:
    """Mid-flight exceptions latch the write key to block blind duplicate retries."""
    attempts = 0

    def flaky_portal_submit(patient_id: str, note: str) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("gateway timed out after POST")
        return {"saved": True}

    safe_submit = openadapt.wrap(
        flaky_portal_submit,
        pinned={"patient_id": "PT-104"},
        check=lambda r: r["saved"] is True,
    )

    with pytest.raises(TimeoutError, match="gateway timed out"):
        safe_submit("PT-104", "Discharge summary")
    assert attempts == 1
    assert safe_submit.last_receipt.outcome == "uncertain"
    assert safe_submit.last_receipt.dispatched is True
    latched_key = safe_submit.last_receipt.request_key

    # Blind duplicate retry is blocked before flaky_portal_submit runs again
    with pytest.raises(WrapBlockedError) as blocked:
        safe_submit("PT-104", "Discharge summary")
    assert attempts == 1
    assert blocked.value.code == "uncertain_write_blocked"

    # Releasing the key allows the operator-reconciled retry
    assert safe_submit.release(latched_key) == 1
    res = safe_submit("PT-104", "Discharge summary")
    assert res == {"saved": True}
    assert attempts == 2
    assert safe_submit.last_receipt.outcome == "confirmed"


def test_uncertain_check_and_observation_error_latch_until_release() -> None:
    """check() returning 'uncertain' or raising an observation error latches retries."""
    calls = 0
    mode = "uncertain_token"

    def save_record(record_id: str) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"record_id": record_id}

    def verify_screen(result: dict[str, Any]) -> Any:
        if mode == "uncertain_token":
            return "uncertain"
        if mode == "dom_error":
            raise RuntimeError("toast detached during DOM read")
        return {"ok": True, "checked_how": "reopened"}

    wrapped = openadapt.wrap(save_record, check=verify_screen)

    with pytest.raises(WrapVerificationError) as err1:
        wrapped("REC-1")
    assert err1.value.code == "verification_uncertain"
    assert wrapped.last_receipt.outcome == "uncertain"
    assert calls == 1

    with pytest.raises(WrapBlockedError):
        wrapped("REC-1")
    assert calls == 1

    wrapped.release()
    mode = "dom_error"
    with pytest.raises(WrapVerificationError) as err2:
        wrapped("REC-1")
    assert err2.value.code == "verification_uncertain"
    assert calls == 2

    wrapped.release()
    mode = "ok"
    assert wrapped("REC-1") == {"record_id": "REC-1"}
    assert calls == 3
    assert wrapped.last_receipt.outcome == "confirmed"
    assert wrapped.last_receipt.checked_how == "reopened"


def test_idempotent_request_id_replays_without_double_execution(
    tmp_path: Path,
) -> None:
    """Explicit request_id caches confirmed results and rejects payload conflicts."""
    ledger_file = tmp_path / "wrap_ledger.json"
    executions = 0

    def create_invoice(account_id: str, cents: int) -> dict[str, Any]:
        nonlocal executions
        executions += 1
        return {"invoice_id": f"INV-{executions}", "cents": cents}

    wrapped = openadapt.wrap(
        create_invoice,
        ledger=ledger_file,
        check=lambda r: r["cents"] > 0,
    )

    first = wrapped("ACC-1", 5000, request_id="req-inv-001")
    assert first == {"invoice_id": "INV-1", "cents": 5000}
    assert executions == 1
    assert wrapped.last_receipt.replayed is False
    assert wrapped.last_receipt.dispatched is True

    # Retry in the same process returns cached result without calling create_invoice
    second = wrapped("ACC-1", 5000, request_id="req-inv-001")
    assert second == first
    assert executions == 1
    assert wrapped.last_receipt.replayed is True
    assert wrapped.last_receipt.dispatched is False

    # Retry from a new wrapper instance backed by the same ledger file also replays
    reloaded = openadapt.wrap(
        create_invoice,
        ledger=ledger_file,
        check=lambda r: r["cents"] > 0,
    )
    third = reloaded("ACC-1", 5000, request_id="req-inv-001")
    assert third == first
    assert executions == 1
    assert reloaded.last_receipt.replayed is True

    # Reusing the same request_id for a different payload blocks immediately
    with pytest.raises(WrapBlockedError) as conflict:
        reloaded("ACC-1", 9999, request_id="req-inv-001")
    assert conflict.value.code == "request_id_conflict"
    assert executions == 1


def test_return_receipt_and_refutation_behavior() -> None:
    """return_receipt=True returns WrapResult with verified/ok flags."""

    def save_draft(text: str) -> dict[str, Any]:
        return {"saved": bool(text)}

    wrapped = openadapt.wrap(
        save_draft,
        check=lambda out: out["saved"] is True,
        return_receipt=True,
    )

    good = wrapped("hello")
    assert isinstance(good, WrapResult)
    assert good.ok is True
    assert good.verified is True
    assert good.outcome == "confirmed"
    assert good.value == {"saved": True}

    bad = wrapped("")
    assert isinstance(bad, WrapResult)
    assert bad.ok is False
    assert bad.verified is False
    assert bad.outcome == "refuted"
    assert bad.receipt.reason == "post-action check returned False"


def test_receipt_sha256_and_ed25519_signature() -> None:
    """Receipts carry deterministic SHA-256 digests and optional Ed25519 signatures."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    signing_key = Ed25519PrivateKey.generate()
    wrong_key = Ed25519PrivateKey.generate().public_key()

    safe_fn = openadapt.wrap(
        lambda item_id: {"saved": True, "item_id": item_id},
        pinned={"item_id": "ITM-42"},
        check=lambda r: r["saved"] is True,
        signing_key=signing_key,
    )

    out = safe_fn("ITM-42")
    assert out["saved"] is True
    receipt = safe_fn.last_receipt
    assert len(receipt.sha256) == 64
    assert receipt.signature is not None
    assert receipt.public_key is not None
    assert receipt.verify() is True
    assert verify_wrap_receipt(receipt, public_key=signing_key.public_key()) is True
    assert verify_wrap_receipt(receipt, public_key=wrong_key) is False

    tampered = receipt.to_dict()
    tampered["checked_how"] = "system_of_record"
    assert verify_wrap_receipt(tampered) is False


def test_wrap_agent_runner_proxy_without_mutating_target() -> None:
    """Non-callable agent objects with .run / .step are wrapped via a clean proxy."""

    class MiniBrowserAgent:
        def __init__(self) -> None:
            self.runs: list[str] = []

        def run(self, task: str, *, patient_id: str) -> dict[str, Any]:
            self.runs.append(f"{patient_id}:{task}")
            return {"done": True, "patient_id": patient_id}

        async def astep(self, action: str, *, patient_id: str) -> dict[str, Any]:
            await asyncio.sleep(0)
            self.runs.append(f"step:{patient_id}:{action}")
            return {"done": True, "patient_id": patient_id}

    raw_agent = MiniBrowserAgent()
    safe_agent = openadapt.wrap(
        raw_agent,
        pinned={"patient_id": "PT-104"},
        check=lambda r: r["done"] is True,
    )

    # Underlying instance is not mutated
    assert "last_receipt" not in raw_agent.__dict__
    assert safe_agent.runs == []

    with pytest.raises(WrapBlockedError):
        safe_agent.run("update allergies", patient_id="PT-888")
    assert raw_agent.runs == []

    res = safe_agent.run("update allergies", patient_id="PT-104")
    assert res["done"] is True
    assert safe_agent.last_receipt.outcome == "confirmed"
    assert (
        safe_agent.last_receipt.action
        == "test_wrap_agent_runner_proxy_without_mutating_target.<locals>.MiniBrowserAgent.run"
    )

    step_res = asyncio.run(safe_agent.astep("click save", patient_id="PT-104"))
    assert step_res["done"] is True
    assert len(raw_agent.runs) == 2


def test_contract_integration_routes_through_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Passing contract= verifies writes against the system of record via verifier.py."""
    from openadapt.verify import Contract

    monkeypatch.setenv("OPENADAPT_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal"))

    db_path = tmp_path / "records.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE notes (id INTEGER PRIMARY KEY AUTOINCREMENT, patient_id TEXT NOT NULL, body TEXT NOT NULL)"
        )
        conn.commit()

    contract = Contract.from_dict(
        {
            "schema_version": "openadapt.verify/v1",
            "verifier": {
                "kind": "sql",
                "sqlite_database": str(db_path),
                "sql_query": "SELECT * FROM notes",
                "timeout_s": 0.2,
                "poll_interval_s": 0.05,
            },
            "record": {
                "name": "notes",
                "match": ["patient_id"],
                "fields": {"patient_id": "patient_id", "body": "body"},
            },
            "frame": {"identity_field": "id"},
        },
        name="save_note",
    )

    should_write = True

    def write_note(patient_id: str, body: str) -> dict[str, Any]:
        if should_write:
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    "INSERT INTO notes (patient_id, body) VALUES (?, ?)",
                    (patient_id, body),
                )
                conn.commit()
        return {"submitted": True}

    safe_write = openadapt.wrap(
        write_note,
        contract=contract,
        pinned={"patient_id": "PT-104"},
        workdir=tmp_path / "verify_work",
    )

    res = safe_write("PT-104", "Labs reviewed")
    assert res == {"submitted": True}
    assert safe_write.last_receipt.outcome == "confirmed"
    assert safe_write.last_receipt.checked_how == "system_of_record"
    assert safe_write.last_receipt.contract_receipt_sha256 is not None

    # Silent write drop is caught by the SQL contract read-back
    should_write = False
    unpinned_write = openadapt.wrap(
        write_note,
        contract=contract,
        workdir=tmp_path / "verify_work_2",
    )
    with pytest.raises(WrapVerificationError):
        unpinned_write("PT-205", "Dropped note")
    assert unpinned_write.last_receipt.outcome in ("refuted", "uncertain")
    assert unpinned_write.last_receipt.checked_how == "system_of_record"


def test_shared_ledger_across_functions() -> None:
    """A shared WrapLedger instance isolates or shares request keys cleanly."""
    shared = WrapLedger()
    calls: list[str] = []

    @openadapt.wrap(ledger=shared, idempotent=True, check=lambda r: r is True)
    def step_one(record_id: str) -> bool:
        calls.append(f"one:{record_id}")
        return True

    @openadapt.wrap(ledger=shared, idempotent=True, check=lambda r: r is True)
    def step_two(record_id: str) -> bool:
        calls.append(f"two:{record_id}")
        return True

    assert step_one("R-1") is True
    assert step_two("R-1") is True
    # Distinct function identities do not collide when request_id is omitted
    assert calls == ["one:R-1", "two:R-1"]
    # Calling step_one("R-1") again replays from idempotent cache
    assert step_one("R-1") is True
    assert calls == ["one:R-1", "two:R-1"]


def test_callable_agent_instance_and_expect_predicate() -> None:
    """Callable agent instances and expect= predicates work with zero-arg or 1-arg checks."""
    screen_state = {"banner": "Saved chart PT-104"}

    class AsyncCallableAgent:
        async def __call__(self, prompt: str, *, patient_id: str) -> str:
            await asyncio.sleep(0)
            return f"ran:{patient_id}:{prompt}"

    wrapped_agent = openadapt.wrap(
        AsyncCallableAgent(),
        pinned={"patient_id": "PT-104"},
        expect=lambda: "PT-104" in screen_state["banner"],
    )
    assert wrapped_agent.__name__ == "AsyncCallableAgent"
    out = asyncio.run(wrapped_agent("summarize visit", patient_id="PT-104"))
    assert out == "ran:PT-104:summarize visit"
    assert wrapped_agent.last_receipt.outcome == "confirmed"
    assert wrapped_agent.last_receipt.checked_how == "same_screen"
