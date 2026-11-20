"""Realistic stateful web portals for end-to-end ``form_tx`` evaluation.

Provides two distinct, zero-mock multi-screen web applications backed by real
server state and runnable either over ``ThreadingHTTPServer`` (loopback TCP) or
attached directly to a Playwright ``BrowserContext`` via ``context.route`` (for
sandboxed environments where Chromium TCP sockets are disabled):

1. ``StatefulUnderwritingPortal``:
   - Multi-step quoting wizard with server-driven Partial Page Rendering (PPR),
     busy overlays, and volatile re-minted DOM element IDs on every reload.
   - Conditional question cascades (``has_pool = yes`` spawns ``pool_fenced``
     and ``has_trampoline``; ``roof_year < 2010`` spawns ``roof_update_type``).
   - External bureau prefill injection (injects an unlisted household spouse row
     and/or prior loss count unless reviewed).
   - Child popup window (``window.open('/estimator/popup')``) with a
     body-portaled custom combobox that computes replacement cost and syncs it
     back to the parent wizard.
   - Independent Quote Summary read-back page with normalized formatting.

2. ``LegacyHospitalQueuePortal`` (Held-Out Application):
   - 2FA login wall + session TTL + read-only ``/api/keepalive`` endpoint.
   - Cascading async XHR body-portaled combobox (``Department`` -> ``Doctor``).
   - Duplicate-phone confirmation ``<dialog open>`` modal when registering a
     family member sharing an existing phone number.
   - Mints a unique patient identifier (``HMS-2026-XXXXX``) and exposes an
     independent ``/hms/search`` read-back lookup screen.
"""

from __future__ import annotations

import html
import json
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional


@dataclass
class PortalResponse:
    status: int = 200
    content_type: str = "text/html; charset=utf-8"
    body: str = ""
    headers: dict[str, str] = field(default_factory=dict)


def _parse_form_or_json(raw_body: str) -> dict[str, Any]:
    if not raw_body:
        return {}
    stripped = raw_body.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    parsed = urllib.parse.parse_qs(raw_body, keep_blank_values=True)
    return {k: v[-1] if v else "" for k, v in parsed.items()}


