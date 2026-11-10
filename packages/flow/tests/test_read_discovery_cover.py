"""``--probe-reads``: look for a read that covers the whole save.

A probed collection read sees only the written collection, so a write to
another collection would pass it and the check is refused. Before refusing,
the generator probes the sibling collections the app itself read and the
prefix root, and promotes the root only when it holds the written record and
every record the collection and the siblings returned.

Synthetic local apps and made-up values only; no model calls.
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

import pytest
import requests
import yaml

from openadapt_flow import read_discovery
from openadapt_flow.record_check_synthesis import render, synthesize
from openadapt_flow.scaffold_verifier import READ_DISCOVERY_FILE, build_record_check

from ._record_check_fixtures import Capture

NOTE = "Synthetic memo: release after the second review"
PARAMS = {"account": "acct-1001", "memo": NOTE}
_FLAG = re.compile(r"^\s*needs_operator_confirmation:\s*true\s*$", re.M)


class StoreApp:
    """A local JSON app with two collections: ``transfers`` and ``fees``.

    ``POST /api/transfer`` saves a transfer; with ``stray`` it also writes a
    fee (a wrong save the screen can't see). ``root`` says what ``GET /api``
    returns: ``"split"`` (one list per collection), ``"merged"`` (one list of
    every record), ``"partial"`` (transfers only), ``"redirect"`` (a 302 to a
    route that holds everything) or ``None`` (404). ``fees_route`` serves
    ``GET /api/fees``. Every request is logged with its headers.
    """

    def __init__(self, *, root: Optional[str], fees_route: bool = True) -> None:
        self.transfers: list[dict[str, Any]] = [
            {"id": 1, "account": "acct-0001", "memo": "earlier memo"}
        ]
        self.fees: list[dict[str, Any]] = [
            {"id": 501, "account": "acct-0001", "fee": "2.50"}
        ]
        self.stray = False
        self.root = root
        self.fees_route = fees_route
        self.log: list[tuple[str, str, dict[str, str]]] = []
        app = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                return None

            def _send(self, status: int, body: Any, **headers: str) -> None:
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(raw)

            def _everything(self) -> list[dict[str, Any]]:
                return [dict(r, kind="transfer") for r in app.transfers] + [
                    dict(r, kind="fee") for r in app.fees
                ]

            def do_GET(self) -> None:  # noqa: N802
                path = urlsplit(self.path).path
                app.log.append(("GET", path, dict(self.headers.items())))
                if path == "/api/transfers":
                    self._send(200, {"records": app.transfers})
                elif path == "/api/fees" and app.fees_route:
                    self._send(200, {"records": app.fees})
                elif path in ("/api", "/api/") and app.root == "split":
                    self._send(200, {"transfers": app.transfers, "fees": app.fees})
                elif path in ("/api", "/api/") and app.root == "omit_empty":
                    # A collection with no rows is left out of the body.
                    body = {"transfers": app.transfers}
                    if app.fees:
                        body["fees"] = app.fees
                    self._send(200, body)
                elif path in ("/api", "/api/") and app.root == "nested":
                    self._send(
                        200, {"transfers": app.transfers, "billing": {"fees": app.fees}}
                    )
                elif path in ("/api", "/api/") and app.root == "idmap":
                    fees = {str(r["id"]): r for r in app.fees}
                    self._send(200, {"transfers": app.transfers, "fees": fees})
                elif path in ("/api", "/api/") and app.root == "merged":
                    self._send(200, {"records": self._everything()})
                elif path in ("/api", "/api/") and app.root == "partial":
                    self._send(200, {"records": app.transfers})
                elif path in ("/api", "/api/") and app.root == "redirect":
                    self._send(302, {}, Location="/hidden/everything")
                elif path == "/hidden/everything":
                    self._send(200, {"records": self._everything()})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                path = urlsplit(self.path).path
                app.log.append(("POST", path, dict(self.headers.items())))
                if path != "/api/transfer":
                    self._send(404, {"error": "not found"})
                    return
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                rec = {"id": max(r["id"] for r in app.transfers) + 1, **body}
                app.transfers.append(rec)
                if app.stray:
                    app.fees.append(
                        {
                            "id": max([r["id"] for r in app.fees], default=500) + 1,
                            "account": body.get("account"),
                            "fee": "9.99",
                        }
                    )
                self._send(200, {"ok": True, "id": rec["id"]})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def gets(self) -> list[str]:
        return [path for method, path, _ in self.log if method == "GET"]

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _recording(rec: Path, app: StoreApp, *, read_fees: bool = False) -> Path:
    """One save through ``app``. With ``read_fees`` the app also lists fees
    before the save, with a cookie and a token in the query string."""

    rec.mkdir(parents=True, exist_ok=True)
    cap = Capture()
    if read_fees:
        cap.get(app.url + "/api/fees?token=synthetic-token", 1.0)
        cap.lines[-1]["request_headers"] = {"Cookie": "sid=synthetic-session"}
    cap.post_json(app.url + "/api/transfer", dict(PARAMS), 1.9)
    for line in cap.lines:
        line["frame_url"] = app.url + "/form"
    events = [
        {"i": 0, "kind": "type", "text": PARAMS["memo"], "t": 1.2},
        {
            "i": 1,
            "kind": "click",
            "t": 2.0,
            "text": "Authorize Release",
            "structural": {"role": "button", "name": "Authorize Release"},
        },
    ]
    (rec / "events.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8"
    )
    (rec / "meta.json").write_text(json.dumps({"params": PARAMS}), encoding="utf-8")
    cap.write(rec)
    return rec


def _judge(proposal: Any, app: StoreApp, body: dict[str, str]) -> str:
    from openadapt_flow.rl_env.reward import EpisodeJudge
    from openadapt_flow.verify_run import parse_verify_config

    text = _FLAG.sub("", render(proposal))
    cfg = parse_verify_config(yaml.safe_load(text), source="generated")
    judge = EpisodeJudge(cfg, dict(body), episode_id="cover-test")
    try:
        requests.post(app.url + "/api/transfer", json=body, timeout=5)
        return judge.judge("success", feasible=True).verdict
    finally:
        judge.close()


@pytest.fixture
def make_app():
    apps: list[StoreApp] = []

    def make(**kwargs: Any) -> StoreApp:
        app = StoreApp(**kwargs)
        apps.append(app)
        return app

    yield make
    for app in apps:
        app.stop()


# ---------------------------------------------------------------------------
# Promotion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("root", ["split", "merged"])
def test_root_that_covers_the_save_is_promoted_and_catches_a_stray_row(
    tmp_path, make_app, root
):
    app = make_app(root=root)
    rec = _recording(tmp_path / "rec", app, read_fees=True)
    before = (list(app.transfers), list(app.fees))
    app.log.clear()
    proposal = synthesize(rec, probe_reads=True)

    assert not proposal.refused
    assert proposal.working, proposal.misses
    found = proposal.search.read_discovery
    assert found["status"] == "promoted"
    assert found["read"] == "/api"
    assert found["collection_read"] == "/api/transfers"
    assert found["covering_read"]["status"] == "promoted"
    assert found["needs_operator_confirmation"] is True
    assert proposal.search.closed_world_basis == "probed"
    paths = [p["path"] for p in found["probes"]]
    assert paths[:2] == ["/api/transfer", "/api/transfers"]
    assert "/api/fees" in paths and "/api" in paths
    # Read-only and recorded: only GETs reached the app, nothing changed.
    assert {method for method, _, _ in app.log} == {"GET"}
    assert (app.transfers, app.fees) == before
    assert len(app.gets()) == len(found["probes"])

    target = next(s for s in proposal.search.surfaces if s.is_target)
    assert target.store_wide and target.config["records_path"] == "/api"
    faults = {(r["fault"], r["result"]) for r in proposal.proof()["faults"]}
    assert ("collateral_unaudited", "MISSED") not in faults
    assert ("collateral_unaudited", "caught") in faults
    assert proposal.proof()["closed_world"] == "probed"

    good = {"account": "acct-3003", "memo": "Synthetic memo: covered run"}
    assert _judge(proposal, app, good) == "CONFIRMED"
    app.stray = True
    bad = {"account": "acct-4004", "memo": "Synthetic memo: stray fee run"}
    assert _judge(proposal, app, bad) == "REFUTED"


@pytest.mark.parametrize("root", ["split", "omit_empty"])
def test_a_stray_row_in_an_empty_or_absent_collection_is_refuted(
    tmp_path, make_app, root
):
    """Regression: an empty sibling list (``"fees": []``) or a collection the
    root left out while empty must still be read, so a stray fee written
    there after the probe is REFUTED, not CONFIRMED."""

    app = make_app(root=root)
    app.fees = []
    rec = _recording(tmp_path / "rec", app)
    proposal = synthesize(rec, probe_reads=True)
    assert proposal.working, proposal.misses
    found = proposal.search.read_discovery
    assert found["read"] == "/api"
    names = {s.config.get("records_key") for s in proposal.search.surfaces}
    if root == "split":
        assert "fees" in names
    other = [s for s in proposal.search.surfaces if s.config.get("records_other_lists")]
    assert len(other) == 1
    details = [
        r["detail"] for r in proposal.proof()["faults"] if r["result"] == "caught"
    ]
    assert any("did not show when probed" in d for d in details)
    if root == "split":
        assert any(d.endswith("_fees") for d in details)

    good = {"account": "acct-3003", "memo": "Synthetic memo: covered run"}
    assert _judge(proposal, app, good) == "CONFIRMED"
    app.stray = True
    bad = {"account": "acct-4004", "memo": "Synthetic memo: stray fee run"}
    assert _judge(proposal, app, bad) == "REFUTED"


def test_a_reviewed_list_that_disappears_reads_as_unreadable(make_app):
    from openadapt_flow.runtime.effects.rest import RestRecordVerifier

    app = make_app(root="omit_empty")
    reader = RestRecordVerifier(
        app.url,
        records_path="/api",
        records_key="transfers",
        records_complete=True,
        list_keys=("fees", "transfers"),
    )
    app.fees = []
    assert reader._fetch_records() is None
    assert "fees" in (reader.read_problem or "")
    app.fees = [{"id": 501, "account": "acct-0001", "fee": "2.50"}]
    assert reader._fetch_records() is not None


@pytest.mark.parametrize("seeded", [False, True])
@pytest.mark.parametrize("root", ["nested", "idmap"])
def test_a_root_holding_an_object_is_refused(tmp_path, make_app, root, seeded):
    """Regression: a collection nested in an object, or an id-keyed map, is a
    shape the check can't guard, so the root is never promoted (fail closed
    on shape) and no check exists that could CONFIRM a stray fee."""

    app = make_app(root=root)
    if not seeded:
        app.fees = []
    rec = _recording(tmp_path / "rec", app)
    proposal = synthesize(rec, probe_reads=True)
    assert proposal.refused and not proposal.working
    found = proposal.search.read_discovery
    assert found["read"] == "/api/transfers"
    assert found["covering_read"]["status"] == "not found"
    key = "billing" if root == "nested" else "fees"
    assert (
        f"root holds a non-list value that may contain records: {key}"
        in (found["reason"])
    )
    assert not any(s.store_wide for s in proposal.search.surfaces)
    assert not any(
        r["fault"] == "collateral_unaudited" and r["result"] == "caught"
        for r in proposal.proof()["faults"]
    )
    out = tmp_path / "out"
    assert build_record_check(rec, out, probe_reads=True) is None
    assert not (out / "record_check.proposed.yaml").exists()


@pytest.mark.parametrize("later", ["nested", "idmap"])
def test_an_object_that_appears_after_promotion_is_never_confirmed(
    tmp_path, make_app, later
):
    app = make_app(root="split")
    rec = _recording(tmp_path / "rec", app)
    proposal = synthesize(rec, probe_reads=True)
    assert proposal.working, proposal.misses
    app.root = later
    app.stray = True
    bad = {"account": "acct-4004", "memo": "Synthetic memo: stray fee run"}
    assert _judge(proposal, app, bad) != "CONFIRMED"


def test_a_list_of_non_records_in_the_root_is_refused(tmp_path, make_app):
    app = make_app(root="split")
    app.fees = ["not a record"]  # type: ignore[list-item]
    rec = _recording(tmp_path / "rec", app)
    proposal = synthesize(rec, probe_reads=True)
    assert proposal.refused
    assert (
        "holds a list of non-records under fees"
        in (proposal.search.read_discovery["reason"])
    )


def test_promoted_covering_read_is_written_to_read_discovery_json(tmp_path, make_app):
    app = make_app(root="split")
    rec = _recording(tmp_path / "rec", app, read_fees=True)
    out = tmp_path / "out"
    result = build_record_check(rec, out, probe_reads=True)
    assert result is not None and result.working
    saved = json.loads((out / READ_DISCOVERY_FILE).read_text())
    assert saved["status"] == "promoted" and saved["read"] == "/api"
    proof = json.loads((out / "record_check.proof.json").read_text())
    assert proof["read_discovery"]["covering_read"]["status"] == "promoted"


# ---------------------------------------------------------------------------
# Refusal
# ---------------------------------------------------------------------------


def test_no_root_read_keeps_the_refusal_with_the_reason(tmp_path, make_app):
    app = make_app(root=None)
    rec = _recording(tmp_path / "rec", app)
    proposal = synthesize(rec, probe_reads=True)
    assert proposal.refused and not proposal.working
    found = proposal.search.read_discovery
    assert found["status"] == "refused"
    assert found["covering_read"]["status"] == "not found"
    assert "collateral_unaudited" in found["reason"]
    assert "No read covering the whole save was found" in found["reason"]
    assert "GET /api: no read here (status 404)" in found["reason"]
    assert "the app read no other collection under /api" in found["reason"]
    roots = [p for p in found["probes"] if p.get("kind") == "root"]
    assert [p["path"] for p in roots] == ["/api", "/api/"]


def test_a_sibling_alone_covers_only_part_of_the_save_and_refuses(tmp_path, make_app):
    """The app read ``/api/fees`` too, but nothing shows the whole store: the
    two collections together still leave every other table unread."""

    app = make_app(root=None)
    rec = _recording(tmp_path / "rec", app, read_fees=True)
    proposal = synthesize(rec, probe_reads=True)
    assert proposal.refused
    found = proposal.search.read_discovery
    assert any(
        p["path"] == "/api/fees" and p["kind"] == "sibling" for p in found["probes"]
    )
    assert found["covering_read"]["status"] == "not found"
    assert "collateral_unaudited" in found["reason"]


def test_a_root_without_a_siblings_records_is_not_the_whole_store(tmp_path, make_app):
    """``GET /api`` holds the written record but not the fees the app read:
    it covers only part of the save, so the refusal stands."""

    app = make_app(root="partial")
    rec = _recording(tmp_path / "rec", app, read_fees=True)
    proposal = synthesize(rec, probe_reads=True)
    assert proposal.refused
    found = proposal.search.read_discovery
    assert "does not hold every record of /api/fees" in found["reason"]
    assert not any(s.store_wide for s in proposal.search.surfaces)


def test_an_unreadable_sibling_refuses(tmp_path, make_app):
    """The app read ``/api/fees`` while recording, but the probe can't: the
    root can't be shown to hold its records, so nothing is promoted."""

    app = make_app(root="split")
    rec = _recording(tmp_path / "rec", app, read_fees=True)
    app.fees_route = False
    proposal = synthesize(rec, probe_reads=True)
    assert proposal.refused
    assert "could not read it" in proposal.search.read_discovery["reason"]


