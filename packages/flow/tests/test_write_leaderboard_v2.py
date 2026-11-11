"""Write leaderboard v2, offline: the claim, the intervals, the fault rewrite, the oracle.

benchmark/write_leaderboard_v2: the pieces whose behaviour the pre-registered
protocol fixes, checked without any app, browser or model.
"""

from __future__ import annotations

import json

import pytest

from benchmark.write_leaderboard_v2.apps.base import App, norm
from benchmark.write_leaderboard_v2.common import parse_claim, wilson
from benchmark.write_leaderboard_v2.proxy import Save, rewrite


def test_claim_is_the_last_result_line() -> None:
    assert parse_claim("working\nRESULT: DONE 42\n")["status"] == "done"
    assert parse_claim("RESULT: DONE\nRESULT: NOT DONE the save failed")["status"] == "not_done"
    assert parse_claim("I saved it.")["status"] == "not_done"


def test_wilson_matches_known_values() -> None:
    lo, hi = wilson(28, 111)
    assert lo == pytest.approx(0.1807, abs=1e-3)
    assert hi == pytest.approx(0.3404, abs=1e-3)
    assert wilson(0, 0) == (0.0, 0.0)
    assert wilson(0, 10)[0] == 0.0


def test_rewrite_form_json_multipart() -> None:
    assert rewrite(b"a=1&phone_pro=555-0191&b=", "application/x-www-form-urlencoded", {"phone_pro": "555-0119"}) == (
        b"a=1&phone_pro=555-0119&b="
    )
    assert json.loads(rewrite(b'{"industry": "Retail"}', "application/json", {"industry": "Legal"})) == {"industry": "Legal"}
    body = b'--X\r\nContent-Disposition: form-data; name="issue[estimated_hours]"\r\n\r\n2.5\r\n--X--\r\n'
    out = rewrite(body, "multipart/form-data; boundary=X", {"issue[estimated_hours]": "25"})
    assert b"\r\n\r\n25\r\n--X--" in out
    with pytest.raises(KeyError):
        rewrite(b"a=1", "application/x-www-form-urlencoded", {"missing": "x"})


def test_save_matcher() -> None:
    s = Save(r"^/contact/card\.php$", body_require=r"(^|&)action=add(&|$)")
    assert s.matches("POST", "/contact/card.php", b"token=x&action=add&lastname=Kim")
    assert not s.matches("POST", "/contact/card.php", b"action=update")
    assert not s.matches("GET", "/contact/card.php", b"action=add")


class _Toy(App):
    columns = {"name": "text", "phone": "phone"}


def test_oracle_verdicts() -> None:
    app = _Toy("t", 1, 2)
    seed = [{"id": "1", "name": "Seed", "phone": "555-0100"}]
    task = {"name": "New Person", "phone": "555-0191"}
    ok = seed + [{"id": "2", "name": "NEW  person", "phone": "(555) 0191"}]
    assert app.judge(seed, ok, task)["verdict"] == "correct"
    wrong = seed + [{"id": "2", "name": "New Person", "phone": "555-0119"}]
    assert app.judge(seed, wrong, task)["verdict"] == "wrong"
    dup = ok + [{"id": "3", "name": "New Person", "phone": "555-0191"}]
    assert app.judge(seed, dup, task)["collateral"] is True
    side = [{"id": "1", "name": "Seed", "phone": "555-0104"}, ok[1]]
    v = app.judge(seed, side, task)
    assert v["verdict"] == "wrong" and v["changed"] == ["1"]
    assert app.judge(seed, seed, task)["verdict"] == "missing"


def test_normalisation() -> None:
    assert norm("date", "1990-7-8") == "1990-07-08"
    assert norm("number", "2.50") == norm("number", "2.5")
    assert norm("text", " 41 ELM  Street ") == "41 elm street"