def _format_us_phone(raw: str) -> str:
    digits = "".join(ch for ch in str(raw) if ch.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return raw.strip()


def _format_usd(dollars: int | float) -> str:
    return f"${dollars:,.2f}"


class BasePortalApp:
    """Common HTTP/Playwright-route dispatch base for stateful test portals."""

    base_origin: str = "http://portal.local"

    def handle_request(
        self,
        method: str,
        path_with_query: str,
        headers: dict[str, str],
        body: str,
    ) -> PortalResponse:
        raise NotImplementedError

    def attach_to_playwright_context(
        self, context: Any, origin: Optional[str] = None
    ) -> str:
        """Bind this portal onto a Playwright ``BrowserContext`` via ``route``."""
        bound_origin = (origin or self.base_origin).rstrip("/")
        cookie_jar: dict[str, str] = {}

        def _route_handler(route: Any) -> None:
            req = route.request
            parsed = urllib.parse.urlsplit(req.url)
            path_q = parsed.path or "/"
            if parsed.query:
                path_q = f"{path_q}?{parsed.query}"
            req_headers = {k.lower(): v for k, v in req.headers.items()}
            if cookie_jar and "cookie" not in req_headers:
                req_headers["cookie"] = "; ".join(
                    f"{k}={v}" for k, v in cookie_jar.items()
                )
            post_body = req.post_data or ""
            resp = self.handle_request(
                req.method.upper(), path_q, req_headers, post_body
            )
            out_headers = dict(resp.headers)
            set_cookie = out_headers.get("Set-Cookie") or out_headers.get("set-cookie")
            if set_cookie:
                pair = set_cookie.split(";", 1)[0]
                if "=" in pair:
                    ck, cv = pair.split("=", 1)
                    cookie_jar[ck.strip()] = cv.strip()
            route.fulfill(
                status=resp.status,
                content_type=resp.content_type,
                headers=out_headers,
                body=resp.body,
            )

        context.route(f"{bound_origin}/**", _route_handler)
        return bound_origin

    def start_http_server(self) -> tuple[ThreadingHTTPServer, str]:
        """Start a live ``ThreadingHTTPServer`` on loopback for non-sandboxed runs."""
        portal = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _dispatch(self) -> None:
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(length).decode("utf-8") if length > 0 else ""
                hdrs = {k.lower(): v for k, v in self.headers.items()}
                resp = portal.handle_request(self.command.upper(), self.path, hdrs, raw)
                encoded = resp.body.encode("utf-8")
                self.send_response(resp.status)
                self.send_header("Content-Type", resp.content_type)
                self.send_header("Content-Length", str(len(encoded)))
                for hk, hv in resp.headers.items():
                    self.send_header(hk, hv)
                self.end_headers()
                self.wfile.write(encoded)

            do_GET = _dispatch  # noqa: N815
            do_POST = _dispatch  # noqa: N815
            do_PUT = _dispatch  # noqa: N815

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


# =============================================================================
# Portal 1: Stateful Underwriting Wizard + Popup Replacement Estimator
# =============================================================================


class StatefulUnderwritingPortal(BasePortalApp):
    """Multi-screen quoting wizard with PPR reloads, prefill injection, and popup estimator."""

    base_origin = "http://underwriting.portal.local"

    def __init__(
        self,
        *,
        ppr_delay_ms: int = 120,
        inject_unlisted_spouse: bool = False,
        injected_spouse_name: str = "Jordan Avery",
        injected_spouse_dob: str = "1986-04-19",
        prefill_prior_claims: Optional[str] = None,
    ) -> None:
        self.ppr_delay_ms = ppr_delay_ms
        self.inject_unlisted_spouse = inject_unlisted_spouse
        self.injected_spouse_name = injected_spouse_name
        self.injected_spouse_dob = injected_spouse_dob
        self.prefill_prior_claims = prefill_prior_claims
        self._lock = threading.Lock()
        self._render_seq = 0
        self.reset_draft()
        self.committed_quotes: dict[str, dict[str, Any]] = {}
        self.mutation_log: list[tuple[str, str]] = []

    def reset_draft(self) -> None:
        household: list[dict[str, str]] = []
        if self.inject_unlisted_spouse:
            household.append(
                {
                    "row_id": "hh_prefill_spouse",
                    "full_name": self.injected_spouse_name,
                    "relationship": "Spouse",
                    "dob": self.injected_spouse_dob,
                }
            )
        self.draft: dict[str, Any] = {
            "first_name": "",
            "last_name": "",
            "dob": "",
            "phone": "",
            "marital_status": "",
            "prior_claims_count": (
                self.prefill_prior_claims
                if self.prefill_prior_claims is not None
                else ""
            ),
            "household_members": household,
            "street_address": "",
            "year_built": "",
            "roof_year": "",
            "roof_update_type": "",
            "has_pool": "",
            "pool_fenced": "",
            "has_trampoline": "",
            "living_area_sqft": "",
            "stories": "",
            "finish_grade": "",
            "replacement_cost_dollars": 0,
            "deductible": "",
            "liability_limit": "",
        }
        self._banner_error = ""

    def _next_id(self, suffix: str) -> str:
        self._render_seq += 1
        return f"dyn_{self._render_seq}_{suffix}"

    def handle_request(
        self,
        method: str,
        path_with_query: str,
        headers: dict[str, str],
        body: str,
    ) -> PortalResponse:
        parsed = urllib.parse.urlsplit(path_with_query)
        path = parsed.path

        with self._lock:
            if (
                method in {"POST", "PUT", "PATCH", "DELETE"}
                and path == "/api/quotes/commit"
            ):
                self.mutation_log.append((method, path))

            if path in {"/", "/wizard/applicant"} and method == "GET":
                return PortalResponse(body=self._render_applicant_screen())

            if path == "/api/applicant/save" and method == "POST":
                data = _parse_form_or_json(body)
                for k in (
                    "first_name",
                    "last_name",
                    "dob",
                    "phone",
                    "marital_status",
                    "prior_claims_count",
                ):
                    if k in data:
                        self.draft[k] = str(data[k]).strip()
                missing = [
                    label
                    for key, label in (
                        ("first_name", "First Name"),
                        ("last_name", "Last Name"),
                        ("dob", "Date of Birth"),
                        ("phone", "Primary Phone"),
                        ("marital_status", "Marital Status"),
                        ("prior_claims_count", "Prior Losses (5 Years)"),
                    )
                    if not self.draft.get(key)
                ]
                if missing:
                    self._banner_error = (
                        f"Missing required applicant fields: {', '.join(missing)}"
                    )
                    return PortalResponse(
                        content_type="application/json",
                        body=json.dumps({"ok": False, "error": self._banner_error}),
                    )
                # Ensure primary applicant is in household table if absent
                primary_name = (
                    f"{self.draft['first_name']} {self.draft['last_name']}".strip()
                )
                if not any(
                    m["row_id"] == "hh_primary" for m in self.draft["household_members"]
                ):
                    self.draft["household_members"].insert(
                        0,
                        {
                            "row_id": "hh_primary",
                            "full_name": primary_name.title(),
                            "relationship": "Primary Insured",
                            "dob": self.draft["dob"],
                        },
                    )
                self._banner_error = ""
                return PortalResponse(
                    content_type="application/json",
                    body=json.dumps({"ok": True, "next_url": "/wizard/property"}),
                )

            if path == "/api/household/dismiss" and method == "POST":
                data = _parse_form_or_json(body)
                rid = data.get("row_id", "")
                self.draft["household_members"] = [
                    m for m in self.draft["household_members"] if m["row_id"] != rid
                ]
                return PortalResponse(
                    content_type="application/json",
                    body=json.dumps(
                        {"ok": True, "remaining": len(self.draft["household_members"])}
                    ),
                )

            if path == "/wizard/property" and method == "GET":
                return PortalResponse(body=self._render_property_screen())

            if path == "/api/property/ppr" and method == "POST":
                if self.ppr_delay_ms > 0:
                    time.sleep(self.ppr_delay_ms / 1000.0)
                data = _parse_form_or_json(body)
                for k in (
                    "street_address",
                    "year_built",
                    "roof_year",
                    "roof_update_type",
                    "has_pool",
                    "pool_fenced",
                    "has_trampoline",
                ):
                    if k in data:
                        self.draft[k] = str(data[k]).strip()
                if self.draft["has_pool"].lower() != "yes":
                    self.draft["pool_fenced"] = ""
                    self.draft["has_trampoline"] = ""
                return PortalResponse(
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "ok": True,
                            "fields_html": self._render_property_fields_fragment(),
                        }
                    ),
                )

            if path == "/api/property/next" and method == "POST":
                data = _parse_form_or_json(body)
                for k in (
                    "street_address",
                    "year_built",
                    "roof_year",
                    "roof_update_type",
                    "has_pool",
                    "pool_fenced",
                    "has_trampoline",
                ):
                    if k in data:
                        self.draft[k] = str(data[k]).strip()
                errors: list[str] = []
                for req_k, label in (
                    ("street_address", "Property Street Address"),
                    ("year_built", "Year Built"),
                    ("roof_year", "Roof Year Installed"),
                    ("has_pool", "Swimming Pool on Premises"),
                ):
                    if not self.draft.get(req_k):
                        errors.append(f"{label} is required")
                try:
                    ry = (
                        int(self.draft["roof_year"])
                        if self.draft["roof_year"]
                        else 9999
                    )
                except ValueError:
                    ry = 9999
                if ry < 2010 and not self.draft.get("roof_update_type"):
                    errors.append(
                        "Roof Update Type is required for roofs installed before 2010"
                    )
                if self.draft.get("has_pool", "").lower() == "yes":
                    if not self.draft.get("pool_fenced"):
                        errors.append("Pool Fenced with Self-Latching Gate is required")
                    if not self.draft.get("has_trampoline"):
                        errors.append(
                            "Trampoline or Diving Apparatus disclosure is required"
                        )
                if int(self.draft.get("replacement_cost_dollars") or 0) <= 0:
                    errors.append(
                        "Replacement Cost Estimator must be completed before continuing"
                    )
                if errors:
                    self._banner_error = "; ".join(errors)
                    return PortalResponse(
                        content_type="application/json",
                        body=json.dumps({"ok": False, "error": self._banner_error}),
                    )
                self._banner_error = ""
                return PortalResponse(
                    content_type="application/json",
                    body=json.dumps({"ok": True, "next_url": "/wizard/coverage"}),
                )

            if path == "/estimator/popup" and method == "GET":
                return PortalResponse(body=self._render_estimator_popup())

            if path == "/api/estimator/calculate" and method == "POST":
                data = _parse_form_or_json(body)
                sqft = int(
                    str(data.get("living_area_sqft", "0") or "0").replace(",", "")
                )
                stories = str(data.get("stories", "1") or "1")
                grade = str(data.get("finish_grade", "Standard") or "Standard")
                if sqft <= 0 or not grade:
                    return PortalResponse(
                        status=400,
                        content_type="application/json",
                        body=json.dumps(
                            {
                                "ok": False,
                                "error": "Living area and finish grade required",
                            }
                        ),
                    )
                mult = {"standard": 210, "custom": 265, "luxury": 340}.get(
                    grade.lower(), 210
                )
                story_bonus = 15000 if stories in {"2", "2 Stories"} else 0
                rc = sqft * mult + story_bonus
                self.draft["living_area_sqft"] = str(sqft)
                self.draft["stories"] = stories
                self.draft["finish_grade"] = grade
                self.draft["replacement_cost_dollars"] = rc
                return PortalResponse(
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "ok": True,
                            "replacement_cost_dollars": rc,
                            "formatted": _format_usd(rc),
                        }
                    ),
                )

            if path == "/wizard/coverage" and method == "GET":
                return PortalResponse(body=self._render_coverage_screen())

            if path == "/api/quotes/commit" and method == "POST":
                data = _parse_form_or_json(body)
                for k in ("deductible", "liability_limit"):
                    if k in data:
                        self.draft[k] = str(data[k]).strip()
                if not self.draft["deductible"] or not self.draft["liability_limit"]:
                    return PortalResponse(
                        content_type="application/json",
                        body=json.dumps(
                            {
                                "ok": False,
                                "error": "Deductible and Liability Limit are required",
                            }
                        ),
                    )
                rc = int(self.draft["replacement_cost_dollars"])
                ded = int(
                    str(self.draft["deductible"]).replace("$", "").replace(",", "")
                )
                if ded > rc * 0.05:
                    return PortalResponse(
                        content_type="application/json",
                        body=json.dumps(
                            {
                                "ok": False,
                                "error": "Underwriting rule: Deductible cannot exceed 5% of Replacement Cost",
                            }
                        ),
                    )
                quote_no = f"QT-2026-{10000 + len(self.committed_quotes) + 1}"
                base_prem = round(rc * 0.0035)
                pool_surcharge = 185 if self.draft["has_pool"].lower() == "yes" else 0
                claims_surcharge = 240 * int(self.draft["prior_claims_count"] or "0")
                ded_discount = -120 if ded >= 2500 else 0
                annual_premium = max(
                    450, base_prem + pool_surcharge + claims_surcharge + ded_discount
                )
                saved = dict(self.draft)
                saved["quote_number"] = quote_no
                saved["annual_premium_dollars"] = annual_premium
                self.committed_quotes[quote_no] = saved
                return PortalResponse(
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "ok": True,
                            "quote_number": quote_no,
                            "summary_url": f"/quotes/summary/{quote_no}",
                        }
                    ),
                )

            if path.startswith("/quotes/summary/") and method == "GET":
                quote_no = path.rsplit("/", 1)[-1]
                saved = self.committed_quotes.get(quote_no)
                if not saved:
                    return PortalResponse(status=404, body="<h1>Quote not found</h1>")
                return PortalResponse(body=self._render_quote_summary(saved))

        return PortalResponse(status=404, body="Not found")

    def _render_applicant_screen(self) -> str:
        fn_id = self._next_id("fname")
        ln_id = self._next_id("lname")
        dob_id = self._next_id("dob")
        ph_id = self._next_id("phone")
        ms_id = self._next_id("marital")
        cl_id = self._next_id("claims")

        rows_html = []
        for idx, member in enumerate(self.draft["household_members"]):
            rid = html.escape(member["row_id"])
            dismiss_btn = (
                f'<button type="button" class="dismiss-hh" data-dismiss-row="{rid}" '
                f"onclick=\"dismissMember('{rid}')\">Remove</button>"
                if rid != "hh_primary"
                else ""
            )
            rows_html.append(
                f'<tr data-entity-row="{idx}" data-row-id="{rid}">'
                f'<td data-col="full_name">{html.escape(member["full_name"])}</td>'
                f'<td data-col="relationship">{html.escape(member["relationship"])}</td>'
                f'<td data-col="dob">{html.escape(member["dob"])}</td>'
                f"<td>{dismiss_btn}</td></tr>"
            )

        return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Quote Wizard - Applicant</title></head>
