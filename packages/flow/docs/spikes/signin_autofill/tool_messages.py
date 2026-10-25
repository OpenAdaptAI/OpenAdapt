"""What the sign-in tool tells an agent. Pure strings, no macOS imports.

In the spike, a vague tool error ("Error executing tool") led Claude Code to
offer to take the person's username and password and submit the form itself.
So the agent never sees a raw outcome or exception text. Each failure maps to a
fixed message that says only what failed and what the person can do
themselves. The rule against passwords lives in the tool description.
"""

from __future__ import annotations

DESCRIPTION = (
    "Sign in to the Spike test app (http://localhost:47811) in the person's own "
    "Chrome and open its protected page. The person taught this sign-in step "
    "and approves it each time: OpenAdapt chooses their saved login in Chrome's "
    "list, and Chrome fills the password itself after the person confirms with "
    "Touch ID. This tool takes no arguments. It never accepts or needs a "
    "password, and you must never ask the person for a password, username or "
    "any other credential, whether this tool succeeds or fails. If it fails, "
    "tell the person what failed and that they can sign in themselves in their "
    "browser. Works only on the taught site."
)

SIGNED_IN = (
    "Signed in. The protected page (http://localhost:47811/protected) is open "
    "in the person's Chrome window."
)

_NEXT = (
    " Nothing was entered on the page. The person can sign in themselves in "
    "their browser, then call this tool again or carry on."
)

FAILURES = {
    "wrong_site": "Not signed in: the page in Chrome is not the taught site.",
    "no_saved_login": (
        "Not signed in: Chrome did not offer a saved login on the login page."
    ),
    "not_approved": (
        "Not signed in: the person did not approve the sign-in in Chrome's "
        "Touch ID prompt."
    ),
    "not_ready": (
        "Not signed in: the login page was not open and ready in Chrome, so "
        "OpenAdapt stopped before doing anything."
    ),
    "rejected": "Not signed in: the site did not accept the saved login.",
    "unconfirmed": "Not signed in: the sign-in could not be confirmed.",
}


def classify(outcome: str) -> str:
    """Map a replay outcome (never shown to the agent) to a failure kind."""
    text = outcome.lower()
    if "origin" in text:
        return "wrong_site"
    if "saved-login list did not appear" in text:
        return "no_saved_login"
    if "did not approve" in text:
        return "not_approved"
    if "server rejected" in text:
        return "rejected"
    if "within 5 s" in text:
        return "unconfirmed"
    return "not_ready"


def failure_message(kind: str) -> str:
    return FAILURES.get(kind, FAILURES["not_ready"]) + _NEXT
