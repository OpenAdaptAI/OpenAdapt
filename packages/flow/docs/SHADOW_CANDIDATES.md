# Shadow-mined candidates

OpenAdapt Capture's shadow mode (off by default) can notice that a person
repeated the same cross-application task and, when the person clicks
**Review draft**, export the matching episodes. This page explains what Flow
does with that export. Capture's
[SHADOW_MINING.md](https://github.com/OpenAdaptAI/openadapt-capture/blob/main/docs/SHADOW_MINING.md)
covers how the episodes are mined and the privacy posture.

The code is `openadapt_flow/compiler/motif_induction.py`.

## What goes in

An `openadapt.shadow.episodes/v2` JSON document. Each episode is a list of
value-free symbols: an application, a control role, a scrubbed URL pattern,
short plain-text labels for buttons and menu items made of common words, and
references in place of other labels and typed values.

A reference such as `label-a` or `value-c` is local to one export. Two steps
with the same reference had the same label or value; that is all the export
says. Capture's keyed digests never leave Capture: its keys are destroyed
every 24 hours, and a digest that outlived its key could be matched against
guesses for short identifiers such as a record number or an SSN, so no digest
may reach a draft.

The schema is strict. `induce_candidate` refuses:

- a `v1` document (it carried keyed digests);
- a step with a `label_digest` or `value_digest` field, or any other unknown
  field;
- a token with a hex part of eight or more characters;
- a malformed reference;
- a plain-text label that contains digits or `@`;
- an unknown step kind, or a status other than `candidate`.

A label that Capture scrubbed (`<dynamic_label>`) is treated as no label.

## What comes out

```python
from openadapt_flow.compiler.motif_induction import induce_candidate, write_candidate

candidate = induce_candidate(export)          # dict or ShadowEpisodesExport
write_candidate(candidate, "shadow-candidates/abc")
```

`induce_candidate` turns each episode into a linear trace and runs the
existing multi-trace induction (`induce_program`) over them. Induction infers
parameters, loops, and optional steps, or refuses, exactly as it does for
recorded demonstrations. The result is a `MotifCandidate`:

| Field | Meaning |
|---|---|
| `status` | Always `candidate`. |
| `admitted`, `runnable` | Always `False`. The model rejects any other value. |
| `induction_certified` | Whether induction found the episodes structurally consistent. This is a statement about shape only. |
| `draft` | The induced program, or `None` when induction refused. |
| `record_next` | What to record or decide next. The first item is always "record this task once with OpenAdapt Capture". |
| `proposed_checks` | Verification checks the episodes suggest. |
| `admission_path` | The steps that turn the candidate into a program. |

`write_candidate` writes `candidate.json` and, when there is a draft,
`draft-workflow.json`. It never writes `workflow.json`, so no Flow command
treats the folder as a bundle, and it refuses a folder that already holds one.

## Why a candidate can't run

- **No anchors.** Shadow mining keeps no pixels, so every anchor in the draft
  is a placeholder (`shadow-unrecorded.png` at 0,0).
- **No values.** Parameter examples are references such as `<email:value-b>`.
- **Writes need review.** A click on a Submit-, Save-, or Send-like control,
  or a click right before a form submit, is marked `irreversible` with
  `risk_review_required`, so certification refuses it until a person reviews
  it.
- **Proposals aren't contracts.** A proposed check (`file_written`,
  `record_written`, `row_added`, `page_reached`) has `status="proposed"` and
  `admitted=False`, and is never attached to a step as an effect. The episodes
  that suggested a check can't approve it.

## How a candidate is admitted

Record the task once with Capture. Then run one command:

```bash
openadapt flow admit --candidate abc --out admissions/abc --name report-mail
# or, with your own recordings of the same task:
openadapt flow admit rec-1 rec-2 --out admissions/report-mail --name report-mail
```

(`openadapt-flow admit` without the launcher.) `--candidate` reads
`shadow-candidates/abc/` and the recordings in its `recordings/` folder.
The command compiles each recording, induces one program from two or more
recordings, and proposes checks. Then it stops **once** and prints a short
summary:

```text
Admission 3f9c0a1b2d4e: report-mail
  from shadow candidate abc
  2 recording(s), induced into one program; 6 step(s), 1 write(s)
  Proposed checks (applied on accept):
  - send: field_equals, tier 1
  - send: identity:canonical_ladder
  4 fault case(s) to qualify against
  Suggested by shadow episodes (not evidence, not applied):
  - file_written: the spreadsheet is saved
Type accept to apply these checks, or reject:
```

`accept` applies the checks with the same gates as `qualify accept`,
including the oracle gate, and reseals the bundle. Then qualify it as usual
with `openadapt flow qualify admissions/abc/bundle --verify verify.yaml`.
Nothing certifies until that campaign passes.

