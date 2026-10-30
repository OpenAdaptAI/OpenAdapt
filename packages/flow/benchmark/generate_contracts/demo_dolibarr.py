"""Dolibarr (first study, instance 1): demonstrations, then the generator.

    FCG_INSTANCE=1 python benchmark/generate_contracts/demo_dolibarr.py WORKDIR

Makes one demonstration write per example in ``setup_view.yaml`` through
Dolibarr's REST API as the bot (an example whose record already exists is
skipped: the right outcome is no write), then runs ``propose``.
"""

from __future__ import annotations

import calendar
import sys
from pathlib import Path

import pymysql
import requests

from demo_lib import demo, fixture, propose, setup_view, write_inputs

APP = "dolibarr"


def main(work: Path) -> None:
    fx = fixture(APP)
    fx.reset()
    bot = fx.bot()
    ro = fx.read_access()
    read = {
        "sql_driver": "pymysql",
        "sql_connect_args": {"host": ro["host"], "port": ro["port"], "user": ro["user"], "database": ro["database"]},
        "sql_password_env": "FCG_DOLIBARR_READER_PASSWORD",
    }
    write_inputs(work, APP, read)
    db = pymysql.connect(host=ro["host"], port=ro["port"], user=ro["user"], password=ro["password"], database=ro["database"])

    def q(sql: str, args: tuple = ()) -> list[tuple]:
        with db.cursor() as cur:
            cur.execute(sql, args)
            return list(cur.fetchall())

    def api(method: str, path: str, payload: object = None) -> object:
        r = requests.request(method, bot["base_url"] + path, headers=bot["headers"], json=payload, timeout=60)
        r.raise_for_status()
        return r.json()

    def customer(name: str, town: str) -> int | None:
        db.commit()
        rows = q("SELECT rowid FROM llx_societe WHERE BINARY nom=%s AND BINARY town=%s", (name, town))
        return int(rows[0][0]) if len(rows) == 1 else None

    def country(label: str) -> int:
        return int(q("SELECT rowid FROM llx_c_country WHERE label=%s", (label,))[0][0])

    def product(ref: str) -> tuple[int, str, str]:
        db.commit()
        rid, price, tva = q("SELECT rowid, price, tva_tx FROM llx_product WHERE BINARY ref=%s", (ref,))[0]
        return int(rid), str(price), str(tva)

    for k in setup_view(APP)["kinds"]:
        for ex in k["examples"]:
            v = ex["values"]
            kind = k["kind"]
            if kind == "create_customer":
                if customer(v["name"], v["town"]) is not None:
                    # Already there (a "make sure" example): demonstrate a
                    # second create with values of our own instead.
                    v = {**v, "name": "Atelier Démo Générateur", "address": "8 place du Marché",
                         "zip": "21000", "town": "Dijon", "phone": "03 80 30 31 32",
                         "email": "atelier@demo-generateur.example"}
                payload = {"name": v["name"], "address": v["address"], "zip": v["zip"], "town": v["town"],
                           "country_id": country(v["country"]), "phone": v["phone"], "email": v["email"], "client": 1}  # fmt: skip
                demo(work, kind, v, lambda p=payload: api("POST", "/thirdparties", p))
            elif kind == "create_product":
                db.commit()
                if q("SELECT 1 FROM llx_product WHERE BINARY ref=%s", (v["ref"],)):
                    v = {**v, "ref": "GEN-DEMO-2", "label": "Coffret démo générateur",
                         "price_excl_vat": "12.40", "vat_rate": "20"}
                payload = {"ref": v["ref"], "label": v["label"], "type": 0, "price": v["price_excl_vat"],
                           "price_base_type": "HT", "tva_tx": v["vat_rate"], "status": 1, "status_buy": 1}  # fmt: skip
                demo(work, kind, v, lambda p=payload: api("POST", "/products", p))
            elif kind == "create_draft_invoice":
                y, m, d = (int(x) for x in v["invoice_date"].split("-"))
                lines = []
                for ln in v["lines"]:
                    pid, price, tva = product(ln["product_ref"])
                    lines.append({"fk_product": pid, "qty": ln["qty"], "subprice": price, "tva_tx": tva, "product_type": 0})
                payload = {"socid": customer(v["customer"], v["town"]), "type": 0,
                           "date": calendar.timegm((y, m, d, 12, 0, 0, 0, 0, 0)), "lines": lines}  # fmt: skip
                demo(work, kind, v, lambda p=payload: api("POST", "/invoices", p))
            elif kind == "update_customer":
                cid = customer(v["customer"], v["town"])
                change = {key[4:]: x for key, x in v.items() if key.startswith("new_") and x is not None}
                demo(work, kind, v, lambda c=cid, p=change: api("PUT", f"/thirdparties/{c}", p))
    bot_id = q("SELECT rowid FROM llx_user WHERE login='fcg_bot'")[0][0]
    report = propose(work, {"bot_user_id": str(bot_id)})
    print("questions:", len(report["questions"]))
    fx.reset()


if __name__ == "__main__":
    main(Path(sys.argv[1]))
