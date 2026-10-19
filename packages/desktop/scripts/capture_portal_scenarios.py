#!/usr/bin/env python3
# ruff: noqa: E501
"""Capture every phone state, in dark and light, at 390x844.

This is a presentation-fixture generator. It starts no runner, reads no run
directory, and never uses customer evidence. The form in the save screenshots
is a synthetic page rendered here, and every name and number on it is made
up. The generated PNG files stay in the caller-selected directory.

Each state is the real shell (``openadapt_desktop/portal/shell``) driven by a
stubbed transport and then by real taps and drags, so a capture shows what
the code does, not a mock-up of it.

Example:
    uv run --extra build python scripts/capture_portal_scenarios.py \
      --out /tmp/openadapt-phone-states
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from collections.abc import Callable
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "openadapt_desktop" / "portal" / "shell"
VIEWPORT = {"width": 390, "height": 844}
SCHEMES = ("dark", "light")

SAVE_ID = "5" * 32
SAVE_ID_2 = "6" * 32

# A synthetic referral form. Nothing here is a real person or record.
FORM_ROWS = [
    ("Patient", "Sample, Alex"),
    ("Refer to", "Cardiology"),
    ("Clinician", "Dr. R. Example"),
    ("Reason", "Chest pain on exertion, 3 wk"),
    ("Priority", "Urgent"),
]

FIELDS = [
    {"label": "Refer to", "before": None, "after": "Cardiology"},
    {"label": "Clinician", "before": None, "after": "Dr. R. Example"},
    {"label": "Reason", "before": None, "after": "Chest pain on exertion, 3 wk"},
    {"label": "Priority", "before": "Routine", "after": "Urgent"},
]

CLAIM_FIELDS = [
    {"label": "Claim", "before": None, "after": "CLM-00000"},
    {"label": "Service", "before": None, "after": "99214 · Office visit"},
    {"label": "Date", "before": None, "after": "2026-10-15"},
    {"label": "Amount", "before": None, "after": "$142.00"},
]


def _form_html() -> str:
    rows = "".join(f"<label><span>{name}</span><b>{value}</b></label>" for name, value in FORM_ROWS)
    return f"""<!doctype html><meta charset="utf-8"><style>