The command stops early and asks for nothing when there is nothing to
accept:

- no recording yet (it prints the candidate's record-next list);
- induction refuses;
- the check proposal HALTs.

Without a terminal, it writes the summary to `admission-pending.json` and
exits 3. A person decides later with `openadapt flow admit decide DIR`.

### Letting an agent decide

The same steps are MCP tools when the operator starts the agent server with
`--admissions DIR`:

```bash
openadapt-agent serve --mode production --bundles ./bundles \
  --admissions ./admit --delegate-approvals agent:reviewer \
  --approver-grant ./reviewer-grant.json
```

- `prepare_admission` takes recording folder names under
  `DIR/recordings` or a `candidate_id`, and returns the summary and a
  `pending_sha256`.
- `list_admissions` lists admissions and their status.
- `decide_admission` takes the admission, the `pending_sha256` and
  `accept` or `reject`.

Without `--delegate-approvals`, `decide_admission` asks a person in the
client's own form, and the record says `person:mcp-form via mcp`. With it,
the agent decides as the delegated approver, but only under the approver
grant in `--approver-grant` that a registered person signed
(`openadapt-flow approver-grant sign`; see
[`APPROVAL_AND_VERIFICATION.md`](APPROVAL_AND_VERIFICATION.md)). Flow checks
the grant on every decision: it must verify against the signer registry, name
the agent, cover the workflow, and let an agent approve consequential saves.
Without such a grant the agent's decision is refused. The record says
`agent:reviewer via mcp under grant <digest> signed by <person>` and keeps the
signed grant.

The rules are the same for every decider:

- **Who decided is recorded.** `admission-decision.json`, in the admission
  folder and in the accepted bundle, names who prepared and who decided, by
  kind, id and route.
- **No self-approval.** An agent can't decide an admission it prepared. A
  server started without `--delegate-approvals` records its preparer as
  `agent:mcp-client`, which names no one; only a person decides those.
- **Signed by this machine.** `admission-pending.json` and
  `admission-decision.json` are signed with the local admission key (the JIT
  key directory, `$OPENADAPT_JIT_KEY_DIR` or `~/.openadapt/jit`). An edited or
  hand-written file is refused, so write access to the admission folder or
  the bundle is not enough to change who prepared or decided. Keep the key
  directory away from the agents it governs.
- **The agent sees codes, not screen text.** The summaries the MCP tools
  return carry step ids, check kinds, counts and record-next codes. Induction's
  record-next lines and halt reasons can quote labels from the recordings, so
  they are shown only at the terminal.
- **The `regulated` policy pack needs a person.**
- **A decision covers exactly what was shown.** It names the
  `pending_sha256` of the summary, and the bundle must still match its
  digest. There is one decision per admission.
- **No gate is relaxed.** Shadow suggestions are never applied, a HALTed
  proposal can't be accepted, and no release admission is signed here.

### What admission changes for runs

Accepting admits the workflow to the `problems_only` approval mode. Its runs
no longer wait before every save. They stop only when a check fails, the
screen changed, or a step falls outside what was qualified, and then a person
decides. Read-back is still required: a save counts as done without a person
only when OpenAdapt read the saved record back from your system's data and it
matched. Per-save approval (`each_item`, with one-tap approval from a phone
paired to the Desktop decision portal) stays available as the strictest
setting. `openadapt-flow run` reads this from `admission-decision.json` in the
bundle and prints who admitted it. Five things keep it narrow:

- A `--approval-mode` flag or the deployment's `runtime.approval_mode` still
  wins.
- The decision record must carry a valid signature by the local admission
  key. A record without one, or edited after signing, gives no mode.
- It applies only to the exact admitted bundle. After any change to the
  bundle, each save waits for a person again.
- An agent's admission applies only while its signed grant still verifies.
  If the grant expires, is swapped or no longer names the agent, each save
  waits for a person again.
- `problems_only` keeps its own gates (see "Earning only problems" in
  [`APPROVAL_AND_VERIFICATION.md`](APPROVAL_AND_VERIFICATION.md)). Until the
  workflow passes the readiness test and its switch is on, every save still
  waits for a person, and repeated problems switch it back.

## Example

The test fixtures in `tests/fixtures/shadow/` come from Capture's miner on
synthetic streams:

- `report_mail`: save a spreadsheet, then mail it. Induction emits a draft
  with two parameters (recipient and subject) and proposes two checks (the
  file changes; a message is sent).
- `invoice_exact`: copy an invoice number from a spreadsheet cell into a web
  form. The cell differs every time, so induction refuses with
  `ambiguous_selection` ("which row?"), and the candidate has no draft.
- `invoice_variant`: the same task with an extra check box in one episode.
  Induction reads it as an optional step.
