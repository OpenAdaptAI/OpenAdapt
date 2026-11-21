"""``openadapt-flow ledger``: add, list, show, summary, export, push."""

from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path

import pytest

from openadapt_flow.run_ledger.cli import LedgerRefused, main, outbound_lines, push
from openadapt_flow.run_ledger.store import LedgerStore

FIXTURES = Path(__file__).parent / "fixtures" / "run_ledger"

#: Every value string the fixtures carry. None may leave the machine.
FIXTURE_VALUES = (
    "Synthetic follow-up in two weeks",
    "Synthetic follow-up in three weeks",
    "SYN-CODE-77",
    "Synthetic intake note",
    "SYN-PATIENT-0042",
    "Synthetic final text",
    "Synthetic summary",
    "Synthetic question text",
)


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "home"
    monkeypatch.setenv("OPENADAPT_HOME", str(path))
    monkeypatch.delenv("OPENADAPT_API_KEY", raising=False)
    return path


def _add_all(capsys: pytest.CaptureFixture[str]) -> None:
    for fixture in sorted(p for p in FIXTURES.iterdir() if p.is_dir()):
        assert main(["ledger", "add", str(fixture), "--task-type", "intake"]) == 0
    capsys.readouterr()


def test_add_dedupes_and_writes_private_files(capsys, home):
    run = FIXTURES / "shadow_mismatch"
    assert main(["ledger", "add", str(run)]) == 0
    first = capsys.readouterr().out
    assert "CAUGHT_ERROR (shadow_field_mismatch)" in first and "added" in first
    assert main(["ledger", "add", str(run)]) == 0
    assert "already in the ledger" in capsys.readouterr().out
    store = LedgerStore()
    assert len(store.rows()) == 1
    rows_mode = stat.S_IMODE(os.stat(store.rows_path).st_mode)
    row = store.rows()[0]
    side_mode = stat.S_IMODE(os.stat(store.sidecar_path(row.row_id)).st_mode)
    assert rows_mode == 0o600 and side_mode == 0o600
    assert store.root == home / "ledger"
    # The run directory is never written to.
    assert sorted(p.name for p in run.iterdir()) == ["proposed_write.json"]


def test_a_changed_run_gets_a_new_row_and_summary_keeps_the_newest(tmp_path, capsys):
    run = tmp_path / "run"
    shutil.copytree(FIXTURES / "exception_open", run)
    assert main(["ledger", "add", str(run)]) == 0
    answer = run / "exceptions" / "answer-card-0002.json"
    doc = json.loads(answer.read_text())
    doc["decided_at"] = "2026-11-20T13:12:00Z"
    answer.write_text(json.dumps(doc))
    assert main(["ledger", "add", str(run)]) == 0
    store = LedgerStore()
    assert len(store.rows()) == 2
    (latest,) = store.latest()
    assert latest.human.intervention_s == 120.0


def test_bad_task_type_is_refused(capsys):
    assert (
        main(
            ["ledger", "add", str(FIXTURES / "flight_held"), "--task-type", "Jane Doe"]
        )
        == 2
    )
    assert "task_type" in capsys.readouterr().err
    assert LedgerStore().rows() == []


