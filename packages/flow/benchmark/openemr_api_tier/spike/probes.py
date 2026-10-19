"""Step 4b: the cases where a recorded request is not enough on its own.

Each probe resets nothing by itself; ``main`` resets and seeds the fixture
once before and once after. Every probe patient is made up
(``probe-*@example.invalid``). For each probe we record what the server did
(HTTP status, whether a chart was written) and what the API step did.

    python -m benchmark.openemr_api_tier.spike.probes --out <file.json>
"""

from __future__ import annotations

import argparse
import copy
import json
import time
from pathlib import Path
from typing import Any

from benchmark.openemr_api_tier.common import (
    REFERRALS,
    reset_and_seed,
    spec_for,
    sql_patients,
)
from benchmark.openemr_api_tier.spike.bench import API_BUNDLE
from benchmark.openemr_api_tier.spike.formtier import SessionFormActuator, client_rules
from benchmark.openemr_api_tier.spike_fixture import SpikeFixture


def probe_params(tag: str, **over: str) -> dict[str, str]:
    p = spec_for(REFERRALS[0]).params()
    # A distinct made-up name per probe, so OpenEMR's fuzzy duplicate search
    # does not match one probe's chart against another's.
    p.update(fname=tag.capitalize(), lname=tag.capitalize() + "ova", email=f"probe-{tag}@example.invalid",
             DOB="1980-01-0" + str(len(tag) % 9 + 1))
    p.update(over)
    return p


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    from openadapt_flow.ir import Workflow

    f = SpikeFixture()
    vals = f._runtime_values()
    tier = json.loads((API_BUNDLE / "session_form.json").read_text())
    binding = Workflow.load(API_BUNDLE).steps[0].api_binding
    creds = dict(username=vals["OPENEMR_ACTOR_USER"], password=vals["OPENEMR_ACTOR_PASSWORD"])
    results: dict[str, Any] = {"fixture_before": reset_and_seed(f), "probes": []}

    def act(t: dict[str, Any] = tier) -> SessionFormActuator:
        return SessionFormActuator(f.ui_base_url, t, **creds)

    def count() -> int:
        return len(sql_patients(f))

    def post(a_: SessionFormActuator, body: list[tuple[str, str]]) -> Any:
        return a_.session.post(f.ui_base_url + binding.url_template, data=body, allow_redirects=False, timeout=30)

    def record(name: str, question: str, n0: int, **obs: Any) -> None:
        time.sleep(0.5)
        row = {"probe": name, "question": question, "new_charts": count() - n0, **obs}
        results["probes"].append(row)
        print(json.dumps(row), flush=True)

    def fresh(t: dict[str, Any] = tier) -> tuple[SessionFormActuator, dict[str, Any]]:
        """A newly signed-in session and its freshly read form."""
        a_ = act(t)
        a_.sign_in()
        controls, err = a_.read_form()
        assert err is None, err
        return a_, controls

    # 1. CSRF token from another session.
    a1, c1 = fresh()
    a2, c2 = fresh()
    tok1 = c1["csrf_token_form"]["default"]
    tok2 = c2["csrf_token_form"]["default"]
    body, _ = a2.build_body(binding, probe_params("stalecsrf"), c2)
    body = [(k, tok1 if k == "csrf_token_form" else v) for k, v in body]
    n0 = count()
    r = post(a2, body)
    record("stale_csrf", "Does a save with another session's CSRF token write?", n0, http=r.status_code,
           tokens_differ_between_sessions=tok1 != tok2, response_has_success=("set_pid=" in r.text),
           response_head=r.text[:120].replace("\n", " "))

    # 2. No CSRF token at all.
    body, _ = a2.build_body(binding, probe_params("nocsrf"), c2)
    body = [(k, v) for k, v in body if k != "csrf_token_form"]
    n0 = count()
    r = post(a2, body)
    record("missing_csrf", "Does a save without a CSRF token write?", n0, http=r.status_code,
           response_has_success=("set_pid=" in r.text))

    # 3. Token lifetime inside one session: same token after a save?
    a3, c3 = fresh()
    body, _ = a3.build_body(binding, probe_params("rotation"), c3)
    n0 = count()
    r = post(a3, body)
    c3b, _ = a3.read_form()
    record("token_rotation", "Does the CSRF token change after a save in the same session?", n0, http=r.status_code,
           token_same_after_save=c3["csrf_token_form"]["default"] == c3b["csrf_token_form"]["default"],
           response_has_success=("set_pid=" in r.text))

    # 4. Skip the duplicate check and post the r6 possible duplicate directly.
    a4, c4 = fresh()
    body, _ = a4.build_body(binding, spec_for(REFERRALS[5]).params(), c4)
    n0 = count()
    r = post(a4, body)
    record("skip_duplicate_check", "Does the server itself stop a possible duplicate (r6) when the "
           "duplicate-check request is skipped?", n0, http=r.status_code, response_has_success=("set_pid=" in r.text))

    # 5. The same save sent twice (what a naive retry would do).
    a5, c5 = fresh()
    body, _ = a5.build_body(binding, probe_params("doublesubmit"), c5)
    n0 = count()
    r1 = post(a5, body)
    r2 = post(a5, body)
    record("double_submit", "Does sending the same save twice write twice?", n0, http=[r1.status_code, r2.status_code])

    # 6. A session that never signed in (or whose session expired).
    body, _ = a5.build_body(binding, probe_params("nosession"), c5)
    n0 = count()
    r = post(act(), body)
    record("no_session", "Does a save without a signed-in session write, and what comes back?", n0, http=r.status_code,
           location=r.headers.get("location"), response_head=r.text[:160].replace("\n", " "))

    # 7. Through the actuator: the form gained a control since the recording.
    t7 = copy.deepcopy(tier)
    t7["form_page"]["controls"] = [c for c in t7["form_page"]["controls"] if c != "form_mname"]
    n0 = count()
    out = act(t7).actuate(binding, probe_params("formdrift"))
    record("form_controls_changed", "Does the API step refuse before sending when the form's fields differ "
           "from the recording?", n0, status=out.status.value, reason=out.reason)

    # 8. An admin makes Middle Name required in the layout.
    f._db("UPDATE openemr.layout_options SET uor=2 WHERE form_id='DEM' AND field_id='mname';")
    n0 = count()
    out = act().actuate(binding, probe_params("rulechange"))
    record("validation_rule_changed", "Middle Name made required in the layout: does the API step refuse "
           "instead of saving a record the browser form would have blocked?", n0, status=out.status.value,
           reason=out.reason)
    # Same change: what would the recorded rules say, and does the server enforce the new one?
    t8 = copy.deepcopy(tier)
    a8 = act(t8)
    a8.sign_in()
    t8["form_page"]["client_rules"] = client_rules(a8.session.get(f.ui_base_url + tier["form_page"]["path"]).text)
    c8, _ = a8.read_form()
    _, rule_err = a8.build_body(binding, probe_params("rulechange"), c8)
    body, _ = a8.build_body(binding, probe_params("rulechange"), c8, enforce_rules=False)
    n0 = count()
    r = post(a8, body)
    record("server_enforces_rule", "With Middle Name required in the layout, does the server reject a POST "
           "that leaves it empty?", n0, http=r.status_code, response_has_success=("set_pid=" in r.text),
           api_step_refusal=rule_err)
    f._db("UPDATE openemr.layout_options SET uor=1 WHERE form_id='DEM' AND field_id='mname';")

    # 9. A select label the form does not offer.
    n0 = count()
    out = act().actuate(binding, probe_params("badlabel", state_label="Atlantis"))
    record("unknown_select_label", "state_label not among the form's options: refuse before sending?", n0,
           status=out.status.value, reason=out.reason)

    results["fixture_after"] = reset_and_seed(f)
    Path(a.out).write_text(json.dumps(results, indent=1) + "\n")


if __name__ == "__main__":
    main()
