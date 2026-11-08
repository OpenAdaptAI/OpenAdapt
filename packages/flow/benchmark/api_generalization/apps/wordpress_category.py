"""WordPress: an editor adds a post category (wp-admin, Posts > Categories)."""

from __future__ import annotations

from typing import Any

from benchmark.api_generalization.apps.wp import WordPressBase, wpsql
from benchmark.api_generalization.common import (
    ExecSqlVerifier,
    rec_click,
    rec_select,
    rec_text,
)
from openadapt_flow.runtime.effects.effect import Effect, EffectKind, ValueExpr

QUERY = ("SELECT t.term_id AS id, t.name, tt.description, COALESCE(p.name, 'None') AS parent "
         "FROM wp_terms t JOIN wp_term_taxonomy tt ON tt.term_id = t.term_id "
         "LEFT JOIN wp_terms p ON p.term_id = tt.parent WHERE tt.taxonomy = 'category'")
JUDGE = ("SELECT t.term_id AS id, t.name, t.slug, tt.taxonomy, tt.description, tt.parent, "
         "COALESCE(p.name, 'None') AS parent_name, tt.count FROM wp_terms t "
         "JOIN wp_term_taxonomy tt ON tt.term_id = t.term_id LEFT JOIN wp_terms p ON p.term_id = tt.parent "
         "ORDER BY t.term_id")


class App(WordPressBase):
    name = "wordpress_category"
    workflow = "add-category"
    form_path = "/wp-admin/edit-tags.php?taxonomy=category"
    save_path_regex = r"^/wp-admin/admin-ajax\.php$"
    save_body_require = r"(^|&)action=add-tag(&|$)"
    unique_param = "name"
    normal_values = [
        dict(name="Community Events", parent="None", description="Fairs, meetups and open days."),
        dict(name="Road Works", parent="Uncategorized", description="Closures and detours."),
        dict(name="Library Hours", parent="None", description="Opening times and holidays."),
    ]
    blank_values = dict(name="Notices", parent="None", description="")
    dup_values = dict(name="Same Text", parent="None", description="Same Text")
    invalid_values = dict(name="Uncategorized", parent="None", description="A name that already exists.")
    invalid_note = "a category name that already exists at that level (the server rejects it)"

    def record(self, rec: Any, page: Any, v: dict[str, str]) -> None:
        rec_text(rec, page, "#tag-name", v["name"], "name")
        rec_select(rec, page, "#parent", v["parent"], "parent")
        rec_text(rec, page, "#tag-description", v["description"], "description")
        rec_click(rec, page, "#submit")
        page.wait_for_timeout(1500)

    def rows(self) -> list[dict[str, Any]]:
        return wpsql(JUDGE)

    def verifier(self) -> Any:
        return ExecSqlVerifier(wpsql, QUERY, timeout_s=10.0)

    def expect_fields(self, v: dict[str, str]) -> dict[str, str]:
        return {"name": v["name"], "taxonomy": "category", "description": v["description"],
                "parent_name": v["parent"]}

    def effects(self) -> list[Effect]:
        match = {"name": ValueExpr(param="name")}
        out = [Effect(kind=EffectKind.RECORD_WRITTEN, match=match, expected_count=1, risk="reversible",
                      probe="exactly one category with this name", timeout_s=8.0)]
        for f in ("description", "parent"):
            out.append(Effect(kind=EffectKind.FIELD_EQUALS, match=match, field=f, value=ValueExpr(param=f),
                              risk="reversible", probe=f"category {f} read-back", timeout_s=8.0))
        return out
