"""Dolibarr: a sales assistant creates a customer (Third parties > New third party)."""

from __future__ import annotations

from typing import Any

from benchmark.api_generalization.apps.doli import DolibarrBase, dsql
from benchmark.api_generalization.common import (
    ExecSqlVerifier,
    rec_click,
    rec_select2_click,
    rec_text,
)
from openadapt_flow.runtime.effects.effect import Effect, EffectKind, ValueExpr

QUERY = "SELECT rowid AS id, nom AS name, town, email, phone FROM llx_societe"
JUDGE = ("SELECT rowid AS id, nom AS name, town, email, phone, client, status, fk_typent, entity "
         "FROM llx_societe ORDER BY rowid")


class App(DolibarrBase):
    name = "dolibarr_thirdparty"
    workflow = "new-customer"
    form_path = "/societe/card.php?action=create"
    save_path_regex = r"^/societe/card\.php$"
    unique_param = "name"
    normal_values = [
        dict(name="Harbor Office Supply", town="Exampleton", email="orders@harbor.example.invalid", phone="555-0171"),
        dict(name="Northgate Catering", town="Sampleville", email="hello@northgate.example.invalid", phone="555-0172"),
        dict(name="Blue Fern Cleaning", town="Testford", email="info@bluefern.example.invalid", phone="555-0173"),
    ]
    blank_values = dict(name="Plain Name Traders", town="", email="", phone="")
    dup_values = dict(name="Mirror Town", town="Mirror Town", email="mirror@example.invalid", phone="555-0174")
    invalid_values = dict(name="Bad Mail Traders", town="Testford", email="no-at-sign.example.invalid", phone="555-0175")
    invalid_note = "email without @ (the server rejects it)"

    def record(self, rec: Any, page: Any, v: dict[str, str]) -> None:
        rec_text(rec, page, "#name", v["name"], "name")
        rec_select2_click(rec, page, "customerprospect", "Customer")
        rec_text(rec, page, "#town", v["town"], "town")
        rec_text(rec, page, "#phone", v["phone"], "phone")
        rec_text(rec, page, "#email", v["email"], "email")
        rec_click(rec, page, "input[name=save]")

    def rows(self) -> list[dict[str, Any]]:
        return dsql(JUDGE)

    def verifier(self) -> Any:
        return ExecSqlVerifier(dsql, QUERY, timeout_s=10.0)

    def expect_fields(self, v: dict[str, str]) -> dict[str, str]:
        return {"name": v["name"], "town": v["town"], "email": v["email"], "phone": v["phone"],
                "client": "1", "status": "1"}

    def effects(self) -> list[Effect]:
        match = {"name": ValueExpr(param="name")}
        out = [Effect(kind=EffectKind.RECORD_WRITTEN, match=match, expected_count=1, risk="reversible",
                      probe="exactly one third party with this name", timeout_s=8.0)]
        for f in ("town", "email", "phone"):
            out.append(Effect(kind=EffectKind.FIELD_EQUALS, match=match, field=f, value=ValueExpr(param=f),
                              risk="reversible", probe=f"third party {f} read-back", timeout_s=8.0))
        return out