<body data-authenticated-shell="true" data-screen-id="applicant">
  <h1>New Personal Property Quote</h1>
  <div id="banner" role="alert" data-validation-banner="true" style="display:none"></div>
  <fieldset>
    <legend>Applicant Information</legend>
    <div><label for="{fn_id}">Applicant First Name *</label>
      <input id="{fn_id}" name="first_name" required value="{html.escape(self.draft['first_name'])}"></div>
    <div><label for="{ln_id}">Applicant Last Name *</label>
      <input id="{ln_id}" name="last_name" required value="{html.escape(self.draft['last_name'])}"></div>
    <div><label for="{dob_id}">Date of Birth *</label>
      <input id="{dob_id}" name="dob" type="date" required value="{html.escape(self.draft['dob'])}"></div>
    <div><label for="{ph_id}">Primary Phone *</label>
      <input id="{ph_id}" name="phone" required value="{html.escape(self.draft['phone'])}"></div>
    <div><label for="{ms_id}">Marital Status *</label>
      <select id="{ms_id}" name="marital_status" required>
        <option value="">-- Select --</option>
        <option value="Single" {"selected" if self.draft["marital_status"] == "Single" else ""}>Single</option>
        <option value="Married" {"selected" if self.draft["marital_status"] == "Married" else ""}>Married</option>
      </select></div>
    <div><label for="{cl_id}">Prior Losses (5 Years) *</label>
      <input id="{cl_id}" name="prior_claims_count" required value="{html.escape(str(self.draft['prior_claims_count']))}"></div>
  </fieldset>
  <fieldset>
    <legend>Prefilled Household Members</legend>
    <table id="household-table" data-entity-table="household_member">
      <thead><tr><th>Name</th><th>Relationship</th><th>DOB</th><th>Action</th></tr></thead>
      <tbody>{"".join(rows_html)}</tbody>
    </table>
  </fieldset>
  <button id="btn-applicant-next" type="button" onclick="saveApplicant()">Next: Property Details</button>
  <script>
    async function dismissMember(rowId) {{
      await fetch('/api/household/dismiss', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{row_id: rowId}})
      }});
      const tr = document.querySelector(`tr[data-row-id="${{rowId}}"]`);
      if (tr) tr.remove();
    }}
    async function saveApplicant() {{
      const payload = {{}};
      for (const el of document.querySelectorAll('input[name], select[name]')) {{
        payload[el.name] = el.value;
      }}
      const res = await fetch('/api/applicant/save', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify(payload)
      }});
      const data = await res.json();
      if (!data.ok) {{
        const b = document.getElementById('banner');
        b.textContent = data.error;
        b.style.display = 'block';
        return;
      }}
      window.location.href = data.next_url;
    }}
  </script>
