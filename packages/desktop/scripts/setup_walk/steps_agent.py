"""The person's agent sets the workflow up over MCP; the person only approves.

The agent's calls go through openadapt_desktop.agent_mcp to the engine's
local socket, exactly as an MCP client's would. The window shows what the
agent answered. The person approves the practice item's save and the
workflow, in the window.
"""

import threading

from walk import Walk, button

FIRST_ITEM_NOTE = "Synthetic second visit: booked a call"


def run(walk: Walk, *, reset_store, agent_log: list, **_: object) -> None:
    from openadapt_desktop import agent_mcp

    page = walk.page
    walk.give(
        "Workflows",
        "navigation",
        "Check it",
        lambda: button(page, "Check it").click(),
        name="workflows",
    )
    walk.wait_text("Is this the right record check?", timeout=120_000)

    def agent() -> None:
        def call(name: str, **arguments: object) -> object:
            result = agent_mcp.handle(name, dict(arguments))
            agent_log.append({"tool": name, "arguments": arguments, "result": result})
            return result

        workflows = call("list_workflows")
        workflow_id = workflows[0]["workflow_id"]
        call("get_check_setup", workflow_id=workflow_id)
        call("answer_record_check", workflow_id=workflow_id)
        call("answer_values", workflow_id=workflow_id, changes=["note"])
        reset_store()
        call("run_checks", workflow_id=workflow_id)

    thread = threading.Thread(target=agent, daemon=True)
    thread.start()
    # The agent's practice item holds the engine's command queue until it
    # ends, as any run does; only the approval commands bypass it. So the
    # approval dialog comes first, and the window catches up afterwards.
    page.get_by_role("button", name="Approve and save").wait_for(timeout=900_000)
    walk.give(
        "Approval dialog",
        "confirmation",
        "Approve and save (the practice item's save)",
        lambda: button(page, "Approve and save").click(),
        name="practice-approve-save",
    )
    thread.join(timeout=1_800)
    walk.wait_text("Your agent answered for you", timeout=300_000)
    page.wait_for_function(
        "() => [...document.querySelectorAll('button')].some((b) => b.textContent.trim() === "
        "'Approve this workflow' && !b.disabled)",
        timeout=300_000,
    )
    page.locator("summary", has_text="What you").first.click()
    walk.give(
        "Check it: question 3",
        "decision",
        "Approve? Approve this workflow",
        lambda: button(page, "Approve this workflow").click(),
        name="q3-approve",
    )
    walk.wait_text("Approved and locked", timeout=300_000)
    walk.shot("approved")
