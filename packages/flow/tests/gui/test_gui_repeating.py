"""Repeating structures on four kinds of app.

- a web form (group booking) with "Add another attendee";
- an EMR-like medication table that starts empty, with unlabeled rows;
- a spreadsheet-like grid with no browser ("Insert row");
- a SaaS invoice whose "Add line item" control is a link.

Each group is learned from one demonstration, then filled for an intake with
a different number of items. Every instance is read back against its own item.
"""

from __future__ import annotations

from typing import Any

import pytest

from openadapt_flow.gui.dom import PageActuator
from openadapt_flow.gui.model import ControlSpec, Recorder, match_all, match_one
from openadapt_flow.gui.repeating import (
    RepeatSpec,
    Slot,
    fill_instances,
    find_instances,
    judge_instances,
    learn_repeats,
    location_rows,
    map_slots,
)
from openadapt_flow.verdict import Status
from tests.gui import _apps
from tests.gui._grid import GridApp, Sheet

# -- web form: group booking -------------------------------------------------------


def booking(
    *,
    start: int = 1,
    prefill: str = "",
    add: str = "one",
    add_posts: bool = False,
    upper: bool = False,
    auto_blank: bool = False,
) -> _apps.App:
    adds = {"one": "addOne();", "none": "", "two": "addOne();addOne();"}[add]
    post = "fetch('/draft',{method:'POST',body:'{}'});" if add_posts else ""
    blur = "this.value=this.value.toUpperCase();" if upper else ""
    auto = "autoAdd(this);" if auto_blank else ""

    def form(app: _apps.App, path: str, method: str, f: dict) -> tuple[int, str]:
        return 200, _apps.page(
            "Book",
            f"""<main><h1>Group booking</h1><form method="post" action="/book">
<div id="people"></div>
<button type="button" id="add" onclick="{adds}{post}">Add another attendee</button>
<button type="submit">Book</button></form>
<script>
let n=0;
function autoAdd(el){{if(el.closest('fieldset')===last()&&el.value)addOne();}}
function last(){{const f=document.querySelectorAll('#people fieldset');return f[f.length-1];}}
function addOne(){{n++;const fs=document.createElement('fieldset');
fs.innerHTML='<legend>Attendee '+n+'</legend>'
+'<label>Full name <input name="name'+n+'" onchange="{blur}{auto}"></label>'
+'<label>Date of birth <input name="dob'+n+'"></label>';
document.getElementById('people').appendChild(fs);}}
for(let i=0;i<{start};i++)addOne();
if({"true" if prefill else "false"})document.querySelector('[name=name1]').value='{prefill}';
</script></main>""",
        )

    def book(app: _apps.App, path: str, method: str, f: dict) -> tuple[int, str]:
        app.state["booking"] = {k: v[0] for k, v in f.items()}
        return 200, _apps.page("Booked", "<main><h1>Booked</h1></main>")

    return _apps.App(
        "book.test",
        {
            ("GET", "/book"): form,
            ("POST", "/book"): book,
            ("POST", "/draft"): lambda a, p, m, f: (200, "{}"),
        },
    )


DEMO_PEOPLE = [
    {"name": "Ann Example", "dob": "1990-01-02"},
    {"name": "Ben Example", "dob": "1988-03-04"},
]
PEOPLE = [
    {"name": "Cal Sample", "dob": "1975-05-06"},
    {"name": "Dot Sample", "dob": "1979-07-08"},
    {"name": "Eve Sample", "dob": "2001-09-10"},
]