</body></html>"""

    def _render_property_fields_fragment(self) -> str:
        st_id = self._next_id("street")
        yb_id = self._next_id("yrbuilt")
        ry_id = self._next_id("roofyr")
        pl_id = self._next_id("pool")
        rc_val = (
            _format_usd(self.draft["replacement_cost_dollars"])
            if self.draft["replacement_cost_dollars"]
            else ""
        )

        extra_roof = ""
        try:
            ry_num = int(self.draft["roof_year"]) if self.draft["roof_year"] else 9999
        except ValueError:
            ry_num = 9999
        if ry_num < 2010:
            ru_id = self._next_id("roofupd")
            cur_ru = self.draft["roof_update_type"]
            extra_roof = f"""
            <div class="conditional-block">
              <label for="{ru_id}">Roof Update Type *</label>
              <select id="{ru_id}" name="roof_update_type" required>
                <option value="">-- Select --</option>
                <option value="Full Replacement" {"selected" if cur_ru == "Full Replacement" else ""}>Full Replacement</option>
                <option value="Partial Repair" {"selected" if cur_ru == "Partial Repair" else ""}>Partial Repair</option>
                <option value="None" {"selected" if cur_ru == "None" else ""}>None</option>
              </select>
            </div>"""

        extra_pool = ""
        if self.draft["has_pool"].lower() == "yes":
            pf_id = self._next_id("poolfenced")
            tr_id = self._next_id("trampoline")
            cur_pf = self.draft["pool_fenced"]
            cur_tr = self.draft["has_trampoline"]
            extra_pool = f"""
            <div class="conditional-block">
              <label for="{pf_id}">Pool Fenced with Self-Latching Gate *</label>
              <select id="{pf_id}" name="pool_fenced" required>
                <option value="">-- Select --</option>
                <option value="Yes" {"selected" if cur_pf == "Yes" else ""}>Yes</option>
                <option value="No" {"selected" if cur_pf == "No" else ""}>No</option>
              </select>
            </div>
            <div class="conditional-block">
              <label for="{tr_id}">Trampoline or Diving Apparatus on Premises *</label>
              <select id="{tr_id}" name="has_trampoline" required>
                <option value="">-- Select --</option>
                <option value="Yes" {"selected" if cur_tr == "Yes" else ""}>Yes</option>
                <option value="No" {"selected" if cur_tr == "No" else ""}>No</option>
              </select>
            </div>"""

        cur_pool = self.draft["has_pool"]
        return f"""
        <div><label for="{st_id}">Property Street Address *</label>
          <input id="{st_id}" name="street_address" required value="{html.escape(self.draft['street_address'])}"></div>
        <div><label for="{yb_id}">Year Built *</label>
          <input id="{yb_id}" name="year_built" required value="{html.escape(self.draft['year_built'])}"></div>
        <div><label for="{ry_id}">Roof Year Installed *</label>
          <input id="{ry_id}" name="roof_year" required value="{html.escape(self.draft['roof_year'])}" onchange="triggerPpr()"></div>
        {extra_roof}
        <div><label for="{pl_id}">Swimming Pool on Premises *</label>
          <select id="{pl_id}" name="has_pool" required onchange="triggerPpr()">
            <option value="">-- Select --</option>
            <option value="Yes" {"selected" if cur_pool == "Yes" else ""}>Yes</option>
            <option value="No" {"selected" if cur_pool == "No" else ""}>No</option>
          </select></div>
        {extra_pool}
        <div>
          <label for="replacement_cost_display">Estimated Replacement Cost *</label>
          <input id="replacement_cost_display" name="replacement_cost_display" readonly value="{html.escape(rc_val)}">
          <button id="btn-open-estimator" type="button" onclick="openEstimator()">Launch Replacement Cost Estimator</button>
        </div>"""

    def _render_property_screen(self) -> str:
        fragment = self._render_property_fields_fragment()
        return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Quote Wizard - Property</title></head>
<body data-authenticated-shell="true" data-screen-id="property">
  <h1>Property &amp; Underwriting Details</h1>
  <div id="ppr-busy" data-busy="false" style="display:none">Refreshing underwriting questions...</div>
  <div id="banner" role="alert" data-validation-banner="true" style="display:none"></div>
  <fieldset id="property-fieldset">
    <legend>Dwelling Characteristics</legend>
    <div id="ppr-container">{fragment}</div>
  </fieldset>
  <button id="btn-property-next" type="button" onclick="submitProperty()">Next: Coverage &amp; Quote</button>
  <script>
    function collectFields() {{
      const payload = {{}};
      for (const el of document.querySelectorAll('#ppr-container input[name], #ppr-container select[name]')) {{
        if (!el.readOnly) payload[el.name] = el.value;
      }}
      return payload;
    }}
    async function triggerPpr() {{
      const busy = document.getElementById('ppr-busy');
      busy.setAttribute('data-busy', 'true');
      busy.setAttribute('aria-busy', 'true');
      busy.style.display = 'block';
      const payload = collectFields();
      const res = await fetch('/api/property/ppr', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify(payload)
      }});
      const data = await res.json();
      document.getElementById('ppr-container').innerHTML = data.fields_html;
      busy.setAttribute('data-busy', 'false');
      busy.removeAttribute('aria-busy');
      busy.style.display = 'none';
    }}
    function openEstimator() {{
      window.open('/estimator/popup', 'ReplacementEstimator', 'width=640,height=520');
    }}
    window.addEventListener('message', (ev) => {{
      if (ev.data && ev.data.type === 'ESTIMATOR_RESULT') {{
        const out = document.getElementById('replacement_cost_display');
        if (out) out.value = ev.data.formatted;
      }}
    }});
    async function submitProperty() {{
      const res = await fetch('/api/property/next', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify(collectFields())
      }});
      const data = await res.json();
      if (!data.ok) {{
        const b = document.getElementById('banner');
        b.textContent = data.error;
        b.style.display = 'block';
        return;
      }}
      window.location.href = data.next_url;
    }}
  </script>
</body></html>"""

    def _render_estimator_popup(self) -> str:
        return """<!doctype html>
<html><head><meta charset="utf-8"><title>Replacement Cost Estimator</title></head>
<body data-screen-id="estimator_popup">
  <h2>Dwelling Replacement Cost Calculator</h2>
  <fieldset>
    <legend>Structure Valuation Inputs</legend>
    <div><label for="est_sqft">Living Area (Sq Ft) *</label>
      <input id="est_sqft" name="living_area_sqft" required value=""></div>
    <div><label for="est_stories">Number of Stories *</label>
      <select id="est_stories" name="stories" required>
        <option value="">-- Select --</option>
        <option value="1">1 Story</option>
        <option value="2">2 Stories</option>
      </select></div>
    <div>
      <label id="lbl_grade" for="est_grade_btn">Interior Finish Grade *</label>
      <button id="est_grade_btn" type="button" role="combobox" aria-labelledby="lbl_grade"
        aria-expanded="false" data-selected-value="" onclick="toggleGradeMenu()">Select...</button>
    </div>
  </fieldset>
  <button id="btn-calc-apply" type="button" onclick="calcAndApply()">Calculate &amp; Apply to Quote</button>
  <script>
    function toggleGradeMenu() {
      let existing = document.getElementById('grade-portal-listbox');
      if (existing) { existing.remove(); return; }
      const btn = document.getElementById('est_grade_btn');
      btn.setAttribute('aria-expanded', 'true');
      setTimeout(() => {
        const ul = document.createElement('ul');
        ul.id = 'grade-portal-listbox';
        ul.setAttribute('role', 'listbox');
        ul.setAttribute('data-portal-listbox', 'true');
        for (const opt of ['Standard', 'Custom', 'Luxury']) {
          const li = document.createElement('li');
          li.setAttribute('role', 'option');
          li.setAttribute('data-option-value', opt);
          li.textContent = opt + ' Finish';
          li.onclick = () => {
            btn.textContent = opt + ' Finish';
            btn.setAttribute('data-selected-value', opt);
            btn.setAttribute('aria-expanded', 'false');
            ul.remove();
          };
          ul.appendChild(li);
        }
        document.body.appendChild(ul);
      }, 40);
    }
    async function calcAndApply() {
      const sqft = document.getElementById('est_sqft').value;
      const stories = document.getElementById('est_stories').value;
      const grade = document.getElementById('est_grade_btn').getAttribute('data-selected-value') || '';
      const res = await fetch('/api/estimator/calculate', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({living_area_sqft: sqft, stories: stories, finish_grade: grade})
      });
      const data = await res.json();
      if (data.ok) {
        if (window.opener) {
          window.opener.postMessage({type: 'ESTIMATOR_RESULT', formatted: data.formatted, cents: data.replacement_cost_dollars * 100}, '*');
        }
        window.close();
      }
    }
  </script>
</body></html>"""

    def _render_coverage_screen(self) -> str:
        rc = _format_usd(self.draft["replacement_cost_dollars"])
        return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Quote Wizard - Coverage</title></head>
