# Synthetic transaction verification harness

This harness uses three purpose-built HTTP/SQLite servers. It does not run
third-party EHR, ERP or claims products, and it does not establish production
readiness or independent held-out application generalization.

The original runner could replace a failed qualifier with an empty successful
certificate. That fallback has been removed, and so have the numbers it
produced. The sandbox qualifier now returns a probe report that can never
authorize execution, so the default `tx_acid` arm stops with
`transaction benchmark qualification failed` on every server. Until an
independent admission path exists, this harness produces no `tx_acid`
measurement; pass your own `tx_arm_factory` to measure another backend.

## Systems of Record (`servers.py`)

Each server runs `http.server.ThreadingHTTPServer` backed by SQLite in `WAL` mode (`PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;`), bulk-seeding `50,000` rows via `executemany`:

1. **`ClinicalChartServer` (EHR Archetype, `/api/patients`):**
   - Caps `GET /api/patients?_limit=200` at 200 rows without snapshot tokens, emitting `links.next` whenever `>200` rows exist.
   - Provides O(1) indexed reads on `GET /api/patients/{id}` and `GET /api/patients?email=...` with `ETag`, integer `version`, and `last_author`.
   - Normalizes `fname`/`lname` via Title Case, `street`/`city` via `UPPER`, and full US/CA state names (`"Massachusetts" -> "MA"`, `"California" -> "CA"`, `"Ontario" -> "ON"`).
   - Seeds realistic 2–3 character names (`"Bo"`, `"Li"`, `"Amy"`, `"Al"`) and confusable record IDs (`"PT-0O1l8"` vs `"PT-0O118"`).
2. **`ReplaceTaskServer` (Replace-Style ERP/Task Archetype, `/api/tasks`):**
   - `PUT /api/tasks/{id}` replaces the entire record: omitted fields (`description`, `assignee`, `due_date`, `labels`, `budget_cents`, `department`) are silently overwritten with `""` or `0` while returning `200 OK`.
   - `POST /api/tasks` is non-idempotent (retrying creates duplicate rows).
3. **`ClaimsLedgerServer` (Additional Synthetic Ledger Archetype, `/api/claims`):**
   - Enforces `ETag` / `If-Match` (`412 Precondition Failed` on stale precondition) and strictly increments `version`.
   - Computes server-side `updated_at` and monotonic `audit_seq`.
   - Normalizes phone numbers (`"(416) 555-0199" -> "+14165550199"`) and ISO dates (`"2026-11-19" -> "2026-11-19T00:00:00Z"`).

### Concurrency & Write-Path Fault Injection

- **`BackgroundTrafficGenerator(server, num_workers=20)`:** Runs 20 concurrent background worker threads continuously issuing creates (`POST`) and updates (`PATCH`/`PUT`) against non-target records in the same SQLite-WAL table.
- **`FaultInjectingProxy`:** Injects all 6 production write-path faults (`partial_put_clobber`, `silent_drop_200_ok`, `wrong_value_mutation`, `duplicate_post_on_retry`, `collateral_extra_write`, `wrong_target_record`) plus `same_record_concurrent_race`.

## Benchmark Arms (`baselines.py` & `runner.py`)

| Arm | Mechanism | Scope of this harness |
| :--- | :--- | :--- |
| `naive_2xx` (`NaiveAgent2xxArm`) | Sends intended mutation directly and trusts HTTP `2xx` | Silently clobbers omitted fields on `PUT` and accepts 100% of planted faults |
| `legacy_whole_table` (`LegacyWholeTableContractArm`) | Pre/post `GET ?_limit=200` snapshot diff + literal string equality + `<4`-char short-param halt | Refuses `INDETERMINATE` on `>200` rows or concurrent worker diffs; false-stops on `"Bo"`/`"Li"` and normalized fields |
| `tx_acid` (`TransactionalCommitEngine` / `ReferenceTxArm`) | Target-record read/merge/write checks, observed-request accounting, explicit codecs and conditional HTTP writes | Known synthetic fault cases; results depend on the selected backend and must retain qualification failures |

## Running the Suite

```bash
PYTHONPATH="packages/flow:packages/types/src:packages/verify/src" \
  python3 packages/flow/benchmark/tx_acid/runner.py \
  --seed-rows 50000 \
  --workers 20 \
  --json-out /tmp/tx_acid_report.json \
  --md-out /tmp/tx_acid_report.md
```

Custom commit-layer factories can be plugged directly into `run_tx_acid_benchmark(tx_arm_factory=...)`; when `tx_arm_factory` is omitted, `runner.py` automatically imports `SessionWireTap`, `TransactionalCommitEngine`, `FrameAndIntentJudge`, `induce_lens_from_har`, and `certify_lens_on_sandbox` from `openadapt_flow.tx` when available.
