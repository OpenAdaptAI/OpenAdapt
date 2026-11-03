"""The read-only browser teach lane's guards, in a real headless Chromium.

The full teach -> tool -> file loop is tests/e2e/test_browser_teach_e2e.py.
These check the pieces that keep a compiled run from doing the wrong thing:
it stops on look-alike or relabelled controls, the browser sends no write
request and stays on one site, and teach_finish refuses what it can't
compile honestly.
"""

from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path
from typing import Any, Iterator

import pytest

pytest.importorskip("playwright")

from openadapt_flow.agent import browser_teach as bt  # noqa: E402
from openadapt_flow.agent.bridge import BridgeError  # noqa: E402
from openadapt_flow.agent.cli import main as agent_main  # noqa: E402
from openadapt_flow.agent.example_app import serve  # noqa: E402


@pytest.fixture(scope="module")
def portal() -> Iterator[str]:
    base, stop = serve(0)
    yield base
    stop()


@pytest.fixture
def browser(portal: str, tmp_path: Path) -> Iterator[bt._Browser]:
    thread = bt._BrowserThread()
    box: dict = {}
    thread.call(lambda: box.update(b=bt._Browser(bt._origin(portal), tmp_path, True)))
    browser = box["b"]
    browser.thread = thread  # type: ignore[attr-defined]
    yield browser
    thread.call(browser.close)
    thread.stop()


def _on(browser: bt._Browser, fn):
    return browser.thread.call(fn)  # type: ignore[attr-defined]


def _writes(base: str) -> int:
    with urllib.request.urlopen(f"{base}/_stats", timeout=5) as response:
        return json.loads(response.read())["writes"]


def test_two_lookalike_buttons_stop_the_run(browser: bt._Browser, portal: str) -> None:
    def check():
        page = browser.page
        page.goto(f"{portal}/")
        page.set_content("<button>Export</button><button>Export</button>")
        target = {"role": "button", "name": "Export", "label": "Export"}
        with pytest.raises(bt.StepStopped, match="2 controls match 'Export'"):
            bt._resolve(page, target, 500)

    _on(browser, check)


def test_relabelled_control_stops_the_run(browser: bt._Browser, portal: str) -> None:
    def check():
        page = browser.page
        page.goto(f"{portal}/")
        page.set_content('<button id="go">Export all</button>')
        target = {"css": 'button[id="go"]', "label": "Export"}
        with pytest.raises(bt.StepStopped, match="now labelled 'Export all'"):
            bt._resolve(page, target, 500)

    _on(browser, check)


def test_missing_control_stops_the_run(browser: bt._Browser, portal: str) -> None:
    def check():
        page = browser.page
        page.goto(f"{portal}/")
        target = {"role": "link", "name": "Export CSV", "label": "Export CSV"}
        with pytest.raises(bt.StepStopped, match="not on the page"):
            bt._resolve(page, target, 300)

    _on(browser, check)


def test_links_to_one_place_are_one_target(browser: bt._Browser, portal: str) -> None:
    def check():
        page = browser.page
        page.goto(f"{portal}/")
        raw = page.evaluate(bt._OBSERVE_JS)
        _, controls = bt._render(raw)
        link = next(c for c in controls if c["label"] == "Sales report")
        target = bt._unique_target(page, link)
        assert target is not None and target["same_href"] == "/reports"
        assert bt._resolve(page, target, 500) is not None
        page.set_content('<a href="/a">Sales report</a><a href="/b">Sales report</a>')
        with pytest.raises(bt.StepStopped, match="go to different places"):
            bt._resolve(page, target, 500)

    _on(browser, check)


def test_browser_sends_no_write_request(browser: bt._Browser, portal: str) -> None:
    def check():
        page = browser.page
        page.goto(f"{portal}/reports")
        sent = page.evaluate(
            "() => fetch('/reports/email', {method: 'POST', body: 'x'})"
            ".then(() => 'sent', () => 'failed')"
        )
        assert sent == "failed"
        assert browser.blocked == ["POST /reports/email, a write this run doesn't send"]

    _on(browser, check)
    assert _writes(portal) == 0


