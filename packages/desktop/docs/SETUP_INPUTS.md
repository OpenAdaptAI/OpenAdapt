# From "I recorded something" to "it runs checked": what a person gives

This page counts every input a person gives OpenAdapt Desktop between a
finished recording and the first real item. It compares
the screens before this change (Calm UI, PR #1310) with the screens after it,
on the same recording of the same app, and links a screenshot of each input.

## How it was measured

`scripts/setup_walk/walk.py` drives the Desktop UI (from `npm run dev`) in a
headless browser against the real Desktop engine, real Flow, and MockMed's
transactional server (`openadapt_flow.mockmed.fault_server`, `?fault=ok`).
Nothing is mocked: every answer the screens show comes from the engine, and
the practice item, the drills and the first item are real Flow runs that save
to MockMed's store. All data is synthetic.

The recording is the MockMed triage task Flow's tutorial records: open the
first referral, start a triage encounter, type a note, save. Desktop's
recorder doesn't keep the app's network traffic yet, so the walk writes the
recording's `network.jsonl` itself, in the `openadapt.network-capture/1`
format: the app's real save request and one read of `GET /api/db` right after
the save. That read stands in for the screen an app shows after saving; Flow's
own record-check tests build the same MockMed recording
(`packages/flow/tests/_record_check_fixtures.py`).

What counts as an input:

| Kind | Meaning |
| --- | --- |
| decision | A button that commits an answer. |
| choice | A radio button, select or checkbox the person has to set or check. |
| free text | A field the person types into. |
| confirmation | A statement the person confirms, including approving a save. |
| navigation | Opening a screen or a folded section. Counted apart. |
| item | The first item's own value, its Run button and its save approval. Running any item needs these, before and after. Counted apart. |

## The counts

| | Before (#1310) | After |
| --- | --- | --- |
| Setup inputs, recording to approval | 10, then blocked | 4: three decisions and one save approval |
| of which free text | 2 (a field name, a settings file path) | 0 |
| Navigation | 4 | 2 |
| Certified, sealed and approved | No | Yes |
| First item ran | No | Yes, gated by the person's approval; Flow's record check confirmed the save |
| First item shown as "done and checked" | No | No: it needs a signed qualification admission (see below) |

After this change the person makes three decisions and approves one save:

1. **Is this the right record check?** Yes.
2. **Which values change per item?** Yes, practice once with these test values.
   The practice item then stops before its save until the person approves it
   (the confirmation below).
3. **Approve?** Approve this workflow.

The practice save's approval stays a person's: it is the one real save
before the workflow is approved, and Flow's each-item gate holds it.

With the person's agent answering over MCP (the `agent` walk), the person
gives two inputs: the practice save's approval and Approve.

## Before: where it stopped

Ten inputs in, Flow refused the practice item before it started. Check it
showed "The practice run didn't pass" with "Qualification run did not retain
a valid report" as the detail. Flow's actual reasons were:

- the clinical-write policy needs an idempotency key on the save's record
  check, and Check it's form has no field for one (MockMed sends a fixed key,
  not a typed value, and the exact checks only take a key from a typed value);
- five steps stayed unclassified until a person classifies each in Advanced:
  exact checks, a page about 10,000 pixels tall
  ([screenshot](setup-walk/before/17-exact-checks.png)).

Under those were four more problems, each of which also stopped a Check it
run on a web app:

- Desktop started the check without an environment observer a web app with
  no OpenAdapt markers or session cookie can satisfy;
- the project got no identity policy for the save when the compiled step was
  already armed, so part 2 showed "Confirmed" while Flow refused;
- Desktop stopped writing `run-report-receipt.json` in July but still required
  it, so no practice item or drill result could be recorded;
- Desktop retained only the run report, not the frames it names, so Flow
  couldn't credit a drill.

And one in the approval itself: approving certified the plaintext version and
then sealed a copy, but Flow binds case results to the exact executable
workflow, so the sealed copy was never certified and couldn't run.

| # | Kind | Input | Screenshot |
| --- | --- | --- | --- |
|  | navigation | Check it | [01-workflows.png](setup-walk/before/01-workflows.png) |
| 1 | decision | Start the check with the app from your recording | [02-start-check.png](setup-walk/before/02-start-check.png) |
| 2 | choice | What does this step save? (It adds a new entry) | [03-effect-kind.png](setup-walk/before/03-effect-kind.png) |
| 3 | choice | Which value identifies the record? (Note) | [04-effect-match-param.png](setup-walk/before/04-effect-match-param.png) |
| 4 | free text | What is that ID called in your system's records? (note) | [05-effect-match-field.png](setup-walk/before/05-effect-match-field.png) |
| 5 | choice | How should OpenAdapt read the record back? (system's records) | [06-effect-tier.png](setup-walk/before/06-effect-tier.png) |
| 6 | decision | Save how it confirms the save | [07-effect-save.png](setup-walk/before/07-effect-save.png) |
|  | navigation | Show part 3 (its confirm button is inside the fold) | [08-unfold-part-3.png](setup-walk/before/08-unfold-part-3.png) |
| 7 | decision | These look right | [09-these-look-right.png](setup-walk/before/09-these-look-right.png) |
|  | navigation | Change where it runs | [11-open-where.png](setup-walk/before/11-open-where.png) |
| 8 | free text | Deployment settings file (for IT) | [12-practice-settings-file.png](setup-walk/before/12-practice-settings-file.png) |
| 9 | confirmation | These are test values, and I'll keep the app in view | [13-practice-attest.png](setup-walk/before/13-practice-attest.png) |
| 10 | decision | Run the practice item | [14-practice-run.png](setup-walk/before/14-practice-run.png) |
|  | navigation | Advanced: exact checks (the 5 unclassified steps) | [17-exact-checks.png](setup-walk/before/17-exact-checks.png) |

## After

The walk took 10 minutes, most of it the practice item and five drills.

| # | Kind | Input | Screenshot |
| --- | --- | --- | --- |
|  | navigation | Check it | [01-workflows.png](setup-walk/after/01-workflows.png) |
| 1 | decision | Is this the right record check? Yes, use this check | [02-q1-record-check.png](setup-walk/after/02-q1-record-check.png) |
| 2 | decision | Which values change per item? Yes, practice once with these test values | [03-q2-values.png](setup-walk/after/03-q2-values.png) |
| 3 | confirmation | Approve and save (the practice item's save) | [04-practice-approve-save.png](setup-walk/after/04-practice-approve-save.png) |
| 4 | decision | Approve? Approve this workflow | [05-q3-approve.png](setup-walk/after/05-q3-approve.png) |
|  | navigation | Run it | [07-run-it.png](setup-walk/after/07-run-it.png) |
|  | item | Note (the first item's own value) | [08-item-note.png](setup-walk/after/08-item-note.png) |
|  | item | Run 1 item | [09-item-run.png](setup-walk/after/09-item-run.png) |
|  | item | Approve and save (the first item's save) | [10-item-approve-save.png](setup-walk/after/10-item-approve-save.png) |

## The first item, and the step that's still missing

After approval the walk runs one real item from Run it: it types a new note,
clicks Run 1 item, and approves the save. Flow's report for that run shows the
right-record check verified on screen and the save confirmed by MockMed's
store at the strongest tier. Desktop still shows it as **Finished, not
checked** and sends it to Needs you. That is correct, and it is a decision
for the founder, not something Check it should infer:

- A run gets Flow's transaction outcome ("done and checked") only under the
  Standard or Regulated profile.
- Under those profiles Flow refuses to act without a **signed qualification
  admission**: "Standard and Regulated actuation requires a signed
  qualification admission. Nothing was executed." Today that admission comes
  from OpenAdapt Cloud or the organization's signer. Approving in Check it
  doesn't produce one.
- So the learned record-check settings don't select a profile, the same as an
  IT settings file without one, and the item runs gated per save but
  unclassified.

The open question is whether a person's Approve in Check it should sign a
local admission for this computer, or whether admission stays with Cloud.

## With the person's agent

The `agent` walk runs the same recording with an agent doing the setup. Its
calls go through `openadapt_desktop.agent_mcp` to the engine's local socket,
exactly as an MCP client's would:

1. `list_workflows`
2. `get_check_setup` with `{"workflow_id": "24ad4eda"}`
3. `answer_record_check` with `{"workflow_id": "24ad4eda"}`
4. `answer_values` with `{"workflow_id": "24ad4eda", "changes": ["note"]}`
5. `run_checks` with `{"workflow_id": "24ad4eda"}`

The person gives two inputs, both in the Desktop window: approving the
practice item's save, and Approve. The window says the agent answered and
lists what the person is approving
([screenshot](setup-walk/agent/03-q3-approve.png)).

| # | Kind | Input | Screenshot |
| --- | --- | --- | --- |
|  | navigation | Check it | [01-workflows.png](setup-walk/agent/01-workflows.png) |
| 1 | confirmation | Approve and save (the practice item's save) | [02-practice-approve-save.png](setup-walk/agent/02-practice-approve-save.png) |
| 2 | decision | Approve? Approve this workflow | [03-q3-approve.png](setup-walk/agent/03-q3-approve.png) |

## What changed

- **Pre-filled from the recording** (`openadapt_desktop/check_setup.py`,
  engine command `get_check_setup`):
  - the record check, from the read after the save (Flow's record-check
    synthesis, #1284), bound as the same effect a person would type in the
    form, with the app's own anti-double-save key and the fields the save
    sent. Its fault test runs on exactly that binding, and Check it says what
    it caught and what it can't catch;
  - which steps only read: the network capture shows the app sent data only
    when the person saved;
  - the right-record check from the recording, the app, and its environment;
  - which values change per item, from one recording or from several
    recordings of the same workflow (a value the same in all of them is
    proposed as fixed).
- **Only three questions.** A yes sends the same qualification commands the
  long form sends (`answer_check_setup`). "Change" opens the parts. Without a
  learned record check, the parts ask as before.
- **Values that don't change** are filled in by `run_item` with the recorded
  value, and Run it doesn't ask for them.
- **The version that runs is the version checked.** Check it seals the
  workflow before the practice item, so the cases, the approval and every run
  bind one sealed version.
- **Agents.** `python -m openadapt_desktop.agent_mcp` is a stdio MCP server
  that forwards to the running Desktop's local socket. An agent can read the
  setup, answer the first two questions and run the checks. Its answers are
  recorded as the agent's, and the window says so. It can't approve a
  workflow or a save.

Nothing here certifies or approves on its own, and no check is weaker: the
learned binding is the form's binding with more fields matched, the record
check runs under the Standard profile, and Flow still compares every
environment, identity and evidence digest.

## Limits

- Desktop's recorder doesn't keep network traffic yet. Until it does, a real
  Desktop recording has no read to learn from, and Check it asks the parts.
- Desktop binds a learned check only when it is a REST read on one saving
  step. A check across several tables, or a FHIR or SQL read, goes through
  the form or a settings file from IT.
- The learned check here misses three kinds of bad save on MockMed (listed on
  the screen). The right-record check before the save covers a save on
  another patient; a stray write to another table isn't covered.
- MockMed's demo key is the same for every save on one patient, and its
  server keeps only the first save with a key. The walk empties MockMed's
  test store before the practice item and the first item, as Flow's tutorial
  does before each run.
- Flow's generic web observer binds the page load, so an app that reloads the
  whole page mid-run stops at the next input. That fails closed; such an app
  needs the signed-in browser path, which binds the session instead.
- While the agent's practice item runs, it holds the engine's command queue,
  like any run. The approval dialog still opens (approvals bypass the queue),
  but the window shows the agent's answers only once the run ends.
- When Flow refuses a run before it starts (exit 2, "Nothing was executed"),
  Desktop reports that the save may have gone through. That is safe but
  wrong, and it is not fixed here.

## Reproduce

```bash
cd packages/desktop
npm run dev
OPENADAPT_DESKTOP_FLOW_BIN=<openadapt-flow> python scripts/setup_walk/walk.py --variant after --out docs/setup-walk/after
```

`--variant before` runs the steps for the screens on commit 926d6c940.
`--variant agent` has an agent answer over MCP while the person approves.
`python scripts/setup_walk/shrink.py <folder>` shrinks the screenshots.
