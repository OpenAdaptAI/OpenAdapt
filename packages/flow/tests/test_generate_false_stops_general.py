"""Generator causes of false stops, as one model per cause (made-up schemas
and values; no app's names).

- a field no demonstration gave, placed by its column's name;
- a create that leaves out a text field whose column can't hold NULL;
- an "already there" check that pins only the task's own values;
- a list given as plain values, or empty;
- an update change given as null (the task empties the field).
"""

from __future__ import annotations

from openadapt_flow.generate import verify_contract as gen
from openadapt_flow.generate.demo_analysis import ColumnRole, Location
from openadapt_flow.generate.sql_store import Column, ForeignKey, Schema, Table


def _schema() -> Schema:
    cols = [
        Column("id", "integer", False, None, auto=True),
        Column("tag", "varchar", False, None),
        Column("notes", "text", True, None),
        Column("email", "varchar", False, None),
        Column("website", "varchar", False, "''::character varying"),
        Column("currency", "varchar", False, "'USD'::character varying"),
        Column("number", "varchar", True, None),
        Column("visible", "smallint", False, "1"),
        Column("deleted_at", "timestamp", True, None),
        Column("_custom_mac_address_1", "text", True, None),
        Column("_custom_mac_address_2", "text", True, None),
        Column("_custom_serial_code_1", "text", True, None),
        Column("owner_id", "integer", True, None),
        Column("_custom_owner_ref_id", "text", True, None),
    ]
    t = Table("items", cols, ["id"], [ForeignKey("owner_id", "users", "id")])
    return Schema(dialect="postgresql", database="db", tables={"items": t})


def _plan() -> gen.KindPlan:
    return gen.KindPlan("k", mode="create", anchor="items")


# -- placed by the column's name ------------------------------------------------


def test_exact_name_still_wins() -> None:
    loc = gen._by_name(_schema(), _plan(), "notes")
    assert loc == Location((), ("notes",), "exact")


def test_unique_column_holding_the_words_is_placed() -> None:
    loc = gen._by_name(_schema(), _plan(), "serial_code")
    assert loc is not None and loc.columns == ("_custom_serial_code_1",)


def test_two_candidates_are_not_placed() -> None:
    assert gen._by_name(_schema(), _plan(), "mac_address") is None


def test_no_candidate_or_a_key_column_is_not_placed() -> None:
    assert gen._by_name(_schema(), _plan(), "colour") is None
    # Words found only in a key-shaped column: never placed there.
    assert gen._by_name(_schema(), _plan(), "owner_ref") is None


def test_words_must_be_whole_and_in_order() -> None:
    assert gen._by_name(_schema(), _plan(), "address_mac") is None
    assert gen._by_name(_schema(), _plan(), "mac_addr") is None


# -- a text field left out of a create ------------------------------------------


def test_text_column_with_empty_or_no_default_may_be_left_out() -> None:
    t = _schema().tables["items"]
    assert gen._empty_default(t.column("email"))
    assert gen._empty_default(t.column("website"))
    # A non-empty default: the app fills it, so "left out" isn't "empty".
    assert not gen._empty_default(t.column("currency"))
    assert not gen._empty_default(t.column("visible"))


# -- "already there" ------------------------------------------------------------


def test_already_there_ignores_values_a_create_sets() -> None:
    s = _schema()
    assert (
        gen._already_there_role(s, "items", "number", ColumnRole("constant", "0014"))
        is None
    )
    assert gen._already_there_role(s, "items", "notes", ColumnRole("free")) is None
    assert (
        gen._already_there_role(s, "items", "currency", ColumnRole("constant", "USD"))
        is None
    )


def test_already_there_keeps_deleted_and_switched_off() -> None:
    s = _schema()
    kept = gen._already_there_role(s, "items", "deleted_at", ColumnRole("free"))
    assert kept is not None and kept.role == "free"
    flag = gen._already_there_role(s, "items", "visible", ColumnRole("constant", "1"))
    assert flag is not None and flag.value == "1"
    # Who made it may differ for a record a person entered.
    assert (
        gen._already_there_role(
            s, "items", "owner_id", ColumnRole("actor", detail={"param": "u"})
        )
        is None
    )


# -- value shapes ----------------------------------------------------------------


def test_plain_list_becomes_one_key_items() -> None:
    entry = {"list_items": {"tags": ["name"]}}
    out = gen.adapt_values(entry, {"tags": ["remote", "urgent"], "x": 1})
    assert out == {"tags": [{"name": "remote"}, {"name": "urgent"}], "x": 1}


def test_empty_list_is_nothing_given() -> None:
    out = gen.adapt_values({"list_items": {"tags": ["name"]}}, {"tags": []})
    assert out == {"tags": None}


def test_items_already_shaped_or_with_several_keys_pass_as_given() -> None:
    entry = {"list_items": {"lines": ["price", "sku"], "tags": ["name"]}}
    vals = {"lines": ["a"], "tags": [{"name": "a"}]}
    assert gen.adapt_values(entry, vals) == vals


# -- an update change given as null ---------------------------------------------


UPDATE = {
    "mode": "update",
    "fields": {"identity": ["subject"], "changes": ["due", "ratio"], "lists": []},
}


def test_null_change_with_no_other_change_empties_the_field() -> None:
    v = {"subject": "s", "due": None, "ratio": None}
    assert gen._cleared(UPDATE, v, {"subject"}) == ["due", "ratio"]


def test_null_beside_a_given_change_is_left_out() -> None:
    v = {"subject": "s", "due": None, "ratio": 3}
    assert gen._cleared(UPDATE, v, {"subject", "ratio"}) == []


def test_cleared_fits_only_an_update_change() -> None:
    spec = UPDATE["fields"]
    assert gen._fits(spec, {"subject"}, {}, ["due"])
    assert not gen._fits(spec, {"subject"}, {}, ["nope"])
    assert not gen._fits(spec, {"subject"}, {})
    create = {"required": ["a"], "optional": ["b"], "lists": []}
    assert not gen._fits(create, {"a"}, {}, ["b"])


def test_cleared_set_has_its_own_slug() -> None:
    a = gen.Variant("k", ["subject"], cleared=["due"])
    b = gen.Variant("k", ["subject"])
    assert a.slug != b.slug