<body data-authenticated-shell="true" data-screen-id="coverage">
  <h1>Coverage Selection</h1>
  <div id="banner" role="alert" data-validation-banner="true" style="display:none"></div>
  <fieldset>
    <legend>Policy Coverages</legend>
    <div><label for="cov_a">Coverage A - Dwelling Limit</label>
      <input id="cov_a" name="coverage_a" readonly value="{html.escape(rc)}"></div>
    <div><label for="cov_ded">All-Peril Deductible *</label>
      <select id="cov_ded" name="deductible" required>
        <option value="">-- Select --</option>
        <option value="1000">$1,000</option>
        <option value="2500">$2,500</option>
        <option value="5000">$5,000</option>
        <option value="50000">$50,000</option>
      </select></div>
    <div><label for="cov_liab">Personal Liability Limit *</label>
      <select id="cov_liab" name="liability_limit" required>
        <option value="">-- Select --</option>
        <option value="300000">$300,000</option>
        <option value="500000">$500,000</option>
      </select></div>
  </fieldset>
  <button id="btn-issue-quote" type="button" onclick="issueQuote()">Generate Official Quote</button>
  <script>
    async function issueQuote() {{
      const ded = document.getElementById('cov_ded').value;
      const liab = document.getElementById('cov_liab').value;
      const res = await fetch('/api/quotes/commit', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{deductible: ded, liability_limit: liab}})
      }});
      const data = await res.json();
      if (!data.ok) {{
        const b = document.getElementById('banner');
        b.textContent = data.error;
        b.style.display = 'block';
        return;
      }}
      window.location.href = data.summary_url;
    }}
  </script>
