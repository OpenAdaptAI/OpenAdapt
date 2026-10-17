# Plain words and plain results

This package holds the words OpenAdapt surfaces use for each concept, and the
table that turns a run's outcome into a result a person can act on. Both are
JSON files, so code in any language can read them. Python code can use the
`plain_outcome()` function and the `openadapt-vocab-lint` checker that ship
with the package.

| File | What it holds |
|---|---|
| `openadapt_types/vocabulary/vocabulary.json` | The plain term for each concept, its one-line meaning, and the technical terms that may appear only one level deeper |
| `openadapt_types/vocabulary/outcomes.json` | How every run outcome OpenAdapt emits maps to one plain result, and where each outcome value comes from |
| `openadapt_types/vocabulary/outcome-vectors.json` | 51 hand-written cases that any implementation of `outcomes.json` must pass |
| `openadapt_types/vocabulary/lint.py` | The checker. It uses only the Python standard library |
| `openadapt_types/schemas/plain-outcome-v1.json` | JSON Schema for one plain result |
| `openadapt_types/schemas/plain-progress-v1.json` | JSON Schema for a run that hasn't ended yet |

## Surface levels

Every user-facing file sits at one of three levels. A technical term has a
`min_level`, the shallowest level where it may appear.

| Level | Who reads it | Examples |
|---|---|---|
| `business` | A buyer or a new user, on the first screens they see | Website business pages, the top of a README, the docs Evaluate path, Cloud owner views, Desktop first run, email |
| `detail` | Someone who opened a section on purpose to see the exact term | A Technical details disclosure, an IT or security section, a run detail view, partner integration pages, CLI `--verbose` output |
| `engineering` | Engineers and auditors | Reference docs, evidence views, `report.json`, API fields, code |

The plain term works at every level. Every technical term stays allowed at
the engineering level, so engineers and auditors keep the exact name.

## The vocabulary

| Plain term | Technical terms one level deeper | Meaning |
|---|---|---|
| workflow | | The task you hand off, such as entering a referral. |
| showing the task, or recording | demonstration, capture, trace | A person does the task once while OpenAdapt watches. |
| the automation | program, compiled program, bundle, compile | What OpenAdapt builds from the recording. |
| run | replay | One time the automation does the task. |
| Done and checked | VERIFIED | It saved the entry and read the record back to confirm it. |
| Stopped before saving | HALTED_BEFORE_EFFECT, CANCELED | Something didn't match, so it stopped before changing anything and asked a person. Nothing was written. |
| Check the record | RECONCILIATION_REQUIRED, ROLLED_BACK, reconciliation | A save may have gone through. A person checks the record before anything is retried. |
| Finished, not checked | COMPLETED_UNVERIFIED | It did the steps but didn't confirm the saved result. |
| Didn't finish | FAILED_PLATFORM, FAILED | It couldn't complete the task. The next step says what's known about whether anything was written. |
| Blocked before saving | REJECTED_POLICY | A rule, an approval, or the right-record check blocked the run before it saved anything. Nothing was written. |
| receipt, or signed receipt | Seal, attestation | A record of what was entered and how it was checked. |
| readiness test | qualification, qualification profile, admission, certification | Testing the automation on your system with your cases before it goes live. |
| approved fix | governed repair, repair candidate | When a screen changes, OpenAdapt proposes a fix and a person approves it. |
| screen changes | drift, re-resolve, self-healing | The app looks different, so OpenAdapt finds the same field again or stops. |
| right-record check | identity gate, wrong-patient defense | It confirms it's in the right patient's chart before typing. |
| record check | effect check, read-back, system-of-record read, effect contract, oracle | It reads the saved record back from the system to confirm the change. |
| a person steps in | attended operation, takeover | Someone takes over or decides, then the automation continues. |
| the computer that runs it | runner | A machine you control, or one we host. |

Some internal words have no plain term. Say what they mean instead: governed,
Standard profile, substrate, fixture, REST+SQL parity, non-target delta,
sha256, Program State Console, and MCP. `vocabulary.json` gives a suggestion
for each one.

A bare `HALTED` has no plain term either, because on its own it doesn't say
whether anything was written. Say "Check the record". Say "Stopped before
saving" only when `transaction_outcome` is `HALTED_BEFORE_EFFECT`. The
registry refuses an outcome value filed under a result it doesn't read as on
its own, so the lint can't suggest the wrong words.

A pattern matches the word, not its sense. "Admission" also means a hospital
admission, and "capture" and "program" have everyday meanings. When a business
page uses one of those senses, add a `vocab-allow` comment that says so.

## Run results

A run ends in one of six results. `outcomes.json` maps every outcome value to
one of them.

