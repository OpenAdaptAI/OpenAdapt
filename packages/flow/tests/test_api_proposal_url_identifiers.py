"""Record identifiers in an API step's URLs are run parameters.

The reviewer's case for #1269: a save to /save.php?record_id=17 compiled to
/save.php (the query lost), while /new.php?record_id=17 stayed literal in the
form-fetch URL, so a later run read and wrote record 17. Synthetic values only.
"""

from __future__ import annotations

import json

from openadapt_flow import network_capture as nc
from openadapt_flow.compiler.api_proposal import propose_api_step
from openadapt_flow.runtime.actuators import ActuationStatus
from openadapt_flow.runtime.actuators.web_form import WebFormActuator, fill_url
from tests.test_api_tier_web_form import (
    FORM_HTML,
    FakeTransport,
    _recording,
    _workflow,
)


def _with_record_id(rec, *, save_url, form_url, record_id="17", extra=None):
    meta = json.loads((rec / "meta.json").read_text())
    meta["params"].update({"record_id": record_id, **(extra or {})})
    (rec / "meta.json").write_text(json.dumps(meta))
    rows = [json.loads(x) for x in (rec / nc.NETWORK_FILE).read_text().splitlines()]
    for row in rows[1:]:
        if row.get("method") == "POST":
            row["url"] = save_url
        elif "<form" in (row.get("response_text") or ""):
            row["url"] = form_url
    (rec / nc.NETWORK_FILE).write_text("".join(json.dumps(r) + "\n" for r in rows))
    return rec


def _propose(tmp_path, **kw):
    rec = _with_record_id(_recording(tmp_path), **kw)
    wf = _workflow()
    wf.params["record_id"] = kw.get("record_id", "17")
    return propose_api_step(wf, rec)


def test_a_record_id_in_the_save_and_form_urls_becomes_a_parameter(tmp_path):
    _wf, step, reasons = _propose(
        tmp_path,
        save_url="http://app/app/save.php?record_id=17&mode=edit",
        form_url="http://app/app/new.php?record_id=17",
    )
    assert step is not None, reasons
    binding = step.api_binding
    assert binding.url_template == "/app/save.php?record_id={record_id}&mode=edit"
    assert binding.web_form.form_path == "/app/new.php?record_id={record_id}"


class _RecordTransport(FakeTransport):
    def get(self, path):
        self.gets.append(path)
        page = self.form if path.startswith("/app/new.php") else self.dup
        return type("R", (), {"status": 200, "text": page, "headers": {}})()


def test_a_later_run_reads_and_writes_its_own_record(tmp_path):
    _wf, step, reasons = _propose(
        tmp_path,
        save_url="http://app/app/save.php?record_id=17",
        form_url="http://app/app/new.php?record_id=17",
    )
    assert step is not None, reasons
    transport = _RecordTransport(
        form=FORM_HTML.replace("<session:csrf_token_form>", "live")
    )
    out = WebFormActuator(transport).actuate(
        step.api_binding,
        {
            "lname": "Marsh",
            "state_label": "Massachusetts",
            "note": "Referred",
            "record_id": "42",
        },
    )
    assert out.status is ActuationStatus.ACTUATED, out.reason
    assert transport.gets[0] == "/app/new.php?record_id=42"
    ((path, _body),) = transport.posts
    assert path == "/app/save.php?record_id=42"


def test_an_ambiguous_url_value_is_refused(tmp_path):
    _wf, step, reasons = _propose(
        tmp_path,
        save_url="http://app/app/save.php?record_id=17",
        form_url="http://app/app/new.php?record_id=17",
        extra={"visit_id": "17"},
    )
    assert step is None
    assert "ambiguous" in " ".join(reasons)


def test_a_save_to_another_origin_is_refused(tmp_path):
    _wf, step, reasons = _propose(
        tmp_path,
        save_url="https://elsewhere.example/app/save.php",
        form_url="http://app/app/new.php",
    )
    assert step is None
    assert "another origin" in " ".join(reasons)


def test_fill_url_encodes_values_and_keeps_the_rest():
    assert fill_url("/p/{id}/save?x={q}&mode=edit", {"id": "a/b", "q": "x y&z"}) == (
        "/p/a%2Fb/save?x=x+y%26z&mode=edit"
    )
    assert fill_url("/plain.php", {}) == "/plain.php"


def test_a_hidden_field_echoing_the_url_record_id_is_the_same_parameter(tmp_path):
    rec = _recording(tmp_path)
    rows = [json.loads(x) for x in (rec / nc.NETWORK_FILE).read_text().splitlines()]
    for row in rows[1:]:
        if "<form" in (row.get("response_text") or ""):
            row["response_text"] = row["response_text"].replace(
                '<input name="form_note"',
                '<input type="hidden" name="pid" value="17"><input name="form_note"',
            )
        if row.get("method") == "POST":
            row["form"].append(["pid", "17"])
    (rec / nc.NETWORK_FILE).write_text("".join(json.dumps(r) + "\n" for r in rows))
    rec = _with_record_id(
        rec,
        save_url="http://app/app/save.php?record_id=17",
        form_url="http://app/app/new.php?record_id=17",
    )
    wf = _workflow()
    wf.params["record_id"] = "17"
    _wf, step, reasons = propose_api_step(wf, rec)
    assert step is not None, reasons
    pid = {f.name: f for f in step.api_binding.web_form.fields}["pid"]
    assert (pid.source, pid.param) == ("param", "record_id")
