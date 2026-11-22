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
| `started_at`, `ended_at`, `duration_s` | From a source that keeps the run's own start and end (wrap, compiled runs, flight, compile-agent). Otherwise from the wall-clock moments the run's files record (an approval asked and answered, a shadow hold, the egress timeline's first and last model call, a read-back issued, an exception card): `ended_at` is the latest, and `started_at` is `ended_at` minus the run's own duration, or the earliest moment if that is earlier. `null` only when no source records a moment. `duration_s` is always a source's own measure. |
| `model` | `calls`, `input_tokens`, `output_tokens`, `cost_usd`, and `cost_source` (`metered`, `reported` or `unknown`). A value a source doesn't give is `null`, never `0`. |
| `human` | `intervals` (`approval`, `question`, `exception`, `second_factor`, each with `opened_at` and `decided_at`) and `intervention_s`, their sum. If any interval has no `decided_at`, `intervention_s` is `null`. With no intervals it is `0` only when a source shows no person was asked (a shadow hold, which never asks; an agent-guard run with approval `not_gated`; a flight with no gate events), and `null` (unknown) otherwise. A shadow run's person did the real save in a separate recorded run, so the shadow row itself is `0`. |
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
| ESCALATED | The run stopped for a person, a person declined it, or a person did the step. | `approval_held`, `person_declined`, `approval_declined`, `question_asked`, `exception_card`, `second_factor`, `halted_before_effect`, `gate_held`, `shadow_matched` |
| CAUGHT_ERROR | A check caught a wrong save. A person declining is not a catch on its own. | `readback_refuted`, `save_blocked_wrong_record`, `save_blocked_other_origin`, `save_blocked_double_save`, `save_blocked_out_of_scope`, `gate_refused`, `shadow_wrong_record`, `shadow_field_mismatch` |
| FAILED | Crashed, timed out, canceled or gave up, with no evidence of a write. | `crashed_no_write`, `timed_out_no_write`, `agent_gave_up`, `shadow_no_proposal`, `shadow_timed_out`, `platform_failed`, `canceled_no_write`, `policy_rejected_no_write` |
| UNKNOWN | Nothing settles it. Never folded into another bucket. | `shadow_write_landed`, `completed_unverified`, `shadow_not_compared`, `shadow_extra_target`, `shadow_agent_error`, `egress_refused`, `reconciliation_required`, `screen_readback_only`, `lost_reply`, `write_unconfirmed`, `saved_awaiting_check`, `rolled_back`, `self_approved`, `observe_only_would_block`, `schema_unrecognised`, `source_unreadable`, `task_id_conflict`, `unmapped_outcome`, `no_sources` |

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
- **A decline is not a catch.** A person declining an approval (a guard
  `declined` block, an item-approval answer that isn't `approve`, a
  compile-agent gate `refused_by_hold`, a P7 `GATE_REFUSED` with a record
  check that shows no write) is ESCALATED (`person_declined`). It is
  CAUGHT_ERROR only when deterministic evidence shows an actual error: a
  refuted read-back, a differing field, a blocked wrong record. Those
  outrank ESCALATED anyway. `approval_declined` is an approval nobody gave
  in time (`request_expired`, `approval_wait_timed_out`).
- **"No write" needs evidence.** A crash, failure or timeout can come after a
  request went out. `crashed_no_write` and `timed_out_no_write` are used only
  where the source also shows nothing was sent (the agent guard's own save
  events, the shadow guard's landed-write count). A bare status is UNKNOWN
  (`write_unconfirmed`).

### Engine outcomes on main

| Source value | Bucket |
| --- | --- |
| `TransactionOutcome` VERIFIED | VERIFIED (with an independent read-back) |
| HALTED_BEFORE_EFFECT | ESCALATED |
| RECONCILIATION_REQUIRED, COMPLETED_UNVERIFIED, ROLLED_BACK, SAVED_AWAITING_CHECK | UNKNOWN |
| FAILED_PLATFORM, CANCELED, REJECTED_POLICY | FAILED |
| `ExecutionOutcome` (when no transaction outcome) VERIFIED / COMPLETED_UNVERIFIED / HALTED / FAILED / ROLLED_BACK | VERIFIED / UNKNOWN / UNKNOWN / UNKNOWN (`write_unconfirmed`) / UNKNOWN |
| verify-run receipt `outcome` verified / halted_before_effect / reconciliation_required / failed_platform / rolled_back_verified | VERIFIED / ESCALATED / UNKNOWN / FAILED / UNKNOWN |
| verify-run receipt `outcome` rejected_policy (the executor approved its own write, which landed) | UNKNOWN (`self_approved`) |
| verify-run `effect_verdict` REFUTED, with an effect row the store shows (`observed_effect` `present` or `conflicting`) | CAUGHT_ERROR |
| verify-run `effect_verdict` REFUTED with the effect `absent` (a halt before the save, a save that never landed) | the receipt `outcome` decides |
| `GuardVerdict` VERIFIED / REFUTED / RECONCILIATION_REQUIRED | VERIFIED / CAUGHT_ERROR / UNKNOWN |
| Guard block code `declined` / `request_expired`, `approval_wait_timed_out`, `approval_required` / P5's could-not-check codes (`baseline_unreadable`, `answer_malformed` ...) | ESCALATED (`person_declined`) / ESCALATED (`approval_declined`) / FAILED (`policy_rejected_no_write`; the save was blocked before it was sent) |
| Agent runner `RunOutcome.status` success / halt / refused / timeout / error | UNKNOWN / UNKNOWN / FAILED / UNKNOWN / UNKNOWN |
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
| `run1.json` (compile-agent; `agent` is `AgentLedger.to_dict()`) or `agent_ledger.json` | `model_calls`, tokens, `cost_usd` and `usage_known`, `finished`, wall-clock `started_at`/`finished_at` when they are epochs, `gates` refusals (`refused_by_hold` is a person declining: `person_declined`; other refusals are `gate_refused`) | `summary`, `final_text` and per-call `actions` are never read |

Wall-clock times come only from fields that hold them. The guard's `t` and
`seconds` and compile-agent's monotonic readings give durations, not times.
File modification times are never used.

### Agent-control-plane sources

The pieces of a control-plane pilot write these. The tests read committed
copies of their real (synthetic) files from the 2026-11-21 end-to-end run;
`tests/test_run_ledger_joins.py` is the join check.

| Source | Keys read | Outcome |
| --- | --- | --- |
| Readiness run directory (P1): a folder with `run.json` (`openadapt.readiness-task-run/v1`) | `run.json` `task_id`, `status`, `agent_wall_s`, `landed_writes`, `usage`, `model_cost_usd`; `case.json` (`openadapt.readiness-case/v1`) `task_id`, `status`, `landed_writes`, `fields[].name`, `kind`, `human`, `proposed`; `proposed_write.json` `fields[].name`, `value`; `usage.json` `calls`, tokens, `model`, `cost_usd`; `egress.json` `model_calls`, `cost_usd`, `cost_source` | See the shadow table. Nothing else in the folder is read: a `guard_ledger_row.json` or `guard_events.jsonl` there belongs to the shadow hold. |
| `guard_ledger_row.json` (`openadapt.guard-ledger-row/v1`, P5) | `task_id`, `fields_checked[]`, `fields_differing[]` (names), `approval_opened_at`, `approval_decided_at`, `readback_tier` | None: the row is never a bucket source (`verdict`, `guard_verdict` and `verdict_reason` aren't read). The guard's `summary.json` and the read-back decide. A differing field shows as `differs`. |
| P7 fallback `result.json` (`<cache>/fallback/<task>/<id>/`, with `replay`) or a `wrap-dispatch --result` envelope (with `outcome` and `fallback`); no schema field, recognised by `path`, `status`, `verified` and those keys | `status`, `verified`, `path`, `seconds`, `model_calls`, `cost_usd`, `usage_known`, the policy record (`replay.fallback`, or the envelope's `fallback`) `task_record_check`, `replay.program.receipt_outcome`, `compile.run1.gates[].at` / `decision`, `compile.task_key`, `request_id` | See the fallback table. The envelope's `outcome` is not read. The agent attempt under `run/` is not read again. |
| `$OPENADAPT_HOME/exceptions/<run_ref>/` (P6): `card-<id>.json` (`openadapt.exception-card/v1`), `answer-<id>.json` (`openadapt.exception-answer/v1`) | card `task_id`, `kind`, `opened_at`, `card_id`; answer `card_id`, `kind`, `opened_at`, `decided_at` | ESCALATED: `second_factor` for that kind, else `exception_card`. An expired card (`decided_at: null`) or one not answered yet makes the person's time unknown. A card for another task id makes the row `task_id_conflict`. |
| `flight.json` (`openadapt.flight-run/v1`, P3) | `task_id`, `executor_label`, `started_at`, `ended_at`, `model`, `gate_events[].kind` | A `held` with no later `allowed` or `refused`: ESCALATED (`gate_held`). A `refused`: CAUGHT_ERROR (`gate_refused`). |
| A lone `proposed_write.json` (outside a readiness folder) | `task_id`, `fields[].name`, `value` | UNKNOWN (`shadow_not_compared`): nothing compared it. |

P6's run reference is the operator's `exceptions serve --run-ref` (or a
random `mcp-...`); no run directory names it. `ledger add --run-ref REF`
reads `exceptions/REF/`. Without it no exception folder is read: nothing
in a run directory says which folder is its own.

`answer` and signatures in exception files, card summaries, `approver_kind`,
`hold_ms` and `diff_path` in a guard row, and `timeline_path` in a flight file
are not read.

#### Shadow runs (J1)

A shadow run holds every save, so it is never VERIFIED. The person's own
save is the real one.

| Case | Bucket (reason) |
| --- | --- |
| `landed_writes` > 0 or `FAILED_SCOPE` (a write reached the app) | UNKNOWN (`shadow_write_landed`) |
| Run `EXTRA_TARGET` (a write from, or a target outside, the guarded tab) | UNKNOWN (`shadow_extra_target`) |
| Run `EGRESS_REFUSED`, or `run.json` `egress.refused: true` | UNKNOWN (`egress_refused`). A healthy `--egress blocked` run also writes `mode: blocked`; only `refused` tells them apart. |
| `WRONG_RECORD` | CAUGHT_ERROR (`shadow_wrong_record`) |
| `MISMATCHED` | CAUGHT_ERROR (`shadow_field_mismatch`) |
| `MATCHED` | ESCALATED (`shadow_matched`): a person did the step; not a catch |
| `NOT_COMPARABLE`, `NOT_CHECKED`, no `case.json` | UNKNOWN (`shadow_not_compared`) |
| No proposal, run `TIMEOUT` / `NO_WRITE_HELD`, `landed_writes: 0` | FAILED (`shadow_timed_out` / `shadow_no_proposal`) |
| No proposal, run `AGENT_ERROR` (set before the guard counted anything) | UNKNOWN (`shadow_agent_error`) |

Every shadow reason is a `shadow_*` one (or `egress_refused`), so a readiness
trial never shows as a production failure.

Fields come from `case.json`: `matched` is matched; `mismatched`, `missing`,
`extra` and `wrong_record` differ; `uncompared` is not checked. A proposed
field the case doesn't list (an expected-record field that matched, or an
ignored one) is `not_checked`. Values go to the sidecar only. Duration is
`agent_wall_s`. Model usage is `run.json` `usage` (else `usage.json`),
`reported`; an `egress.json` (else the copy in `run.json` `egress`, unless
refused) replaces calls and cost with the proxy's count (`metered` when its
`cost_source` says so).

#### Fallback runs (J5)

| `status` (`path: agent_fallback`) | Bucket (reason) |
| --- | --- |
| VERIFIED with P7's `verified` and a record check that is `readable: true` and passed | VERIFIED (`readback_verified`, independent: the record check read the system of record before and after) |
| VERIFIED, but the record check isn't `readable: true` (a failed read, or an older check without the flag) | UNKNOWN (`reconciliation_required`) |
| GATE_REFUSED, record check `readable: true` with no write | ESCALATED (`person_declined`) |
| GATE_REFUSED or COST_CAP_EXCEEDED, record check missing, not `readable: true`, or showing a write | UNKNOWN (`write_unconfirmed`). An unreadable store also reports zero counts, so zeros alone prove nothing. |
| COST_CAP_EXCEEDED, no write | FAILED (`policy_rejected_no_write`) |
| GATE_NOT_PROVED | UNKNOWN (`self_approved`) |
| RECONCILIATION_REQUIRED | UNKNOWN (`reconciliation_required`) |
| FALLBACK_GATE_UNATTENDED, FALLBACK_NO_AGENT, FALLBACK_COST_UNBOUNDED, FALLBACK_REQUEST_REUSED (agent not started) | ESCALATED (`halted_before_effect`) when the program halted before effect, else UNKNOWN (`write_unconfirmed`) |
| Anything else | UNKNOWN (`unmapped_outcome`), or CAUGHT_ERROR (`readback_refuted`) when the record check shows an unverified write |

An item-approval gate records `fallback.gate_answers` (request ids, decisions,
signer) but no times; its request and answer files sit in
`<approvals>/<fallback request id>/item-approval/` (`wrap-dispatch
--fallback-approvals`). `ledger add --approvals DIR` reads them there: the
person's time runs from the request's `created_at` to the answer's
`answered_at`. Without `--approvals` the time is unknown. A `result.json`'s
record check names the fields of the new record; the row keeps those the
run's config (`--config verify.yaml`) declares, `matched` when the check is
readable and passed. The envelope carries no field names.

On `path: program` or `held` the envelope is read like a wrap result;
`NO_ADMITTED_PROGRAM` is FAILED (`policy_rejected_no_write`, nothing ran).
A fallback gate in `result.json` keeps one time, not an opened/decided pair,
so a person's time on it is unknown (`null`), not zero. The envelope keeps
no gate times at all, so its row's time is unknown too. A wrap run record
(`openadapt-agent.run/v1`) whose result `path` is `agent_fallback` is read
like an agent's first run: P7's `verified` is VERIFIED, anything else without
the policy record is UNKNOWN (`write_unconfirmed`).

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
"added_at": ..., "declared_fields": [...]}`. `declared_fields` are the field
names the run's own config declares (see `export` below); they stay beside
the row, not in it. `run_digest` hashes the source files' paths and bytes, and
`run_ref` hashes the run's path. A second `add` of an unchanged run does
nothing (dedupe on task id and run digest). A run whose files changed (an
approval answered after the first `add`) gets a new row; `summary`, `export`
and `push` use the newest row per run. Writes go through a temporary file and
a rename under an exclusive lock (`flock` on POSIX, `msvcrt.locking` on
Windows). On Windows the files rely on the user profile's ACL, since there is
no POSIX mode to set.

## Commands

| Command | Does |
| --- | --- |
| `ledger add RUN_DIR [--task-type T] [--task-id ID] [--run-ref REF] [--config FILE ...]` | Reads the run and appends its row. `RUN_DIR` can also be one wrap run record or one `wrap-dispatch` envelope. `--run-ref` names the run's P6 exception folder. `--config` names the run's guard task JSON or `verify.yaml` when they aren't in `RUN_DIR`. |
| `ledger list [--json]` | Every row. |
| `ledger show ROW_ID [--show-values]` | One row; with `--show-values`, also the sidecar's values. |
| `ledger summary [--since ISO] [--task-type T] [--json]` | Runs per outcome, a person's minutes, model cost, median duration. Rows whose time or cost is unknown are counted as unknown, not as zero. |
| `ledger export --out FILE [--require-scrubber]` | Value-free JSONL. |
| `ledger push [--host URL] [--dry-run]` | POSTs each row without `sources`, as `openadapt.agent-run-summary/v1`, to `/api/runs/ingest-agent-run` with `OPENADAPT_API_KEY`. |

`export` and `push` screen each row and refuse a row when:

- a value from any local sidecar appears in what they would send;
- a row has a key outside its schema, or fails validation;
- openadapt-privacy's text scrubber is installed and would rewrite a label
  (task id, task type, executor label or field name), or can't run (for
  example, its spaCy model is missing). Without the scrubber, `export` says
  so and relies on the sidecar check; `--require-scrubber` refuses instead.
  One exemption: a field name the run's own config declared at `add` time.
  Presidio rewrites the name `patient_id` to `<ORGANIZATION>`; a declared
  name is a form's identifier, not a value. Declared means: a guard task's
  `expected_record` and `intended_fields` keys (`openadapt.guard-task/v1`,
  `task.json` or `guard_task.json` in the run, or `--config`; only a task
  whose `task_id` is the run's), a
  `verify.yaml`'s effect `match` keys (a `verify.proposed.yaml` doesn't
  count), and a readiness `case.json`'s reference fields (not its `extra`
  ones; and for `FAILED_SCOPE` or a case with no proposal, whose field rows
  compare never built, nothing). Any other name the scrubber would rewrite still refuses, and an
  entry added before this rule exempts nothing.
  The same rule covers the row's `task_id`: it is let through only when the
  run's own files name it (a readiness `run.json` or `case.json`, or a guard
  task whose `task_id` matches). Presidio reads `readiness-mockmed-0002` as
  an organisation; P1's own run file names it, so it may leave. An id given
  only with `--task-id` still refuses when the scrubber rewrites it. P7's own
  `result.json` or dispatcher envelope declares its request id (or task key)
  the same way. The entry keeps these as `declared_task_ids`.

Cloud receives `null` for every value the sources don't give
(`started_at`, `ended_at`, `duration_s`, the `model` counts and cost,
`readback_tier`, `intervention_s`). Real `push --dry-run` output over the
synthetic fixtures is committed at
`packages/types/tests/fixtures/agent_run_summary_v1_examples.jsonl`; a test
validates it against `AgentRunSummaryV1`'s JSON Schema.

A refused row stays local and the rest still go: each refused row is listed
on stderr with its reason, and the command exits 2. A scrubber that can't run
refuses everything.

`push` also refuses before anything else when `OPENADAPT_API_KEY` isn't set,
and sends only to a destination the hosted destination policy accepts (the
same check `ingest-report` uses).

The task view also lists the verb as `openadapt inspect ledger`
(`openadapt verify ledger` is the protected-write ledger, a different thing).

The verb needs an openadapt-types release that has
`openadapt_types.run_ledger`. Building the parser doesn't import it, so an
install with an older openadapt-types still starts; `ledger` itself then
says to upgrade openadapt-types.
