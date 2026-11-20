"""The synthetic business the environment starts from: our own data, no PII.

Every company, person, address, email and phone below is invented. Emails use
``example.com``; phones use the reserved 555-01xx range. The seed is posted
through Dolibarr's own REST API, so totals, references and links are the ones
the app computes itself.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable

US = 11  # Dolibarr's country id for the United States


def ts(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(hour=12, tzinfo=timezone.utc).timestamp())


# name, kind (1 customer, 2 prospect, 0 supplier only), town, zip, email, phone, supplier
COMPANIES = [
    ("Harbor Lane Bakery", 1, "Portland", "97201", "orders@harborlane.example.com", "503-555-0101", 0),
    ("Cobalt Ridge Outfitters", 1, "Denver", "80202", "ap@cobaltridge.example.com", "303-555-0102", 0),
    ("Willow Creek Dental", 1, "Austin", "78701", "office@willowcreekdental.example.com", "512-555-0103", 0),
    ("Juniper Street Books", 1, "Madison", "53703", "hello@juniperbooks.example.com", "608-555-0104", 0),
    ("Bluefin Marine Supply", 1, "Tampa", "33602", "billing@bluefinmarine.example.com", "813-555-0105", 0),
    ("Granite Peak Fitness", 1, "Boise", "83702", "accounts@granitepeak.example.com", "208-555-0106", 0),
    ("Larkspur Event Rentals", 1, "Sacramento", "95814", "events@larkspur.example.com", "916-555-0107", 0),
    ("Maple Hollow Farm Store", 1, "Burlington", "05401", "store@maplehollow.example.com", "802-555-0108", 0),
    ("Redstone Architects", 2, "Phoenix", "85004", "studio@redstonearch.example.com", "602-555-0109", 0),
    ("Silverline Courier Co", 1, "Columbus", "43215", "dispatch@silverline.example.com", "614-555-0110", 0),
    ("Tidewater Coffee Roasters", 1, "Norfolk", "23510", "wholesale@tidewater.example.com", "757-555-0111", 0),
    ("Oakmont Veterinary Clinic", 1, "Raleigh", "27601", "frontdesk@oakmontvet.example.com", "919-555-0112", 0),
    ("Pinecone Paper Mill", 0, "Duluth", "55802", "sales@pineconepaper.example.com", "218-555-0113", 1),
    ("Ironwood Hardware Distributors", 0, "Toledo", "43604", "orders@ironwoodhw.example.com", "419-555-0114", 1),
    ("Harbor Light Bistro", 2, "Portland", "97204", "manager@harborlight.example.com", "503-555-0115", 0),
    ("Cobalt Ridge Climbing Gym", 1, "Boulder", "80302", "front@cobaltgym.example.com", "720-555-0116", 0),
]

# company index, first, last, job, email, phone
CONTACTS = [
    (0, "Maria", "Lopez", "Owner", "maria.lopez@harborlane.example.com", "503-555-0121"),
    (0, "Dev", "Patel", "Head Baker", "dev.patel@harborlane.example.com", "503-555-0122"),
    (1, "Erin", "Walsh", "Accounts Payable", "erin.walsh@cobaltridge.example.com", "303-555-0123"),
    (2, "Samuel", "Okafor", "Office Manager", "s.okafor@willowcreekdental.example.com", "512-555-0124"),
    (3, "Priya", "Raman", "Buyer", "priya@juniperbooks.example.com", "608-555-0125"),
    (4, "Tom", "Becker", "Purchasing", "tom.becker@bluefinmarine.example.com", "813-555-0126"),
    (5, "Alicia", "Nguyen", "General Manager", "alicia@granitepeak.example.com", "208-555-0127"),
    (6, "Marcus", "Hale", "Operations", "marcus@larkspur.example.com", "916-555-0128"),
    (9, "Hannah", "Kim", "Dispatch Lead", "hannah.kim@silverline.example.com", "614-555-0129"),
    (10, "Maria", "Lopez", "Wholesale Manager", "m.lopez@tidewater.example.com", "757-555-0130"),
    (11, "Grace", "Abbott", "Practice Manager", "grace@oakmontvet.example.com", "919-555-0131"),
    (15, "Erin", "Walsh", "Front Desk", "erin@cobaltgym.example.com", "720-555-0132"),
]

# ref, label, type (0 product, 1 service), price, stock
PRODUCTS = [
    ("PAPER-A4-500", "Copy paper A4, 500 sheets", 0, "6.50", 240),
    ("PAPER-LTR-500", "Copy paper Letter, 500 sheets", 0, "6.25", 310),
    ("TONER-K-200", "Black toner cartridge 200", 0, "64.00", 18),
    ("TONER-C-200", "Cyan toner cartridge 200", 0, "71.00", 9),
    ("LABEL-4X6-1K", "Shipping labels 4x6, roll of 1000", 0, "22.00", 55),
    ("BOX-MED-25", "Corrugated boxes medium, bundle of 25", 0, "38.00", 40),
    ("TAPE-PK-36", "Packing tape, case of 36", 0, "54.00", 22),
    ("CHAIR-ERG-01", "Ergonomic office chair", 0, "289.00", 6),
    ("DESK-STD-60", "Standing desk 60 inch", 0, "540.00", 3),
    ("SVC-DELIVERY", "Local delivery", 1, "35.00", None),
    ("SVC-SETUP-HR", "On-site setup, per hour", 1, "85.00", None),
    ("SVC-PRINT-1K", "Custom print run, per 1000", 1, "120.00", None),
]

# invoices: company index, date, lines [(product index, qty)], state
# state: draft | open | paid | partial:<amount>
INVOICES = [
    (0, "2026-09-03", [(0, 10), (9, 1)], "paid"),
    (0, "2026-10-06", [(4, 2), (5, 3)], "open"),
    (1, "2026-09-15", [(7, 2)], "open"),
    (1, "2026-10-12", [(2, 1), (3, 1)], "partial:50.00"),
    (2, "2026-10-02", [(1, 20)], "paid"),
    (3, "2026-10-20", [(4, 1), (6, 1)], "open"),
    (4, "2026-10-22", [(5, 4), (6, 2), (9, 1)], "open"),
    (5, "2026-10-28", [(8, 1), (10, 3)], "draft"),
    (6, "2026-10-30", [(11, 2)], "open"),
    (9, "2026-11-02", [(4, 5)], "draft"),
    (10, "2026-11-03", [(0, 30), (1, 30)], "partial:100.00"),
    (11, "2026-11-05", [(2, 2)], "open"),
]

# orders: company index, date, lines, validate?
ORDERS = [
    (2, "2026-10-25", [(0, 15), (2, 1)], True),
    (5, "2026-11-01", [(7, 4)], True),
    (7, "2026-11-04", [(1, 10), (9, 1)], False),
    (10, "2026-11-06", [(4, 3)], True),
]

PROPOSALS = [
    (8, "2026-10-29", [(8, 2), (10, 4)], True),
    (14, "2026-11-02", [(11, 1)], False),
    (3, "2026-11-05", [(0, 40)], True),
]

PROJECTS = [
    ("PJ-HL-2026", "Harbor Lane store refit", 0),
    ("PJ-GP-2026", "Granite Peak member mailing", 5),
]

CATEGORIES = [("Wholesale", [10, 4, 0]), ("Retail", [3, 7]), ("Healthcare", [2, 11])]


class Api:
    def __init__(self, base: str, key: str) -> None:
        self.base, self.key = base.rstrip("/"), key

    def call(self, method: str, path: str, body: Any = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"DOLAPIKEY": self.key, "Content-Type": "application/json",
                                              "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 - loopback fixture
                raw = r.read().decode()
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"{method} {path}: {exc.code} {exc.read().decode()[:400]}") from exc
        return json.loads(raw) if raw.strip() else None


def _lines(lines: list[tuple[int, int]], product_ids: list[int]) -> list[dict[str, Any]]:
    out = []
    for pi, qty in lines:
        ref, label, kind, price, _ = PRODUCTS[pi]
        out.append({"fk_product": product_ids[pi], "qty": qty, "subprice": price, "tva_tx": 0,
                    "product_type": kind, "desc": label})
    return out


def load(base: str, key: str) -> dict[str, Any]:
    api = Api(base, key)
    socs = []
    for name, kind, town, zp, email, phone, supplier in COMPANIES:
        socs.append(int(api.call("POST", "/thirdparties", {
            "name": name, "client": kind, "fournisseur": supplier, "code_client": "-1" if kind else None,
            "code_fournisseur": "-1" if supplier else None, "town": town, "zip": zp,
            "address": "100 Example Way", "country_id": US, "email": email, "phone": phone, "status": 1})))
    for ci, first, last, job, email, phone in CONTACTS:
        api.call("POST", "/contacts", {"socid": socs[ci], "firstname": first, "lastname": last,
                                       "poste": job, "email": email, "phone_pro": phone, "statut": 1})
    wh = int(api.call("POST", "/warehouses", {"label": "Main Warehouse", "ref": "Main Warehouse",
                                              "lieu": "Synthetic Supplies HQ", "statut": 1}))
    prods = []
    for ref, label, kind, price, stock in PRODUCTS:
        pid = int(api.call("POST", "/products", {"ref": ref, "label": label, "type": kind, "price": price,
                                                 "price_base_type": "HT", "tva_tx": 0, "status": 1,
                                                 "status_buy": 1}))
        prods.append(pid)
        if stock:
            api.call("POST", "/stockmovements", {"product_id": pid, "warehouse_id": wh, "qty": stock,
                                                 "movementlabel": "Opening stock"})
    bank = int(api.call("POST", "/bankaccounts", {
        "ref": "OPERATING", "label": "Operating Account", "type": 1, "currency_code": "USD",
        "country_id": US, "date_solde": ts("2026-08-31"), "solde": 0, "clos": 0, "courant": 1,
        "bank": "Example Community Bank"}))
    for ci, day, lines, state in INVOICES:
        inv = int(api.call("POST", "/invoices", {"socid": socs[ci], "date": ts(day), "type": 0,
                                                 "cond_reglement_id": 2, "mode_reglement_id": 2,
                                                 "lines": _lines(lines, prods)}))
        if state == "draft":
            continue
        api.call("POST", f"/invoices/{inv}/validate", {"notrigger": 0})
        if state == "paid":
            total = api.call("GET", f"/invoices/{inv}")["total_ttc"]
            api.call("POST", f"/invoices/{inv}/payments", {
                "datepaye": ts(day) + 86400 * 10, "paymentid": 2, "closepaidinvoices": "yes",
                "accountid": bank, "num_payment": f"ACH-{inv:04d}", "comment": "Seed payment"})
            del total
        elif state.startswith("partial:"):
            amount = state.split(":", 1)[1]
            api.call("POST", "/invoices/paymentsdistributed", {
                "arrayofamounts": {str(inv): {"amount": amount, "multicurrency_amount": ""}},
                "datepaye": ts(day) + 86400 * 7, "paymentid": 2, "closepaidinvoices": "no",
                "accountid": bank, "num_payment": f"ACH-{inv:04d}", "comment": "Seed partial payment"})
    for ci, day, lines, validate in ORDERS:
        oid = int(api.call("POST", "/orders", {"socid": socs[ci], "date": ts(day),
                                               "lines": _lines(lines, prods)}))
        if validate:
            api.call("POST", f"/orders/{oid}/validate", {"idwarehouse": 0, "notrigger": 0})
    for ci, day, lines, validate in PROPOSALS:
        pid = int(api.call("POST", "/proposals", {"socid": socs[ci], "date": ts(day), "duree_validite": 30,
                                                  "lines": _lines(lines, prods)}))
        if validate:
            api.call("POST", f"/proposals/{pid}/validate", {"notrigger": 0})
    for ref, title, ci in PROJECTS:
        api.call("POST", "/projects", {"ref": ref, "title": title, "socid": socs[ci], "date_start": ts("2026-10-01"),
                                       "statut": 1, "usage_task": 1})
    for label, members in CATEGORIES:
        cid = int(api.call("POST", "/categories", {"label": label, "type": "customer"}))
        for ci in members:
            api.call("POST", f"/categories/{cid}/objects/customer/{socs[ci]}")
    return {"thirdparties": socs, "products": prods, "warehouse": wh, "bank": bank}


def before_sql(execute: Callable[..., Any]) -> None:
    """Company settings the seed's documents are created under."""

    # The company works in US dollars (the install default is the euro).
    execute("UPDATE llx_const SET value='USD' WHERE name='MAIN_MONNAIE'")


def finish_sql(execute: Callable[..., Any]) -> None:
    """Settings the UI tasks rely on, set once before the baseline."""

    # Leave the session-specific API key unset and keep the admin's own
    # sign-in counters out of the baseline's way.
    execute("UPDATE llx_user SET api_key=NULL")
    # Dolibarr's home page pings dolibarr.org once per install; the agent's
    # browser blocks it, and a failed ping writes a row. Mark it disabled.
    for name, value in (("MAIN_FIRST_PING_OK_DATE", "20261101000000"), ("MAIN_FIRST_PING_OK_ID", "disabled")):
        execute("INSERT INTO llx_const (name, entity, value, type, visible, note) VALUES (%s, 0, %s, 'chaine', 0, '') "
                "ON DUPLICATE KEY UPDATE value=VALUES(value)", (name, value))