| Result | Label | May have changed something | Safe to retry | Comes from |
|---|---|---|---|---|
| `done` | Done and checked | yes | no | `VERIFIED`, `verified` |
| `stopped_before_change` | Stopped before saving | no | yes | `HALTED_BEFORE_EFFECT`, `halted_before_effect`, `CANCELED` |
| `check_the_record` | Check the record | yes | no | `RECONCILIATION_REQUIRED`, `reconciliation_required`, `waiting_for_reconciliation`, `ROLLED_BACK`, `rolled_back_verified`, a coarse `HALTED` or overlay `halted` alone, a timeout after the run started, and anything unknown |
| `finished_not_checked` | Finished, not checked | yes | no | `COMPLETED_UNVERIFIED`, and a coarse success that doesn't show a record check |
| `did_not_finish` | Didn't finish | depends | depends | `FAILED_PLATFORM` and `failed_platform` (nothing written), or a coarse `FAILED` or error (unknown) |
| `refused` | Blocked before saving | no | yes | `REJECTED_POLICY`, `rejected_policy`, an Agent refusal before the run started |

"Safe to retry" means a new attempt can't duplicate or collide with a write
from this attempt. It doesn't promise that the new attempt will succeed.

### Which value decides

Outcome values come in three tiers. The first tier that has a value decides.
The one exception only adds caution: when `transaction_outcome` and
`execution_outcome` disagree, the result is "Check the record".

| Tier | Sources | Where they come from |
|---|---|---|
| 1 | `transaction_outcome`, `execute_terminal_outcome`, `execute_state` | Flow's `report.json` and hosted callbacks; an Execute status, receipt, or webhook |
| 2 | `execution_outcome` | Flow's coarse outcome |
| 3 | `agent_status`, `connector_status`, `clinic_tool_status`, `program_terminal_state`, `overlay_phase` | The Agent, a Flow connector, a clinic tool result, where the program graph ended, and the on-screen overlay |

Tiers 2 and 3 are coarse. A halt from them never says that nothing was written. <!-- vocab-allow: no-change-claim -- states the rule, not a run result -->
An Agent timeout or error, or a connector failure, says so only when you pass
`dispatched=False` because the request never reached the app.

### The safety rule

The plain result comes from the precise outcome: Flow's `transaction_outcome`
or the Execute `terminal_outcome`. A coarse `HALTED` on its own never proves
that nothing was written, so it reads "Check the record".

Only an outcome that proves no effect sets `may_have_changed` to false and
`safe_to_retry` to true. The registry validator keeps a fixed list of the
eleven rows allowed to say so in `lint.py` (`NO_EFFECT_PROOFS`), and refuses
any other row that says nothing changed. Adding a row to that list is a safety
review.

These inputs always fail closed to "Check the record":

- a value the table doesn't know, including a new engine value;
- inputs that disagree, such as a `transaction_outcome` paired with an
  `execution_outcome` it can't have;
- no outcome at all;
- `delivery_uncertain=True`, whatever the outcome says.

The phrase "Stopped for review" may appear only where a surface claims
nothing about whether a write happened.

### Proof is separate from the result

`proof` says what backs the result: `sealed` for a signed OpenAdapt receipt
whose signature you checked, `local` for a run report on the computer that
ran it, and `none` when only the status value backs it. A verified write with
only local proof is still `done`. Whether local proof is enough is a policy
decision for the caller, not a reason to report a saved entry as an error.

## Use it from Python

```python
import json
from pathlib import Path

from openadapt_types import plain_outcome, plain_outcome_for_run_report

result = plain_outcome(
    transaction_outcome="RECONCILIATION_REQUIRED",
    execution_outcome="HALTED",
)
result.label             # "Check the record"
result.may_have_changed  # True
result.safe_to_retry     # False
result.next_action       # "A save may have gone through. Check the record before anyone runs this again."
result.technical_code    # "transaction_outcome=RECONCILIATION_REQUIRED"
result.exact             # the status codes it came from, for a Technical details view

report = json.loads(Path("run/report.json").read_text())
plain_outcome_for_run_report(report)   # proof is "local"
```

Pass every outcome value you have. The function picks the most precise one.
Other helpers read an `ExecuteStatusV1`, an `ExecuteEvidenceReceiptV1`, and a
terminal Execute webhook. The webhook helper reports `sealed` proof only after
the HMAC signature checks.

Don't pass the outcome on a process receipt (`ProcessEvidenceReceiptV1`) as
`execute_terminal_outcome`. It's the outcome of the step that ended the
process, and an earlier step may already have saved. `outcomes.json` lists it
under `not_run_outcomes` for that reason.

Set `dispatched=False` only when you know the request never reached the app,
for example when the Agent timed out before Flow started. Pass Flow's hosted
callback field `started` as `dispatched` and `uncertain_delivery` as
`delivery_uncertain`.

A run that hasn't ended has no result. `plain_outcome()` raises
`PlainOutcomeInProgress` for it, and the exception carries a
`PlainProgressV1` with a label such as "Waiting for a person".
`plain_outcome_for_execute_status()` returns the progress object instead of
raising.

A control overlay frame carries only a phase. Pass it as `overlay_phase`. An
end phase such as `halted` reads the same as the coarse outcome it mirrors. A
phase such as `executing` or `paused` raises `PlainOutcomeInProgress` with the
"Running" or "Waiting for a person" label. `idle` and `recording` aren't runs.
On their own they count as no outcome at all, which reads "Check the record".

