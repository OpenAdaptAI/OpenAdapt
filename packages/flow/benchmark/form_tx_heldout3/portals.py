"""Registry of the third held-out portal set.

    from benchmark.form_tx_heldout3.portals import PORTALS
    spec = PORTALS["corvane"]
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
from .pellbrook import Pellbrook

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
        "credentials_file": "benchmark/form_tx_heldout3/fixtures/portal_logins.json",
    }


PORTALS: dict[str, PortalSpec] = {
    "corvane": PortalSpec(
        name="corvane",
        title="Corvane Mutual AgentLink homeowners",
        description="Homeowners (HO-3) quote wizard for agents: browser-rendered steps with a left step list, each "
                    "step saved through a JSON API; customer-record and public-property-record prefill, lender "
                    "directory type-ahead, replacement-cost estimator dialog, review step, sign-in page when the "
                    "session ends",
        factory=Corvane,
        entry_path="/quote",
        login=_login("corvane", "/login", "Agency code", "Password"),
        cases=_cases("corvane"),
        dev_case=_dev("corvane"),
        intake_vocabulary=_VOCAB["corvane"],
    ),
    "pellbrook": PortalSpec(
        name="pellbrook",
        title="Pellbrook Marine boat quote",
        description="Consumer boat and watercraft quote as a conversation: one server-chosen question per card, "
                    "choice chips, conditional questions, returning-customer prefill, book-value lookup, "
                    "summary with a Change button per answer, quote page",
        factory=Pellbrook,
        entry_path="/boat/quote",
        login=_login("pellbrook", "/signin", "Email", "Password"),
        cases=_cases("pellbrook"),
        dev_case=_dev("pellbrook"),
        intake_vocabulary=_VOCAB["pellbrook"],
    ),
}
