"""pf rules for the service account: rendering and checking only, never applied."""

from __future__ import annotations

import pytest

from openadapt_flow.no_outbound_allow import AllowEntry, manual_entry
from openadapt_flow.pf_firewall import (
    ANCHOR,
    PfRuleError,
    missing_pf_conf_lines,
    pf_conf_lines,
    plan,
    render_anchor,
    validate_anchor,
)

SESSION = AllowEntry(
    network="10.20.0.15/32",
    origin="derived",
    host="10.20.0.15",
    port=3389,
    role="session",
    source="site.rdp",
)


def _rules(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln and not ln.startswith("#")]


def test_rules_pass_loopback_and_the_session_host_then_block_the_account():
    text = render_anchor("svc-records", [SESSION, manual_entry("10.30.0.0/24")])
    assert _rules(text) == [
        "pass out quick on lo0 all",
        "pass out quick proto tcp from any to 10.20.0.15 port 3389 "
        "user svc-records keep state",
        "pass out quick proto { tcp udp } from any to 10.30.0.0/24 "
        "user svc-records keep state",
        "block return out log quick proto { tcp udp } from any to any user svc-records",
    ]
    assert validate_anchor(text, "svc-records") == []


def test_empty_allow_list_blocks_everything_but_loopback():
    rules = _rules(render_anchor("svc-records", []))
    assert rules[0] == "pass out quick on lo0 all"
    assert rules[-1].startswith("block return out")
    assert len(rules) == 2


def test_duplicate_entries_render_once():
    assert len(_rules(render_anchor("svc", [SESSION, SESSION]))) == 3


@pytest.mark.parametrize("user", ["root", "", "svc records", "x;pass out all"])
def test_bad_or_root_account_is_refused(user):
    with pytest.raises(PfRuleError):
        render_anchor(user, [SESSION])


def test_public_entry_is_refused_at_render():
    public = AllowEntry(network="8.8.8.8/32", origin="manual")
    with pytest.raises(PfRuleError, match="public"):
        render_anchor("svc", [public])


def test_check_catches_hand_edits():
    good = render_anchor("svc", [SESSION])
    widened = good.replace("to 10.20.0.15 port 3389", "to 8.8.8.8 port 443")
    assert any("public" in p for p in validate_anchor(widened, "svc"))
    other_user = good.replace("user svc keep", "user admin keep")
    assert any("scoped to 'admin'" in p for p in validate_anchor(other_user, "svc"))
    no_block = "\n".join(ln for ln in good.splitlines() if not ln.startswith("block"))
    assert "the last rule must block all other outbound traffic" in validate_anchor(
        no_block, "svc"
    )
    pass_all = good + "pass out quick all\n"
    assert any("not a recognised shape" in p for p in validate_anchor(pass_all, "svc"))
    assert validate_anchor("# only comments\n", "svc") == ["anchor has no rules"]


def test_pf_conf_lines_detected():
    lines = pf_conf_lines()
    assert lines[0] == f'anchor "{ANCHOR}"'
    assert missing_pf_conf_lines('scrub-anchor "com.apple/*"\n') == list(lines)
    assert missing_pf_conf_lines("\n".join(lines) + "\n") == []


def test_plan_renders_and_checks():
    p = plan("svc", [SESSION])
    assert p.anchor_path == f"/etc/pf.anchors/{ANCHOR}"
    assert "user svc" in p.anchor_text