body{{margin:0;font:15px -apple-system,Segoe UI,sans-serif;background:#fff;color:#1b2333}}
nav{{background:#eef2f7;padding:10px 18px;font-size:13px;color:#56627a;display:flex;gap:22px}}
h1{{font-size:18px;margin:18px 18px 8px}} form{{padding:0 18px;display:grid;gap:10px}}
label{{display:grid;grid-template-columns:110px 1fr;align-items:center;gap:10px}}
label span{{color:#56627a;font-size:13px}} b{{font-weight:500;border:1px solid #c9d2df;border-radius:6px;padding:6px 8px}}
.save{{margin:16px 18px;background:#2b5bd7;color:#fff;border-radius:6px;padding:8px 18px;display:inline-block}}
</style><nav><span>Calendar</span><span>Patients</span><span>Referrals</span><span>Billing</span></nav>
<h1>New referral</h1><form>{rows}</form><span class="save">Save</span>"""


def _save_detail(
    *,
    risk: str,
    fields: list[dict[str, Any]] | None,
    sha: str,
    step_index: int = 4,
    intent: str = "Save the referral",
    question: str = "Save this?",
) -> dict[str, Any]:
    presentation: dict[str, Any] = {
        "question": question,
        "step_intent": intent,
        "step_index": step_index,
        "step_class": risk,
        "explanation": "Before it saves, OpenAdapt reads the form again. If anything changed, it stops and asks again.",
        "screenshot_route": f"/api/save-approvals/{SAVE_ID}/frame",
        "screenshot_sha256": sha,
        "decline_reasons": ["wrong_values", "wrong_record", "screen_problem"],
    }
    if fields is not None:
        presentation["fields"] = fields
    return {
        "task": {
            "task_kind": "save_approval",
            "risk_class": risk,
            "capability_digest": "sha256:" + "a" * 64,
            "signature": "hmac-sha256:" + "b" * 64,
            "allowed_actions": ["verify_and_resume", "reject"],
            "created_at": "@-14m",
            "expires_at": "@+46m",
        },
        "task_digest": "sha256:" + "c" * 64,
        "presentation": presentation,
    }


def _run_detail(*, risk: str = "unknown") -> dict[str, Any]:
    return {
        "task": {
            "task_kind": "identity",
            "risk_class": risk,
            "delivery_state": "not_delivered",
            "capability_digest": "sha256:" + "a" * 64,
            "signature": "hmac-sha256:" + "b" * 64,
            "allowed_actions": ["verify_and_resume", "reject", "teach", "escalate"],
            "created_at": "@-14m",
            "expires_at": "@+38m",
            "evidence": {"identity_required_count": 3, "identity_confirmed_count": 2},
        },
        "task_digest": "sha256:" + "c" * 64,
        "presentation": {
            "question": "Is the intended record open now?",
            "explanation": "The record on screen didn't match the intended one.",
            "after_artifact_id": "fixture-frame",
            "halt": {
                "category": "identity",
                "step_ordinal": 3,
                "step_count": 6,
                "action_kind": "click",
                "target_role": "button",
                "target_label": "Save",
                "resolution_ladder": [],
                "will_recheck": [
                    {"check": "record_identity", "count": 3},
                    {"check": "postconditions", "count": 1},
                ],
            },
        },
    }


ACCEPTED = {
    "state": "accepted_pending_runner",
    "reason_code": "pending_runner",
    "action": "verify_and_resume",
    "decision_digest": "sha256:" + "e" * 64,
    "decided_at": "@0",
}
REJECTED = {
    "state": "rejected",
    "reason_code": "rejected_by_operator",
    "action": "reject",
    "decision_digest": "sha256:" + "f" * 64,
    "decided_at": "@0",
}


def _fixtures(sha: str) -> dict[str, dict[str, Any]]:
    """Each named state: what the stubbed computer answers."""
    save = _save_detail(risk="consequential", fields=FIELDS, sha=sha)
    return {
        "unpaired": {"paired": False},
        "save": {"saves": [SAVE_ID], "details": {SAVE_ID: save}, "reply": ACCEPTED},
        "save-two-waiting": {
            "saves": [SAVE_ID, SAVE_ID_2],
            "runs": ["run1"],
            "details": {SAVE_ID: save, "run1": _run_detail()},
            "reply": ACCEPTED,
        },
        "save-screenshot-only": {
            "saves": [SAVE_ID],
            "details": {SAVE_ID: _save_detail(risk="consequential", fields=None, sha=sha)},
        },
        "save-no-picture": {
            "saves": [SAVE_ID],
            "details": {SAVE_ID: _save_detail(risk="consequential", fields=FIELDS, sha="0" * 64)},
        },
        "save-irreversible": {
            "saves": [SAVE_ID],
            "details": {
                SAVE_ID: _save_detail(
                    risk="irreversible",
                    fields=CLAIM_FIELDS,
                    sha=sha,
                    step_index=5,
                    intent="Submit the claim to the payer",
                )
            },
            "reply": ACCEPTED,
        },
        "save-decline": {"saves": [SAVE_ID], "details": {SAVE_ID: save}, "reply": REJECTED},
        "save-changed": {
            "saves": [SAVE_ID],
            "details": {SAVE_ID: save},
            "reply": {"status": 409, "body": {"detail": "task_changed"}},
        },
        "save-gone": {
            "saves": [SAVE_ID],
            "details": {SAVE_ID: {"status": 404, "body": {"detail": "request_not_waiting"}}},
        },
        "save-not-attended": {
            "saves": [SAVE_ID],
            "details": {SAVE_ID: {"status": 404, "body": {"detail": "Not Found"}}},
        },
        "run": {"runs": ["run1"], "details": {"run1": _run_detail()}},
        "run-halted": {
            "runs": ["run1"],
            "details": {"run1": _run_detail()},
            "reply": {
                "state": "halted",
                "reason_code": "continuation_halted",
                "action": "verify_and_resume",
            },
        },
        "run-uncertain": {
            "runs": ["run1"],
            "details": {"run1": _run_detail()},
            "reply": {"status": 503, "body": None},
        },
        "run-not-accepted": {
            "runs": ["run1"],
            "details": {"run1": _run_detail()},
            "reply": {"status": 409, "body": {"detail": "the human decision task changed"}},
        },
        "offline": {"saves": [SAVE_ID], "details": {SAVE_ID: save}, "offline": True},
        "all-clear": {},
        "saves-not-attended": {"saves_status": 404},
        "unreachable": {"unreachable": True},
        "expired": {
            "saves": [SAVE_ID],
            "details": {SAVE_ID: {**save, "task": {**save["task"], "expires_at": "@+3s"}}},
        },
    }


STUB = r"""
const fx = window.__fx;
const nativeFetch = window.fetch.bind(window);
const now = Date.now();
// "@-14m" / "@+46m" / "@+3s" / "@0": times relative to page load, so the
// countdowns read the same on every run.
function stamp(value) {
  if (typeof value !== "string" || value[0] !== "@") return value;
  const m = /^@([+-]?\d+)([smh]?)$/.exec(value);
  const unit = { s: 1e3, m: 6e4, h: 36e5, "": 1e3 }[m[2]];
  return new Date(now + Number(m[1]) * unit).toISOString();
}
function walk(value) {
  if (Array.isArray(value)) return value.map(walk);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, walk(v)]));
  }
  return stamp(value);
}
const state = walk(fx);
if (state.offline) Object.defineProperty(navigator, "onLine", { get: () => false });
if (state.paired !== false) {
  sessionStorage.setItem("portal_token", "fixture-token");
  sessionStorage.setItem("portal_csrf", "fixture-csrf");
} else {
  sessionStorage.clear();
}
const reply = (status, body) => ({ status, ok: status < 400, json: async () => body });
window.fetch = async (url, options = {}) => {
  url = String(url);
  if (state.unreachable) throw new TypeError("Failed to fetch");
  if (url === "/api/portal/session") return reply(200, { device_label: "Front-desk PC" });
  if (url === "/api/portal/tasks") return reply(200, (state.runs || []).map((id) => ({ id })));
  if (url === "/api/portal/save-approvals") {
    if (state.saves_status) return reply(state.saves_status, { detail: "Not Found" });
    return reply(200, (state.saves || []).map((request_id) => ({ request_id })));
  }
  if (url.endsWith("/frame") || url.includes("/evidence?")) return nativeFetch("/frame.png", options);
  if (url.includes("/actions/")) {
    const r = state.reply || {};
    return "status" in r ? reply(r.status, r.body) : reply(200, r);
  }
  const id = url.split("/").pop();
  const detail = (state.details || {})[id];
  if (!detail) return reply(404, { detail: "Not Found" });
  return "status" in detail ? reply(detail.status, detail.body) : reply(200, detail);
};
"""


def _write_site(site: Path, name: str, fixture: dict[str, Any]) -> None:
    index = (SHELL / "index.html").read_text(encoding="utf-8")
    stub = f"<script>window.__fx={json.dumps(fixture)};{STUB}</script>"
    index = index.replace(
        '<script src="/app.js" defer></script>', f'{stub}<script src="/app.js"></script>'
    )
    (site / f"{name}.html").write_text(index, encoding="utf-8")


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return


def _serve(directory: Path) -> tuple[ThreadingHTTPServer, Thread]:
    def handler(*args: object, **kwargs: object) -> _QuietHandler:
        return _QuietHandler(*args, directory=str(directory), **kwargs)

    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _click(page: Any, selector: str) -> None:
    page.locator(selector).first.click()
    page.wait_for_timeout(150)


def _ready(page: Any) -> None:
    # The picture is verified before Approve is offered; wait for that.
    page.wait_for_function(
        """() => !document.querySelector('#evidence-slot') ||
                 !/Loading/.test(document.querySelector('#evidence-slot').textContent)"""
    )
    page.wait_for_timeout(300)


def _approve_and_wait(page: Any) -> None:
    _click(page, '#actions [data-answer="verify_and_resume"]')
    page.wait_for_timeout(5600)


def _armed(page: Any) -> None:
    _click(page, '#actions [data-answer="verify_and_resume"]')


def _matched(page: Any) -> None:
    _armed(page)
    page.locator("[data-opt]", has_text="2026-10-15").click()
    page.wait_for_timeout(150)


def _holding(page: Any) -> None:
    _matched(page)
    box = page.locator("#hold").bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.wait_for_timeout(650)


def _drag(page: Any, dx: int) -> None:
    box = page.locator("#card").bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 3
    page.mouse.move(x, y)
    page.mouse.down()
    page.mouse.move(x + dx / 2, y + 4, steps=4)
    page.mouse.move(x + dx, y + 8, steps=4)
    page.wait_for_timeout(100)


def _receipt(page: Any) -> None:
    _approve_and_wait(page)
    page.wait_for_timeout(2800)


# Each capture: (file stem, fixture name, steps after load).
CAPTURES: list[tuple[str, str, Callable[[Any], None] | None]] = [
    ("pairing", "unpaired", None),
    ("save", "save", None),
    ("save-queue", "save-two-waiting", None),
    ("save-swipe-right", "save", lambda p: _drag(p, 90)),
    ("save-swipe-left", "save", lambda p: _drag(p, -90)),
    ("save-undo", "save", lambda p: _click(p, '#actions [data-answer="verify_and_resume"]')),
    (
        "save-decline-reasons",
        "save-decline",
        lambda p: _click(p, '#actions [data-answer="reject"]'),
    ),
    ("save-screenshot-only", "save-screenshot-only", None),
    ("save-full-screen", "save", lambda p: _click(p, "#evidence")),
    ("save-more", "save", lambda p: _click(p, "#more")),
    ("save-no-picture", "save-no-picture", None),
    ("save-irreversible", "save-irreversible", None),
    ("save-irreversible-armed", "save-irreversible", _armed),
    ("save-irreversible-matched", "save-irreversible", _matched),
    ("save-irreversible-holding", "save-irreversible", _holding),
    ("save-changed", "save-changed", _approve_and_wait),
    ("save-gone", "save-gone", None),
    ("save-not-attended", "save-not-attended", None),
    ("run", "run", None),
    ("run-more", "run", lambda p: _click(p, "#more")),
    ("run-paused-again", "run-halted", _approve_and_wait),
    ("run-uncertain", "run-uncertain", _approve_and_wait),
    ("run-not-accepted", "run-not-accepted", _approve_and_wait),
    ("offline", "offline", None),
    ("expired", "expired", lambda p: p.wait_for_timeout(3600)),
    ("all-clear", "all-clear", None),
    ("all-clear-with-receipt", "save", _receipt),
    ("saves-not-attended", "saves-not-attended", None),
    ("unreachable", "unreachable", None),
]


def _capture(out: Path, only: set[str] | None) -> list[str]:
    from playwright.sync_api import sync_playwright

    written: list[str] = []
    with tempfile.TemporaryDirectory(prefix="openadapt-phone-states-") as temp:
        site = Path(temp)
        for asset in ("app.js", "styles.css", "manifest.webmanifest"):
            shutil.copy(SHELL / asset, site / asset)
        server, thread = _serve(site)
        try:
            origin = f"http://127.0.0.1:{server.server_port}"
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                # The synthetic form, rendered once, is the exact screenshot.
                form = browser.new_page(viewport={"width": 760, "height": 420})
                form.set_content(_form_html())
                frame = form.screenshot()
                form.close()
                (site / "frame.png").write_bytes(frame)
                for name, fixture in _fixtures(hashlib.sha256(frame).hexdigest()).items():
                    _write_site(site, name, fixture)
                for scheme in SCHEMES:
                    context = browser.new_context(
                        viewport=VIEWPORT, device_scale_factor=2, color_scheme=scheme
                    )
                    for stem, fixture_name, steps in CAPTURES:
                        if only and stem not in only:
                            continue
                        page = context.new_page()
                        page.goto(f"{origin}/{fixture_name}.html", wait_until="networkidle")
                        _ready(page)
                        if steps is not None:
                            steps(page)
                        # Result rings draw in for about 1.3 s; capture them finished.
                        page.wait_for_timeout(1500)
                        target = out / f"{stem}-{scheme}.png"
                        page.screenshot(path=str(target))
                        written.append(target.name)
                        page.close()
                    context.close()
                browser.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", type=Path, required=True, help="Output directory for the PNG files."
    )
    parser.add_argument("--only", nargs="*", help="Capture only these states, by file stem.")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    written = _capture(args.out, set(args.only) if args.only else None)
    print(f"Wrote {len(written)} phone states to {args.out}")


if __name__ == "__main__":
    main()
