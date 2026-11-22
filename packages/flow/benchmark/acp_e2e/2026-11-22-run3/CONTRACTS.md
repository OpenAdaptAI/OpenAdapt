# Agent control plane: frozen join contracts (2026-11-21)

Rule: the consumer adapts to what the producer really emits. A producer
changes only where data is missing (listed below as "producer adds").
P2's outcome rule (`run_ledger/outcome.py`) is the only place a bucket is
decided. Real producer artifacts live in
/Users/abrichr/oa/worktrees/acp-demo-out (synthetic). Each consumer PR commits a
copy of the producer's real artifact as a test fixture and tests the join.

PRs and worktrees (all under /Users/abrichr/oa/worktrees/):

| Piece | PR | Branch | Worktree |
|---|---|---|---|
| P1 readiness / shadow scorecard | #2498 | claude/acp-P1-shadow-scorecard | acp-P1-shadow-scorecard |
| P2 accounting ledger | #2478 | claude/acp-P2-accounting-ledger | acp-P2-ledger |
| P3 flight recorder | #2493 | claude/acp-P3-flight-recorder-wrapper | acp-P3-flight |
| P5 gate card + read-back + row | #2492 | claude/acp-P5-gate-card-readback-ledger | acp-P5-gate-card-readback-ledger |
| P6 exception queue | #2494 | claude/acp-P6-exception-queue | acp-P6-exception-queue |
| P7 gated fallback on drift | #2491 | claude/acp-P7-gated-fallback-on-drift | acp-P7-fallback |
| Cloud ledger dashboard | openadapt-cloud#551 | claude/acp-P4-cloud-ledger-dashboard | acp-P4-cloud-ledger |

## Stacking decisions