def test_a_write_outside_the_prefix_refuses(tmp_path, make_app):
    app = make_app(root="split")
    rec = _recording(tmp_path / "rec", app)
    lines = (rec / "network.jsonl").read_text().splitlines()
    extra = json.loads(lines[-1])
    extra["url"] = app.url + "/audit/entry"
    extra["t"] = 1.95
    (rec / "network.jsonl").write_text("\n".join(lines + [json.dumps(extra)]) + "\n")
    proposal = synthesize(rec, probe_reads=True)
    assert proposal.refused
    assert "went outside /api" in proposal.search.read_discovery["reason"]


def test_declared_complete_read_skips_the_covering_search(tmp_path, make_app):
    app = make_app(root="split")
    rec = _recording(tmp_path / "rec", app)
    proposal = synthesize(rec, probe_reads=True, complete_read=True)
    found = proposal.search.read_discovery
    assert "covering_read" not in found
    assert found["read"] == "/api/transfers"


# ---------------------------------------------------------------------------
# Probe rules: no credentials, no redirects, GET on this machine only
# ---------------------------------------------------------------------------


def test_probes_send_no_credentials_and_drop_the_query(tmp_path, make_app):
    app = make_app(root="split")
    rec = _recording(tmp_path / "rec", app, read_fees=True)
    app.log.clear()
    synthesize(rec, probe_reads=True)
    assert app.log
    for method, path, headers in app.log:
        assert method == "GET"
        names = {k.lower() for k in headers}
        assert not names & {"cookie", "authorization", "proxy-authorization"}
        assert "token" not in path
    found = synthesize(rec, probe_reads=True).search.read_discovery
    assert "synthetic-token" not in json.dumps(found)
    assert "synthetic-session" not in json.dumps(found)


