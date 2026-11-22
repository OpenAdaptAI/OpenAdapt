"""The guarded save's approval card: each field's value now, the value saved,
and its source, bound to the request the person answers.

Synthetic MockMed fault server on localhost; no browser, no model calls.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmark.harness.guard_demo import NOTE, FakeAgent, tiny_png, verify_yaml
from openadapt_flow.agent_guard.approval_fields import (
    NOT_READ,
    SOURCES_FILENAME,
    build_card,
    read_sources_for,
)
from openadapt_flow.guard import GuardedSave, GuardTask, load_task
from openadapt_flow.mockmed.fault_server import serve as fault_serve
from openadapt_flow.verify_run import parse_verify_config

MISREAD = "Referral to orthopaedics for knee pain"


@pytest.fixture
def sor():
    url, db, stop = fault_serve()
    try:
        yield url.rstrip("/"), db
    finally:
        stop()


def _task(task_id: str = "card-task-0001") -> GuardTask:
    return GuardTask(
        task_id=task_id,
        expected_record={"patient_id": "p1"},
        intended_fields={"note": NOTE},
    )


def _source_task(tmp_path: Path, note: str, cited: str) -> GuardTask:
    """A task whose note cites a referral letter (``cited`` is the letter's)."""

    from openadapt_flow.provenance import (
        ParamProvenance,
        ProvenanceFile,
        SourceRef,
        document_sha256,
    )

    letter = tmp_path / "referral.txt"
    letter.write_text(f"Referral\nPatient: p1\nNote: {cited}\n")
    prov = ProvenanceFile(
        params={
            "note": ParamProvenance(
                value=note,
                source=SourceRef(
                    kind="document",
                    sha256=document_sha256(letter),
                    line=3,
                    label="Note",
                ),
                extractor="model:first-reader",
            )
        }
    )
    path = tmp_path / "task.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "openadapt.guard-task/v1",
                "task_id": "card-source-0001",
                "expected_record": {"patient_id": "p1"},
                "intended_fields": {"note": note},
                "provenance": json.loads(prov.model_dump_json()),
                "sources": [letter.name],
            }
        )
    )
    return load_task(path)


def test_the_request_carries_before_after_and_source_for_each_field(
    sor, tmp_path: Path
) -> None:
    from openadapt_flow.console.save_approvals import (
        list_save_approvals,
        save_approval_detail,
    )

    task = _source_task(tmp_path, NOTE, NOTE)
    guarded = GuardedSave(parse_verify_config(verify_yaml(sor[0])), tmp_path / "g")
    guard = guarded.begin(task, screen_text=FakeAgent(sor[0]).open_chart("p1"))
    guard.commit(screenshot_png=tiny_png())
    request = guard._request
    # A create: nothing was there before. No request is held here (the
    # caller sends the save itself), so what it sends is not read: the task's
    # value is shown beside it, never in its place.
    assert [f.model_dump() for f in request.fields] == [
        {"label": "note", "before": None, "after": NOT_READ}
    ]
    sources = read_sources_for(guard.dir, request)
    assert sources == [
        {
            "label": "note",
            "source": NOTE,
            "status": "agree",
            "differs": False,
            "intended": NOTE,
            "held_differs": False,
        }
    ]
    assert (guard.dir / SOURCES_FILENAME).stat().st_mode & 0o777 == 0o600
    # The phone's presentation shows both, from the console that serves it.
    [row] = list_save_approvals(tmp_path)
    shown = save_approval_detail(tmp_path, row.request_id)["presentation"]
    assert shown["fields"] == [{"label": "note", "before": None, "after": NOT_READ}]
    assert shown["sources"] == sources
    assert shown["values_checked"] is False
    assert "exactly" not in shown["explanation"].lower()
    assert "not checked before saving" in shown["explanation"]
    # The signed task the phone answers is PHI-free; the values stay local.
    task_doc = save_approval_detail(tmp_path, row.request_id)["task"]
    assert NOTE not in json.dumps(task_doc)


def test_a_source_that_disagrees_shows_what_the_document_says(
    sor, tmp_path: Path
) -> None:
    # The task cites the letter for the note, but the letter says another one.
    task = _source_task(tmp_path, MISREAD, NOTE)
    card = build_card(task, parse_verify_config(verify_yaml(sor[0])))
    [source] = card.sources
    assert (source.source, source.status, source.differs) == (NOTE, "disagree", True)
    assert source.intended == MISREAD
    assert card.fields[0].after == NOT_READ