- P1 (#2498) depends on P3's egress API (J7). P1 merges P3's branch and its
  PR base is set to `claude/acp-P3-flight-recorder-wrapper`. CI runs on any
  base (ci.yml `pull_request` has no branch filter).
- Desktop version: only P5 (#2492) bumps Desktop (0.31.0). P6's
  `packages/desktop` changes move into P5's branch as their own commit under
  P5's 0.31.0 changelog section; P6 then touches no `packages/desktop` file,
  so its release-intent check does not run. (Stacking alone does not work:
  check_release_intent fails a feat/fix net change with no bump.)
- Every new `openadapt-flow` verb needs a `FLOW_NAMES` / `cli_tasks` entry
  (`readiness`, `flight`, `wrap-dispatch`, `ledger`, `exceptions`).

## J1. P1 per-task run dir -> P2

Producer files in `<out>/runs/<task_id>/`:

- `run.json` `openadapt.readiness-task-run/v1`: `task_id, status (HELD|AGENT_ERROR|...), ended, agent_wall_s, exit_code, held, landed_writes, double_save, other_origin_refused, proposed, before_screen, requests, notes[], usage{calls,input_tokens,output_tokens,cost_usd,model}|null, model_cost_usd`.
- `proposed_write.json` `openadapt.proposed-write/v1`: `fields[] {name, parse, value}`, `method, url_path, origin, identity{code,ok}, refused, request_index, resource_type, held_at, screenshot, screen_text, withheld_fields, task_id`.
- `usage.json` `openadapt.agent-usage/v1`: `calls, input_tokens, output_tokens, model, cost_usd|null` (agent-reported).
- **Producer adds:** `case.json` `openadapt.readiness-case/v1`, written by
  `readiness compare` into each task run dir, identical to that task's entry
  in `card/cases.json`: `task_id, status (MATCHED|MISMATCHED|NOT_COMPARABLE|NOT_CHECKED), reason, fields[] {name, kind (matched|mismatched|uncompared|...), human, proposed}, identity, contamination, double_save, landed_writes, undeclared_fields, out_of_scope_requests, agent_time_s, human_time_s, model_cost_usd`.
- **Producer adds:** `egress.json` (J7 summary) when the agent ran behind the flight proxy.
- **Producer stops:** ShadowHoldGuard sets `self.ledger_dir = None` after `super().__init__` so no `guard_ledger_row.json` is written for a shadow hold.

Consumer (P2): a run dir with a readiness `run.json` is a shadow run. Nothing
was saved, so a shadow run is never VERIFIED and a matched shadow case is not
CAUGHT_ERROR. A MISMATCHED/wrong-record case is the error the shadow caught.
Duration and cost come from run.json (`agent_wall_s`, usage) or egress.json
(metered, preferred). Ignore any `guard_ledger_row.json` in a shadow run dir.

## J2. P1 `scorecard.json` -> Cloud#551

Producer emits (as in acp-demo-out/1-shadow/card/scorecard.json): `schema, generated_at, agent_label, mode, scope_statement, scope_ok (bool), failed_scope (int), replay_state (string), cases_total, matched, mismatched, not_comparable, not_checked, risks{wrong_record,out_of_scope_request,double_save,undeclared_field}, field_errors[] {task_id, field, kind}, agent_time_s{median,mean}, human_time_s{median,mean}, model_cost_usd{total,per_task,unknown_count}, savings{formula, minutes_saved_per_task, est_value_usd, estimate (bool), inputs{agent_median_s, human_median_s, hourly_cost_usd, matched_cases, model_cost_usd, why_no_value (string|null)}}, landed_writes`.

**Producer adds:**
- `task_type` (identifier, `--task-type`, default `unspecified`), always present.
- `model_cost_usd.source`: `metered` | `reported` | `unknown`.
- `egress`: `{"mode": "scrubbed" | "blocked" | "unscrubbed", "enforced": bool, "model_calls": int | null}`.
- A case with an unguarded extra target is NOT_CHECKED with reason `extra_target`, counted in `failed_scope`; `scope_ok` is false then. No new risks key.

Consumer (Cloud): accept every key above, including `why_no_value` as a
string or null and `estimate` as a bool. Unknown keys still refuse. Show
`savings` as an estimate when `estimate` is true. Show `scope_ok: false`
prominently.

## J3. P5 `guard_ledger_row.json` -> P2

Producer emits `openadapt.guard-ledger-row/v1`: `task_id, verdict (a bucket name: CAUGHT_ERROR|ESCALATED|VERIFIED|UNKNOWN|FAILED), approver_kind, approval_opened_at, approval_decided_at, fields_checked[], fields_differing[], hold_ms, readback_tier, diff_path`.

Consumer (P2): the bucket comes from the guard's own `summary.json`
GuardVerdict plus the read-back sources, never from the row's `verdict`. The
row supplies only approval times, `approver_kind`, field names and
`readback_tier`. One run gives one row and one bucket.

Producer fix (P5): `CommitGuard.__init__` reads `getattr(guarded, "state_dir", None)` so a guard built with no store does not crash.

## J4. P6 exception answers -> P2

Producer writes `$OPENADAPT_HOME/exceptions/<run_id>/answer-<sha256>.json`
`openadapt.exception-answer/v1`: `answer, approver_kind, card_id, decided_at, kind, opened_at, public_key, reason, route, schema, signature`.
Consumer (P2) reads that location (not `RUN_DIR/exceptions/`), keyed by the
run id P6 uses; read P6's registry to find how the run id is derived.

## J5. P7 fallback `result.json` -> P2

Producer: `<cache>/fallback/<task>/<fallback-id>/result.json` and the
dispatcher envelope (see acp-demo-out/4-fallback/kept). Consumer (P2) adds a
reader; a refused gate is ESCALATED or FAILED with no write, never
`schema_unrecognised`.

## J6. P2 push payload -> Cloud#551

Producer: `openadapt.agent-run-summary/v1` (`AgentRunSummaryV1` in
`packages/types/openadapt_types/run_ledger`, P2 branch). Unknown values are
`null`: `started_at, ended_at, duration_s, model.calls, token counts, cost,
readback_tier, intervention_s`.

Consumer (Cloud): accept `null` for every nullable field in that schema,
render it as "unknown", keep it out of medians and sums, and count how many
are unknown.

Producer fix (P2): `ledger export/push` refused every row because Presidio
rewrites the field name `patient_id`. Exempt only field names declared by the
run's own task or verify config (fail closed for anything else).

## J7. P3 egress API -> P1 readiness

P3 provides `openadapt_flow.flight.egress`:

```python
class EgressRefused(Exception): ...

class ScrubbedEgress:  # context manager
    def __init__(self, run_dir: Path, *, spend_cap_usd: float | None,
                 allow_unenforced: bool = False,
                 upstreams: Mapping[str, str] | None = None): ...
    env: dict[str, str]            # ANTHROPIC_BASE_URL / OPENAI_BASE_URL etc.
    def wrap(self, argv: list[str]) -> list[str]: ...  # OS egress block when available
    def summary(self) -> dict: ...  # {"mode", "enforced", "model_calls", "cost_usd", "cost_source", "refused_requests"}
```

- Raises `EgressRefused` at start when the text or image scrubber can't run
  (openadapt-privacy, the spaCy model or tesseract missing), and when the OS
  egress block is unavailable and `allow_unenforced` is false.
- Every string in the forwarded body is scrubbed except opaque signed blocks
  (thinking / reasoning) and tool definitions; images always.
- Writes `<run_dir>/egress.json` with `summary()`.
- A run whose agent reports more model calls in usage.json than the proxy
  saw, or any call the sandbox blocked, is refused (non-zero exit, no
  scorecard for that run).