def test_browser_stays_on_the_taught_site(browser: bt._Browser, portal: str) -> None:
    def check():
        page = browser.page
        page.goto(f"{portal}/")
        page.set_content('<a href="https://example.com/">Elsewhere</a>')
        page.get_by_role("link", name="Elsewhere").click()
        browser.settle()
        assert browser.blocked == [
            "leaving " + bt._origin(portal) + " for https://example.com"
        ]

    _on(browser, check)


def _ref(page: str, label: str) -> str:
    found = re.search(r"\[(e\d+)\] \S+ \"" + re.escape(label) + '"', page)
    assert found, page
    return found.group(1)


def test_finish_refuses_without_a_download_or_with_an_unused_input(
    portal: str, tmp_path: Path
) -> None:
    bridge = bt.BrowserTeachBridge(tmp_path / "tools", tmp_path / "files")
    started = bridge.dispatch(
        "teach_start",
        {
            "name": "report",
            "url": f"{portal}/reports",
            "task": "Export the sales report",
            "inputs": {"start_date": "2026-02-01", "end_date": "2026-02-10"},
        },
    )
    page = started["page"]
    with pytest.raises(BridgeError, match="no file has downloaded"):
        bridge.dispatch("teach_finish", {})
    page = bridge.dispatch(
        "teach_act",
        {"action": "type", "ref": _ref(page, "From date"), "input": "start_date"},
    )["page"]
    # Typed as text, but it is the input's value: it becomes the input.
    page = bridge.dispatch(
        "teach_act",
        {"action": "type", "ref": _ref(page, "To date"), "text": "2026-02-10"},
    )["page"]
    page = bridge.dispatch(
        "teach_act", {"action": "click", "ref": _ref(page, "Show report")}
    )["page"]
    done = bridge.dispatch(
        "teach_act", {"action": "click", "ref": _ref(page, "Export CSV")}
    )
    assert done["rows"] > 0
    with pytest.raises(BridgeError, match="no column 'when'"):
        bridge.dispatch(
            "teach_finish",
            {
                "checks": {
                    "between": [
                        {"column": "when", "from": "start_date", "to": "end_date"}
                    ]
                }
            },
        )
    finished = bridge.dispatch("teach_finish", {})
    assert finished["status"] == "ready"
    steps = json.loads(Path(finished["program"]).read_text())["steps"]
    assert [s["value"] for s in steps if s["op"] == "fill"] == [
        {"input": "start_date"},
        {"input": "end_date"},
    ]

    bridge2 = bt.BrowserTeachBridge(tmp_path / "tools2", tmp_path / "files")
    bridge2.dispatch(
        "teach_start",
        {
            "name": "report",
            "url": f"{portal}/reports",
            "task": "Export the sales report",
            "inputs": {"start_date": "2026-02-01", "region": "West"},
        },
    )
    page = bridge2.dispatch("teach_observe", {})["page"]
    for args in (
        {"action": "type", "ref": _ref(page, "From date"), "input": "start_date"},
        {"action": "type", "ref": _ref(page, "To date"), "text": "2026-02-03"},
        {"action": "click", "ref": _ref(page, "Show report")},
    ):
        page = bridge2.dispatch("teach_act", args)["page"]
    bridge2.dispatch("teach_act", {"action": "click", "ref": _ref(page, "Export CSV")})
    with pytest.raises(BridgeError, match="input 'region' was never typed"):
        bridge2.dispatch("teach_finish", {})
    bridge2.dispatch("teach_abort", {})


