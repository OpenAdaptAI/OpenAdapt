"""Registry of the fourth held-out portal set.

    from benchmark.form_tx_heldout4.portals import PORTALS
    spec = PORTALS["wrenfield"]
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
from .corvane import Corvane
from .thistlebrook import Thistlebrook
from .wrenfield import Wrenfield

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
        "credentials_file": "benchmark/form_tx_heldout4/fixtures/portal_logins.json",
    }


PORTALS: dict[str, PortalSpec] = {
    "wrenfield": PortalSpec(
        name="wrenfield",
        title="Wrenfield Mutual condo quote",
        description="Condo unit-owners (HO-6) quote in a consumer single-page wizard with a stepper; steps save "
                    "through a JSON API, async ARIA association combobox, improvements estimator dialog, review "
                    "page read back from the server with per-section Edit, in-page sign-in dialog when the "
                    "session ends",
        factory=Wrenfield,
        entry_path="/condo/quote",
        login=_login("wrenfield", "/account/login", "Email", "Password"),
        cases=_cases("wrenfield"),
        dev_case=_dev("wrenfield"),
        intake_vocabulary=_VOCAB["wrenfield"],
    ),
    "corvane": PortalSpec(
        name="corvane",
        title="Corvane AgentLink motorcycle rating worksheet",
        description="Motorcycle quote in an agency rating worksheet: server-rendered accordion panels that each "
                    "save through a JSON call, add-motorcycle dialog whose VIN decoder opens a popup window that "
                    "writes back and closes itself, custom parts and equipment worksheet dialog, rated-quote page "
                    "before submission, sign-in page when the session ends",
        factory=Corvane,
        entry_path="/rater/worksheet",
        login=_login("corvane", "/agent/login", "Agent code", "Password"),
        cases=_cases("corvane"),
        dev_case=_dev("corvane"),
        intake_vocabulary=_VOCAB["corvane"],
    ),
    "thistlebrook": PortalSpec(
        name="thistlebrook",
        title="Thistlebrook Pet Health quote",
        description="Pet health quote in a consumer flow that shows one question card at a time with a progress "
                    "bar; ARIA radio chips, async ARIA breed combobox, plan builder, summary with a Change button "
                    "per screen, sign-in card inside the flow when the session ends",
        factory=Thistlebrook,
        entry_path="/quote",
        login=_login("thistlebrook", "/signin", "Email", "Password"),
        cases=_cases("thistlebrook"),
        dev_case=_dev("thistlebrook"),
        intake_vocabulary=_VOCAB["thistlebrook"],
    ),
}