</body></html>"""

    def _render_quote_summary(self, saved: dict[str, Any]) -> str:
        hh_names = ", ".join(m["full_name"] for m in saved["household_members"])
        return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Quote Summary - {html.escape(saved['quote_number'])}</title></head>
<body data-authenticated-shell="true" data-screen-id="quote_summary" data-summary-ready="true">
  <h1>Bound-Ready Quote Summary</h1>
  <fieldset>
    <legend>Quote Outputs</legend>
    <div><label for="sum_quote_no">Quote Number</label>
      <input id="sum_quote_no" readonly value="{html.escape(saved['quote_number'])}"></div>
    <div><label for="sum_premium">Annual Premium</label>
      <input id="sum_premium" readonly value="{html.escape(_format_usd(saved['annual_premium_dollars']))}"></div>
    <div><label for="sum_rc">Replacement Cost</label>
      <input id="sum_rc" readonly value="{html.escape(_format_usd(saved['replacement_cost_dollars']))}"></div>
  </fieldset>
  <fieldset>
    <legend>Rated Risk &amp; Coverage Verification</legend>
    <div><label for="sum_fname">Applicant First Name</label>
      <input id="sum_fname" readonly value="{html.escape(saved['first_name'].title())}"></div>
    <div><label for="sum_lname">Applicant Last Name</label>
      <input id="sum_lname" readonly value="{html.escape(saved['last_name'].title())}"></div>
    <div><label for="sum_dob">Date of Birth</label>
      <input id="sum_dob" readonly value="{html.escape(saved['dob'])}"></div>
    <div><label for="sum_phone">Primary Phone</label>
      <input id="sum_phone" readonly value="{html.escape(_format_us_phone(saved['phone']))}"></div>
    <div><label for="sum_street">Property Street Address</label>
      <input id="sum_street" readonly value="{html.escape(saved['street_address'].upper())}"></div>
    <div><label for="sum_pool">Swimming Pool on Premises</label>
      <input id="sum_pool" readonly value="{html.escape(saved['has_pool'])}"></div>
    <div><label for="sum_trampoline">Trampoline or Diving Apparatus on Premises</label>
      <input id="sum_trampoline" readonly value="{html.escape(saved['has_trampoline'] or 'N/A')}"></div>
    <div><label for="sum_claims">Prior Losses (5 Years)</label>
      <input id="sum_claims" readonly value="{html.escape(str(saved['prior_claims_count']))}"></div>
    <div><label for="sum_ded">All-Peril Deductible</label>
      <input id="sum_ded" readonly value="{html.escape(_format_usd(int(saved['deductible'])))}"></div>
    <div><label for="sum_marital">Marital Status</label>
      <input id="sum_marital" readonly value="{html.escape(str(saved['marital_status']))}"></div>
    <div><label for="sum_year_built">Year Built</label>
      <input id="sum_year_built" readonly value="{html.escape(str(saved['year_built']))}"></div>
    <div><label for="sum_roof_year">Roof Year</label>
      <input id="sum_roof_year" readonly value="{html.escape(str(saved['roof_year']))}"></div>
    <div><label for="sum_roof_update">Roof Update Type</label>
      <input id="sum_roof_update" readonly value="{html.escape(str(saved['roof_update_type']))}"></div>
    <div><label for="sum_pool_fenced">Pool Fenced</label>
      <input id="sum_pool_fenced" readonly value="{html.escape(str(saved['pool_fenced']))}"></div>
    <div><label for="sum_liability">Liability Limit</label>
      <input id="sum_liability" readonly value="{html.escape(str(saved['liability_limit']))}"></div>
    <div><label for="sum_hh">Household Members</label>
      <input id="sum_hh" readonly value="{html.escape(hh_names)}"></div>
  </fieldset>
</body></html>"""


# =============================================================================
# Portal 2 (Held-Out): Legacy Hospital Queue & Patient Registration Portal
# =============================================================================


