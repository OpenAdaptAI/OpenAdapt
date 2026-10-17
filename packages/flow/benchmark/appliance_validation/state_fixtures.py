"""Labeled screen fixtures for the drift-oracle (RemoteStateVerifier) study.

Each fixture is a full app-like screen rendered with Playwright (the same
rendering path dense_surface uses), plus the ``expected_state`` string the
runtime would pass to ``RemoteStateVerifier.holds`` and the ground-truth label:

* ``truth="yes"`` -- TRUE-RESCUE: the expected state IS semantically present but
  rendered under drift (dark theme / serif / scale / low contrast) so a literal
  OCR string match could miss it. The oracle SHOULD say "yes".
* ``truth="no"``  -- FALSE-RESCUE risk: the expected state did NOT happen, yet
  the screen ambiguously resembles success (an error banner in a success-shaped
  layout, a blank/partial/stale screen, a different record, the opposite action
  confirmed). The oracle MUST say "no"/"uncertain"; a "yes" here is the residual
  risk LIMITS.md warns about.

Nothing here imports the shipped runtime; it only produces PNG + label tuples.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StateFixture:
    fid: str
    kind: str  # "true_rescue" | "false_rescue"
    truth: str  # "yes" | "no"
    expected_state: str
    html: str
    note: str


# --- HTML builders ---------------------------------------------------------

_BASE_CSS = (
    "*{margin:0;box-sizing:border-box}"
    "html,body{width:100%;height:100%}"
    "body{display:flex;align-items:center;justify-content:center;"
    "font-family:{font};padding:40px}"
    ".card{max-width:640px;width:100%;padding:32px 36px;border-radius:10px;"
    "box-shadow:0 1px 4px rgba(0,0,0,.15)}"
    ".title{font-size:{title_px}px;font-weight:700;margin-bottom:14px}"
    ".body{font-size:20px;line-height:1.5}"
    ".pill{display:inline-block;padding:4px 12px;border-radius:14px;"
    "font-size:15px;font-weight:600;margin-bottom:18px}"
)


def _page(inner: str, *, bg: str, font: str = "Arial", title_px: int = 30) -> str:
    css = _BASE_CSS.replace("{font}", font).replace(
        "{title_px}", str(title_px)
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'><style>"
        f"body{{background:{bg}}}{css}</style></head><body>{inner}</body></html>"
    )


def _banner(
    *,
    title: str,
    body: str,
    pill: str,
    card_bg: str,
    fg: str,
    pill_bg: str,
    pill_fg: str,
    accent: str,
) -> str:
    return (
        f"<div class='card' style='background:{card_bg};color:{fg};"
        f"border-left:8px solid {accent}'>"
        f"<span class='pill' style='background:{pill_bg};color:{pill_fg}'>{pill}</span>"
        f"<div class='title'>{title}</div>"
        f"<div class='body'>{body}</div></div>"
    )


# --- Fixture corpus --------------------------------------------------------
# Kept deliberately small and hand-labelled so every case is auditable.

FIXTURES: list[StateFixture] = [
    # ---------------- TRUE-RESCUE (truth = yes, under drift) ----------------
    StateFixture(
        fid="tr_saved_dark",
        kind="true_rescue",
        truth="yes",
        expected_state="the patient record was saved successfully",
        html=_page(
            _banner(
                title="Patient record saved",
                body="All changes to this chart have been committed.",
                pill="SUCCESS",
                card_bg="#12241a",
                fg="#e6f5ec",
                pill_bg="#1f7a4d",
                pill_fg="#eafff2",
                accent="#2ecc71",
            ),
            bg="#0c1512",
            font="Arial",
        ),
        note="dark theme, green success banner",
    ),
    StateFixture(
        fid="tr_appt_serif_scaled",
        kind="true_rescue",
        truth="yes",
        expected_state="an appointment was successfully booked",
        html=_page(
            _banner(
                title="Appointment confirmed for Jane Doe",
                body="Fri 18 Jul, 10:30 AM with Dr. Okafor. A confirmation "
                "has been sent.",
                pill="CONFIRMED",
                card_bg="#ffffff",
                fg="#1a1a1a",
                pill_bg="#dff3e6",
                pill_fg="#1f7a4d",
                accent="#2ecc71",
            ),
            bg="#eef1f4",
            font="Georgia",
            title_px=40,
        ),
        note="serif font, scaled title, booking confirmation",
    ),
    StateFixture(
        fid="tr_order_lowcontrast",
        kind="true_rescue",
        truth="yes",
        expected_state="the lab order was submitted successfully",
        html=_page(
            _banner(
                title="Order submitted",
                body="Confirmation #A1024. CBC + metabolic panel routed to lab.",
                pill="DONE",
                card_bg="#f4f6f8",
                fg="#5a6472",
                pill_bg="#e4ecf2",
                pill_fg="#5a6472",
                accent="#9fb3c8",
            ),
            bg="#fbfcfd",
            font="Arial",
        ),
        note="low-contrast grey-on-grey success",
    ),
    StateFixture(
        fid="tr_rx_dark_italic",
        kind="true_rescue",
        truth="yes",
        expected_state="the prescription was sent to the pharmacy",
        html=_page(
            "<div class='card' style='background:#101826;color:#dde8f5;"
            "border-left:8px solid #4aa3ff;font-style:italic'>"
            "<span class='pill' style='background:#1c3a5e;color:#cfe6ff'>SENT</span>"
            "<div class='title'>Prescription transmitted</div>"
            "<div class='body'>Amoxicillin 500mg routed to Bayside Pharmacy.</div>"
            "</div>",
            bg="#0a0f18",
        ),
        note="dark, italic, e-prescribe confirmation",
    ),
    StateFixture(
        fid="tr_changes_toast",
        kind="true_rescue",
        truth="yes",
        expected_state="the changes were saved",
        html=_page(
            "<div class='card' style='background:#173d2a;color:#eafff2;"
            "border-left:8px solid #2ecc71;font-size:15px;max-width:360px'>"
            "<div class='title' style='font-size:20px'>&#10003; Changes saved</div>"
            "<div class='body' style='font-size:16px'>Autosaved just now.</div>"
            "</div>",
            bg="#0c1512",
        ),
        note="small dark toast (OCR-hostile size)",
    ),
    StateFixture(
        fid="tr_signed_scaled",
        kind="true_rescue",
        truth="yes",
        expected_state="the encounter note was signed",
        html=_page(
            _banner(
                title="Note signed &amp; finalized",
                body="Progress note locked. Author: Dr. Delgado.",
                pill="SIGNED",
                card_bg="#ffffff",
                fg="#12351f",
                pill_bg="#dff3e6",
                pill_fg="#1f7a4d",
                accent="#2ecc71",
            ),
            bg="#e9efe9",
            font="Georgia",
            title_px=44,
        ),
        note="large serif, sign-off confirmation",
    ),
    # ---------------- FALSE-RESCUE (truth = no, success-shaped) -------------
    StateFixture(
        fid="fr_save_failed",
        kind="false_rescue",
        truth="no",
        expected_state="the patient record was saved successfully",
        html=_page(
            _banner(
                title="Save failed",
                body="The record could not be saved. Please try again.",
                pill="ERROR",
                card_bg="#2a1414",
                fg="#f8e0e0",
                pill_bg="#7a1f1f",
                pill_fg="#ffeaea",
                accent="#e74c3c",
            ),
            bg="#150a0a",
        ),
        note="error banner in the same card layout as success",
    ),
    StateFixture(
        fid="fr_blank_form",
        kind="false_rescue",
        truth="no",
        expected_state="the patient record was saved successfully",
        html=_page(
            "<div class='card' style='background:#ffffff;color:#333'>"
            "<div class='title'>Edit patient</div>"
            "<div class='body'>Name <input style='width:100%;padding:8px'/><br><br>"
            "MRN <input style='width:100%;padding:8px'/><br><br>"
            "<button style='padding:8px 18px'>Save</button></div></div>",
            bg="#eef1f4",
        ),
        note="blank edit form; nothing was saved",
    ),
    StateFixture(
        fid="fr_saving_spinner",
        kind="false_rescue",
        truth="no",
        expected_state="the patient record was saved successfully",
        html=_page(
            "<div class='card' style='background:#ffffff;color:#444'>"
            "<div class='title'>Saving&hellip;</div>"
            "<div class='body'>Please wait while your changes are submitted.</div>"
            "</div>",
            bg="#eef1f4",
        ),
        note="in-progress/partial state, not yet saved",
    ),
    StateFixture(
        fid="fr_appt_cancelled",
        kind="false_rescue",
        truth="no",
        expected_state="an appointment was successfully booked",
        html=_page(
            _banner(
                title="Appointment cancelled",
                body="The appointment for Jane Doe has been cancelled.",
                pill="CANCELLED",
                card_bg="#ffffff",
                fg="#1a1a1a",
                pill_bg="#fbe3d6",
                pill_fg="#a1471f",
                accent="#e67e22",
            ),
            bg="#eef1f4",
        ),
        note="opposite action confirmed (cancel, not book)",
    ),
    StateFixture(
        fid="fr_wrong_record",
        kind="false_rescue",
        truth="no",
        expected_state="the record for Jane Doe was saved",
        html=_page(
            _banner(
                title="Record saved",
                body="Patient: John Smith (MRN MG771902) has been saved.",
                pill="SUCCESS",
                card_bg="#12241a",
                fg="#e6f5ec",
                pill_bg="#1f7a4d",
                pill_fg="#eafff2",
                accent="#2ecc71",
            ),
            bg="#0c1512",
        ),
        note="success, but for a DIFFERENT patient than expected",
    ),
    StateFixture(
        fid="fr_logged_out",
        kind="false_rescue",
        truth="no",
        expected_state="the patient record was saved successfully",
        html=_page(
            _banner(
                title="Signed out successfully",
                body="You have been logged out of the session.",
                pill="SUCCESS",
                card_bg="#12241a",
                fg="#e6f5ec",
                pill_bg="#1f7a4d",
                pill_fg="#eafff2",
                accent="#2ecc71",
            ),
            bg="#0c1512",
        ),
        note="green success banner for an unrelated action (logout)",
    ),
    StateFixture(
        fid="fr_validation_error",
        kind="false_rescue",
        truth="no",
        expected_state="the intake form was submitted successfully",
        html=_page(
            _banner(
                title="Please fix the highlighted fields",
                body="2 required fields are missing. The form was not submitted.",
                pill="REVIEW",
                card_bg="#2a2410",
                fg="#f7efce",
                pill_bg="#7a6a1f",
                pill_fg="#fff8d6",
                accent="#f1c40f",
            ),
            bg="#141005",
        ),
        note="validation error in a banner-shaped layout; not submitted",
    ),
    StateFixture(
        fid="fr_stale_dashboard",
        kind="false_rescue",
        truth="no",
        expected_state="the lab order was submitted successfully",
        html=_page(
            "<div class='card' style='background:#ffffff;color:#333'>"
            "<div class='title'>Dashboard</div>"
            "<div class='body'>Welcome back. You have 3 pending tasks. "
            "No recent orders.</div></div>",
            bg="#eef1f4",
        ),
        note="stale/neutral screen; the order was never placed",
    ),
]
