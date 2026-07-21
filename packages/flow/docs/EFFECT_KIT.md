# The effect-verifier kit — declare a write's effect, ship its verification

**Status:** kit surface implemented + CI contract-proven. Per-substrate
maturity is listed honestly below — the SQL and file/SFTP verifiers are
**contract-proven against local fixtures (sqlite, fakes) in CI, not
live-proven against a production database or SFTP endpoint**; the FHIR
verifier additionally has an opt-in live-OpenEMR test
(`tests/test_effect_fhir_live_openemr.py`).

> Without a declared effect and a configured verifier, OpenAdapt falls back to
> screen evidence for a consequential write. This kit exists so declaring the
> effect and configuring the verifier is a **reviewed YAML section**, not a
> bespoke per-deployment integration.

The underlying design (three-valued verdicts, fail-safe-to-HALT, the shared
judge) is [`docs/design/EFFECT_VERIFIER.md`](./design/EFFECT_VERIFIER.md).
This page is the **operator kit**: what to declare, how to configure each
substrate, how run parameters and secrets bind, and two worked examples lifted
from the reference apps.

## Concept

1. **The bundle declares WHAT must be true** — typed `Effect` contracts on
   each consequential step (`record_written` / `field_equals`, at-most-once
   counts, idempotency keys, `{param: ...}` references that bind to the run's
   governed parameters). Contracts are substrate-neutral.
2. **The deployment declares WHERE truth lives** — the `effects:` section of
   `deployment.yaml` wires exactly one `EffectVerifier` (REST / FHIR / SQL /
   file / document-hash) plus its secret-isolated auth.
3. **The runtime refuses to guess.** Every verdict is CONFIRMED / REFUTED /
   INDETERMINATE; both non-confirmed verdicts HALT. A step that declares
   effects with no verifier configured HALTs. An escalated failure emits a
   typed `ReconciliationTask` (see below) — halt + evidence, never silent.
