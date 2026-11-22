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
            "screen": None,
            "screen_differs": False,
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


def test_screen_values_match_a_field_by_id_name_or_label() -> None:
    from openadapt_flow.agent_guard.screen_values import match_fields, readable_png

    def c(key: str, id_: str, name: str, label: str, value: str, visible=True):
        return {
            "key": key,
            "id": id_,
            "name": name,
            "label": label,
            "value": value,
            "visible": visible,
        }

    controls = [
        c("textarea:note:1", "note", "", "Note", "typed"),
        # A hidden template with the same name is not what a person saw.
        c("textarea:note:2", "note-tpl", "note", "", "template", visible=False),
        c("input:x:1", "x", "enc_type", "", "Triage"),
        c("input:a:1", "a", "", "Amount", "1"),
        c("input:b:1", "b", "", "Amount", "2"),
        c("input:dob:1", "dob", "", "Date of birth", "1990-01-01", visible=False),
    ]
    found = match_fields(
        controls, ["note", "type", "amount", "dob"], {"type": "enc_type"}
    )
    # Two controls labelled Amount disagree: not read rather than a guess.
    # dob matches only a hidden control: not read on screen.
    assert found == {"note": "typed", "type": "Triage"}
    assert match_fields(None, ["note"]) == {}
    assert readable_png(tiny_png())
    assert not readable_png(b"") and not readable_png(b"not a png at all, no header")


def test_the_card_shows_the_screen_value_beside_the_task_value(sor) -> None:
    cfg = parse_verify_config(verify_yaml(sor[0]))
    card = build_card(_task(), cfg, claim_fields=None, screen_fields={"note": MISREAD})
    [row] = card.sources
    assert (row.screen, row.screen_differs, row.intended) == (MISREAD, True, NOTE)
    assert card.screen_read is True and card.values_read is False
    assert card.fields[0].after == NOT_READ
    assert not card.nothing_visible(None)
    blind = build_card(_task(), cfg, claim_fields=None)
    assert blind.nothing_visible(None) and blind.nothing_visible(b"garbage")
    assert not blind.nothing_visible(tiny_png())


def test_the_page_read_skips_controls_that_are_not_rendered() -> None:
    """READ_CONTROLS_JS in a real headless Chromium: a display:none copy, an
    aria-hidden copy and a zero-size field with the task's field name are
    read as hidden; the field the person sees is the only screen value."""

    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    from openadapt_flow.agent_guard.cdp import NOTICE_ID
    from openadapt_flow.agent_guard.screen_values import (
        READ_CONTROLS_JS,
        controls_from,
        match_fields,
    )

    html = (
        '<label for="note">Note</label><textarea id="note">seen</textarea>'
        '<div style="display:none"><textarea name="note">tpl</textarea></div>'
        '<div aria-hidden="true"><input name="note" value="aria"></div>'
        '<input name="note" value="tiny" style="width:0;height:0;border:0;padding:0">'
        '<div style="display:none"><input id="dob" value="1990-01-01"></div>'
    )
    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            page = browser.new_page()
            page.set_content(html)
            raw = page.evaluate(READ_CONTROLS_JS % {"notice": json.dumps(NOTICE_ID)})
        finally:
            browser.close()
    controls = controls_from(raw)
    assert controls is not None
    assert [c["value"] for c in controls if c["visible"]] == ["seen"]
    assert match_fields(controls, ["note", "dob"]) == {"note": "seen"}


@pytest.mark.parametrize("count,checked", [(1, True), (25, False)])
def test_values_checked_only_when_the_card_left_no_field_out(
    sor, tmp_path: Path, count: int, checked: bool
) -> None:
    """A task over the card's field cap never reads as checked, even when
    every field the card shows came from the held request."""

    from openadapt_flow.approval_mode.item_approval import MAX_SAVE_FIELDS
    from openadapt_flow.console.save_approvals import (
        list_save_approvals,
        save_approval_detail,
    )

    assert MAX_SAVE_FIELDS < 25
    fields = {"note": NOTE, **{f"extra{i:02d}": "x" for i in range(count - 1)}}
    task = GuardTask(
        task_id=f"card-cap-{count:04d}",
        expected_record={"patient_id": "p1"},
        intended_fields=fields,
    )
    cfg = parse_verify_config(verify_yaml(sor[0]))
    guarded = GuardedSave(cfg, tmp_path / "g")
    guard = guarded.begin(task, screen_text=FakeAgent(sor[0]).open_chart("p1"))
    guard.approval_card = build_card(task, cfg, claim_fields=dict(fields))
    guard.commit(screenshot_png=tiny_png())
    [row] = list_save_approvals(tmp_path)
    shown = save_approval_detail(tmp_path, row.request_id)["presentation"]
    assert shown["values_checked"] is checked
    assert ("exactly" in shown["explanation"].lower()) is False
