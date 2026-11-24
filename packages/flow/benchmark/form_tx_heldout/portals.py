"""Registry of the held-out portals.

    from benchmark.form_tx_heldout.portals import PORTALS
    spec = PORTALS["harborline"]
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
from .cobalt import Cobalt
from .harborline import Harborline
from .pinecrest import Pinecrest
from .tidewater import Tidewater


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
        "credentials_file": "benchmark/form_tx_heldout/fixtures/portal_logins.json",
    }


PORTALS: dict[str, PortalSpec] = {
    "harborline": PortalSpec(
        name="harborline",
        title="Harborline Mutual AgentLink",
        description="Legacy table-layout homeowners wizard, 4 steps plus review, server-rendered, pop-up estimator window",
        factory=Harborline,
        entry_path="/quote/new",
        login=_login("harborline", "/agent/login", "User ID", "Password"),
        cases=_cases("harborline"),
        dev_case=_dev("harborline"),
        intake_vocabulary=_VOCAB["harborline"],
    ),
    "cobalt": PortalSpec(
        name="cobalt",
        title="Cobalt Ridge Insurance auto quote",
        description="Modern single-page auto quote, div/ARIA widgets, async type-ahead listboxes, 4 steps plus review",
        factory=Cobalt,
        entry_path="/quote",
        login={**_login("cobalt", "", "Email", "Password"), "required_at_start": False, "path": None,
               "kind": "dialog_on_session_expiry"},
        cases=_cases("cobalt"),
        dev_case=_dev("cobalt"),
        intake_vocabulary=_VOCAB["cobalt"],
    ),
    "pinecrest": PortalSpec(
        name="pinecrest",
        title="Pinecrest Assurance agent workspace",
        description="Homeowners quote app inside an iframe, full re-render on every change, modal estimator dialog, 3 steps plus review",
        factory=Pinecrest,
        entry_path="/portal",
        login=_login("pinecrest", "/login", "Username", "Password"),
        cases=_cases("pinecrest"),
        dev_case=_dev("pinecrest"),
        intake_vocabulary=_VOCAB["pinecrest"],
    ),
    "tidewater": PortalSpec(
        name="tidewater",
        title="Tidewater Bay agency quick quote",
        description="Home plus auto bundle quick quote, server-rendered pages built from shadow-DOM custom elements, 3 steps plus review",
        factory=Tidewater,
        entry_path="/qq/start",
        login=_login("tidewater", "/signin", "Agent ID", "Passphrase"),
        cases=_cases("tidewater"),
        dev_case=_dev("tidewater"),
        intake_vocabulary=_VOCAB["tidewater"],
    ),
}