def test_http_get_strips_credentials_from_the_url(make_app):
    app = make_app(root="split")
    host = urlsplit(app.url)
    status, text, _ = read_discovery.http_get(
        f"http://user:secret@{host.hostname}:{host.port}/api/transfers"
    )
    assert status == 200 and text
    [(_, _, headers)] = app.log
    assert not {k.lower() for k in headers} & {"authorization", "cookie"}


def test_redirects_are_not_followed(tmp_path, make_app):
    app = make_app(root="redirect")
    rec = _recording(tmp_path / "rec", app, read_fees=True)
    app.log.clear()
    proposal = synthesize(rec, probe_reads=True)
    assert proposal.refused
    assert "/hidden/everything" not in app.gets()
    roots = [p for p in proposal.search.read_discovery["probes"] if p["kind"] == "root"]
    assert roots and all(p["status"] == 302 for p in roots)
    assert all(p["outcome"] == "no read here" for p in roots)


def test_cover_never_probes_a_remote_base(make_app):
    from openadapt_flow import record_check_synthesis as rcs

    sent: list[str] = []
    search = rcs.ReadSearch()
    search.read_discovery = {"probes": []}
    search.write_requests = [
        {"method": "POST", "url": "https://app.example/api/transfer"}
    ]
    promoted = rcs.Surface(
        name="api_transfers",
        config={"base_url": "https://app.example", "records_path": "/api/transfers"},
        after=[],
        before=[],
        before_recorded=False,
        source="probe",
    )
    assert (
        read_discovery.cover(
            PARAMS, search, promoted, captured=[], get=lambda url: sent.append(url)
        )
        is None
    )
    assert sent == []
    assert "not the local machine" in search.read_discovery["covering_read"]["reason"]


def test_sibling_paths_come_from_the_apps_own_gets():
    origin = "http://127.0.0.1:9"
    captured = [
        {"method": "GET", "url": origin + "/api/fees?token=x"},
        {"method": "GET", "url": origin + "/api/transfers"},
        {"method": "GET", "url": origin + "/api/fees/12345"},
        {"method": "GET", "url": "http://127.0.0.1:10/api/ledger"},
        {"method": "POST", "url": origin + "/api/notes"},
        {"method": "GET", "url": origin + "/static/app.js"},
        {"method": "GET", "url": origin + "/api/accounts", "status": 200},
        {"method": "GET", "url": origin + "/api/missing", "status": 404},
    ]
    for line in captured:
        line.setdefault("status", 200)
    assert read_discovery.sibling_paths(
        captured, origin=origin, prefix="/api", written="transfers"
    ) == ["/api/fees", "/api/accounts"]
