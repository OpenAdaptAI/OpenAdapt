"""Render a guard approval request with Desktop's real phone portal (app.js),
backed by the real PortalApp and Flow's console functions over CONSOLE_ROOT.
The browser's https://openadapt.clinic.example/* requests are answered
in-process by PortalApp.handle (no network). Headless."""
import json, sys
from pathlib import Path
from typing import Any
from openadapt_desktop.portal.flow_client import FlowResponse
from openadapt_desktop.portal.ingress import resolve_ingress
from openadapt_desktop.portal.pairing import DevicePairingStore
from openadapt_desktop.portal.server import PortalApp

ORIGIN = "https://openadapt.clinic.example"
root, out = Path(sys.argv[1]), sys.argv[2]

class Cfg:
    portal_ingress_mode = "customer_ingress"; portal_public_origin = ORIGIN
    portal_bind_host = ""; portal_ingress_acknowledged = True
    portal_port = 8443; portal_console_port = 7863
    def __init__(self, d): self.data_dir = d

class Console:
    """Same bodies the Flow console's /api/save-approvals routes return (console/app.py)."""
    def request(self, route: str, **kw: Any) -> FlowResponse:
        from openadapt_flow.console import save_approvals as sa
        if route == "save_approvals":
            return FlowResponse(200, [r.model_dump() for r in sa.list_save_approvals(root)], None, "application/json")
        if route == "save_approval_detail":
            return FlowResponse(200, sa.save_approval_detail(root, kw["request_id"]), None, "application/json")
        if route == "save_approval_frame":
            return FlowResponse(200, None, sa.save_approval_frame(root, kw["request_id"]), "image/png")
        if route == "tasks":
            return FlowResponse(200, [], None, "application/json")
        return FlowResponse(404, {"detail": "not in demo"}, None, "application/json")

import tempfile
pairings = DevicePairingStore(runner_id="runner_demo")
app = PortalApp(resolve_ingress(Cfg(Path(tempfile.mkdtemp()))), pairings, Console())
pairing = pairings.create(ORIGIN, reachable_from_phone=True)
claim = pairings.claim(pairing.secret, "Demo phone")
pairings.approve(claim["pairing_id"], claim["confirm_code"])
log = []
from playwright.sync_api import sync_playwright
with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    ctx = b.new_context(viewport={"width": 430, "height": 932}, service_workers="block")
    ctx.add_init_script(f"sessionStorage.setItem('portal_token', {json.dumps(claim['session_token'])}); sessionStorage.setItem('portal_csrf', {json.dumps(claim['csrf_token'])});")
    def handler(route):
        req = route.request
        path = req.url[len(ORIGIN):] or "/"
        headers = {k.lower(): v for k, v in req.headers.items()}
        headers["origin"] = ORIGIN
        if req.method == "GET": headers.pop("content-type", None)
        status, hdrs, body = app.handle(req.method, path, headers, (req.post_data_buffer or b""))
        log.append((req.method, path, status))
        route.fulfill(status=status, headers=dict(hdrs), body=body)
    ctx.route(ORIGIN + "/**", handler)
    p = ctx.new_page()
    p.goto(ORIGIN + "/"); p.wait_for_timeout(2500)
    p.screenshot(path=out, full_page=True)
    Path(out + ".dom.txt").write_text(p.inner_text("body"))
    b.close()
Path(out + ".requests.json").write_text(json.dumps(log, indent=1))
print(json.dumps(log))
