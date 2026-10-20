"""Inputs on the screens after this change: three questions.

The same recording and the same MockMed as the "before" walk. MockMed's demo
key is the same for every save on one patient, and its server keeps only the
first save with a key, so the walk empties MockMed's test store before the
practice item and before the first item, as Flow's tutorial does before each
of its runs. Nothing else is set up by hand: no settings file, no form.
"""

from walk import Walk, button

#: The first real item: a new note, typed in Run it.
FIRST_ITEM_NOTE = "Synthetic second visit: booked a call"


def run(walk: Walk, *, reset_store, **_: object) -> None:
    page = walk.page
    walk.give(
        "Workflows",
        "navigation",
        "Check it",
        lambda: button(page, "Check it").click(),
        name="workflows",
    )
    walk.wait_text("Is this the right record check?", timeout=120_000)
    page.wait_for_timeout(1500)

    walk.give(
        "Check it: question 1",
        "decision",
        "Is this the right record check? Yes, use this check",
        lambda: button(page, "Yes, use this check").click(),
        name="q1-record-check",
    )
    page.wait_for_function(
        "() => [...document.querySelectorAll('button')].some((b) => b.textContent.trim() === "
        "'Yes, practice once with these test values' && !b.disabled)",
        timeout=300_000,
    )

    reset_store()
    walk.give(
        "Check it: question 2",
        "decision",
        "Which values change per item? Yes, practice once with these test values",
        lambda: button(page, "Yes, practice once with these test values").click(),
        name="q2-values",
    )
    page.get_by_role("button", name="Approve and save").wait_for(timeout=600_000)
    walk.give(
        "Approval dialog",
        "confirmation",
        "Approve and save (the practice item's save)",
        lambda: button(page, "Approve and save").click(),
        name="practice-approve-save",
    )
    # The drills run on their own after the practice item.
    approve = page.get_by_role("button", name="Approve this workflow")
    page.wait_for_function(
        "() => [...document.querySelectorAll('button')].some((b) => b.textContent.trim() === "
        "'Approve this workflow' && !b.disabled)",
        timeout=1_500_000,
    )
    walk.give(
        "Check it: question 3",
        "decision",
        "Approve? Approve this workflow",
        lambda: approve.click(),
        name="q3-approve",
    )
    walk.wait_text("Approved and locked", timeout=300_000)
    walk.shot("approved")

    walk.give(
        "Check it", "navigation", "Run it", lambda: button(page, "Run it").click(), name="run-it"
    )
    page.wait_for_timeout(2000)
    page.get_by_role("radio", name="Type one").or_(
        page.get_by_role("button", name="Type one")
    ).first.click()
    page.wait_for_timeout(500)
    walk.give(
        "Run it",
        "item_value",
        "Note (the first item's own value)",
        lambda: page.get_by_label("Note").fill(FIRST_ITEM_NOTE),
        name="item-note",
    )
    reset_store()
    walk.give(
        "Run it",
        "item_run",
        "Run 1 item",
        lambda: button(page, "Run 1 item").click(),
        name="item-run",
    )
    page.get_by_role("button", name="Approve and save").wait_for(timeout=600_000)
    walk.give(
        "Approval dialog",
        "item_approval",
        "Approve and save (the first item's save)",
        lambda: button(page, "Approve and save").click(),
        name="item-approve-save",
    )
    walk.wait_text("See results", timeout=600_000)
    page.wait_for_timeout(1500)
    walk.shot("first-item-done")