def _record_booking(page: Any) -> tuple[RepeatSpec, list]:
    app = booking()
    with _apps.serve(page, app):
        act = PageActuator(page)
        page.goto(f"{app.origin}/book")
        rec = Recorder(act)

        def fill(n: int, person: dict) -> None:
            s = act.observe()
            names = match_all(ControlSpec("textbox", "Full name"), s)
            dobs = match_all(ControlSpec("textbox", "Date of birth"), s)
            rec.set_text(names[n].handle, person["name"])
            rec.set_text(dobs[n].handle, person["dob"])

        fill(0, DEMO_PEOPLE[0])
        rec.click(
            match_one(
                ControlSpec("button", "Add another attendee"), act.observe()
            ).handle
        )
        fill(1, DEMO_PEOPLE[1])
    (spec,) = learn_repeats(rec.steps)
    mapped, problems = map_slots(spec, rec.steps, DEMO_PEOPLE)
    assert not problems, problems
    return mapped, rec.steps


def test_web_form_group_is_learned_with_its_fields_and_item_keys(page) -> None:
    spec, _ = _record_booking(page)
    assert spec.add == ControlSpec("button", "Add another attendee", dialog=False)
    assert spec.instance_kind == "group"
    assert spec.slots == (
        Slot("textbox", "Full name", "name"),
        Slot("textbox", "Date of birth", "dob"),
    )


def test_web_form_adds_fills_and_checks_one_instance_per_item(page) -> None:
    spec, _ = _record_booking(page)
    app = booking()
    with _apps.serve(page, app):
        act = PageActuator(page)
        page.goto(f"{app.origin}/book")
        out = fill_instances(act, spec, PEOPLE, run_id="r1")
        assert out.held, out.reason
        assert out.added == 2
        act.click(match_one(ControlSpec("button", "Book"), act.observe()).handle)
    assert app.state["booking"] == {
        "name1": "Cal Sample",
        "dob1": "1975-05-06",
        "name2": "Dot Sample",
        "dob2": "1979-07-08",
        "name3": "Eve Sample",
        "dob3": "2001-09-10",
    }


@pytest.mark.parametrize(
    ("variant", "items", "reason", "status"),
    [
        ({"add": "none"}, PEOPLE, "from 1 to 1 instances", Status.NOT_CHECKED),
        ({"add": "two"}, PEOPLE, "from 1 to 3 instances", Status.NOT_CHECKED),
        ({"prefill": "Zed Other"}, PEOPLE, "already holds data", Status.NOT_CHECKED),
        ({"add_posts": True}, PEOPLE, "wrote 1 times", Status.NOT_CHECKED),
        ({"start": 3}, PEOPLE[:2], "never deletes", Status.NOT_CHECKED),
        ({"upper": True}, PEOPLE, "does not show its item", Status.REFUTED),
        ({"auto_blank": True}, PEOPLE, "does not show its item", Status.REFUTED),
    ],
)
def test_web_form_group_that_does_not_line_up_stops(
    page, variant, items, reason, status
) -> None:
    spec, _ = _record_booking(page)
    app = booking(**variant)
    with _apps.serve(page, app):
        act = PageActuator(page)
        page.goto(f"{app.origin}/book")
        out = fill_instances(act, spec, items, run_id="r2")
    assert out.status is status and reason in out.reason, out.reason
    if status is Status.NOT_CHECKED:
        assert not out.wrote or "wrote" in out.reason


def test_an_item_missing_a_key_stops_before_anything_is_added(page) -> None:
    spec, _ = _record_booking(page)
    app = booking()
    with _apps.serve(page, app):
        act = PageActuator(page)
        page.goto(f"{app.origin}/book")
        out = fill_instances(act, spec, [PEOPLE[0], {"name": "No Dob"}])
    assert (
        out.status is Status.NOT_CHECKED and out.added == 0 and "dob" not in out.bound
    )


# -- EMR-like medication table: starts empty, rows have no labels --------------------


