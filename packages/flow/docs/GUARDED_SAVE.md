# Guarded save

Preview. The API and the tool names may change before a release.

A guarded save wraps one write by any agent: Claude computer use, Operator,
your own harness, a script, or a person. The agent still types and clicks
Save. The guard adds three checks around that one write:

1. **Before the write**, it checks that the record open on the screen is the
   one the task names, and reads the system of record so that a new row can
   be told from an old one.
2. **At the write**, it holds the save until a person approves it, when the
   approval mode asks for that. A task is committed once and never again.
3. **After the write**, it reads the system of record again, through its API
   or database, not through the screen. The verdict is `VERIFIED`, `REFUTED`
   or `RECONCILIATION_REQUIRED`, with a receipt.

The guard makes no model calls, and nothing leaves your computer. It reuses
the read-back that [`verify-run`](VERIFY_RUN.md) uses, the save approvals
that Desktop and the phone portal answer, and the record checks in
[`EFFECT_KIT.md`](EFFECT_KIT.md).

## Verdicts

| Verdict | What the system of record shows | What to do |
|---|---|---|
| `VERIFIED` | The declared record, exactly once | Nothing. The task is done. |
| `REFUTED` | A different write: a duplicate, or a wrong value | Don't retry. A person corrects the record. |
| `RECONCILIATION_REQUIRED` | No sign of the write, or the store couldn't be read | Don't retry. The write may still land. A person checks the record. |

After a task is committed, `begin` refuses that task again. It returns
`already_saved` after `VERIFIED` and `needs_reconciliation` after any other
verdict. A refusal before the commit (wrong record, declined approval,
unreadable store) writes nothing, so the task can start again.

## What you need

- A `verify.yaml`, the same file `verify-run` reads. It names the
  system-of-record reader (`rest`, `sql`, `fhir`, `graphql`, and the other
  kinds in `EFFECT_KIT.md`) and the record the save must produce.
- A task for each write, from whoever assigned the work:

```json
{
  "schema_version": "openadapt.guard-task/v1",
  "task_id": "referral-task-01",
  "expected_record": {"patient_id": "p1"},
  "intended_fields": {"note": "Referral to orthopedics for knee pain"},
  "authorization_ref": "TICKET-1234"
}
```

`expected_record` holds the values that identify the record, such as a
patient ID, a name and a date of birth. Every value must be on the open
record's screen before the write. `intended_fields` holds what the write
saves. `verify.yaml` refers to both by name:

```yaml
schema_version: openadapt.verify/v1
verifier:
  kind: rest
  base_url: http://localhost:8080
  records_path: /api/db
effects:
  - kind: record_written
    match:
      patient_id: {param: patient_id}
      type: Referral
      note: {param: note}
    expected_count: 1
    count_new_only: true
    timeout_s: 5    # how long the read-back waits for a late write
```

The agent can't change the task. It names a `task_id`, so it can't choose the
record or the values it is checked against.

## Python API

```python
from openadapt_flow.guard import GuardedSave, GuardRefused, load_task

guarded = GuardedSave.from_file("verify.yaml", "runs/guarded", approval="each_item")
task = load_task("tasks/referral-task-01.json")

try:
    guard = guarded.begin(task, screen_text=harness.open_record_text(), agent_id="cua-01")
    step = guard.commit(screenshot_png=harness.screenshot())
    while step.status == "waiting_for_approval":
        step = guard.commit(wait_s=30)   # a person approves on Desktop or a phone
except GuardRefused as refused:
    stop(refused.code)                  # nothing was written

harness.click_save()                    # once
result = guard.confirm()
result.verdict                          # GuardVerdict.VERIFIED, REFUTED, or RECONCILIATION_REQUIRED
result.receipt_path                     # runs/guarded/<guard_id>/effect-receipt.json
```

- `begin(task, screen_text=..., agent_id=...)` checks identity and reads the
  baseline. It raises `GuardRefused` with `identity_mismatch`,
  `identity_unreadable`, `baseline_unreadable`, `already_saved` or
  `needs_reconciliation`.
- `commit(screenshot_png=..., screen_text=..., wait_s=...)` returns `go` or
  `waiting_for_approval`. Pass `screen_text` to check the open record again
  right before the write. With `approval="each_item"` (the default), the
  first call needs the screenshot the person will see.
- `confirm()` reads the system of record and returns a `GuardResult`.
  `retry_allowed` is always `False`. Calling it again returns the same
  result.

`screen_text` is the open record's text as your harness reads it: the
accessibility tree, the DOM text, or OCR. It shouldn't be the model's own
description of the screen. Each expected value must appear as a whole token
after whitespace and case are normalized, so `p1` doesn't match `p12` and
`Jane Sample` doesn't match `Jane Sampleton`. Pass the open record's text,
not a whole screen that lists several patients: a list that includes the
expected patient passes the check.

## MCP tools

```bash
openadapt-agent serve --guard verify.yaml --guard-tasks tasks/ --guard-state runs/guarded
```

This serves only three tools over local stdio:

| Tool | Arguments | Returns |
|---|---|---|
| `guard_begin` | `task_id`, `screen_text`, optional `agent_id` | `guard_id`, or `refused` |
| `guard_commit` | `guard_id`, `screenshot_png_base64` (first call), optional `screen_text` | `go`, `waiting_for_approval`, or `refused` |
| `guard_confirm` | `guard_id` | `verdict`, `outcome`, `receipt_path`, `retry_allowed: false`, `what_to_do` |

A refusal comes back as `{"ok": false, "refused": "<code>", "write_allowed": false}`.
`--guard-approval not_gated` turns off the per-save approval. Guards are
kept in the server's memory. If the server restarts between `guard_commit`
and `guard_confirm`, the task stays committed and needs a person.

## Approval

Under `each_item`, `commit` writes an approval request in the guard's folder
(`<state>/<guard_id>/item-approval/`), the same files a Flow run
writes. The approver policy is pinned to person-only, and the agent is
recorded as the initiator, so it can't answer its own request. A person
answers with:

```bash
openadapt-flow item-approval show runs/guarded/<guard_id>
```

```bash
openadapt-flow item-approval approve runs/guarded/<guard_id> --request-id <id> --request-sha256 <sha> --decided-by human --route desktop_run_it --unauthenticated --approver-key <approver-key.pem>
```

The answer counts only because the person's registered approver key signs
it (`OPENADAPT_JIT_KEY_PASSPHRASE` holds the key's passphrase; see
[`APPROVAL_MODES.md`](APPROVAL_MODES.md#the-approver-key)). `GuardedSave`
reads registered keys from `key_dir`, or from `$OPENADAPT_JIT_KEY_DIR`.

If `--guard-state` is the runs folder the console watches, or a folder
directly inside it, the paired phone also shows the request. The receipt binds the approval: its
`evidence.authorization.approver` names a person, and `self_approved` is
`false`.

## Values with a source document

A task can say where its values came from. Add `provenance` (the
`params.provenance.json` intake writes, as an object) and `sources` (paths to
the documents it cites, relative to the task file):

```json
{
  "schema_version": "openadapt.guard-task/v1",
  "task_id": "referral-0001",
  "expected_record": {"patient_id": "p1"},
  "intended_fields": {"note": "Referral to orthopedics for knee pain"},
  "provenance": {"schema_version": "openadapt.param-provenance/v1", "params": {"note": {"value": "Referral to orthopedics for knee pain", "source": {"kind": "document", "sha256": "sha256:...", "line": 3, "label": "Note"}, "extractor": "intake:agent"}}},
  "sources": ["referral.pdf"]
}
```

`begin` reads each source a second time with the deterministic label parse.
If that reading does not confirm a value (a different value, no value, or a
value that the label parse itself produced, which is not a second reading),
the save goes to a person under any approval mode, and the approval request
names the fields. `confirm` then compares the saved record with the source
document, not only with the task, so a misread value that was saved
correctly is still refuted. A task whose values differ from its own
provenance is refused.

## The receipt

`confirm` writes an `openadapt.effect-receipt/v1` file, the same format
`verify-run` writes (see [`VERIFY_RUN.md`](VERIFY_RUN.md) and
[`RECEIPTS.md`](RECEIPTS.md)). It holds no record values, URLs or agent text.
Its `correlation_id` is the `guard_id`. The guard sets
`write_attempted=True` on the read-back, so a store that shows nothing is
`reconciliation_required` with `delivery_uncertain: true`, not
`halted_before_effect`.

Each guard's folder also holds `guard.json` (states, codes and digests
only) and `baseline.json`. The baseline holds raw records and the run salt;
it is written with mode 0600 and never leaves the folder. Delete it when you
no longer need it. `<state>/tasks/<task_id>.json` is the ledger that stops a
second commit.

## Try it

```bash
python -m openadapt_flow.guard_demo
```

The demo starts MockMed on localhost. A fake agent signs in to the chart
pages, opens a chart, and posts a referral to MockMed's API, as a click on
Save would. A simulated person approves each save.

```text
== correct
   agent opened chart p1; task names p1
   guard_begin: identity verified, baseline read
   guard_commit: waiting_for_approval
   person approved the save
   guard_commit: go
   agent clicked Save
   guard_confirm: VERIFIED (receipt outcome verified)
   rows in the system of record: 0 -> 1
== wrong_patient
   agent opened chart p2; task names p1
   guard_begin refused: identity_mismatch
   rows in the system of record: 1 -> 1
== dropped
   agent opened chart p1; task names p1
   guard_begin: identity verified, baseline read
   guard_commit: waiting_for_approval
   person approved the save
   guard_commit: go
   agent clicked Save (server fault: no_persist)
   guard_confirm: RECONCILIATION_REQUIRED (receipt outcome reconciliation_required)
   retry of the same task: refused (needs_reconciliation)
   rows in the system of record: 1 -> 1
```

## Limits

- The identity check reads text the harness supplies. It doesn't take its
  own screenshot yet.
- Guards don't survive a restart of the process that holds them. The task
  ledger does, so a lost guard fails closed.
- There is no command yet to mark a task as reconciled. Until there is, a
  person checks the record and then removes `<state>/tasks/<task_id>.json`.
- One write per guard. A save that writes several records needs one effect
  per record in `verify.yaml`, read back together.
