---
name: openadapt-gui-write
description: "Your AI agent decides what to enter. OpenAdapt enters it in the app and checks that it saved."
---

# OpenAdapt: enter it in the app and check that it saved

Use this when your agent has decided what to enter and the app has no usable API. Call list_workflows, then run_workflow with the workflow name, its inputs, and your own request_id.

## The three tools

1. `list_workflows` returns each workflow's `name`, `purpose`, `done_means`, and typed `inputs`.
2. `run_workflow` takes `workflow`, `inputs`, `request_id`, and optional `wait_seconds`. Use your own id for the piece of work, such as a referral id, and send the same id if you retry. The same id never writes twice. Don't put patient details in it.
3. `get_run` takes a `run_id` and returns the result when a run was still going.

## What the outcome means

| outcome | What happened | What to do |
| --- | --- | --- |
| `done` | Saved and checked. | Nothing more for this request. |
| `needs_review` | Stopped before saving. Nothing was written. A person decides. | Don't start the same work again. Check back with `get_run`. |
| `not_sure_if_saved` | It may or may not have saved. | Never retry. A person checks the record. |
| `did_not_run` | Nothing was written. | Fix the problem in `what_happened`, then retry with the same `request_id` if `safe_to_retry` is true. |

Only outcome done means the change was saved and checked. needs_review means it stopped before saving and a person decides, so don't start the same work again. not_sure_if_saved means a person must check the record, so never retry it. did_not_run means nothing was written: fix the problem and retry with the same request_id when safe_to_retry is true.

`proof` says how strong the evidence behind `done` is: `local` means the record check ran on the computer that ran the workflow, `sealed` means a signed receipt exists, and `simulated` means a sandbox result that opened no app.

## Try it

The sandbox serves one synthetic workflow, `add_triage_note`. Its `sandbox_case` input shows each outcome: `normal`, `duplicate_record`, `false_saved_banner`, `timeout_after_save`, and `app_offline`.

```bash
claude mcp add openadapt -- uvx --python 3.12 --from git+https://github.com/OpenAdaptAI/openadapt-agent openadapt-agent serve
```

PyPI has openadapt-agent 2.0.1, which predates the sandbox. From version 2.0.2 on, this shorter line does the same:

```bash
claude mcp add openadapt -- uvx --python 3.12 openadapt-agent serve
```

The skill name is openadapt-gui-write. It is never called computer use: your agent decides, and OpenAdapt does the entry.
