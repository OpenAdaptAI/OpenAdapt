# Run ledger

`openadapt-flow ledger` keeps one accounting row per run, whatever did the
work: an OpenAdapt agent, a compiled program, another vendor's agent, or a
person. A row says how the run ended, why, how long it took, what its model
calls cost, how many minutes a person spent on it, which fields it wrote and
whether each one checked out. It holds no record values.

```bash
openadapt-flow ledger add RUN_DIR --task-type intake
openadapt-flow ledger summary --since 2026-11-01
openadapt-flow ledger export --out rows.jsonl
OPENADAPT_API_KEY=... openadapt-flow ledger push
```

`add` reads files that other commands already wrote. It never changes them,
calls a model, or uses the network.

## The row

Schema `openadapt.run-ledger/v1`, defined in
`openadapt_types.run_ledger.RunLedgerRowV1`:

| Field | Meaning |
| --- | --- |
| `row_id` | A new UUID per row. |
| `task_id` | The task id a source names (guard task, flight, shadow write). Without one, `run-` and a digest of the run's path. |
| `task_type` | The operator's label from `--task-type` (default `unspecified`). Lower-case words only, so it can't carry a name. |
| `executor_kind`, `executor_label` | `agent`, `program`, `human` or `unknown`, and a short label such as `browser-use`. |
| `inputs_commitment` | `sha256:` of a random salt plus the run's inputs. The salt is only in the private sidecar. `null` when no source names the inputs. |
| `started_at`, `ended_at`, `duration_s` | From sources that keep wall-clock times. Relative or monotonic clocks give a duration only. |
| `model` | `calls`, `input_tokens`, `output_tokens`, `cost_usd`, and `cost_source` (`metered`, `reported` or `unknown`). A value a source doesn't give is `null`, never `0`. |
| `human` | `intervals` (`approval`, `question`, `exception`, `second_factor`, each with `opened_at` and `decided_at`) and `intervention_s`, their sum. If any interval has no `decided_at`, `intervention_s` is `null`. With no intervals in the run's files it is `0`. |
| `outcome`, `outcome_reason` | See below. |
| `fields_written`, `fields_differing` | Field names with `matched`, `differs` or `not_checked`, and how many differ. Names only. |
| `readback_tier` | The weakest read: `independent_readback`, `screen_readback` or `unchecked`. |
| `receipt_digest` | The run receipt's digest, when there is one. |
| `sources` | The files the row came from, relative to the run directory. |

Every string field is a closed pattern or enum, and the models forbid
unknown keys, so a value has no field to travel in.

## The private sidecar

`add` writes `$OPENADAPT_HOME/ledger/private/<row_id>/ledger.private.json`
(mode 0600, directory 0700). It holds the inputs salt and, for each field a
source gave values for, what was meant to be saved, what was saved, and a
diff. Only `ledger show ROW_ID --show-values` prints it. `export` and `push`
look for every sidecar value in what they would send and refuse on a match.

## The outcome rule

Five buckets. Each `outcome_reason` belongs to exactly one, and a row whose
reason names another bucket is refused.

| Bucket | When | Reasons |
| --- | --- | --- |
| VERIFIED | An independent read-back verified the save and no hold is pending. | `readback_verified` |
| ESCALATED | The run stopped for a person, or a person did the step. | `approval_held`, `approval_declined`, `question_asked`, `exception_card`, `second_factor`, `halted_before_effect`, `gate_held` |
| CAUGHT_ERROR | A check caught a wrong save. | `readback_refuted`, `save_blocked_wrong_record`, `save_blocked_other_origin`, `save_blocked_double_save`, `save_blocked_out_of_scope`, `gate_refused`, `shadow_field_mismatch` |
| FAILED | Crashed, timed out, canceled or gave up, with no evidence of a write. | `crashed_no_write`, `timed_out_no_write`, `agent_gave_up`, `platform_failed`, `canceled_no_write`, `policy_rejected_no_write` |
| UNKNOWN | Nothing settles it. Never folded into another bucket. | `completed_unverified`, `reconciliation_required`, `screen_readback_only`, `lost_reply`, `write_unconfirmed`, `saved_awaiting_check`, `rolled_back`, `self_approved`, `observe_only_would_block`, `schema_unrecognised`, `source_unreadable`, `task_id_conflict`, `unmapped_outcome`, `no_sources` |