def meds(*, sort_after: bool = False) -> _apps.App:
    sorter = (
        "document.getElementById('tb').addEventListener('change',()=>{"
        "const rows=[...document.querySelectorAll('#tb tr')];"
        "if(rows.every(r=>r.querySelector('input').value)){"
        "const vals=rows.map(r=>[...r.querySelectorAll('input')].map(i=>i.value)).sort();"
        "rows.forEach((r,k)=>r.querySelectorAll('input').forEach((i,j)=>i.value=vals[k][j]));}});"
        if sort_after
        else ""
    )

    def chart(app: _apps.App, path: str, method: str, f: dict) -> tuple[int, str]:
        return 200, _apps.page(
            "Medications",
            f"""<main><h1>Chart: Test Patient D</h1>
<table aria-label="Current medications"><thead><tr><td>Drug</td><td>Dose</td></tr></thead>
<tbody id="tb"></tbody></table>
<button onclick="addRow()">Add medication</button>
<script>function addRow(){{const tr=document.createElement('tr');
tr.innerHTML='<td><input aria-label="Drug"></td><td><input aria-label="Dose"></td>';
document.getElementById('tb').appendChild(tr);}}{sorter}</script></main>""",
        )

    return _apps.App("chart.test", {("GET", "/meds"): chart})


DEMO_MEDS = [{"drug": "Drug A", "dose": "10 mg"}, {"drug": "Drug B", "dose": "5 mg"}]
MEDS = [
    {"drug": "Drug Z", "dose": "1 mg"},
    {"drug": "Drug Y", "dose": "2 mg"},
    {"drug": "Drug X", "dose": "3 mg"},
]


def _record_meds(page: Any) -> RepeatSpec:
    app = meds()
    with _apps.serve(page, app):
        act = PageActuator(page)
        page.goto(f"{app.origin}/meds")
        rec = Recorder(act)
        for n, med in enumerate(DEMO_MEDS):
            rec.click(
                match_one(ControlSpec("button", "Add medication"), act.observe()).handle
            )
            s = act.observe()
            rec.set_text(
                match_all(ControlSpec("textbox", "Drug"), s)[n].handle, med["drug"]
            )
            rec.set_text(
                match_all(ControlSpec("textbox", "Dose"), s)[n].handle, med["dose"]
            )
    (spec,) = learn_repeats(rec.steps)
    mapped, problems = map_slots(spec, rec.steps, DEMO_MEDS)
    assert not problems
    return mapped


def test_emr_table_that_starts_empty_is_learned_from_the_second_add(page) -> None:
    spec = _record_meds(page)
    assert spec.instance_kind == "row"
    assert [(s.name, s.item_key) for s in spec.slots] == [
        ("Drug", "drug"),
        ("Dose", "dose"),
    ]


def test_emr_rows_are_checked_by_the_row_each_item_was_written_into(page) -> None:
    spec = _record_meds(page)
    app = meds()
    with _apps.serve(page, app):
        act = PageActuator(page)
        page.goto(f"{app.origin}/meds")
        out = fill_instances(act, spec, MEDS, run_id="r3")
        assert out.held, out.reason
        assert len(set(out.bound.values())) == 3
    # The app sorts the rows once all are filled: each row now shows another
    # item's values, so the check by row refutes even though every value is
    # somewhere on the screen.
    app = meds(sort_after=True)
    with _apps.serve(page, app):
        act = PageActuator(page)
        page.goto(f"{app.origin}/meds")
        out = fill_instances(act, spec, MEDS, run_id="r4")
    assert out.status is Status.REFUTED


# -- spreadsheet-like grid -----------------------------------------------------------


def _ledger(rows: list[list[str]] | None = None) -> GridApp:
    return GridApp(
        [Sheet("Expenses", ("Item", "Amount"), rows if rows is not None else [])]
    )


def _record_grid() -> RepeatSpec:
    grid = _ledger()
    rec = Recorder(grid)
    demo = [{"item": "Rent", "amount": "1200"}, {"item": "Power", "amount": "90"}]
    for r, row in enumerate(demo):
        rec.click("tool:insert")
        rec.set_text(f"cell:Expenses:{r}:0", row["item"])
        rec.set_text(f"cell:Expenses:{r}:1", row["amount"])
    (spec,) = learn_repeats(rec.steps)
    mapped, problems = map_slots(spec, rec.steps, demo)
    assert not problems
    return mapped


