"""mitmproxy addon: the runner's only way out.

Every request the runner (or anything on the private network) sends toward the
internet passes here. The addon writes each one, in full (method, URL, headers
with credentials masked, body), to a JSONL file, and refuses any host that is
not on the allow-list. The end-to-end check then searches that file for the
canary values that exist only inside the private network.
"""

import base64
import json
import os
import time

from mitmproxy import http

ALLOWED = {
    h.strip()
    for h in os.environ.get("EGRESS_ALLOWED_HOSTS", "").split(",")
    if h.strip()
}
LOG = os.environ.get("EGRESS_LOG", "/log/egress.jsonl")
MASK = {"authorization", "x-run-token", "cookie", "x-api-key"}


def request(flow: http.HTTPFlow) -> None:
    host = flow.request.pretty_host
    allowed = host in ALLOWED
    body = flow.request.get_content() or b""
    entry = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "allowed": allowed,
        "method": flow.request.method,
        "host": host,
        "port": flow.request.port,
        "path": flow.request.path,
        "headers": {
            k: ("<masked>" if k.lower() in MASK else v)
            for k, v in flow.request.headers.items()
        },
        "body_b64": base64.b64encode(body).decode(),
        "body_bytes": len(body),
    }
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    if not allowed:
        flow.response = http.Response.make(
            403, b"egress refused by pilot proxy", {"content-type": "text/plain"}
        )