def test_check_file_reports_rows_with_another_value(tmp_path: Path) -> None:
    path = tmp_path / "out.csv"
    path.write_text("id,region\n1,West\n2,East\n")
    program = {
        "result": {"suffix": ".csv", "columns": ["id", "region"]},
        "checks": {"equals": [{"column": "region", "input": "r"}]},
    }
    _, failed, _ = bt._check_file(path, program, {"r": "West"})
    assert failed == ["1 rows have region other than 'West' (first: 'East')"]


def test_check_file_reports_rows_outside_the_range(tmp_path: Path) -> None:
    path = tmp_path / "out.csv"
    path.write_text("id,date\n1,2026-01-05\n2,2026-02-01\n")
    program = {
        "result": {"suffix": ".csv", "columns": ["id", "date"]},
        "checks": {"between": [{"column": "date", "from": "a", "to": "b"}]},
    }
    passed, failed, facts = bt._check_file(
        path, program, {"a": "2026-01-01", "b": "2026-01-31"}
    )
    assert passed == ["its columns match the taught file"]
    assert failed == [
        "1 rows have date outside 2026-01-01 to 2026-01-31 (first: '2026-02-01')"
    ]
    assert facts["rows"] == 2


def test_write_labels_and_post_forms_are_refused() -> None:
    bridge = bt.BrowserTeachBridge.__new__(bt.BrowserTeachBridge)
    for label in ("Save", "Delete report", "Send email", "Pay now"):
        with pytest.raises(BridgeError, match="changes data"):
            bridge._refuse_write({"label": label, "role": "button"}, "clicking")
    with pytest.raises(BridgeError, match="POST"):
        bridge._refuse_write(
            {"label": "Go", "role": "button", "method": "POST"}, "clicking"
        )
    bridge._refuse_write({"label": "Export CSV", "role": "link"}, "clicking")
    bridge._refuse_write(
        {"label": "Show report", "role": "button", "method": "GET"}, "clicking"
    )


def test_bad_programs_on_disk_are_skipped(tmp_path: Path, capsys) -> None:
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / bt.PROGRAM_FILE).write_text("{not json")
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / bt.PROGRAM_FILE).write_text(json.dumps({"schema": "x"}))
    bridge = bt.BrowserTeachBridge(tmp_path, tmp_path / "files")
    assert bridge.programs == {}
    assert capsys.readouterr().err.count("skipped") == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["serve", "--teach-browser", "--url", "http://127.0.0.1:1"],
        ["serve", "--teach-browser", "--bundles", "x"],
        ["serve", "--tools-dir", "x"],
    ],
)
def test_serve_teach_browser_refuses_other_modes(argv: list[str], capsys) -> None:
    assert agent_main(argv) == 2
    assert "--teach-browser" in capsys.readouterr().err


def test_controls_in_a_named_frame_are_listed_and_found(
    browser: bt._Browser, portal: str
) -> None:
    """OpenEMR shows its forms in named iframes. A control there is listed
    with its frame, found again through it, and clicked; a frame with no name
    or id can't be found again, so its controls aren't offered."""

    def check():
        page = browser.page
        page.goto(f"{portal}/")
        page.set_content(
            '<iframe name="pat" srcdoc="<button onclick=\'window.parent.hit=1\'>'
            'Inner Save</button>"></iframe>'
            '<iframe srcdoc="<button>Hidden twin</button>"></iframe>'
        )
        page.wait_for_function("window.frames.length === 2")
        page.frames[1].wait_for_selector("button")
        raw = bt._observe_page(page, bt._origin(portal))
        text, controls = bt._render(raw)
        assert "(a frame with no name is not listed)" in text
        inner = next(c for c in controls if c["label"] == "Inner Save")
        assert inner["frames"] == ['iframe[name="pat"]']
        assert not any(c["label"] == "Hidden twin" for c in controls)
        target = bt._unique_target(page, inner)
        assert target is not None and target["frames"] == ['iframe[name="pat"]']
        bt._resolve(page, target, 1000).click()
        assert page.evaluate("window.hit") == 1

    _on(browser, check)