class LegacyHospitalQueuePortal(BasePortalApp):
    """Held-out hospital HMS portal with 2FA auth wall, portal combobox, and UHID search."""

    base_origin = "http://hms.portal.local"

    DOCTORS_BY_DEPT: dict[str, tuple[str, ...]] = {
        "Cardiology": ("Dr. Kavita Rao", "Dr. Arjun Mehta"),
        "Orthopedics": ("Dr. Vikram Iyer", "Dr. Neha Kulkarni"),
        "Pediatrics": ("Dr. Ananya Sharma", "Dr. Rohan Nair"),
    }

    def __init__(
        self,
        *,
        require_auth: bool = True,
        valid_otp: str = "482910",
        existing_phone_records: Optional[dict[str, str]] = None,
    ) -> None:
        self.require_auth = require_auth
        self.valid_otp = valid_otp
        self._authenticated_tokens: set[str] = set()
        self.keepalive_pings: int = 0
        self.mutation_log: list[tuple[str, str]] = []
        self.patients_by_uhid: dict[str, dict[str, str]] = {}
        self.phone_index: dict[str, str] = dict(
            existing_phone_records or {"9876543210": "HMS-2025-00104 (Ramesh Nair)"}
        )
        self._lock = threading.Lock()

    def expire_all_sessions(self) -> None:
        with self._lock:
            self._authenticated_tokens.clear()

    def grant_authenticated_session(self) -> str:
        with self._lock:
            tok = uuid.uuid4().hex
            self._authenticated_tokens.add(tok)
            return tok

    def _is_authed(self, headers: dict[str, str]) -> bool:
        if not self.require_auth:
            return True
        cookie = headers.get("cookie", "")
        for part in cookie.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                if k == "hms_session" and v in self._authenticated_tokens:
                    return True
        return False

    def handle_request(
        self,
        method: str,
        path_with_query: str,
        headers: dict[str, str],
        body: str,
    ) -> PortalResponse:
        parsed = urllib.parse.urlsplit(path_with_query)
        path = parsed.path
        query = dict(urllib.parse.parse_qsl(parsed.query))

        with self._lock:
            if method in {"POST", "PUT", "PATCH", "DELETE"} and path not in {"/login"}:
                self.mutation_log.append((method, path))

            if path == "/login" and method == "GET":
                return PortalResponse(body=self._render_login_page())

            if path == "/login" and method == "POST":
                data = _parse_form_or_json(body)
                if data.get("otp") == self.valid_otp and data.get("username"):
                    tok = uuid.uuid4().hex
                    self._authenticated_tokens.add(tok)
                    return PortalResponse(
                        content_type="application/json",
                        headers={"Set-Cookie": f"hms_session={tok}; Path=/"},
                        body=json.dumps({"ok": True, "next_url": "/hms/register"}),
                    )
                return PortalResponse(
                    status=401,
                    content_type="application/json",
                    body=json.dumps(
                        {"ok": False, "error": "Invalid credentials or 2FA OTP"}
                    ),
                )

            if not self._is_authed(headers):
                if path.startswith("/api/"):
                    return PortalResponse(
                        status=401,
                        content_type="application/json",
                        body=json.dumps({"ok": False, "auth_required": True}),
                    )
                return PortalResponse(body=self._render_login_page())

            if path == "/api/keepalive" and method == "GET":
                self.keepalive_pings += 1
                return PortalResponse(
                    content_type="application/json",
                    body=json.dumps({"ok": True, "pings": self.keepalive_pings}),
                )

            if path in {"/", "/hms/register"} and method == "GET":
                return PortalResponse(body=self._render_register_page())

            if path == "/api/doctors" and method == "GET":
                dept = query.get("dept", "")
                docs = self.DOCTORS_BY_DEPT.get(dept, ())
                time.sleep(0.05)
                return PortalResponse(
                    content_type="application/json",
                    body=json.dumps({"doctors": list(docs)}),
                )

            if path == "/api/hms/register" and method == "POST":
                data = _parse_form_or_json(body)
                req_fields = (
                    "patient_name",
                    "dob",
                    "sex",
                    "mobile",
                    "visit_type",
                    "department",
                    "consulting_doctor",
                )
                missing = [f for f in req_fields if not str(data.get(f, "")).strip()]
                if missing:
                    return PortalResponse(
                        content_type="application/json",
                        body=json.dumps(
                            {
                                "ok": False,
                                "error": f"Required HMS fields missing: {', '.join(missing)}",
                            }
                        ),
                    )
                digits = "".join(ch for ch in str(data["mobile"]) if ch.isdigit())[-10:]
                existing_match = self.phone_index.get(digits)
                if (
                    existing_match
                    and str(data.get("confirm_family_override", "")).lower() != "true"
                ):
                    return PortalResponse(
                        content_type="application/json",
                        body=json.dumps(
                            {
                                "ok": False,
                                "duplicate_modal": True,
                                "message": (
                                    f"Mobile {digits} matches existing chart {existing_match}. "
                                    "Confirm separate family registration?"
                                ),
                            }
                        ),
                    )
                uhid = f"HMS-2026-{20000 + len(self.patients_by_uhid) + 1}"
                record = {
                    "uhid": uhid,
                    "patient_name": str(data["patient_name"]).strip().title(),
                    "dob": str(data["dob"]).strip(),
                    "sex": str(data["sex"]).strip().upper(),
                    "mobile": digits,
                    "visit_type": str(data["visit_type"]).strip(),
                    "department": str(data["department"]).strip(),
                    "consulting_doctor": str(data["consulting_doctor"]).strip(),
                }
                self.patients_by_uhid[uhid] = record
                self.phone_index[digits] = f"{uhid} ({record['patient_name']})"
                return PortalResponse(
                    content_type="application/json",
                    body=json.dumps({"ok": True, "uhid": uhid}),
                )

            if path == "/hms/search" and method == "GET":
                uhid = query.get("uhid", "").strip()
                rec = self.patients_by_uhid.get(uhid) if uhid else None
                return PortalResponse(body=self._render_search_page(uhid, rec))

        return PortalResponse(status=404, body="Not found")

    def _render_login_page(self) -> str:
        return """<!doctype html>
<html><head><meta charset="utf-8"><title>HMS Staff Sign-In (2FA)</title></head>
<body data-auth-wall="true">
  <h1>Hospital Management System - Staff 2FA Login</h1>
  <div><label for="hms_user">Staff Username</label><input id="hms_user" name="username"></div>
  <div><label for="hms_pass">Password</label><input id="hms_pass" name="password" type="password"></div>
  <div><label for="hms_otp">2FA One-Time Code</label><input id="hms_otp" name="otp" autocomplete="one-time-code"></div>
  <button id="btn-hms-login" type="button" onclick="doLogin()">Sign In</button>
  <script>
    async function doLogin() {
      const res = await fetch('/login', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          username: document.getElementById('hms_user').value,
          password: document.getElementById('hms_pass').value,
          otp: document.getElementById('hms_otp').value
        })
      });
      const data = await res.json();
      if (data.ok) window.location.href = data.next_url;
    }
  </script>
</body></html>"""

    def _render_register_page(self) -> str:
        return """<!doctype html>
<html><head><meta charset="utf-8"><title>HMS - Outpatient Registration</title></head>
<body data-authenticated-shell="true" data-hms-ready="true" data-screen-id="hms_registration">
  <h1>Outpatient Queue Registration</h1>
  <div id="hms-banner" role="alert" data-validation-banner="true" style="display:none"></div>
  <fieldset>
    <legend>Patient Demographics</legend>
    <div><label for="pat_name">Patient Full Name *</label>
      <input id="pat_name" name="patient_name" required value=""></div>
    <div><label for="pat_dob">Date of Birth *</label>
      <input id="pat_dob" name="dob" type="date" required value=""></div>
    <div><label for="pat_sex">Biological Sex *</label>
      <select id="pat_sex" name="sex" required>
        <option value="">-- Select --</option>
        <option value="MALE">Male</option>
        <option value="FEMALE">Female</option>
        <option value="OTHER">Other</option>
      </select></div>
    <div><label for="pat_mobile">Mobile Number *</label>
      <input id="pat_mobile" name="mobile" required value=""></div>
  </fieldset>
  <fieldset>
    <legend>Queue &amp; Department Routing</legend>
    <div><label for="visit_type">Visit Category *</label>
      <select id="visit_type" name="visit_type" required>
        <option value="">-- Select --</option>
        <option value="New Registration">New Registration</option>
        <option value="Follow-Up">Follow-Up</option>
      </select></div>
    <div><label for="dept_sel">Clinical Department *</label>
      <select id="dept_sel" name="department" required onchange="onDeptChange()">
        <option value="">-- Select --</option>
        <option value="Cardiology">Cardiology</option>
        <option value="Orthopedics">Orthopedics</option>
        <option value="Pediatrics">Pediatrics</option>
      </select></div>
    <div>
      <label id="lbl_doc" for="doc_combo_btn">Consulting Doctor *</label>
      <button id="doc_combo_btn" type="button" role="combobox" aria-labelledby="lbl_doc"
        aria-expanded="false" data-selected-value="" onclick="openDoctorDropdown()">Select...</button>
    </div>
    <div id="minted-uhid-box" style="display:none">
      <label for="minted_uhid">Generated Patient UHID</label>
      <input id="minted_uhid" readonly value="">
    </div>
  </fieldset>

  <dialog id="dup-phone-modal" data-modal-open="false">
    <p id="dup-modal-msg"></p>
    <button id="btn-confirm-dup" type="button" onclick="confirmDuplicateOverride()">Confirm Separate Registration</button>
    <button id="btn-cancel-dup" type="button" onclick="closeDupModal()">Cancel</button>
  </dialog>

  <button id="btn-hms-submit" type="button" onclick="submitRegistration(false)">Register Patient</button>
  <script>
    function onDeptChange() {
      const btn = document.getElementById('doc_combo_btn');
      btn.textContent = 'Select...';
      btn.setAttribute('data-selected-value', '');
    }
    async function openDoctorDropdown() {
      let old = document.getElementById('doc-portal-listbox');
      if (old) { old.remove(); return; }
      const dept = document.getElementById('dept_sel').value;
      const btn = document.getElementById('doc_combo_btn');
      btn.setAttribute('aria-expanded', 'true');
      const res = await fetch('/api/doctors?dept=' + encodeURIComponent(dept));
      const data = await res.json();
      const ul = document.createElement('ul');
      ul.id = 'doc-portal-listbox';
      ul.setAttribute('role', 'listbox');
      ul.setAttribute('data-portal-listbox', 'true');
      for (const doc of data.doctors || []) {
        const li = document.createElement('li');
        li.setAttribute('role', 'option');
        li.setAttribute('data-option-value', doc);
        li.textContent = doc;
        li.onclick = () => {
          btn.textContent = doc;
          btn.setAttribute('data-selected-value', doc);
          btn.setAttribute('aria-expanded', 'false');
          ul.remove();
        };
        ul.appendChild(li);
      }
      document.body.appendChild(ul);
    }
    function closeDupModal() {
      const dlg = document.getElementById('dup-phone-modal');
      dlg.setAttribute('data-modal-open', 'false');
      dlg.close();
    }
    async function confirmDuplicateOverride() {
      closeDupModal();
      await submitRegistration(true);
    }
    async function submitRegistration(overrideDup) {
      const payload = {
        patient_name: document.getElementById('pat_name').value,
        dob: document.getElementById('pat_dob').value,
        sex: document.getElementById('pat_sex').value,
        mobile: document.getElementById('pat_mobile').value,
        visit_type: document.getElementById('visit_type').value,
        department: document.getElementById('dept_sel').value,
        consulting_doctor: document.getElementById('doc_combo_btn').getAttribute('data-selected-value') || '',
        confirm_family_override: overrideDup ? 'true' : 'false'
      };
      const res = await fetch('/api/hms/register', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if (data.duplicate_modal) {
        const dlg = document.getElementById('dup-phone-modal');
        document.getElementById('dup-modal-msg').textContent = data.message;
        dlg.setAttribute('data-modal-open', 'true');
        dlg.showModal();
        return;
      }
      if (!data.ok) {
        const b = document.getElementById('hms-banner');
        b.textContent = data.error;
        b.style.display = 'block';
        return;
      }
      window.location.href = '/hms/search?uhid=' + encodeURIComponent(data.uhid);
    }
  </script>
</body></html>"""

    def _render_search_page(self, uhid: str, rec: Optional[dict[str, str]]) -> str:
        details = ""
        if rec:
            details = f"""
            <fieldset id="hms-chart-card">
              <legend>Verified Patient Chart</legend>
              <div><label for="rd_uhid">Unique Patient ID</label>
                <input id="rd_uhid" readonly value="{html.escape(rec['uhid'])}"></div>
              <div><label for="rd_name">Patient Full Name</label>
                <input id="rd_name" readonly value="{html.escape(rec['patient_name'])}"></div>
              <div><label for="rd_dob">Date of Birth</label>
                <input id="rd_dob" readonly value="{html.escape(rec['dob'])}"></div>
              <div><label for="rd_sex">Biological Sex</label>
                <input id="rd_sex" readonly value="{html.escape(rec['sex'])}"></div>
              <div><label for="rd_mobile">Mobile Number</label>
                <input id="rd_mobile" readonly value="{html.escape(rec['mobile'])}"></div>
              <div><label for="rd_dept">Clinical Department</label>
                <input id="rd_dept" readonly value="{html.escape(rec['department'])}"></div>
              <div><label for="rd_visit_type">Visit Type</label>
                <input id="rd_visit_type" readonly value="{html.escape(rec['visit_type'])}"></div>
              <div><label for="rd_doc">Consulting Doctor</label>
                <input id="rd_doc" readonly value="{html.escape(rec['consulting_doctor'])}"></div>
            </fieldset>"""
        return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>HMS - Patient Chart Lookup</title></head>
<body data-authenticated-shell="true" data-summary-ready="true" data-screen-id="hms_search">
  <h1>Patient Lookup &amp; Registration Slip</h1>
  <div>
    <label for="search_uhid">Lookup UHID</label>
    <input id="search_uhid" name="uhid" value="{html.escape(uhid)}">
    <button id="btn-search-uhid" type="button"
      onclick="window.location.href='/hms/search?uhid='+encodeURIComponent(document.getElementById('search_uhid').value)">Search</button>
  </div>
  {details}
</body></html>"""