When sources disagree, the stronger finding wins:
CAUGHT_ERROR > UNKNOWN > ESCALATED > FAILED > VERIFIED. A run in which the
guard blocked a save on the wrong record and the agent then saved the right
one is CAUGHT_ERROR: the row records the catch.

Two more rules:

- **A model never verifies.** An agent's own "finished" (compile-agent's
  `finished`, a model's judgement) counts only when no deterministic source
  says anything. Then it gives UNKNOWN (`completed_unverified`), or FAILED
  when the agent says it gave up. It can't change a read-back, a guard
  decision, a receipt or a person's answer.
- **VERIFIED needs an independent read-back.** A VERIFIED outcome resting on
  a screen read-back is UNKNOWN (`screen_readback_only`).

### Engine outcomes on main

| Source value | Bucket |
| --- | --- |
| `TransactionOutcome` VERIFIED | VERIFIED (with an independent read-back) |
| HALTED_BEFORE_EFFECT | ESCALATED |
| RECONCILIATION_REQUIRED, COMPLETED_UNVERIFIED, ROLLED_BACK, SAVED_AWAITING_CHECK | UNKNOWN |
| FAILED_PLATFORM, CANCELED, REJECTED_POLICY | FAILED |
| `ExecutionOutcome` (when no transaction outcome) VERIFIED / COMPLETED_UNVERIFIED / HALTED / FAILED / ROLLED_BACK | VERIFIED / UNKNOWN / UNKNOWN / FAILED / UNKNOWN |
| verify-run receipt `outcome` verified / halted_before_effect / reconciliation_required / failed_platform / rolled_back_verified | VERIFIED / ESCALATED / UNKNOWN / FAILED / UNKNOWN |
| verify-run receipt `outcome` rejected_policy (the executor approved its own write, which landed) | UNKNOWN (`self_approved`) |
| verify-run `effect_verdict` REFUTED | CAUGHT_ERROR |
| `GuardVerdict` VERIFIED / REFUTED / RECONCILIATION_REQUIRED | VERIFIED / CAUGHT_ERROR / UNKNOWN |
| Agent runner `RunOutcome.status` success / halt / refused / timeout / error | UNKNOWN / UNKNOWN / FAILED / FAILED / FAILED |
| Any other value | UNKNOWN (`unmapped_outcome`) |

`tests/test_run_ledger_outcome.py` imports each enum and fails when a member
has no row in these tables.

## Sources

The collectors are in `openadapt_flow/run_ledger/collect.py`, one function
per source. Each reads files only and refuses a schema version it doesn't
know: the row is then UNKNOWN with `schema_unrecognised`.

### Written by commands on main

