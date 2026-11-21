"""Registry of the second held-out portal set.

    from benchmark.form_tx_heldout2.portals import PORTALS
    spec = PORTALS["ferncastle"]
    server, base_url = spec.start()          # host="127.0.0.1", port=0
    # POST {base_url}/__oracle/load_case  {"case_id": spec.cases[0]}
    # drive the browser from base_url + spec.entry_path
    # GET  {base_url}/__oracle/submissions -> scoring.score_case(case_id, records)
    server.shutdown()

``cases`` lists the scored cases in their required run order;
``dev_case`` is the one inspectable case. ``intake_vocabulary`` lists every
canonical intake key the portal's cases may carry (type, allowed values); it
holds no case data and is safe to read before freezing a layer.
"""

from __future__ import annotations

import json
from pathlib import Path

from .case_defs import CASES
from .common import CREDENTIALS, PortalSpec
from .ferncastle import Ferncastle
from .glasswing import Glasswing
from .okelund import Okelund
from .velloway import Velloway

_VOCAB = json.loads((Path(__file__).resolve().parent / "intake_vocabulary.json").read_text())


def _cases(portal: str) -> list[str]:
    return [c["case_id"] for c in CASES[portal] if c["split"] == "scored"]


def _dev(portal: str) -> str:
    return next(c["case_id"] for c in CASES[portal] if c["split"] == "dev")


def _login(portal: str, path: str, user_label: str, pass_label: str) -> dict:
    return {
        "required_at_start": True,
        "kind": "page",
        "path": path,
        "username_label": user_label,
        "password_label": pass_label,
        "username": CREDENTIALS[portal]["username"],
        "password": CREDENTIALS[portal]["password"],
        "credentials_file": "benchmark/form_tx_heldout2/fixtures/portal_logins.json",
    }


PORTALS: dict[str, PortalSpec] = {
    "ferncastle": PortalSpec(
        name="ferncastle",
        title="Ferncastle Indemnity Producer Desk",
        description="Rental-dwelling (landlord) quote in a tabbed producer workstation; tabs save through a JSON API, "
                    "client-lookup dialog, rate summary, in-page sign-in dialog when the session ends",
        factory=Ferncastle,
        entry_path="/desk/quote",
        login=_login("ferncastle", "/desk/login", "Producer code", "Password"),
        cases=_cases("ferncastle"),
        dev_case=_dev("ferncastle"),
        intake_vocabulary=_VOCAB["ferncastle"],
    ),
    "glasswing": PortalSpec(
        name="glasswing",
        title="Glasswing Home & Renters quote widget",
        description="Renters quote widget embedded in an iframe on a property manager's resident site; one custom element "
                    "with an open shadow root, lease prefill over postMessage, async ARIA address combobox, belongings "
                    "calculator dialog, review and quote screens",
        factory=Glasswing,
        entry_path="/resident",
        login=_login("glasswing", "/signin", "Email", "Password"),
        cases=_cases("glasswing"),
        dev_case=_dev("glasswing"),
        intake_vocabulary=_VOCAB["glasswing"],
    ),
    "okelund": PortalSpec(
        name="okelund",
        title="Okelund Casualty AgentNet personal auto",
        description="Legacy frameset (banner, navigation, main) around server-rendered table forms, one POST per page; "
                    "ZIP fills city and state as typed, vehicle lookup window, review and rated-quote pages, "
                    "sign-in page inside the main frame when the session ends",
        factory=Okelund,
        entry_path="/an/desk",
        login=_login("okelund", "/an/login", "Agent Code", "Password"),
        cases=_cases("okelund"),
        dev_case=_dev("okelund"),
        intake_vocabulary=_VOCAB["okelund"],
    ),
    "velloway": PortalSpec(
        name="velloway",
        title="Velloway Insurance personal umbrella",
        description="Accessible one-page umbrella application: fieldsets, day/month/year dates, error summary, async "
                    "ARIA occupation combobox, repeatable watercraft group, household-account prefill, read-only "
                    "summary with a confirmation checkbox, quote page",
        factory=Velloway,
        entry_path="/umbrella/start",
        login=_login("velloway", "/account/sign-in", "Email address", "Password"),
        cases=_cases("velloway"),
        dev_case=_dev("velloway"),
        intake_vocabulary=_VOCAB["velloway"],
    ),
}