# -- taught writes need a person's signed teach authorization ---------------------
# Real approver keys on a real roster, the real verifier, real write contracts.
# Every refusal here comes before a browser opens. tests/e2e/
# test_browser_teach_write_e2e.py teaches and runs one end to end.

APP = "http://127.0.0.1:1"
ORDER = {"customer": "Acme Ltd", "product": "Desk lamp", "units": "3"}


def _order_contract() -> Any:
    from openadapt_flow.verify import WriteContract

    return WriteContract.from_dict(
        {
            "schema_version": "openadapt.verify/v1",
            "writes": ["POST /login", "POST /orders"],
            "verifier": {
                "kind": "rest",
                "base_url": APP,
                "records_path": "/api/orders",
                "records_key": "records",
            },
            "effects": [
                {
                    "kind": "record_written",
                    "match": {k: {"param": k} for k in ORDER},
                    "expected_count": 1,
                    "count_new_only": True,
                    "timeout_s": 1.0,
                }
            ],
        },
        name="order",
    )


def _bridge(tmp_path: Path, authority: Any = None) -> bt.BrowserTeachBridge:
    return bt.BrowserTeachBridge(
        tmp_path / "tools",
        tmp_path / "files",
        contracts={"order": _order_contract()},
        teach_authorization=authority,
    )


def _teach(bridge: bt.BrowserTeachBridge, name: str = "add_order", **inputs: str):
    return bridge.dispatch(
        "teach_start",
        {
            "name": name,
            "url": f"{APP}/login",
            "task": "Add an order",
            "contract": "order",
            "inputs": {**ORDER, **inputs},
        },
    )


def _refused(bridge: bt.BrowserTeachBridge, code: str, **kw: Any) -> None:
    with pytest.raises(BridgeError, match=re.escape(code)):
        _teach(bridge, **kw)


def test_a_covering_authorization_binds_run_1(tmp_path: Path) -> None:
    from tests._teach_authorization_support import order_authority

    bridge = _bridge(tmp_path, order_authority(tmp_path / "auth", APP, ORDER))
    verification, statement, scope = bridge._authorize_write(
        "add_order", f"{APP}/login", _order_contract(), ORDER, {}
    )
    assert verification.ok and statement.is_file()
    assert dict(scope.record) == {"customer": "Acme Ltd"}
    assert dict(scope.fields) == {"product": "Desk lamp", "units": "3"}
    # The adapter reads run 1's save before it is sent.
    body = b"customer=Acme+Ltd&product=Desk+lamp&units=3"
    assert scope.request_refusal("POST", f"{APP}/orders", body) is None
    other = b"customer=Birch+Co&product=Desk+lamp&units=3"
    assert scope.request_refusal("POST", f"{APP}/orders", other) == (
        "write_wrong_record"
    )
    assert scope.request_refusal("POST", f"{APP}/login", b"u=x") is None
    assert scope.request_refusal("POST", f"{APP}/account/close", b"") == (
        "write_unattributed"
    )


def test_no_statement_refuses_a_taught_write(tmp_path: Path) -> None:
    _refused(_bridge(tmp_path), "teach_auth_missing")


