"""Optional release-notes email signup from the first-launch question.

Nothing is sent unless the person types an address and presses Subscribe.
The address goes to the website's Netlify form ``newsletter`` with
``source=desktop``, in a POST body, and is not stored on this computer.
No install ID, usage data, or other identifier rides along.
"""

from __future__ import annotations

import os
import re
from typing import Any

SIGNUP_URL = "https://openadapt.ai/form.html"
FORM_NAME = "newsletter"
SOURCE = "desktop"
# The website's consent wording version (openadapt-web data/site/emailSignup.mjs).
CONSENT_VERSION = "2026-10-16-v4"
TIMEOUT_S = 10.0
MAX_EMAIL_LENGTH = 254

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def valid_email(value: Any) -> bool:
    return isinstance(value, str) and len(value) <= MAX_EMAIL_LENGTH and bool(_EMAIL.match(value))


def form_fields(email: str) -> dict[str, str]:
    """The exact fields posted; public/form.html on the website declares each."""
    return {
        "form-name": FORM_NAME,
        "bot-field": "",
        "email": email,
        "interest": "updates",
        "source": SOURCE,
        "page": "/desktop",
        "consent_version": CONSENT_VERSION,
    }


def _post(url: str, data: dict[str, str]) -> int:
    import httpx

    response = httpx.post(url, data=data, timeout=TIMEOUT_S)
    return response.status_code


def subscribe(email: Any) -> dict[str, Any]:
    """Send one signup. Returns ``{"ok": bool, "error"?: str}``."""
    if os.environ.get("OPENADAPT_EMAIL_SIGNUP", "").strip().lower() in {"0", "false", "no", "off"}:
        return {"ok": False, "error": "Email signup is turned off on this computer."}
    if not isinstance(email, str):
        return {"ok": False, "error": "Enter an email address."}
    email = email.strip()
    if not valid_email(email):
        return {"ok": False, "error": "Enter an email address, like you@company.com."}
    try:
        status = _post(SIGNUP_URL, form_fields(email))
    except Exception:
        return {
            "ok": False,
            "error": "That didn't go through. Check your connection and try again.",
        }
    if not 200 <= status < 300:
        return {"ok": False, "error": "That didn't go through. Please try again later."}
    return {"ok": True}
