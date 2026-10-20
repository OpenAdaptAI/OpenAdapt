"""Inputs on the screens before this change (Calm UI, #1310).

The answers are the ones a person who knows MockMed would give. The record
check file is the one IT would hand over: MockMed's store read at /api/db.
"""

from pathlib import Path

from walk import Walk, button


def it_settings_file(work: Path, mockmed_url: str) -> Path:
    path = work / "it-deployment.yaml"
    path.write_text(
        "effects:\n"
        "  kind: rest\n"
        f"  base_url: {mockmed_url.rstrip('/')}\n"
        "  records_path: /api/db\n"
        "  records_key: records\n"
    )
    return path


def run(walk: Walk, *, mockmed_url: str, work: Path, **_: object) -> None:
    page = walk.page
    settings = str(it_settings_file(work, mockmed_url))
    walk.give(
        "Workflows",
        "navigation",
        "Check it",
        lambda: button(page, "Check it").click(),
        name="workflows",
    )
    page.wait_for_timeout(4000)
    walk.give(
        "Check it",
        "decision",
        "Start the check with the app from your recording",
        lambda: button(page, "Start the check with the app from your recording").click(),
        name="start-check",
    )
    page.wait_for_timeout(3000)
    walk.give(
        "Check it: how it confirms the save",
        "choice",
        "What does this step save? (It adds a new entry)",
        lambda: page.get_by_label("It adds a new entry to a record").check(),
        name="effect-kind",
    )
    walk.give(
        "Check it: how it confirms the save",
        "choice",
        "Which value identifies the record? (Note)",
        lambda: page.get_by_label("Which value identifies the record?").select_option("note"),
        name="effect-match-param",
        note="Already Note; the person still has to check it",
    )
    walk.give(
        "Check it: how it confirms the save",
        "free_text",
        "What is that ID called in your system's records? (note)",
        lambda: page.get_by_label("What is that ID called in your system's records?").fill("note"),
        name="effect-match-field",
    )
    walk.give(
        "Check it: how it confirms the save",
        "choice",
        "How should OpenAdapt read the record back? (system's records)",
        lambda: page.get_by_label("Read it back from your system's records").check(),
        name="effect-tier",
        note="Already selected; the person still has to check it",
    )
    walk.give(
        "Check it: how it confirms the save",
        "decision",
        "Save how it confirms the save",
        lambda: button(page, "Save how it confirms the save").click(),
        name="effect-save",
    )
    page.wait_for_timeout(2500)
    walk.give(
        "Check it",
        "navigation",
        "Show part 3 (its confirm button is inside the fold)",
        lambda: page.locator("summary", has_text="How it confirms the save").click(),
        name="unfold-part-3",
    )
    walk.give(
        "Check it",
        "decision",
        "These look right",
        lambda: button(page, "These look right").click(),
        name="these-look-right",
    )
    page.wait_for_timeout(2500)
    walk.shot("after-confirm")
    walk.give(
        "Check it: practice run",
        "navigation",
        "Change where it runs",
        lambda: page.locator("summary", has_text="Change where it runs").click(),
        name="open-where",
    )
    walk.give(
        "Check it: practice run",
        "free_text",
        "Deployment settings file (for IT)",
        lambda: page.get_by_label("Deployment settings file (for IT)").fill(settings),
        name="practice-settings-file",
        note=(
            "The record check reads MockMed's store; without this file the practice "
            "run has no record check to run"
        ),
    )
    walk.give(
        "Check it: practice run",
        "confirmation",
        "These are test values, and I'll keep the app in view",
        lambda: page.get_by_label(
            "These are test values, and I'll keep the app in view while it runs."
        ).check(),
        name="practice-attest",
    )
    walk.give(
        "Check it: practice run",
        "decision",
        "Run the practice item",
        lambda: button(page, "Run the practice item").click(),
        name="practice-run",
    )
    # On MockMed Flow refuses this run before it starts (see BLOCKED below);
    # the approval dialog never opens.
    walk.wait_text("The practice run didn't pass.")
    page.locator("summary", has_text="Technical details").first.click()
    walk.shot("practice-refused")
    walk.give(
        "Check it",
        "navigation",
        "Advanced: exact checks (the 5 unclassified steps)",
        lambda: button(page, "Advanced: exact checks").click(),
        name="exact-checks",
    )
    page.wait_for_timeout(3000)
    walk.shot("exact-checks")


#: What Flow said, and what Check it can't do about it (read from the run).
BLOCKED = {
    "where": "Check it: practice run",
    "flow_refusal": [
        "Certification: require_idempotency_key_for: the save's record check carries no "
        "idempotency key",
        "Approval fallback: qualified action path has no effect contract to assign a tier",
    ],
    "shown_to_the_person": (
        "The practice run didn't pass. (technical details: Qualification run did not "
        "retain a valid report)"
    ),
    "why_check_it_cannot_fix_it": [
        "The plain record-check form has no idempotency key. MockMed sends a fixed key "
        "(not a typed value), and exact checks only take a key from a typed value.",
        "Five steps stay unclassified until a person classifies each in exact checks; "
        "unclassified steps count as saves without a record check.",
    ],
}
