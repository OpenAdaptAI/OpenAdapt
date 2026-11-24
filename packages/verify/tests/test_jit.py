"""Unit and mock-page tests for openadapt_verify.jit and mcpify --from-compiled."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openadapt_verify import (
    CompiledPlan,
    McpifyServer,
    ParameterBinder,
    ReadbackResult,
    format_scorecard,
    jit_wrap,
    load_compiled_tools,
    verify_mcpify_receipt,
)
from openadapt_verify.mcpify import main as mcpify_main


class _MockPortalPage:
    """In-memory mock page supporting Playwright-like sync actions and reload."""

    def __init__(
        self, *, submit_selector: str = "#submit-btn", persist: bool = True
    ) -> None:
        self.url: str = "about:blank"
        self.submit_selector: str = submit_selector
        self.persist: bool = persist
        self.draft: dict[str, str] = {}
        self.saved_records: list[dict[str, str]] = []
        self.reloaded: bool = False
        self.available_selectors: set[str] = {
            "#record-id",
            "#amount",
            "#secret-pin",
            submit_selector,
        }

    def rename_submit_button(self, new_selector: str) -> None:
        self.available_selectors.discard(self.submit_selector)
        self.submit_selector = new_selector
        self.available_selectors.add(new_selector)

    def goto(self, url: str) -> None:
        self.url = url

    def fill(self, selector: str, value: str) -> None:
        if selector not in self.available_selectors:
            raise RuntimeError(f"Selector not found: {selector}")
        self.draft[selector] = value

    def select_option(self, selector: str, value: str) -> None:
        if selector not in self.available_selectors:
            raise RuntimeError(f"Selector not found: {selector}")
        self.draft[selector] = value

    def press(self, selector: str, key: str) -> None:
        if selector not in self.available_selectors:
            raise RuntimeError(f"Selector not found: {selector}")
        self.draft[f"{selector}:key"] = key

    def click(self, selector: str) -> None:
        if selector not in self.available_selectors:
            raise RuntimeError(f"Selector not found: {selector}")
        if selector == self.submit_selector and self.persist:
            self.saved_records.append(dict(self.draft))

    def reload(self) -> None:
        self.reloaded = True

    def content(self) -> str:
        # Fresh reload renders only persisted records (never transient draft).
        return json.dumps(self.saved_records)


class _AsyncMockPortalPage:
    """Async counterpart of _MockPortalPage for async jit_wrap tests."""

    def __init__(
        self, *, submit_selector: str = "#submit-btn", persist: bool = True
    ) -> None:
        self._sync = _MockPortalPage(submit_selector=submit_selector, persist=persist)

    async def goto(self, url: str) -> None:
        self._sync.goto(url)

    async def fill(self, selector: str, value: str) -> None:
        self._sync.fill(selector, value)

    async def click(self, selector: str) -> None:
        self._sync.click(selector)

    async def reload(self) -> None:
        self._sync.reload()

    async def content(self) -> str:
        return self._sync.content()


def test_parameter_binder_longest_match_empty_refusal_and_secret_detection() -> None:
    params = {"short_num": "100", "long_num": "10000", "password": "s3cr3t-value"}
    templated, bound = ParameterBinder.bind_value("total=10000 ref=100", params)
    assert templated == "total={{long_num}} ref={{short_num}}"
    assert set(bound) == {"long_num", "short_num"}

    assert ParameterBinder.is_secret_param("password") is True
    assert ParameterBinder.is_secret_param("api_key") is True
    assert ParameterBinder.is_secret_param("auth_token") is True
    assert ParameterBinder.is_secret_param("record_id") is False

    with pytest.raises(ValueError, match="must not be empty"):
        ParameterBinder.bind_value("abc", {"record_id": "   "})

    with pytest.raises(ValueError, match="leaked into compiled plan"):
        ParameterBinder.assert_no_secrets_leaked(
            '{"value": "s3cr3t-value"}',
            {"password": "s3cr3t-value"},
        )


def test_cold_run_compiles_and_warm_run_skips_agent_with_different_params(
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "compiled"
    signing_key = Ed25519PrivateKey.generate()
    agent_calls = 0

    @jit_wrap(
        "submit_invoice",
        cache_dir=cache_dir,
        signing_key=signing_key,
        estimated_cold_usd=0.08,
        description="Submit an invoice and verify via fresh reload.",
    )
    def submit_invoice(page: Any, params: dict[str, str]) -> str:
        nonlocal agent_calls
        agent_calls += 1
        page.goto("https://portal.example.test/invoices")
        page.fill("#record-id", params["invoice_id"])
        page.fill("#amount", params["amount"])
        page.click("#submit-btn")
        return "submitted"

    # Run #1: Cold run invokes agent, verifies via fresh reload, writes cache + signs receipt
    page1 = _MockPortalPage()
    r1 = submit_invoice(page1, {"invoice_id": "INV-1001", "amount": "250.00"})
    assert r1.mode == "cold"
    assert r1.status == "VERIFIED"
    assert agent_calls == 1
    assert r1.plan_path == cache_dir / "submit_invoice.json"
    assert r1.plan_path.is_file()
    assert r1.receipt is not None
    assert (
        verify_mcpify_receipt(r1.receipt, trusted_public_key=signing_key.public_key())
        is True
    )

    # Compiled plan on disk must be parameterized (no hardcoded cold-run values)
    raw_plan = r1.plan_path.read_text(encoding="utf-8")
    assert "{{invoice_id}}" in raw_plan
    assert "{{amount}}" in raw_plan
    assert "INV-1001" not in raw_plan
    assert "250.00" not in raw_plan

    # Run #2: Warm run with DIFFERENT params skips agent completely (0 agent calls)
    page2 = _MockPortalPage()
    r2 = submit_invoice(page2, {"invoice_id": "INV-2099", "amount": "999.50"})
    assert r2.mode == "warm"
    assert r2.status == "VERIFIED"
    assert r2.estimated_usd == 0.0
    assert agent_calls == 1  # Agent was NOT called on warm run!
    assert page2.reloaded is True
    assert page2.saved_records == [{"#record-id": "INV-2099", "#amount": "999.50"}]
    assert r2.stats is not None
    assert r2.stats.cold_runs == 1
    assert r2.stats.warm_runs == 1
    assert r2.stats.total_usd_saved == pytest.approx(0.08)

    scorecard = format_scorecard(r2)
    assert "mode=warm status=VERIFIED" in scorecard
    assert "cost=$0.0000" in scorecard


def test_cold_run_unconfirmed_write_refuses_to_write_cache(tmp_path: Path) -> None:
    cache_dir = tmp_path / "compiled"

    @jit_wrap("silent_drop", cache_dir=cache_dir)
    def silent_drop(page: Any, params: dict[str, str]) -> None:
        page.goto("https://portal.example.test/invoices")
        page.fill("#record-id", params["invoice_id"])
        page.click("#submit-btn")

    # Portal silently drops the write on reload -> read-back fails -> NO cache written
    page = _MockPortalPage(persist=False)
    res = silent_drop(page, {"invoice_id": "INV-404"})
    assert res.mode == "cold"
    assert res.status == "UNCONFIRMED_WRITE"
    assert res.plan_path is None
    assert not (cache_dir / "silent_drop.json").exists()


def test_warm_run_dom_mutation_triggers_bounded_self_heal_and_run3_is_warm_again(
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "compiled"
    agent_calls = 0
    healer_calls = 0

    def step_healer(
        page: Any,
        params: dict[str, str],
        *,
        failed_step_index: int,
        failed_step: Any,
    ) -> None:
        nonlocal healer_calls
        healer_calls += 1
        assert failed_step_index == 3
        assert failed_step.action == "click"
        # Healer discovers the renamed submit button on the live page
        page.click("#submit-btn-v2")

    @jit_wrap(
        "heal_workflow",
        cache_dir=cache_dir,
        healer_fn=step_healer,
        max_heal_steps=3,
    )
    def workflow(page: Any, params: dict[str, str]) -> None:
        nonlocal agent_calls
        agent_calls += 1
        page.goto("https://portal.example.test/invoices")
        page.fill("#record-id", params["invoice_id"])
        page.fill("#amount", params["amount"])
        page.click("#submit-btn")

    # Run #1: Cold
    r1 = workflow(_MockPortalPage(), {"invoice_id": "INV-1", "amount": "10.00"})
    assert r1.mode == "cold" and r1.status == "VERIFIED"
    assert agent_calls == 1

    # Run #2: Submit button renamed to #submit-btn-v2 -> step 3 heals -> cache updated
    mutated_page = _MockPortalPage()
    mutated_page.rename_submit_button("#submit-btn-v2")
    r2 = workflow(mutated_page, {"invoice_id": "INV-2", "amount": "20.00"})
    assert r2.mode == "healed"
    assert r2.status == "VERIFIED"
    assert r2.healed_steps == (3,)
    assert healer_calls == 1
    assert agent_calls == 1

    # Run #3: Warm again on the mutated UI with 0 healer and 0 agent calls!
    mutated_page_3 = _MockPortalPage(submit_selector="#submit-btn-v2")
    r3 = workflow(mutated_page_3, {"invoice_id": "INV-3", "amount": "30.00"})
    assert r3.mode == "warm"
    assert r3.status == "VERIFIED"
    assert r3.healed_steps == ()
    assert healer_calls == 1
    assert agent_calls == 1
    assert mutated_page_3.saved_records == [{"#record-id": "INV-3", "#amount": "30.00"}]


def test_failed_self_heal_does_not_overwrite_cached_plan(tmp_path: Path) -> None:
    cache_dir = tmp_path / "compiled"

    def bad_healer(page: Any, params: dict[str, str]) -> None:
        # Clicks a harmless selector without persisting the write
        page.fill("#amount", params["amount"])

    @jit_wrap("no_poison", cache_dir=cache_dir, healer_fn=bad_healer, max_heal_steps=2)
    def workflow(page: Any, params: dict[str, str]) -> None:
        page.goto("https://portal.example.test/invoices")
        page.fill("#record-id", params["invoice_id"])
        page.fill("#amount", params["amount"])
        page.click("#submit-btn")

    # Cold run succeeds and caches #submit-btn
    r1 = workflow(_MockPortalPage(), {"invoice_id": "INV-10", "amount": "50.00"})
    assert r1.status == "VERIFIED"
    plan_file = cache_dir / "no_poison.json"
    before_bytes = plan_file.read_text(encoding="utf-8")

    # Mutated page where healer fails to persist -> UNCONFIRMED_WRITE -> cache untouched!
    mutated = _MockPortalPage(submit_selector="#renamed-btn")
    r2 = workflow(mutated, {"invoice_id": "INV-11", "amount": "60.00"})
    assert r2.mode == "healed"
    assert r2.status == "UNCONFIRMED_WRITE"
    assert plan_file.read_text(encoding="utf-8") == before_bytes


def test_secret_named_params_never_persisted_in_plaintext(tmp_path: Path) -> None:
    cache_dir = tmp_path / "compiled"

    @jit_wrap("secret_safe", cache_dir=cache_dir)
    def login_and_submit(page: Any, params: dict[str, str]) -> None:
        page.goto("https://portal.example.test/secure")
        page.fill("#record-id", params["record_id"])
        page.fill("#secret-pin", params["api_key"])
        page.click("#submit-btn")

    secret_val = "sk-live-super-secret-998877"
    r1 = login_and_submit(
        _MockPortalPage(),
        {"record_id": "REC-77", "api_key": secret_val},
    )
    assert r1.status == "VERIFIED"
    assert r1.plan_path is not None
    disk_text = r1.plan_path.read_text(encoding="utf-8")
    assert secret_val not in disk_text
    assert "{{api_key}}" in disk_text

    loaded = CompiledPlan.load(r1.plan_path)
    assert loaded.secret_params == ("api_key",)
    assert "api_key" not in loaded.readback_expectations


def test_async_jit_wrap_cold_and_warm(tmp_path: Path) -> None:
    cache_dir = tmp_path / "compiled"
    calls = 0

    @jit_wrap("async_invoice", cache_dir=cache_dir)
    async def submit_async(page: Any, params: dict[str, str]) -> str:
        nonlocal calls
        calls += 1
        await page.goto("https://portal.example.test/async")
        await page.fill("#record-id", params["invoice_id"])
        await page.click("#submit-btn")
        return "ok"

    r1 = asyncio.run(submit_async(_AsyncMockPortalPage(), {"invoice_id": "ASYNC-1"}))
    assert r1.mode == "cold" and r1.status == "VERIFIED"
    assert calls == 1

    page2 = _AsyncMockPortalPage()
    r2 = asyncio.run(submit_async(page2, {"invoice_id": "ASYNC-2"}))
    assert r2.mode == "warm" and r2.status == "VERIFIED"
    assert calls == 1
    assert page2._sync.saved_records == [{"#record-id": "ASYNC-2"}]


def test_mcpify_from_compiled_exposes_compiled_plan_as_callable_mcp_tool(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cache_dir = tmp_path / "compiled"

    @jit_wrap(
        "create_claim", cache_dir=cache_dir, description="Create a claim in the portal."
    )
    def create_claim(page: Any, params: dict[str, str]) -> None:
        page.goto("https://portal.example.test/claims")
        page.fill("#record-id", params["claim_id"])
        page.fill("#amount", params["amount"])
        page.click("#submit-btn")

    r1 = create_claim(_MockPortalPage(), {"claim_id": "CLM-1", "amount": "120.00"})
    assert r1.status == "VERIFIED"

    # 1. load_compiled_tools converts .openadapt/compiled/*.json to WorkflowToolSpec
    specs = load_compiled_tools(cache_dir)
    assert len(specs) == 1
    assert specs[0].name == "create_claim"
    assert set(specs[0].input_schema["required"]) == {"claim_id", "amount"}

    # 2. McpifyServer lists and executes the compiled tool with verified read-back
    server = McpifyServer(
        specs,
        runner=lambda _spec, args: {"written": args},
        readback_oracle=lambda _spec, args, _out: ReadbackResult(
            verdict="VERIFIED",
            reason="Portal reload confirmed claim.",
            expected=dict(args),
            observed=dict(args),
        ),
    )
    server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})

    listed = server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert listed is not None
    assert [t["name"] for t in listed["result"]["tools"]] == ["create_claim"]

    called = server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "create_claim",
                "arguments": {"claim_id": "CLM-999", "amount": "450.00"},
            },
        }
    )
    assert called is not None
    assert called["result"]["isError"] is False
    payload = json.loads(called["result"]["content"][0]["text"])
    assert payload["status"] == "VERIFIED"
    assert payload["llm_calls"] == 0

    # 3. CLI `config --from-compiled <dir>` outputs valid mcpServers snippet
    rc = mcpify_main(
        ["config", "--from-compiled", str(cache_dir), "--client", "claude"]
    )
    assert rc == 0
    cfg = json.loads(capsys.readouterr().out)
    assert "create_claim" in cfg["mcpServers"]
    assert "--from-compiled" in cfg["mcpServers"]["create_claim"]["args"]


def test_parameter_binder_single_pass_prevents_double_substitution() -> None:
    params = {"user": "alice", "field": "user"}
    templated, bound = ParameterBinder.bind_value("owner=alice key=user", params)
    assert templated == "owner={{user}} key={{field}}"
    assert set(bound) == {"user", "field"}
    assert ParameterBinder.substitute(templated, params) == "owner=alice key=user"


def test_empty_recording_refuses_to_compile_and_from_dict_rejects_empty_steps(
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "compiled"
    signing_key = Ed25519PrivateKey.generate()

    @jit_wrap(
        "noop_agent",
        cache_dir=cache_dir,
        signing_key=signing_key,
        readback_fn=lambda _p, _params: True,
    )
    def noop_agent(_page: Any, _params: dict[str, str]) -> str:
        return "did nothing"

    res = noop_agent(_MockPortalPage(), {"invoice_id": "INV-0"})
    assert res.mode == "cold"
    assert res.status == "STOPPED"
    assert res.plan_path is None
    assert res.receipt is None
    assert not (cache_dir / "noop_agent.json").exists()

    with pytest.raises(ValueError, match="non-empty list"):
        CompiledPlan.from_dict(
            {
                "schema_version": "openadapt.jit.plan/v1",
                "name": "bad_plan",
                "steps": [],
            }
        )


def test_full_agent_fallback_healer_replaces_plan_without_duplicating_prefix(
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "compiled"
    submit_sel = "#submit-btn"

    # healer_fn=None -> wrapped fn itself re-runs from scratch on warm failure
    @jit_wrap("full_fallback", cache_dir=cache_dir, max_heal_steps=5)
    def workflow(page: Any, params: dict[str, str]) -> None:
        page.goto("https://portal.example.test/invoices")
        page.fill("#record-id", params["invoice_id"])
        page.fill("#amount", params["amount"])
        page.click(submit_sel)

    r1 = workflow(_MockPortalPage(), {"invoice_id": "INV-1", "amount": "10.00"})
    assert r1.mode == "cold" and r1.status == "VERIFIED"
    assert len(CompiledPlan.load(cache_dir / "full_fallback.json").steps) == 4

    # Rename submit button and let full wrapped agent recover
    submit_sel = "#submit-btn-v2"
    mutated = _MockPortalPage(submit_selector="#submit-btn-v2")
    r2 = workflow(mutated, {"invoice_id": "INV-2", "amount": "20.00"})
    assert r2.mode == "healed" and r2.status == "VERIFIED"

    healed_plan = CompiledPlan.load(cache_dir / "full_fallback.json")
    # Must be 4 steps, NOT 7 (prefix steps 0..2 must not be duplicated)
    assert len(healed_plan.steps) == 4
    assert [s.action for s in healed_plan.steps] == ["goto", "fill", "fill", "click"]

    # Run #3 is warm and clean
    page3 = _MockPortalPage(submit_selector="#submit-btn-v2")
    r3 = workflow(page3, {"invoice_id": "INV-3", "amount": "30.00"})
    assert r3.mode == "warm" and r3.status == "VERIFIED"
    assert page3.saved_records == [{"#record-id": "INV-3", "#amount": "30.00"}]


def test_secret_rejected_in_readback_expectations_and_redacted_in_reason(
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "compiled"

    with pytest.raises(ValueError, match="Secret-named key"):
        jit_wrap(
            "bad_secret_key",
            cache_dir=cache_dir,
            readback_expectations={"password": "{{password}}"},
        )

    with pytest.raises(ValueError, match="Secret parameter reference"):
        jit_wrap(
            "bad_secret_ref",
            cache_dir=cache_dir,
            readback_expectations={"label": "{{api_key}}"},
        )

    secret_val = "tok-very-secret-42"

    @jit_wrap(
        "redact_reason",
        cache_dir=cache_dir,
        readback_fn=lambda _p, params: (
            False,
            f"rejected token={params['auth_token']}",
        ),
    )
    def workflow(page: Any, params: dict[str, str]) -> None:
        page.fill("#record-id", params["record_id"])
        page.fill("#secret-pin", params["auth_token"])
        page.click("#submit-btn")

    res = workflow(_MockPortalPage(), {"record_id": "R-1", "auth_token": secret_val})
    assert res.status == "UNCONFIRMED_WRITE"
    assert secret_val not in res.reason
    assert "<redacted>" in res.reason


def test_receipt_llm_calls_readback_error_handling_and_symlink_containment(
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "compiled"
    signing_key = Ed25519PrivateKey.generate()

    def step_healer(page: Any, _params: dict[str, str]) -> None:
        page.click("#submit-btn-v2")

    @jit_wrap(
        "signed_flow",
        cache_dir=cache_dir,
        signing_key=signing_key,
        healer_fn=step_healer,
    )
    def workflow(page: Any, params: dict[str, str]) -> None:
        page.fill("#record-id", params["record_id"])
        page.click("#submit-btn")

    # Cold run receipt -> llm_calls == 1
    r1 = workflow(_MockPortalPage(), {"record_id": "R-1"})
    assert r1.status == "VERIFIED" and r1.receipt is not None
    assert r1.receipt.payload["llm_calls"] == 1

    # Warm run receipt -> llm_calls == 0
    r2 = workflow(_MockPortalPage(), {"record_id": "R-2"})
    assert r2.mode == "warm" and r2.status == "VERIFIED" and r2.receipt is not None
    assert r2.receipt.payload["llm_calls"] == 0

    # Healed run receipt -> llm_calls == 1
    r3 = workflow(
        _MockPortalPage(submit_selector="#submit-btn-v2"), {"record_id": "R-3"}
    )
    assert r3.mode == "healed" and r3.status == "VERIFIED" and r3.receipt is not None
    assert r3.receipt.payload["llm_calls"] == 1

    # Reload/transport error during read-back -> UNCONFIRMED_WRITE (no crash)
    class _BrokenReloadPage(_MockPortalPage):
        def reload(self) -> None:
            raise ConnectionError("network reset during reload")

    r4 = workflow(
        _BrokenReloadPage(submit_selector="#submit-btn-v2"), {"record_id": "R-4"}
    )
    assert r4.status == "UNCONFIRMED_WRITE"
    assert "ConnectionError" in r4.reason

    # Symlink escaping cache_dir is refused by load_compiled_tools
    outside = tmp_path / "outside.json"
    outside.write_text(
        (cache_dir / "signed_flow.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    escape_link = cache_dir / "escape.json"
    escape_link.symlink_to(outside)
    with pytest.raises(ValueError, match="symlink escaping cache directory"):
        load_compiled_tools(cache_dir)
