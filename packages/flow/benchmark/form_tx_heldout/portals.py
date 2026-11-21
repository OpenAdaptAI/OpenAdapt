"""Registry of the held-out portals.

    from benchmark.form_tx_heldout.portals import PORTALS
    spec = PORTALS["harborline"]
    server, base_url = spec.start()          # host="127.0.0.1", port=0
    # POST {base_url}/__oracle/load_case  {"case_id": spec.cases[0]}
    # drive the browser from base_url + spec.entry_path
    # GET  {base_url}/__oracle/submissions -> scoring.score_case(case_id, records)
    server.shutdown()

``cases`` lists the scored cases in their required run order;
``dev_case`` is the one inspectable case.
"""

from __future__ import annotations

from .case_defs import CASES
from .common import CREDENTIALS, PortalSpec
from .harborline import Harborline


def _cases(portal: str) -> list[str]:
    return [c["case_id"] for c in CASES[portal] if c["split"] == "scored"]


def _dev(portal: str) -> str:
    return next(c["case_id"] for c in CASES[portal] if c["split"] == "dev")


def _login(portal: str, path: str, user_label: str, pass_label: str) -> dict:
    return {
        "required_at_start": True,
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
    ),
}
