"""A command executor for the tests: saves through the API, trusts the answer.

It follows the command contract in executors.Command: the task comes in
environment variables and the report goes to $SILENT_SAVES_REPORT.
"""

import json
import os
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

url = urlparse(os.environ["SILENT_SAVES_URL"])
params = json.loads(os.environ["SILENT_SAVES_PARAMS"])
body = json.dumps({"patient_id": "p1", "type": "Triage", "note": params["note"]})
req = Request(
    f"{url.scheme}://{url.netloc}/api/encounter",
    data=body.encode(),
    method="POST",
    headers={"Content-Type": "application/json"},
)
try:
    with urlopen(req, timeout=10) as resp:
        ok = resp.status == 200
except HTTPError:
    ok = False
with open(os.environ["SILENT_SAVES_REPORT"], "w") as f:
    json.dump({"claimed_success": ok, "final_text": "saved" if ok else "rejected"}, f)
