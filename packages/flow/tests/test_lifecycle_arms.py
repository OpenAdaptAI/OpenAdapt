"""The comparison arms' offline pieces: claims, the script sandbox, prices and the spend cap."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from benchmark.lifecycle.arms import budget, scripts
from benchmark.lifecycle.arms.openemr_ui import claimed_from


def test_claim_is_read_from_the_result_line_only():
    assert claimed_from("Done.\nRESULT: SAVED")[0] == "done"
    assert claimed_from("RESULT: NOT SAVED: a matching patient exists")[0] == "stopped"
    # Words elsewhere never decide the claim.
    assert claimed_from("I saved it successfully.")[0] == "stopped"
    assert claimed_from("")[0] == "stopped"


@pytest.mark.parametrize(
    "src",
    [
        "import os\ndef register(ctx, r): return {}",
        "from subprocess import run\ndef register(ctx, r): return {}",
        "def register(ctx, r):\n    return open('x').read()",
        "def register(ctx, r):\n    return ctx.__class__",
        "def helper(): pass",
    ],
)
def test_sandbox_rejects(src):
    with pytest.raises(scripts.ScriptRejected):
        scripts.check_source(src)


def test_sandbox_allows_a_plain_script():
    fn = scripts.load(
        "import re, time\n"
        "def register(ctx, referral):\n"
        "    return {'result': 'NOT SAVED', 'reason': re.sub('a', 'b', 'a')}\n"
    )
    assert fn(None, {}) == {"result": "NOT SAVED", "reason": "b"}


def test_every_price_is_dated_and_complete():
    table = budget.prices()
    assert table["as_of"] >= "2026-10-20"
    for model, p in table["models"].items():
        assert {"input", "output", "cache_read", "cache_write"} <= set(p), model


def test_usage_is_priced_from_the_table():
    u = budget.Usage("claude-opus-5-5")
    u.add_response(
        SimpleNamespace(
            input_tokens=1_000_000,
            output_tokens=0,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )
    )
    assert u.usd == pytest.approx(budget.price("claude-opus-5-5")["input"])


def test_spend_cap_refuses_before_the_call(tmp_path, monkeypatch):
    ledger = tmp_path / "spend.json"
    monkeypatch.setattr(budget, "SPEND_PATH", ledger)
    monkeypatch.setenv("LIFECYCLE_SPEND_CAP_USD", "1.0")
    budget.add_spend("earlier", "claude-opus-5-5", 0.95)
    m = budget.Meter("item", budget_usd=5.0)
    with pytest.raises(budget.SpendCapReached):
        m.before_call("claude-opus-5-5", 0.10)
    m.before_call("claude-opus-5-5", 0.01)  # still under the cap
    assert json.loads(ledger.read_text())["total_usd"] == pytest.approx(0.95)


def test_item_budget_is_separate_from_the_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(budget, "SPEND_PATH", tmp_path / "spend.json")
    m = budget.Meter("item", budget_usd=0.05)
    with pytest.raises(budget.ItemBudgetExceeded):
        m.before_call("claude-opus-5-5", 0.06)


def test_key_is_never_required_offline(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("LIFECYCLE_KEYCHAIN_SERVICE", raising=False)
    with pytest.raises(SystemExit) as exc:
        budget.ensure_key()
    assert "ANTHROPIC_API_KEY" in str(exc.value)


def _rows():
    cm = {
        "version": "test",
        "teach_prep_s": 300.0,
        "compile_review_s": 600.0,
        "admit_s": 300.0,
    }
    rows = [{"type": "run_start", "cost_model": cm}]
    for arm, cost, outcome in (("a", 0.5, "correct"), ("b", 0.0, "correct")):
        rows.append({"type": "arm_start", "arm": arm, "describe": {}})
        rows.append(
            {
                "type": "phase",
                "stage": "setup",
                "arm": arm,
                "phase": "compile",
                "attended_s": 0,
                "cost_usd": 1.0,
            }
        )
        rows.append({"type": "drift_applied", "arm": arm})
        for i in range(10):
            rows.append(
                {
                    "type": "item_result",
                    "arm": arm,
                    "item_id": f"r{i}",
                    "outcome": outcome,
                    "final_oracle": "correct",
                    "chart_wanted": True,
                    "exception": "clean",
                    "cost_usd": cost,
                    "model_calls": 3,
                    "wall_s": 10.0,
                    "detail": {},
                }
            )
        rows.append(
            {
                "type": "operator",
                "arm": arm,
                "item_id": "r1",
                "kind": "stop",
                "action": "do_by_hand",
                "charged_s": 300.0,
            }
        )
    # The runner writes an arm's in-item maintenance as a maintenance phase row.
    rows.append(
        {
            "type": "phase",
            "stage": "maintenance",
            "arm": "b",
            "phase": "fix_script",
            "cost_usd": 0.4,
            "attended_s": 0,
        }
    )
    return rows


def test_economics_projects_every_component():
    from benchmark.lifecycle.arms import economics

    res = economics.compute(_rows())
    a, b = res["arms"]
    labor = budget.prices()["labor"]["operator_usd_per_hour"]
    # setup: $1 of model plus a 10-minute compile review
    assert a["per_1000_verified"]["setup_usd"] == pytest.approx(
        1.0 + 600 / 3600 * labor, abs=0.01
    )
    # 10 verified items -> x100 to reach 1,000
    assert a["per_1000_verified"]["item_model_usd"] == pytest.approx(0.5 * 10 * 100)
    assert a["per_1000_verified"]["operator_min"] == pytest.approx(5 * 100)
    assert a["automated"] == 9 and a["by_hand"] == 1
    assert b["measured"]["maintenance_model_usd"] == pytest.approx(0.4)
    best = {w["axis"]: w["best"] for w in res["winners"]}
    assert best["Model $ per item"] == ["b"]
    assert "Total $ per 1,000 verified" in economics.render(res)


def test_report_renders_every_section(tmp_path):
    from benchmark.lifecycle.arms import economics, report

    res = economics.compute(_rows())
    (tmp_path / "findings.md").write_text("## Findings\n\nHand-written.")
    md = report.render(res, tmp_path, {"seed": 7, "items": 10, "days": 2})
    for heading in (
        "Total cost per 1,000 verified items",
        "Which arm wins",
        "Correctness",
        "Measured components",
        "## Findings",
        "## Reproduce",
    ):
        assert heading in md