def test_a_statement_that_does_not_cover_the_write_refuses(tmp_path: Path) -> None:
    from tests._teach_authorization_support import order_authority

    cases = {
        "another record kind": dict(record_kind="invoice"),
        "no write adapter": dict(write_adapter=None),
        "another tool": dict(name="close_account"),
    }
    for i, (_why, kw) in enumerate(cases.items()):
        authority = order_authority(tmp_path / f"auth{i}", APP, ORDER, **kw)
        code = (
            "teach_auth_task_mismatch"
            if "name" in kw
            else "teach_auth_write_not_covered"
        )
        _refused(_bridge(tmp_path / f"b{i}", authority), code)
    # Fewer fields than the contract checks.
    narrow = order_authority(
        tmp_path / "narrow", APP, {"customer": "Acme Ltd"}, identity=("customer",)
    )
    _refused(_bridge(tmp_path / "bn", narrow), "teach_auth_input_not_declared")
    # Run 1's values aren't the signed ones.
    authority = order_authority(tmp_path / "values", APP, ORDER)
    _refused(
        _bridge(tmp_path / "bv", authority),
        "teach_auth_input_mismatch",
        customer="Birch Co",
    )
    # Another app.
    elsewhere = order_authority(tmp_path / "else", "http://127.0.0.1:2", ORDER)
    _refused(_bridge(tmp_path / "be", elsewhere), "teach_auth_write_not_covered")


def test_an_expired_statement_refuses(tmp_path: Path) -> None:
    from datetime import datetime, timedelta, timezone

    from openadapt_flow.teach_authorization_fixture import LocalAuthorizer
    from tests._teach_authorization_support import order_spec

    person = LocalAuthorizer(tmp_path / "approvers", "clinic-lead")
    statement = person.issue(
        tmp_path / "statement",
        order_spec(APP, ORDER),
        now=datetime.now(timezone.utc) - timedelta(days=3),
        valid_for=timedelta(days=1),
    )
    _refused(_bridge(tmp_path, person.authority(statement)), "teach_auth_expired")


def test_an_edited_statement_refuses(tmp_path: Path) -> None:
    from tests._teach_authorization_support import order_authority

    authority = order_authority(tmp_path / "auth", APP, ORDER)
    data = json.loads(authority.statement.read_text())
    data["writes"]["fields"]["units"]["value"] = "300"
    data["inputs"]["units"]["value"] = "300"
    authority.statement.write_text(json.dumps(data))
    _refused(_bridge(tmp_path, authority), "teach_auth_bad_signature", units="300")


def test_the_wrong_signer_refuses(tmp_path: Path) -> None:
    from openadapt_flow.teach_authorization_fixture import LocalAuthorizer
    from tests._teach_authorization_support import order_spec

    person = LocalAuthorizer(tmp_path / "approvers", "clinic-lead")
    # A key on no roster.
    stranger = LocalAuthorizer(person.key_dir, "night-desk", register=False)
    statement = stranger.issue(tmp_path / "s1", order_spec(APP, ORDER))
    _refused(
        _bridge(tmp_path / "b1", person.authority(statement)),
        "teach_auth_unknown_signer",
    )
    # A key that signs in the approver's name.
    forged = stranger.issue(
        tmp_path / "s2", order_spec(APP, ORDER), authorizer="clinic-lead"
    )
    _refused(
        _bridge(tmp_path / "b2", person.authority(forged)),
        "teach_auth_bad_signature",
    )


# -- running a taught write tool checks the authorization again --------------------


def _taught_tool(tmp_path: Path, person: Any = None, **issue: Any) -> tuple:
    """A sealed write tool on disk, as teach_finish leaves it."""

    from openadapt_flow.teach_authorization_fixture import LocalAuthorizer
    from tests._teach_authorization_support import admission_key, order_spec

    person = person or LocalAuthorizer(tmp_path / "approvers", "clinic-lead")
    admission_key(person.key_dir)
    statement = person.issue(tmp_path / "statement", order_spec(APP, ORDER), **issue)
    authority = person.authority(statement)
    bridge = _bridge(tmp_path, authority)
    from openadapt_flow import teach_authorization as ta

    verification = ta.verify(statement, key_dir=person.key_dir, root=person.root)
    folder = bridge.tools_dir / "add_order"
    folder.mkdir(parents=True)
    program = bridge._seal_program(
        {
            "schema": bt.PROGRAM_SCHEMA,
            "name": "add_order",
            "task": "Add an order",
            "url": f"{APP}/login",
            "origin": APP,
            "inputs": sorted(ORDER),
            "steps": [],
            "writes": [],
            "result": {"kind": "write", "contract": "order"},
        },
        (verification, statement),
        folder,
    )
    (folder / bt.PROGRAM_FILE).write_text(json.dumps(program))
    # A server started now lists the tool.
    return _bridge(tmp_path, authority), program, person