| File | Reads | Can't tell |
| --- | --- | --- |
| `summary.json` (`openadapt.agent-guard-run/v1`) and `guard_events.jsonl` | Verdict, blocked and would-block codes, holds, whether a save was sent, `ended`, `seconds` | Wall-clock times, field names, the guarded agent's model usage |
| `effect-receipt.json` (verify-run, `openadapt.effect-receipt/v1`) | `outcome`, `effect_verdict`, `verification_tier`, `executor_kind` | Field names (effects are contract hashes) |
| `report.json` (compiled run; no schema field, recognised by shape) | `transaction_outcome` (else `execution_outcome`), `readback.checked_how`, `model_calls`, `est_model_cost_usd`, `started_at`, `total_ms`, `params` (committed only) | Tokens; field names |
| `receipt.json` (`openadapt.run-receipt/v2`) | `effect_assurance` (else `effect_tier_reached`), `receipt_digest`, `model_calls`, `est_cost_usd`, `duration_ms` | Anything about a halted run (only VERIFIED runs get one) |
| `openadapt-agent/runs/<id>.json` (`openadapt-agent.run/v1`, wrap's run store) | `state`, `started_at`, `finished_at`, result `path`, `status`, `verified`, `receipt_outcome`, `model_calls`, `cost_usd`, `seconds` | Tokens, field names; a `running` record is a lost reply |
| `item-approval/request-*.json`, `answer-*.json` | `created_at`, `answered_at`, `decision`, `workflow_name` | An unanswered request's end (time is unknown) |
| `jobs/<job>.json` (`openadapt.task-ledger/v1`) | Each question's `asked_at`, `answered_at`, `request_id` | Question text is never read |
| `run1.json` (compile-agent; `agent` is `AgentLedger.to_dict()`) or `agent_ledger.json` | `model_calls`, tokens, `cost_usd` and `usage_known`, `finished`, wall-clock `started_at`/`finished_at` when they are epochs, `gates` refusals | `summary`, `final_text` and per-call `actions` are never read |

Wall-clock times come only from fields that hold them. The guard's `t` and
`seconds` and compile-agent's monotonic readings give durations, not times.
File modification times are never used.

### Contract-only sources

Sibling pieces write these. The collectors read exactly these keys:

| File | Keys read | Outcome |
| --- | --- | --- |
| `proposed_write.json` (`openadapt.proposed-write/v1`) | `task_id`, `fields[].name`, `fields[].proposed`, `fields[].saved` | Any field differs: CAUGHT_ERROR (`shadow_field_mismatch`). All match: ESCALATED (`halted_before_effect`), since the candidate stopped before saving. |
| `scorecard.json` (`openadapt.readiness-scorecard/v1`) | `tasks[].task_id`, `tasks[].matched` | `matched: false` for this run's task: CAUGHT_ERROR |
| `flight.json` (`openadapt.flight-run/v1`) | `task_id`, `executor_label`, `started_at`, `ended_at`, `model`, `gate_events[].kind` | A `held` with no later `allowed` or `refused`: ESCALATED (`gate_held`). A `refused`: CAUGHT_ERROR (`gate_refused`). |
| `guard_ledger_row.json` (`openadapt.guard-ledger-row/v1`) | `task_id`, `verdict`, `fields_checked`, `fields_differing`, `approval_opened_at`, `approval_decided_at`, `readback_tier` | The verdict as `GuardVerdict`; `fields_differing` > 0 is CAUGHT_ERROR |
| `exceptions/answer-<id>.json` (`openadapt.exception-answer/v1`) | `card_id`, `kind`, `opened_at`, `decided_at` | ESCALATED: `second_factor` for that kind, else `exception_card` |

`answer` in an exception file, `diff_path` and `approver_kind` in a guard row,
and `timeline_path` in a flight file are not read.

### Merging sources

- **Model usage:** metered beats reported beats unknown; the first source
  wins within a rank. Usage is never added across sources, because two files
  often describe the same calls.
- **Times:** the earliest `started_at` and the latest `ended_at`. Without
  both, the first source's duration.
- **Intervals:** one per approval request, even when several files mention it.
- **Fields:** by name, the worst check wins (`differs`, then `not_checked`,
  then `matched`).
- **Read-back tier:** the weakest one.
- **Task id:** two different task ids make the row UNKNOWN
  (`task_id_conflict`).

## The store

`$OPENADAPT_HOME/ledger/rows.jsonl` (default `~/.openadapt/ledger`), mode
0600. Each line is `{"row": ..., "run_digest": ..., "run_ref": ...,
"added_at": ...}`. `run_digest` hashes the source files' paths and bytes, and
`run_ref` hashes the run's path. A second `add` of an unchanged run does
nothing (dedupe on task id and run digest). A run whose files changed (an
approval answered after the first `add`) gets a new row; `summary`, `export`
and `push` use the newest row per run. Writes go through a temporary file and
a rename under an exclusive lock.

## Commands

| Command | Does |
| --- | --- |
| `ledger add RUN_DIR [--task-type T] [--task-id ID]` | Reads the run and appends its row. `RUN_DIR` can also be one wrap run record. |
| `ledger list [--json]` | Every row. |
| `ledger show ROW_ID [--show-values]` | One row; with `--show-values`, also the sidecar's values. |
| `ledger summary [--since ISO] [--task-type T] [--json]` | Runs per outcome, a person's minutes, model cost, median duration. Rows whose time or cost is unknown are counted as unknown, not as zero. |
| `ledger export --out FILE [--require-scrubber]` | Value-free JSONL. |
| `ledger push [--host URL] [--dry-run]` | POSTs each row without `sources`, as `openadapt.agent-run-summary/v1`, to `/api/runs/ingest-agent-run` with `OPENADAPT_API_KEY`. |

`export` and `push` refuse when:

- a value from any local sidecar appears in what they would send;
- a row has a key outside its schema, or fails validation;
- openadapt-privacy's text scrubber is installed and would rewrite a label
  (task id, task type, executor label or field name). Without the scrubber,
  `export` says so and relies on the sidecar check; `--require-scrubber`
  refuses instead.

`push` also refuses before anything else when `OPENADAPT_API_KEY` isn't set,
and sends only to a destination the hosted destination policy accepts (the
same check `ingest-report` uses).