def test_a_value_that_cant_be_read_is_not_read_never_blank(tmp_path: Path) -> None:
    from openadapt_flow.verify_run import parse_verify_config as parse

    # Nothing listens here: the current values can't be read.
    data = verify_yaml("http://127.0.0.1:9")
    data["effects"][0]["count_new_only"] = False
    cfg = parse(data)
    card = build_card(_task(), cfg, claim_fields={"note": NOTE})
    [row] = card.fields
    assert row.before == NOT_READ and row.after == NOTE
    assert card.not_covered == ["note"]
    assert "does not cover: note" in card.intent_note()


def test_a_write_adapter_claim_is_what_the_card_shows(sor) -> None:
    cfg = parse_verify_config(verify_yaml(sor[0]))
    card = build_card(_task(), cfg, claim_fields={"note": "what the request sends"})
    assert card.fields[0].after == "what the request sends"
    # Beside the task's value, flagged: the request sends another note.
    assert card.sources[0].intended == NOTE
    assert card.sources[0].held_differs is True
    assert card.values_read is True and card.values_how == "write_adapter"
    assert "not checked" not in card.intent_note()
    same = build_card(_task(), cfg, claim_fields={"note": NOTE})
    assert same.sources[0].held_differs is False and same.values_read is True
    unread = build_card(_task(), cfg, claim_fields={})
    assert unread.fields[0].after == NOT_READ
    assert unread.not_covered == ["note"]
    assert unread.values_read is False
    assert "could not be read, so they were not checked" in unread.intent_note()


def test_no_held_values_never_shows_the_task_value_as_what_is_sent(sor) -> None:
    """The e2e finding: with no write adapter the card showed the task's note
    as the value saved, under "exactly what OpenAdapt will save", while the
    agent had typed another one."""

    cfg = parse_verify_config(verify_yaml(sor[0]))
    card = build_card(_task(), cfg, claim_fields=None, values_how="no_write_adapter")
    assert card.fields[0].after == NOT_READ
    assert card.sources[0].intended == NOTE
    assert card.values_read is False and card.values_how == "no_write_adapter"
    assert "could not be read, so they were not checked" in card.intent_note()


def test_the_card_can_be_turned_off(sor, tmp_path: Path) -> None:
    guarded = GuardedSave(
        parse_verify_config(verify_yaml(sor[0])),
        tmp_path / "g",
        approval_fields=False,
    )
    guard = guarded.begin(_task(), screen_text=FakeAgent(sor[0]).open_chart("p1"))
    guard.commit(screenshot_png=tiny_png())
    assert guard._request.fields is None
    assert not (guard.dir / SOURCES_FILENAME).exists()


def test_a_sources_file_for_another_request_is_not_shown(sor, tmp_path: Path) -> None:
    task = _source_task(tmp_path, NOTE, NOTE)
    guarded = GuardedSave(parse_verify_config(verify_yaml(sor[0])), tmp_path / "g")
    guard = guarded.begin(task, screen_text=FakeAgent(sor[0]).open_chart("p1"))
    guard.commit(screenshot_png=tiny_png())
    path = guard.dir / SOURCES_FILENAME
    doc = json.loads(path.read_text())
    doc["request_sha256"] = "0" * 64
    path.write_text(json.dumps(doc))
    assert read_sources_for(guard.dir, guard._request) is None


def test_the_differs_flag_uses_the_value_comparison(sor, tmp_path: Path) -> None:
    from dataclasses import replace

    from openadapt_flow.agent_guard.approval_fields import SourceRow, _source_differs

    agree = SourceRow("dob", "1990-01-25", "agree")
    assert not _source_differs(agree, "01/25/1990")
    assert _source_differs(agree, "1991-01-25")
    assert not _source_differs(replace(agree, source=NOT_READ), "x")
    assert not _source_differs(agree, NOT_READ)
    # The request sends another note than the letter cites: flagged.
    task = _source_task(tmp_path, NOTE, NOTE)
    cfg = parse_verify_config(verify_yaml(sor[0]))
    card = build_card(task, cfg, claim_fields={"note": MISREAD})
    assert card.sources[0].differs is True