def test_a_taught_tool_runs_only_while_its_authorization_holds(tmp_path: Path) -> None:
    bridge, program, person = _taught_tool(tmp_path)
    assert bridge._write_refusal(program) is None
    # The server restarted without the authorization's trust.
    assert "needs a teach authorization" in _bridge(tmp_path)._write_refusal(program)
    # An edited program.
    assert "changed after it was taught" in bridge._write_refusal(
        {**program, "origin": "http://127.0.0.1:2"}
    )
    # The signer was removed from the roster.
    from openadapt_flow.teach_authorization_fixture import LocalAuthorizer

    root = LocalAuthorizer(tmp_path / "r2" / "approvers", "admin")
    lead = LocalAuthorizer(root.key_dir, "clinic-lead", added_by=root)
    bridge2, program2, _ = _taught_tool(tmp_path / "r2", person=lead)
    assert bridge2._write_refusal(program2) is None
    root.remove(lead)
    assert "teach_auth_unknown_signer" in bridge2._write_refusal(program2)


def test_a_taught_tool_stops_when_its_authorization_expires(tmp_path: Path) -> None:
    from datetime import datetime, timedelta, timezone

    bridge, program, _ = _taught_tool(
        tmp_path,
        now=datetime.now(timezone.utc) - timedelta(days=3),
        valid_for=timedelta(days=1),
    )
    assert "teach_auth_expired" in bridge._write_refusal(program)
    result = bridge.dispatch("taught_add_order", {"inputs": ORDER})
    assert result["outcome"] == "did_not_run"
    assert "nothing was written" in result["what_happened"]


def test_a_taught_tool_with_a_swapped_statement_does_not_run(tmp_path: Path) -> None:
    from openadapt_flow import teach_authorization as ta
    from tests._teach_authorization_support import order_spec

    bridge, program, person = _taught_tool(tmp_path)
    other = person.issue(tmp_path / "other", order_spec(APP, ORDER))
    kept = bridge.tools_dir / "add_order" / ta.KEPT_DIR
    for name in (ta.STATEMENT_FILE, ta.SIGNATURE_FILE):
        (kept / name).write_bytes(other.with_name(name).read_bytes())
    assert "teach_auth_digest_mismatch" in bridge._write_refusal(program)


def test_the_record_key_is_pinned_even_when_signed_as_a_field() -> None:
    """A statement may list the record's key under fields; it still binds it."""

    from openadapt_flow.agent.example_app import OrderWrites
    from openadapt_flow.teach_scope import TeachScope

    scope = TeachScope(
        task_id="taught.add_order",
        origins=(APP,),
        record={"product": "Desk lamp"},
        fields={"customer": "Acme Ltd", "units": "3"},
        write_adapter=OrderWrites(),
    )
    url = f"{APP}/orders"
    good = b"customer=Acme+Ltd&product=Desk+lamp&units=3"
    evil = b"customer=Evil+Corp&product=Desk+lamp&units=3"
    assert scope.request_refusal("POST", url, good) is None
    assert scope.request_refusal("POST", url, evil) == "write_wrong_record"