def test_list_show_and_values_only_on_request(capsys):
    assert main(["ledger", "add", str(FIXTURES / "shadow_mismatch")]) == 0
    capsys.readouterr()
    assert main(["ledger", "list"]) == 0
    listed = capsys.readouterr().out
    row_id = listed.split()[0]
    assert "shadow_field_mismatch" in listed
    assert main(["ledger", "show", row_id]) == 0
    plain = capsys.readouterr().out
    assert "Synthetic follow-up" not in plain
    assert main(["ledger", "show", row_id, "--show-values"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["values"]["note"]["saved"] == "Synthetic follow-up in three weeks"
    assert main(["ledger", "show", "../../etc/passwd"]) == 2


def test_summary_counts_and_unknowns(capsys):
    _add_all(capsys)
    assert main(["ledger", "summary", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    counts = report["outcomes"]
    assert (
        sum(counts.values())
        == report["rows"]
        == len([p for p in FIXTURES.iterdir() if p.is_dir()])
    )
    assert counts["VERIFIED"] >= 3 and counts["CAUGHT_ERROR"] >= 4
    assert counts["UNKNOWN"] >= 4 and counts["ESCALATED"] >= 5
    assert counts["FAILED"] >= 1
    # The open exception card is unknown time, not zero.
    assert report["intervention_unknown_rows"] >= 1
    assert report["model_cost_unknown_rows"] >= 1
    assert report["median_duration_s"] is not None
    assert main(["ledger", "summary", "--since", "2026-11-20T12:00:00Z", "--json"]) == 0
    later = json.loads(capsys.readouterr().out)
    assert later["rows"] < report["rows"]
    assert main(["ledger", "summary", "--task-type", "nothing", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["rows"] == 0
    assert main(["ledger", "summary"]) == 0
    assert "Model cost" in capsys.readouterr().out


def test_export_contains_no_value_strings(tmp_path, capsys):
    _add_all(capsys)
    out = tmp_path / "export.jsonl"
    assert main(["ledger", "export", "--out", str(out)]) == 0
    text = out.read_text()
    assert len(text.splitlines()) == len(LedgerStore().latest())
    for value in FIXTURE_VALUES:
        assert value not in text, value
    for line in text.splitlines():
        assert json.loads(line)["schema"] == "openadapt.run-ledger/v1"


def test_export_refuses_a_value_that_reached_a_label(tmp_path, capsys):
    # A task id that is also a saved value: the sidecar scan catches it.
    run = tmp_path / "run"
    run.mkdir()
    (run / "proposed_write.json").write_text(
        json.dumps(
            {
                "schema": "openadapt.proposed-write/v1",
                "task_id": "SYN-0042-ALPHA",
                "fields": [{"name": "ref", "proposed": "SYN-0042-ALPHA", "saved": "x"}],
            }
        )
    )
    assert main(["ledger", "add", str(run)]) == 0
    out = tmp_path / "export.jsonl"
    assert main(["ledger", "export", "--out", str(out)]) == 2
    assert "private sidecar" in capsys.readouterr().err
    assert not out.exists()


def test_export_second_guard_uses_the_scrubber(capsys):
    assert main(["ledger", "add", str(FIXTURES / "flight_held")]) == 0

    class Scrubber:
        def scrub_text(self, text: str) -> str:
            return "<PERSON>" if text == "browser-use" else text

    with pytest.raises(LedgerRefused, match="executor_label"):
        outbound_lines(LedgerStore(), summary=False, scrubber=Scrubber())


def test_export_require_scrubber_refuses_when_absent(tmp_path, monkeypatch, capsys):
    import openadapt_flow.privacy as privacy

    monkeypatch.setattr(privacy, "get_text_scrubber", lambda: None)
    assert main(["ledger", "add", str(FIXTURES / "flight_held")]) == 0
    out = tmp_path / "e.jsonl"
    assert main(["ledger", "export", "--out", str(out), "--require-scrubber"]) == 2
    assert main(["ledger", "export", "--out", str(out)]) == 0
    assert "sidecars only" in capsys.readouterr().err


def test_push_is_refused_offline_with_no_key(monkeypatch, capsys):
    import httpx

    def no_network(*_a, **_k):
        raise AssertionError("push must not reach the network without a key")

    monkeypatch.setattr(httpx, "post", no_network)
    assert main(["ledger", "add", str(FIXTURES / "flight_held")]) == 0
    assert main(["ledger", "push"]) == 2
    assert "OPENADAPT_API_KEY" in capsys.readouterr().err
    with pytest.raises(LedgerRefused):
        push(LedgerStore(), poster=no_network, environ={})


def test_push_sends_the_row_minus_sources(monkeypatch, capsys):
    assert main(["ledger", "add", str(FIXTURES / "shadow_mismatch")]) == 0
    sent: list[tuple[str, dict, dict]] = []

    def poster(url: str, headers: dict, payload: dict) -> int:
        sent.append((url, headers, payload))
        return 202

    results = push(
        LedgerStore(),
        host="https://app.openadapt.ai",
        poster=poster,
        environ={"OPENADAPT_API_KEY": "test-key-not-real"},
    )
    assert results[0]["status"] == 202
    ((url, headers, payload),) = sent
    assert url == "https://app.openadapt.ai/api/runs/ingest-agent-run"
    assert headers["Authorization"] == "Bearer test-key-not-real"
    assert payload["schema"] == "openadapt.agent-run-summary/v1"
    assert "sources" not in payload
    for value in FIXTURE_VALUES:
        assert value not in json.dumps(payload)


def test_push_refuses_an_untrusted_destination():
    assert LedgerStore().rows() == []
    with pytest.raises(LedgerRefused, match="destination"):
        push(
            LedgerStore(),
            host="http://203.0.113.7",
            poster=lambda *_: 202,
            environ={"OPENADAPT_API_KEY": "test-key-not-real"},
        )


def test_push_dry_run_needs_no_key(capsys):
    assert main(["ledger", "add", str(FIXTURES / "flight_held")]) == 0
    capsys.readouterr()
    assert main(["ledger", "push", "--dry-run"]) == 0
    payload = json.loads(capsys.readouterr().out.splitlines()[0])
    assert payload["schema"] == "openadapt.agent-run-summary/v1"
