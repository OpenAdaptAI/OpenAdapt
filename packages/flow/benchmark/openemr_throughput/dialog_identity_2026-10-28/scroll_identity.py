"""Identity verdict at OpenEMR's confirm step, form behind scrolled to N positions."""

import json
import os
import sys
from pathlib import Path

from benchmark.openemr_throughput import harness
from openadapt_flow.ir import Resolution, Workflow
from openadapt_flow.runtime import Replayer

demo = harness._demo_module()
fx = harness.make_fixture(
    Path(os.path.expanduser("~/oa/scratch/throughput/fixture/state"))
)
M = Path(os.path.expanduser("~/oa/scratch/real-app-edge-cases"))
bundles = {
    name: (M / f"bundle-{name}", Workflow.load(M / f"bundle-{name}"))
    for name in ("main", "branch")
}
spec_cls = __import__(
    "openadapt_flow.benchmark.openemr_local", fromlist=["SyntheticPatientSpec"]
).SyntheticPatientSpec
params = spec_cls().params()
scrolls = [int(v) for v in sys.argv[1].split(",")]
rows = []
for y in scrolls:
    backend, close = demo.OpenEMRPlaywrightBackend.launch(fx.ui_base_url, headless=True)
    try:
        demo._preauthenticate_browser(backend, fx)
        page = backend.page
        form = demo._form_frame(page)
        form.locator("#form_fname").fill(os.environ.get("P_FNAME", "Wilhelmina"))
        form.locator("#form_lname").fill(os.environ.get("P_LNAME", "Okonkwo-Hale"))
        form.locator("#form_DOB").fill(os.environ.get("P_DOB", "1961-04-09"))
        form.locator("#form_DOB").press("Escape")
        form.locator("#form_sex").select_option(label="Female")
        form.evaluate(f"() => window.scrollTo(0, {y})")
        form.locator("#create").evaluate("el => el.click()")
        point = demo._center(demo._confirm_create_locator(page))
        png = backend.screenshot()
        row = {"scroll": y}
        for name, (bdir, wf) in bundles.items():
            step = wf.steps[-1]
            rep = Replayer(demo._BasePlaywrightBackend(page))
            res = Resolution(
                rung="structural", point=point, confidence=1.0, elapsed_ms=0.0
            )
            check = rep._verify_identity(step, res, png, params, wf, bdir)
            row[name] = check.status
            row[name + "_observed"] = check.observed[:80]
        rows.append(row)
        print(json.dumps(row), flush=True)
    finally:
        close()
out = M / os.environ.get("PERTURB_OUT", "perturb.json")
out.write_text(json.dumps(rows, indent=1))
for name in ("main", "branch"):
    print(
        name, "false stops:", sum(r[name] != "verified" for r in rows), "of", len(rows)
    )