`PlainOutcomeV1` checks itself, so a hand-built or deserialized result gets
the same checks. It refuses a label that doesn't match the result, a "nothing
changed" result with `may_have_changed=True`, a next step that claims nothing
was written when something may have been, and `sealed` proof without an
Execute outcome. It also refuses a result, `may_have_changed`, or
`safe_to_retry` that differs from what the table gives for the values in
`exact`. A `fallback` code only adds caution, so it's accepted with any
values.

## Use it from another language

1. Copy `outcomes.json`, or read it from the installed package.
2. Apply the numbered `merge_rules` in the file, in order.
3. Run every case in `outcome-vectors.json` against your code. Each case names
   the rule it tests.

The TypeScript surfaces (Desktop, Cloud, the website) can vendor both JSON
files the same way they vendor design tokens, with a test that fails when the
copy differs from the pinned release.

## Check text with the lint

Install the package, or copy the whole `openadapt_types/vocabulary`
directory and run the copied `lint.py`. The copy needs only Python 3.10 or
later.

```bash
openadapt-vocab-lint terms --level business README.md site/pages
openadapt-vocab-lint claims src docs
openadapt-vocab-lint check --config .vocab.json
python -m openadapt_types.vocabulary claims src
python vendor/vocabulary/lint.py terms --level business README.md
```

| Command | What it checks |
|---|---|
| `terms --level business\|detail` | No technical term deeper than the file's level |
| `claims` | No "nothing was written", "did not change", "stopped safely", or "safe to re-run" next to a halt, stop, failure, or timeout unless an outcome that proves no effect is nearby <!-- vocab-allow: no-change-claim -- names the phrases the check looks for --> |
| `coverage` | Every value of an outcome enum has a plain result |
| `check --config .vocab.json` | Whatever the config asks for |

Exit status is 0 when clean, 1 for an error finding (or any finding with
`--strict`), and 2 for a usage or registry problem. `--format json` prints
the findings as JSON.

A `.vocab.json` file maps paths to levels:

```json
{
  "levels": {
    "business": ["README.md", "src/screens/Onboarding.tsx"],
    "detail": ["docs/guides/*.md"]
  },
  "claims": ["src", "docs"],
  "exclude": ["*/vendor/*"],
  "coverage": {"values": {"agent_status": ["success", "halt", "refused", "timeout", "error"]}}
}
```

Two escape hatches, both of which need a reason:

- `vocab-allow: <term> -- <reason>` on the line or the line above. Use
  `no-change-claim` as the term to allow a claim, for example when you quote
  old wording to explain a fix.
- Lines between `vocab: advanced` and `vocab: end` comments, or inside
  `<details data-level="advanced">`, are skipped by `terms`. Use them for a
  Technical details section.

## Keep every outcome covered

Tests in this repository fail when an enum here gains a value without a row.
They also fail when a new enum here holds run outcome values, such as
`verified` or `halted`, and `outcomes.json` neither maps it nor sets it aside
under `not_run_outcomes` with a reason. Flow and the Agent define the other
outcome values, so their CI should run the coverage check too:

```bash
# openadapt-flow
openadapt-vocab-lint coverage \
  --enum openadapt_flow.transaction:TransactionOutcome=transaction_outcome \
  --enum openadapt_flow.ir:RunReport.transaction_outcome=transaction_outcome \
  --enum openadapt_flow.ir:RunReport.execution_outcome=execution_outcome \
  --enum openadapt_flow.ir:ExecutionOutcomeEnvelope.outcome=execution_outcome \
  --enum openadapt_flow.ir:State.outcome=program_terminal_state \
  --enum openadapt_flow.runner.hosted_adapter:HostedTerminalEventV2.outcome=transaction_outcome \
  --values connector_status=success,halt,failed

# openadapt-agent
openadapt-vocab-lint coverage --values agent_status=success,halt,refused,timeout,error
```

`--enum` takes an Enum class, a `Literal` alias, or a Pydantic model field.
Add `--partial` for an emitter that sends only some of a source's values.

`outcomes.json` lists each emitter it was checked against under `emitters`,
with the commit it was read at. It lists the states it leaves out on purpose,
each with a reason, under `not_run_outcomes`.

## Checked against

On 2026-10-09:

- The Flow coverage command above, plus `HostedTerminalEventV1.outcome` and
  `terminal_verification_v2:ProductionExecutionOutcome.outcome`, ran against
  `openadapt-flow` origin/main `6e4e4ed0` with 0 errors.
- The overlay rows match Flow's `runtime/control_overlay.py` at the same
  commit: `emit_terminal` sets each end phase from exactly one
  `execution_outcome`, and the replayer emits only `observing`, `executing`,
  and `verifying` mid-run.
- `openadapt-vocab-lint claims` on `openadapt-agent` origin/main `88b1dbd`
  (README, `llms.txt`, the skill, and the runner, copy, and MCP modules)
  reported 10 claims that a halt alone can't support, including "Tell the <!-- vocab-allow: no-change-claim -- quotes the wording the check found -->
  user the record did not change" in `runner.py`, `copy.py`, and the MCP
  server instructions.