def test_a_statement_the_lane_cannot_hold_run_1_to_refuses(tmp_path: Path) -> None:
    from datetime import timedelta

    from openadapt_flow.teach_authorization_fixture import LocalAuthorizer
    from tests._teach_authorization_support import order_authority, order_spec

    person = LocalAuthorizer(tmp_path / "approvers", "clinic-lead")
    long = person.issue(
        tmp_path / "long", order_spec(APP, ORDER), valid_for=timedelta(days=200)
    )
    with pytest.raises(BridgeError, match="more than 90 days"):
        _teach(_bridge(tmp_path / "b1", person.authority(long)))
    rows = order_authority(tmp_path / "rows", APP, ORDER, expected_rows=2)
    with pytest.raises(BridgeError, match="allows 2 rows"):
        _teach(_bridge(tmp_path / "b2", rows))
    # A pin whose key directory would come from the environment.
    from openadapt_flow import teach_authorization as ta

    unpinned = ta.TeachAuthority(rows.statement, approver_root=person.root)
    _refused(_bridge(tmp_path / "b3", unpinned), "teach_auth_identity_unverified")


def test_coverage_maps_literal_matches_and_refuses_other_checks() -> None:
    from openadapt_flow.agent.example_app import OrderWrites
    from openadapt_flow.teach_scope import TeachScope
    from openadapt_flow.verify import WriteContract

    def contract(effect: dict) -> Any:
        return WriteContract.from_dict(
            {
                "schema_version": "openadapt.verify/v1",
                "writes": ["POST /orders"],
                "verifier": {
                    "kind": "rest",
                    "base_url": APP,
                    "records_path": "/api/orders",
                    "records_key": "records",
                },
                "effects": [effect],
            },
            name="order",
        )

    scope = TeachScope(
        task_id="taught.add_order",
        origins=(APP,),
        record={"customer": "Acme Ltd"},
        fields={"product": "Desk lamp"},
        write_adapter=OrderWrites(),
    )
    literal = contract(
        {
            "kind": "record_written",
            "match": {"customer": {"param": "customer"}, "product": "Desk lamp"},
            "expected_count": 1,
        }
    )
    assert bt._contract_coverage(literal, scope, {"customer": "Acme Ltd"}, {}) is None
    other = contract(
        {
            "kind": "record_written",
            "match": {"customer": {"param": "customer"}, "product": "Chair"},
            "expected_count": 1,
        }
    )
    assert "isn't the value" in bt._contract_coverage(
        other, scope, {"customer": "Acme Ltd"}, {}
    )
    narrower = contract(
        {
            "kind": "record_written",
            "match": {"customer": {"param": "customer"}},
            "expected_count": 1,
        }
    )
    assert "aren't the fields" in bt._contract_coverage(
        narrower, scope, {"customer": "Acme Ltd"}, {}
    )


def test_teaching_a_tool_again_replaces_its_statement(tmp_path: Path) -> None:
    from openadapt_flow import teach_authorization as ta
    from tests._teach_authorization_support import order_spec

    bridge, program, person = _taught_tool(tmp_path)
    newer = person.issue(tmp_path / "newer", order_spec(APP, ORDER))
    verification = ta.verify(newer, key_dir=person.key_dir, root=person.root)
    folder = bridge.tools_dir / "add_order"
    again = bridge._seal_program(
        {k: v for k, v in program.items() if k not in ("signature",)},
        (verification, newer),
        folder,
    )
    kept = folder / ta.KEPT_DIR / ta.STATEMENT_FILE
    assert kept.read_bytes() == newer.read_bytes()
    assert bridge._write_refusal(again) is None
    assert "teach_auth_digest_mismatch" in bridge._write_refusal(program)


def test_running_tools_needs_only_the_config_pin(tmp_path: Path) -> None:
    from openadapt_flow import teach_authorization as ta

    _, program, person = _taught_tool(tmp_path)
    pin = ta.TeachAuthority(
        Path("."), key_dir=person.key_dir, approver_root=person.root
    )
    bridge = bt.BrowserTeachBridge(
        tmp_path / "tools",
        tmp_path / "files",
        contracts={"order": _order_contract()},
        approvers=pin,
    )
    assert bridge._write_refusal(program) is None
    # It teaches nothing new without a statement.
    _refused(bridge, "teach_auth_missing", name="add_invoice")