def test_grid_rows_are_learned_and_filled_without_a_browser() -> None:
    spec = _record_grid()
    assert spec.add.name == "Insert row" and spec.instance_kind == "row"
    grid = _ledger()
    items = [
        {"item": "Rent", "amount": "1250"},
        {"item": "Water", "amount": "40"},
        {"item": "Net", "amount": "60"},
    ]
    out = fill_instances(grid, spec, items, run_id="g1")
    assert out.held, out.reason
    assert grid.sheets[0].rows == [["Rent", "1250"], ["Water", "40"], ["Net", "60"]]
    assert location_rows(spec, items, out.bound)[0] == {
        "id": f"{spec.group_id}:0:Item",
        "keys": [f"{spec.group_id}[0].item"],
        "start": "same_screen",
        "locator": {"kind": "region", "value": out.bound["0"], "column": "Item"},
        "expected": {f"{spec.group_id}[0].item": "Rent"},
        "expected_count": 1,
    }


def test_grid_rows_already_holding_data_stop_and_nothing_is_added() -> None:
    spec = _record_grid()
    grid = _ledger([["Old", "5"]])
    out = fill_instances(
        grid, spec, [{"item": "Rent", "amount": "1"}, {"item": "Tax", "amount": "2"}]
    )
    assert out.status is Status.NOT_CHECKED and out.added == 0
    assert grid.sheets[0].rows == [["Old", "5"]]


# -- SaaS invoice: the add control is a link ------------------------------------------


def invoice() -> _apps.App:
    def page_(app: _apps.App, path: str, method: str, f: dict) -> tuple[int, str]:
        return 200, _apps.page(
            "Invoice",
            """<main><h1>New invoice</h1><ul id="lines"></ul>
<a href="#" onclick="add();return false;">Add line item</a>
<script>let n=0;function add(){n++;const li=document.createElement('li');
li.innerHTML='<h4>Line '+n+'</h4><label>Description <input></label><label>Amount <input></label>';
document.getElementById('lines').appendChild(li);}add();</script></main>""",
        )

    return _apps.App("app.test", {("GET", "/invoice"): page_})


def test_saas_invoice_fills_lines_added_by_a_link(page) -> None:
    spec = RepeatSpec(
        "lines",
        ControlSpec("link", "Add line item"),
        "item",
        (Slot("textbox", "Description", "desc"), Slot("textbox", "Amount", "amount")),
    )
    app = invoice()
    with _apps.serve(page, app):
        act = PageActuator(page)
        page.goto(f"{app.origin}/invoice")
        assert [i.name for i in find_instances(act.observe(), spec).instances] == [
            "Line 1"
        ]
        items = [
            {"desc": "Setup", "amount": "100.00"},
            {"desc": "Support", "amount": "40.00"},
        ]
        out = fill_instances(act, spec, items, run_id="s1")
        assert out.held, out.reason


# -- the verdict ------------------------------------------------------------------------


def test_judge_instances_refuses_missing_extra_and_rebound_rows() -> None:
    planned = [{"item": "0", "slot": "Name", "expected": "Ann"}]
    seen = [{"handle": "#a", "slot": "Name", "value": "Ann", "run_id": "r"}]
    assert judge_instances(planned, {"0": "#a"}, seen, run_id="r").status is Status.HELD
    assert judge_instances([], {}, [], run_id="r").status is not Status.HELD
    extra = seen + [{"handle": "#b", "slot": "Name", "value": "", "run_id": "r"}]
    assert (
        judge_instances(planned, {"0": "#a"}, extra, run_id="r").status
        is Status.REFUTED
    )
    assert (
        judge_instances(planned, {"0": "#b"}, seen, run_id="r").status is Status.REFUTED
    )
    assert (
        judge_instances(planned, {"0": "#a"}, [], run_id="r").status
        is Status.NOT_CHECKED
    )
    near = [{**seen[0], "value": "Ann "}]
    assert (
        judge_instances(planned, {"0": "#a"}, near, run_id="r").status is Status.REFUTED
    )
