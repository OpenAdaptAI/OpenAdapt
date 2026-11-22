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
    # Copies of the agent-control-plane pieces' real (synthetic) artifacts.
    "Synthetic note 00 for review",
    "Synthetic note 16 for review",
    "Agent guard benchmark triage note",
    "Jane Sample",
    "Triage",
)


def _run_fixtures() -> list[Path]:
    """Every fixture that is a run (not a config folder or a P6 home)."""

    return sorted(
        p
        for p in FIXTURES.iterdir()
        if p.is_dir()
        and not p.name.endswith("_config")
        and p.name not in ("exceptions_home", "run2_approvals")
    )


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "home"
    monkeypatch.setenv("OPENADAPT_HOME", str(path))
    monkeypatch.delenv("OPENADAPT_API_KEY", raising=False)
    # A deterministic stand-in for openadapt-privacy's scrubber, so these
    # tests don't depend on a spaCy model being installed.
    import openadapt_flow.privacy as privacy

    monkeypatch.setattr(privacy, "get_text_scrubber", lambda: _PassThrough())
    return path


class _PassThrough:
    def scrub_text(self, text: str, is_separated: bool = False) -> str:
        return text


def _add_all(capsys: pytest.CaptureFixture[str]) -> None:
    for fixture in _run_fixtures():
        extra = []
        config = FIXTURES / f"{fixture.name}_config"
        for path in sorted(config.iterdir()) if config.is_dir() else []:
            extra += ["--config", str(path)]
        assert (
            main(["ledger", "add", str(fixture), "--task-type", "intake", *extra]) == 0
        )
    capsys.readouterr()


def test_add_dedupes_and_writes_private_files(capsys, home):
    run = FIXTURES / "shadow_mismatched"
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
    assert sorted(p.name for p in run.iterdir()) == [
        "case.json",
        "egress.json",
        "proposed_write.json",
        "run.json",
        "usage.json",
    ]


def test_a_changed_run_gets_a_new_row_and_summary_keeps_the_newest(
    tmp_path, home, capsys
):
    # P6's real expired card: the answer has no decided_at, so the time is
    # unknown; once a decision time is there, the newest row carries it.
    run = tmp_path / "run"
    run.mkdir()
    folder = home / "exceptions" / "e2e-expired"
    shutil.copytree(FIXTURES / "exceptions_home" / "exceptions" / "e2e-expired", folder)
    assert main(["ledger", "add", str(run), "--run-ref", "e2e-expired"]) == 0
    assert LedgerStore().rows()[0].human.intervention_s is None
    (answer,) = folder.glob("answer-*.json")
    doc = json.loads(answer.read_text())
    doc["decided_at"] = "2026-11-22T02:43:59.700707Z"
    answer.write_text(json.dumps(doc))
    assert main(["ledger", "add", str(run), "--run-ref", "e2e-expired"]) == 0
    store = LedgerStore()
    assert len(store.rows()) == 2
    (latest,) = store.latest()
    assert latest.human.intervention_s == 120.0


def test_a_bad_run_ref_is_refused(capsys):
    run = FIXTURES / "flight_held"
    assert main(["ledger", "add", str(run), "--run-ref", "../x"]) == 2
    assert "run reference" in capsys.readouterr().err


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
    assert main(["ledger", "add", str(FIXTURES / "shadow_wrong_record")]) == 0
    capsys.readouterr()
    assert main(["ledger", "list"]) == 0
    listed = capsys.readouterr().out
    row_id = listed.split()[0]
    assert "shadow_wrong_record" in listed
    assert main(["ledger", "show", row_id]) == 0
    plain = capsys.readouterr().out
    assert "Synthetic note 16" not in plain and '"p3"' not in plain
    assert main(["ledger", "show", row_id, "--show-values"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["values"]["patient_id"]["intended"] == "p3"
    assert shown["values"]["patient_id"]["saved"] == "p2"
    assert main(["ledger", "show", "../../etc/passwd"]) == 2


def test_summary_counts_and_unknowns(capsys):
    _add_all(capsys)
    assert main(["ledger", "summary", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    counts = report["outcomes"]
    assert sum(counts.values()) == report["rows"] == len(_run_fixtures())
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
                "fields": [{"name": "ref", "parse": "json", "value": "SYN-0042-ALPHA"}],
            }
        )
    )
    assert main(["ledger", "add", str(run)]) == 0
    out = tmp_path / "export.jsonl"
    assert main(["ledger", "export", "--out", str(out)]) == 2
    assert "private sidecar" in capsys.readouterr().err
    # The refused row stays local; nothing else was in the ledger.
    assert out.read_text() == ""


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
    assert main(["ledger", "add", str(FIXTURES / "shadow_mismatched")]) == 0
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


def test_the_parser_registers_ledger_without_the_types_schema(monkeypatch):
    # A released install whose openadapt-types predates run_ledger must still
    # build the parser (``--version``); only the ledger verb itself refuses.
    import argparse
    import importlib
    import sys

    for name in [m for m in sys.modules if m.startswith("openadapt_flow.run_ledger")]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "openadapt_types.run_ledger", None)
    cli = importlib.import_module("openadapt_flow.run_ledger.cli")
    parser = argparse.ArgumentParser(allow_abbrev=False)
    cli.register(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["ledger", "list"])
    with pytest.raises(SystemExit) as refused:
        args.func(args)
    assert "upgrade openadapt-types" in str(refused.value.code)


def test_export_refuses_when_the_scrubber_cannot_run(tmp_path, monkeypatch, capsys):
    import openadapt_flow.privacy as privacy

    class Broken:
        def scrub_text(self, text: str, is_separated: bool = False) -> str:
            raise RuntimeError("model not installed")

    monkeypatch.setattr(privacy, "get_text_scrubber", lambda: Broken())
    assert main(["ledger", "add", str(FIXTURES / "flight_held")]) == 0
    out = tmp_path / "e.jsonl"
    assert main(["ledger", "export", "--out", str(out)]) == 2
    assert "could not run" in capsys.readouterr().err
    assert not out.exists()