4. **Certification measures coverage.** `openadapt-flow lint` reports
   per-consequential-step effect coverage (% of irreversible steps declaring a
   contract) and warns per gap (`missing_effect_contract`); a policy that sets
   `require_effects_for_irreversible: true` turns the same gap into a
   certification **failure** (warn-vs-fail is the policy's choice).

## Substrates

| `effects.kind` | Verifier | Probe | Proven how |
|---|---|---|---|
| `onscreen` | `OnScreenReadbackVerifier` | re-OCR the saved value off the live screen — the **no-API default** for GUI-only recordings; auto-derived from the demonstration. **Different-path** (re-open the record) is default-eligible; **same-surface** (re-read the write's own form) is opt-in only | measured in `benchmark/effect_readback/` — different-path false-CONFIRM 0, same-surface > 0. **A read-back CONFIRMED is a consistency signal, NOT transactional proof** (`docs/LIMITS.md`) |
| `rest` | `RestRecordVerifier` | GET a JSON records document (templatable path, secret-isolated auth headers) | live in CI against the MockMed transactional back end; Frappe-shaped read in the reference matrix (PR #131) |
| `fhir` | `FhirEffectVerifier` | FHIR R4 search → flattened resources | CI against a byte-faithful fake FHIR server; **opt-in live test** against a real local OpenEMR |
| `sql` | `SqlRecordVerifier` | ONE read-only SELECT (enforced whitelist), rows judged like any substrate | **contract-proven in CI against sqlite fixtures only** — the query/whitelist/verdict logic is what's proven, not any specific production database |
| `file` | `FileArrivalVerifier` | directory / SFTP listing → `size_ok` + `fresh` + `content_match` per candidate | **contract-proven in CI against temp dirs and a fake SFTP transport** — not live-proven against a real SFTP server |
| `document-hash` | `DocumentHashVerifier` | SHA-256 of each document in a store | live in CI (no external service) |

All substrates share one judge (`runtime/effects/_common.py`), so
at-most-once counting, idempotency-key de-duplication, field read-back,
collateral-loss detection, and the duplicate-write guard below behave
identically everywhere.

### The duplicate-write / idempotency guard (`count_new_only`)

`Effect(kind=record_written, count_new_only=True, expected_count=1)` counts
only records that did **not** exist in the pre-action snapshot: *"exactly one
NEW matching record was created by this action."* Use it when the selector
legitimately matches pre-existing rows (e.g. "an encounter for this patient").
It requires a readable pre-state — an unreachable baseline is INDETERMINATE →
HALT, never a guess. Available on every substrate.

### The SQL table-delta audit

`capture_table_counts(connect, tables)` + `audit_table_deltas(before, after,
expected)` promote the **exact row-count-delta contract** from the governed
Frappe Lending reference matrix (`benchmark/frappe_lending/fixture.py`, PR
#131): every table in the contract must move by exactly its declared delta and
every other audited table by exactly 0. This is a harness-level companion to
the verifier (it brackets a whole run, not one step).

## Configuration reference (`deployment.yaml` → `effects:`)

Complete commented example: [`docs/deployment.example.yaml`](./deployment.example.yaml).
Schema: `openadapt_flow/deployment.py` (`EffectsConfig`).

Two kit-wide conventions:

- **Secrets are references, never literals.** `auth` (rest),
  `access_token_env` (fhir), and `sql_password_env` (sql) name **environment
  variables**; a missing variable fails LOUD at construction (a verifier is
  never wired silently unauthenticated). Resolved secrets never enter
  configs, reports, or contract hashes.

  ```yaml
  effects:
    kind: rest
    auth:
      bearer_env: SOR_BEARER_TOKEN        # or header+value_env, or basic_env
  ```

- **Run-parameter binding is explicit.** `path_params` (rest),
  `search_param_exprs` (fhir), and `sql_query_params` (sql) take the same
  `{param: name}` / `{literal: value}` `ValueExpr` form the bundle's effect
  contracts use (a bare string is a literal). They resolve against the
  governed run parameters (`--params-file` / `--param`, PR #130) **when the
  verifier is built**, and an unresolved `{param: ...}` reference refuses to
  construct — so one bundle + one deployment YAML ships with its verification
  bound to the record each run actually writes.

Per-kind required fields:

| kind | required | optional highlights |
|---|---|---|
| `onscreen` | (none — auto-derived from the demo) | `readback_region`, `readback_min_ratio` (hand-config fallback) |
| `rest` | `base_url` | `records_path` (may contain `{placeholder}`s), `records_key`, `path_params`, `auth` |
| `fhir` | `base_url` | `resource_type`, `search_params`, `search_param_exprs`, `field_paths`, `access_token_env`, `verify_tls` |
| `sql` | `sql_query` + (`sqlite_database` or `sql_driver`) | `sql_query_params`, `sql_connect_args`, `sql_password_env` |
| `file` | `root` | `file_pattern`, `file_min_size`, `file_mtime_window_s`, `file_content_probe` |
| `document-hash` | `root` | `glob` |

The `sql` kind refuses to construct unless `sql_query` passes the read-only
statement filter (single statement, `SELECT`/`WITH` leading keyword, no
comments, no mutating/DDL/control keywords or known side-effecting functions,
values bound only through DB-API parameters). **The filter is defense in
depth, not proof**: on Postgres/MySQL a lexically-clean `SELECT` can still
call a side-effecting function (a UDF, `nextval`, `dblink`), so **always run
the SQL verifier under a dedicated read-only database role** — no
INSERT/UPDATE/DELETE, no EXECUTE on writing functions, no sequence
privileges. The role is the real enforcement; the filter catches config
mistakes early. The SFTP variant of `file` is programmatic-only (inject a
paramiko-compatible `transport` into `FileArrivalVerifier`); YAML wires local
directories.

## Reconciliation tasks (interface only — deliberately no engine)

When verification cannot be reconciled, `reconcile_or_escalate` returns a
`CompensationResult` whose `task` is a typed **`ReconciliationTask`**: kind
(`effect_refuted` / `effect_indeterminate` / `compensation_failed`), the
one-way contract hash (never the resolved values), the verdict evidence
(observed/expected counts and values, matched records), and a
`suggested_action` for the operator. The pattern is **halt + evidence**: the
run stops, the task tells a human exactly what could not be certified, the
human repairs the system of record, re-verifies, and resumes. There is
intentionally **no compensation engine** beyond the single proven safe undo
(duplicate-record deletion via a configured `Compensator`) — automatic repair
of missing/partial/collateral state would be another wrong write.

## Worked example 1 — Frappe Lending (REST + SQL)

Runs against the pinned reference fixture
([`benchmark/frappe_lending/`](../benchmark/frappe_lending/README.md) — its
README documents the pinned compose bring-up and fixture bootstrap). The
bundle's effect
contracts are exactly the ones the reference matrix ships
(`openadapt_flow/benchmark/frappe_lending.py::loan_application_effects`):
one `record_written` (at-most-once for the synthetic applicant) plus a
`field_equals` read-back per entered field, each bound to `{param: ...}`.

`deployment.frappe.yaml` (REST oracle, read-only user, path templated on the
run's applicant):

```yaml
effects:
  kind: rest
  base_url: http://localhost:8000
  records_path: >-
    /api/resource/Loan%20Application?fields=["name","applicant","loan_product","loan_amount","repayment_periods"]&filters=[["Loan Application","applicant","=","{applicant}"]]&limit_page_length=100
  records_key: data
  path_params:
    applicant: { param: applicant }
  auth:
    header: Authorization
    value_env: FRAPPE_ORACLE_AUTH     # "token <api_key>:<api_secret>" of the READ-ONLY oracle user
```

The independent SQL cross-check (same contract, different transport — the
fixture's MariaDB):

```yaml
effects:
  kind: sql
  sql_query: >-
    SELECT name, applicant, loan_product,
           CAST(loan_amount AS CHAR) AS loan_amount,
           CAST(repayment_periods AS CHAR) AS repayment_periods
    FROM `tabLoan Application` WHERE applicant = %(applicant)s
  sql_query_params:
    applicant: { param: applicant }
  sql_driver: pymysql
  # database = the fixture site's DB name (the fixture derives it at runtime —
  # see benchmark/frappe_lending/fixture.py::_site_db_name). The fixture only
  # exposes root; a REAL deployment must use a dedicated read-only DB role
  # (see the enforcement note above).
  sql_connect_args: { host: 127.0.0.1, user: root, database: "<site-db-name>" }
  sql_password_env: FRAPPE_DB_ROOT_PASSWORD
```

Run either with
`openadapt-flow run bundle/ --config deployment.frappe.yaml --params-file params.json`
where `params.json` supplies `applicant`, `loan_product`, etc. The whole-run
table-delta audit (`audit_table_deltas`) is what the reference matrix layers
on top — see `benchmark/frappe_lending/fixture.py::EXPECTED_TABLE_DELTAS`.

## Worked example 2 — OpenEMR (FHIR + SQL)

Runs against the live-OpenEMR fixture
([`benchmark/openemr_live/`](../benchmark/openemr_live/README.md); bring-up:
`docker compose -f benchmark/openemr_live/docker-compose.yml up -d` then
`eval "$(benchmark/openemr_live/setup.sh)"`, which exports
`OPENEMR_FHIR_BASE_URL` + `OPENEMR_FHIR_TOKEN`).

FHIR verifier, patient bound per-run, token secret-isolated:

```yaml
effects:
  kind: fhir
  base_url: https://localhost:9300/apis/default/fhir
  resource_type: Observation
  search_param_exprs:
    patient: { param: patient_ref }     # e.g. "Patient/9", from --params-file
  field_paths:
    id: id
    patient: subject.reference
    status: status
    note: valueString
  access_token_env: OPENEMR_FHIR_TOKEN
  verify_tls: false                     # the local fixture uses a self-signed cert
```

with a bundle effect such as:

```json
{"kind": "record_written",
 "match": {"patient": {"param": "patient_ref"}, "status": {"literal": "final"}},
 "expected_count": 1, "count_new_only": true,
 "probe": "exactly one NEW final Observation for this run's patient"}
```

The SQL cross-check against the fixture's MariaDB (`openemr` database) reads
the same truth through a different transport:

```yaml
effects:
  kind: sql
  sql_query: >-
    SELECT f.encounter, f.pid, f.note
    FROM form_encounter f WHERE f.pid = %(pid)s
  sql_query_params:
    pid: { param: patient_pid }
  sql_driver: pymysql
  sql_connect_args: { host: 127.0.0.1, port: 3306, user: openemr, database: openemr }
  sql_password_env: OPENEMR_DB_PASSWORD
```

**Honesty note:** the FHIR configuration above is exercised end-to-end by the
opt-in live test when the fixture is up; the OpenEMR *SQL* snippet is a
configuration template validated at the kit level (sqlite-backed contract
tests), not a CI-run assertion against OpenEMR's schema.

## What the runtime does with all this (existing behavior)

Replay/run resolves each step's effects against the run params, snapshots the
pre-state, performs the action, then verifies — HALTing on any non-CONFIRMED
verdict (irreversible effects get one reconcile-or-escalate pass). That flow
is unchanged by this kit; the kit adds the declarative construction path, two
substrates, the guard, coverage reporting, and the typed reconciliation
surface. See `docs/design/EFFECT_VERIFIER.md` and
`docs/design/GOVERNED_RUN_AUTHORIZATION.md` (effect contracts are bound into
run authorization, PR #129).
