"""Dolibarr: a sales assistant adds a contact (Third parties > New contact/address)."""

from __future__ import annotations

from typing import Any

from benchmark.api_generalization.apps.doli import DolibarrBase, dsql
from benchmark.api_generalization.common import ExecSqlVerifier, rec_click, rec_text
from openadapt_flow.runtime.effects.effect import Effect, EffectKind, ValueExpr

QUERY = ("SELECT rowid AS id, lastname, firstname, poste, email, phone AS phone_pro FROM llx_socpeople")
JUDGE = ("SELECT p.rowid AS id, p.lastname, p.firstname, p.poste, p.email, p.phone, p.fk_soc, p.civility, "
         "COALESCE(c.code, '') AS country, p.priv, p.statut, p.entity FROM llx_socpeople p "
         "LEFT JOIN llx_c_country c ON c.rowid = p.fk_pays ORDER BY p.rowid")


class App(DolibarrBase):
    name = "dolibarr_contact"
    workflow = "new-contact"
    form_path = "/contact/card.php?action=create"
    save_path_regex = r"^/contact/card\.php$"
    unique_param = "lastname"
    normal_values = [
        dict(lastname="Okafor", firstname="Chidi", poste="Purchasing lead", email="c.okafor@example.invalid", phone_pro="555-0161"),
        dict(lastname="Lindgren", firstname="Maja", poste="Accounts payable", email="m.lindgren@example.invalid", phone_pro="555-0162"),
        dict(lastname="Tanaka", firstname="Hiro", poste="Site manager", email="h.tanaka@example.invalid", phone_pro="555-0163"),
    ]
    blank_values = dict(lastname="Moreau", firstname="", poste="", email="", phone_pro="")
    dup_values = dict(lastname="Quinn", firstname="Quinn", poste="Quinn", email="quinn@example.invalid", phone_pro="555-0164")
    invalid_values = dict(lastname="Bad Mail", firstname="Ivo", poste="Buyer", email="ivo.at.example.invalid", phone_pro="555-0165")
    invalid_note = "email without @ (the server rejects it)"
    default_column = "country"
    changed_default_value = "FR"

    def record(self, rec: Any, page: Any, v: dict[str, str]) -> None:
        rec_text(rec, page, "#lastname", v["lastname"], "lastname")
        rec_text(rec, page, "#firstname", v["firstname"], "firstname")
        rec_text(rec, page, "#title", v["poste"], "poste")
        rec_text(rec, page, "#phone_pro", v["phone_pro"], "phone_pro")
        rec_text(rec, page, "#email", v["email"], "email")
        rec_click(rec, page, "input[name=add]")

    def change_default(self) -> None:
        # The company's own country is the new contact's default country.
        dsql("INSERT INTO llx_const (name, entity, value, type, visible) "
             "VALUES ('MAIN_INFO_SOCIETE_COUNTRY', 1, '1:FR:France', 'chaine', 0)")

    def restore_default(self) -> None:
        dsql("DELETE FROM llx_const WHERE name = 'MAIN_INFO_SOCIETE_COUNTRY'")

    def rows(self) -> list[dict[str, Any]]:
        return dsql(JUDGE)

    def verifier(self) -> Any:
        return ExecSqlVerifier(dsql, QUERY, timeout_s=10.0)

    def expect_fields(self, v: dict[str, str]) -> dict[str, str]:
        return {"lastname": v["lastname"], "firstname": v["firstname"], "poste": v["poste"],
                "email": v["email"], "phone": v["phone_pro"], "country": ""}

    def effects(self) -> list[Effect]:
        match = {"lastname": ValueExpr(param="lastname")}
        out = [Effect(kind=EffectKind.RECORD_WRITTEN, match=match, expected_count=1, risk="reversible",
                      probe="exactly one contact with this last name", timeout_s=8.0)]
        for f in ("firstname", "poste", "email", "phone_pro"):
            out.append(Effect(kind=EffectKind.FIELD_EQUALS, match=match, field=f, value=ValueExpr(param=f),
                              risk="reversible", probe=f"contact {f} read-back", timeout_s=8.0))
        return out
