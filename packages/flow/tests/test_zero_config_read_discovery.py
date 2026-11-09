"""Zero-config record checks: completeness, write detection, read discovery.

Three generator gaps the zero-config benchmark found
(``benchmark/zero_config_verify``):

1. a generated REST read never carried ``records_complete``, so every run of
   the generated check was INDETERMINATE;
2. a Save control with an unfamiliar label was not seen as a write, even when
   the capture showed it POST;
3. when the app never read the saved record back there was no read at all.

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
from openadapt_flow.scaffold_verifier import (
    READ_DISCOVERY_FILE,
    ScaffoldRefused,
    build_record_check,
    candidates_from_recording,
)

from ._record_check_fixtures import Capture, mockmed_recording

NOTE = "Synthetic memo: release after the second review"
PARAMS = {"account": "acct-1001", "memo": NOTE}
_FLAG = re.compile(r"^\s*needs_operator_confirmation:\s*true\s*$", re.M)


# ---------------------------------------------------------------------------
# A small local app: POST /api/<singular>, optional reads
# ---------------------------------------------------------------------------


class App:
    """A local JSON app that saves one kind of record.

    ``list_path`` (a collection read), ``item_prefix`` (``<prefix>/<id>``) and
    ``list_fields`` (which fields the list shows) say which reads exist.
    Every request is logged, so a test can check what a probe sent.
    """

    def __init__(
        self,
        *,
        write_path: str = "/api/transfer",
        list_path: Optional[str] = "/api/transfers",
        item_prefix: Optional[str] = None,
        list_fields: Optional[tuple[str, ...]] = None,
        extra_body: Optional[dict[str, Any]] = None,
    ) -> None:
        self.records: list[dict[str, Any]] = [
            {"id": 1, "account": "acct-0001", "memo": "earlier memo"}
        ]
        self.log: list[tuple[str, str]] = []
        app = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                return None

            def _send(self, status: int, body: Any) -> None:
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:  # noqa: N802
                path = urlsplit(self.path).path
                app.log.append(("GET", path))
                if list_path and path == list_path:
                    rows = [
                        {
                            k: v
                            for k, v in r.items()
                            if not list_fields or k in list_fields
                        }
                        for r in app.records
                    ]
                    self._send(200, {"records": rows, **(extra_body or {})})
                    return
                if item_prefix and path.startswith(item_prefix + "/"):
                    rid = path.rsplit("/", 1)[1]
                    for r in app.records:
                        if str(r["id"]) == rid:
                            self._send(200, r)
                            return
                self._send(404, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                path = urlsplit(self.path).path
                app.log.append(("POST", path))
                if path != write_path:
                    self._send(404, {"error": "not found"})
                    return
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                rec = {"id": max(r["id"] for r in app.records) + 1, **body}
                app.records.append(rec)
                self._send(200, {"ok": True, "id": rec["id"]})

            def do_PUT(self) -> None:  # noqa: N802
                app.log.append(("PUT", urlsplit(self.path).path))
                self._send(405, {})

            do_DELETE = do_PUT  # noqa: N815

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.write_path = write_path
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _recording(
    rec: Path,
    app: App,
    *,
    label: str = "Authorize Release",
    read_back: Optional[str] = None,
    frame_origin: Optional[str] = None,
    write_url: Optional[str] = None,
) -> Path:
    """One save through ``app`` with no read-back unless ``read_back``."""

    rec.mkdir(parents=True, exist_ok=True)
    cap = Capture()
    if read_back:
        cap.get(app.url + read_back, 1.0)
    cap.post_json(app.url + app.write_path, dict(PARAMS), 1.9)
    if write_url:
        # Recorded as sent elsewhere; nothing is sent there.
        cap.lines[-1]["url"] = write_url
    if read_back:
        cap.get(app.url + read_back, 2.3)
    for line in cap.lines:
        line["frame_url"] = (frame_origin or app.url) + "/form"
    events = [
        {"i": 0, "kind": "type", "text": PARAMS["memo"], "t": 1.2},
        {
            "i": 1,
            "kind": "click",
            "t": 2.0,
            "text": label,
            "structural": {"role": "button", "name": label},
        },
    ]
    (rec / "events.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8"
    )
    (rec / "meta.json").write_text(json.dumps({"params": PARAMS}), encoding="utf-8")
    cap.write(rec)
    return rec


@pytest.fixture
def app():
    handle = App()
    yield handle
    handle.stop()


def _confirmed_config(proposal: Any) -> Any:
    from openadapt_flow.verify_run import parse_verify_config

    text = _FLAG.sub("", render(proposal))
    return parse_verify_config(yaml.safe_load(text), source="generated")


def _judge_one_save(cfg: Any, base: str, path: str, body: dict[str, str]) -> str:
    from openadapt_flow.rl_env.reward import EpisodeJudge

    judge = EpisodeJudge(cfg, dict(body), episode_id="zero-config-test")
    try:
        requests.post(base + path, json=body, timeout=5)
        return judge.judge("success", feasible=True).verdict
    finally:
        judge.close()


# ---------------------------------------------------------------------------
# 1. records_complete
# ---------------------------------------------------------------------------


def test_observed_unpaged_read_gets_records_complete_and_confirms(tmp_path, app):
    rec = _recording(tmp_path / "rec", app, label="Save", read_back="/api/transfers")
    proposal = synthesize(rec)

    [surface] = proposal.search.surfaces
    assert surface.complete_basis == "observed"
    section = proposal.verifier["surfaces"]
    assert section[surface.name]["records_complete"] is True
    assert section[surface.content_name]["records_complete"] is True
    assert not any("completeness" in p for p in proposal.search.problems)
    [read] = [r for r in proposal.proof()["reads"] if r["surface"] == surface.name]
    assert read["records_complete"] == "observed"
    assert "unpaged" in read["completeness"]

    cfg = _confirmed_config(proposal)
    body = {"account": "acct-2002", "memo": "Synthetic memo: a different run"}
    assert _judge_one_save(cfg, app.url, app.write_path, body) == "CONFIRMED"


@pytest.mark.parametrize(
    "extra",
    [{"next": "/api/transfers?page=2"}, {"has_more": True}, {"total": 40}],
    ids=["next-link", "has-more", "total-differs"],
)
def test_paged_read_stays_incomplete_even_when_declared(tmp_path, extra):
    handle = App(extra_body=extra)
    try:
        rec = _recording(
            tmp_path / "rec", handle, label="Save", read_back="/api/transfers"
        )
        for complete_read in (False, True):
            proposal = synthesize(rec, complete_read=complete_read)
            [surface] = proposal.search.surfaces
            assert surface.complete_basis is None
            assert "records_complete" not in proposal.verifier["surfaces"][surface.name]
            assert any(
                "REST read completeness is not declared" in p
                for p in proposal.search.problems
            )
            assert not proposal.working
    finally:
        handle.stop()


def test_read_that_lost_a_record_is_not_observed_complete(tmp_path, app):
    rec = _recording(tmp_path / "rec", app, label="Save", read_back="/api/transfers")
    lines = (rec / "network.jsonl").read_text().splitlines()
    first = json.loads(lines[1])
    body = json.loads(first["response_text"])
    body["records"].append({"id": 99, "account": "acct-0099", "memo": "gone later"})
    first["response_text"] = json.dumps(body)
    lines[1] = json.dumps(first)
    (rec / "network.jsonl").write_text("\n".join(lines) + "\n")

    proposal = synthesize(rec)
    [surface] = proposal.search.surfaces
    assert surface.complete_basis is None
    assert any("missing after it" in why for why in surface.incomplete)

    declared = synthesize(rec, complete_read=True)
    assert declared.search.surfaces[0].complete_basis == "declared"
    assert declared.verifier["surfaces"][surface.name]["records_complete"] is True


def test_declared_complete_read_now_emits_records_complete(tmp_path):
    """MockMed-style store read: --complete-read reaches the generated read."""

    from openadapt_flow.mockmed.fault_server import serve

    url, db, stop = serve()
    try:
        rec = mockmed_recording(tmp_path / "rec", url, db)
        proposal = synthesize(rec, complete_read=True)
        for name, cfg in proposal.verifier["surfaces"].items():
            assert cfg["records_complete"] is True, name
        assert not any("completeness" in p for p in proposal.search.problems)
    finally:
        stop()


# ---------------------------------------------------------------------------
# 2. Write detection from the wire and the store
# ---------------------------------------------------------------------------


def test_unfamiliar_label_with_a_post_is_a_write(tmp_path, app):
    rec = _recording(tmp_path / "rec", app)
    [candidate] = candidates_from_recording(rec)
    assert candidate.step_id == "step_001"
    assert candidate.basis.startswith("observed mutating request to the app (POST")
    assert candidate.payload == {"account": "acct-1001", "memo": NOTE}


def test_unfamiliar_label_without_traffic_is_still_refused(tmp_path, app):
    rec = _recording(tmp_path / "rec", app)
    (rec / "network.jsonl").unlink()
    assert candidates_from_recording(rec) == []
    with pytest.raises(ScaffoldRefused, match="REFUSED"):
        build_record_check(rec)


def test_a_post_to_another_origin_is_not_the_apps_write(tmp_path, app):
    rec = _recording(
        tmp_path / "rec",
        app,
        write_url="http://telemetry.invalid/collect",
    )
    assert candidates_from_recording(rec) == []


def test_a_failed_request_is_not_a_write(tmp_path, app):
    rec = _recording(tmp_path / "rec", app)
    lines = (rec / "network.jsonl").read_text().splitlines()
    post = json.loads(lines[1])
    post.update(status=None, failed=True)
    lines[1] = json.dumps(post)
    (rec / "network.jsonl").write_text("\n".join(lines) + "\n")
    assert candidates_from_recording(rec) == []


def test_a_store_delta_that_changes_a_record_is_a_write(tmp_path):
    rec = tmp_path / "rec"
    rec.mkdir()
    before = [{"id": 1, "status": "open"}]
    after = [{"id": 1, "status": "closed"}]
    event = {
        "i": 0,
        "kind": "click",
        "t": 1.0,
        "text": "Done",
        "sor_before": before,
        "sor_after": after,
    }
    (rec / "events.jsonl").write_text(json.dumps(event) + "\n")
    (rec / "meta.json").write_text(json.dumps({"params": {}}))
    [candidate] = candidates_from_recording(rec)
    assert candidate.basis.startswith("observed system-of-record delta")


def test_label_matching_still_counts(tmp_path, app):
    rec = _recording(tmp_path / "rec", app, label="Save")
    (rec / "network.jsonl").unlink()
    [candidate] = candidates_from_recording(rec)
    assert candidate.basis == "write-shaped control label"


def test_unfamiliar_label_reaches_a_working_read(tmp_path, app):
    """Gap 2 used to stop generation before any read was looked at."""

    rec = _recording(tmp_path / "rec", app, read_back="/api/transfers")
    proposal = synthesize(rec)
    assert [s.config["records_path"] for s in proposal.search.surfaces] == [
        "/api/transfers"
    ]
    assert proposal.search.write_requests


# ---------------------------------------------------------------------------
# 3. Read-path discovery
# ---------------------------------------------------------------------------


def test_probe_finds_and_confirms_the_collection_read(tmp_path, app):
    rec = _recording(tmp_path / "rec", app)
    assert synthesize(rec).search.surfaces == []  # nothing to read back

    before = [dict(r) for r in app.records]
    app.log.clear()
    # A person declares the read is the whole store, so the probed read may
    # back a runnable check (without it, see the refusal test below).
    proposal = synthesize(rec, probe_reads=True, complete_read=True)

    [surface] = proposal.search.surfaces
    assert surface.config["records_path"] == "/api/transfers"
    assert surface.source.startswith("probe GET /api/transfers")
    assert surface.complete_basis == "declared"
    assert proposal.verifier["surfaces"][surface.name]["records_complete"] is True
    found = proposal.proof()["read_discovery"]
    assert found["status"] == "promoted"
    assert found["needs_operator_confirmation"] is True
    assert {p["method"] for p in found["probes"]} == {"GET"}
    # Read-only: only GETs reached the app, and the store did not change.
    assert {method for method, _ in app.log} == {"GET"}
    assert app.records == before
    assert all(e.needs_operator_confirmation for e in proposal.effects)

    cfg = _confirmed_config(proposal)
    body = {"account": "acct-3003", "memo": "Synthetic memo: probed read run"}
    assert _judge_one_save(cfg, app.url, app.write_path, body) == "CONFIRMED"


def test_probed_read_that_cannot_see_the_whole_save_is_refused(tmp_path, app):
    """A probe finds the written collection, not the store: a write
    elsewhere would pass, so no runnable check is written."""

    rec = _recording(tmp_path / "rec", app)
    proposal = synthesize(rec, probe_reads=True)
    assert proposal.refused and not proposal.working
    found = proposal.search.read_discovery
    assert found["status"] == "refused"
    assert found["needs_operator_confirmation"] is True
    assert "collateral_unaudited" in found["reason"]
    assert proposal.proof()["status"] == "refused"

    out = tmp_path / "out"
    assert build_record_check(rec, out, probe_reads=True) is None
    assert not (out / "record_check.proposed.yaml").exists()
    saved = json.loads((out / READ_DISCOVERY_FILE).read_text())
    assert saved["status"] == "refused" and saved["needs_operator_confirmation"]
    proof = json.loads((out / "record_check.proof.json").read_text())
    assert proof["status"] == "refused"
    assert proof["read_discovery"]["reason"] == saved["reason"]


def test_cli_writes_the_todo_draft_when_the_probed_read_is_refused(
    tmp_path, app, capsys
):
    from openadapt_flow.__main__ import main

    rec = _recording(tmp_path / "rec", app)
    out = tmp_path / "out"
    code = main(["scaffold-verifier", str(rec), "--probe-reads", "-o", str(out)])
    text = capsys.readouterr().out
    assert code == 0, text
    assert "Read discovery: REFUSED" in text
    assert (out / "effect_contract.yaml").is_file()
    assert not (out / "record_check.proposed.yaml").exists()


def test_no_read_route_leaves_the_read_unavailable(tmp_path):
    handle = App(list_path=None)
    try:
        rec = _recording(tmp_path / "rec", handle)
        proposal = synthesize(rec, probe_reads=True)
        assert proposal.search.surfaces == []
        assert proposal.effects == []
        found = proposal.search.read_discovery
        assert found["status"] == "unavailable"
        assert found["needs_operator_confirmation"] is True
        assert found["probes"] and all(
            p["outcome"] == "no read here" for p in found["probes"]
        )
        assert {m for m, _ in handle.log if m != "POST"} == {"GET"}

        out = tmp_path / "out"
        assert build_record_check(rec, out, probe_reads=True) is None
        saved = json.loads((out / READ_DISCOVERY_FILE).read_text())
        assert saved["status"] == "unavailable"
    finally:
        handle.stop()


def test_a_read_without_the_written_values_is_not_promoted(tmp_path):
    handle = App(list_fields=("id", "account"))
    try:
        rec = _recording(tmp_path / "rec", handle)
        proposal = synthesize(rec, probe_reads=True)
        assert proposal.search.surfaces == []
        assert proposal.search.read_discovery["status"] == "unavailable"
    finally:
        handle.stop()


def test_an_item_read_alone_is_recorded_but_not_promoted(tmp_path):
    handle = App(list_path=None, item_prefix="/api/transfer")
    try:
        rec = _recording(tmp_path / "rec", handle)
        proposal = synthesize(rec, probe_reads=True)
        assert proposal.search.surfaces == []
        found = proposal.search.read_discovery
        assert found["status"] == "unavailable"
        assert "an item read did" in found["reason"]
        assert any(
            p.get("kind") == "item" and p["outcome"].startswith("confirms")
            for p in found["probes"]
        )
    finally:
        handle.stop()


def test_a_remote_write_is_never_probed(tmp_path, app, monkeypatch):
    rec = _recording(
        tmp_path / "rec",
        app,
        frame_origin="https://app.example",
        write_url="https://app.example/api/transfer",
    )
    sent: list[str] = []
    monkeypatch.setattr(read_discovery, "http_get", lambda url: sent.append(url))
    proposal = synthesize(rec, probe_reads=True)
    assert sent == []
    assert proposal.search.read_discovery["status"] == "unavailable"
    assert "not the local" in proposal.search.read_discovery["reason"]

    remote_base = synthesize(
        rec, probe_reads=True, probe_base="https://sandbox.example"
    )
    assert remote_base.search.read_discovery["status"] == "unavailable"
    assert sent == []


def test_probe_base_points_a_remote_recording_at_a_local_copy(tmp_path, app):
    rec = _recording(
        tmp_path / "rec",
        app,
        frame_origin="https://app.example",
        write_url="https://app.example/api/transfer",
    )
    requests.post(app.url + app.write_path, json=dict(PARAMS), timeout=5)
    proposal = synthesize(rec, probe_reads=True, probe_base=app.url, complete_read=True)
    [surface] = proposal.search.surfaces
    assert surface.config["base_url"] == app.url
    assert surface.config["records_path"] == "/api/transfers"


def test_http_get_refuses_a_remote_host():
    with pytest.raises(read_discovery.ProbeRefused):
        read_discovery.http_get("https://example.com/api/things")


@pytest.mark.parametrize(
    ("url", "paths"),
    [
        ("http://127.0.0.1:1/api/order", ["/api/order", "/api/orders"]),
        ("http://127.0.0.1:1/api/entries", ["/api/entries", "/api/entry"]),
        ("http://127.0.0.1:1/api/order/4711", ["/api/order", "/api/orders"]),
    ],
)
def test_candidate_reads_follow_the_written_path(url, paths):
    line = {"method": "POST", "url": url, "response_text": json.dumps({"id": 7})}
    cands = read_discovery.candidates_for(line, {})
    assert [c.path for c in cands if c.kind == "collection"] == paths
    items = [c.path for c in cands if c.kind == "item"]
    assert items and all(p.rsplit("/", 1)[1] in {"7", "4711"} for p in items)


def test_cli_flags(tmp_path, app, capsys):
    from openadapt_flow.__main__ import main

    rec = _recording(tmp_path / "rec", app)
    code = main(
        [
            "scaffold-verifier",
            str(rec),
            "--probe-reads",
            "--complete-read",
            "-o",
            str(tmp_path / "out"),
        ]
    )
    out = capsys.readouterr().out
    assert "Record check:" in out, out
    proof = json.loads((tmp_path / "out" / "record_check.proof.json").read_text())
    assert proof["read_discovery"]["status"] == "promoted"
    assert code in (0, 1)
