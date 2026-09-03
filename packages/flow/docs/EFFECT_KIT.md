# The effect-verifier kit — declare an outcome, ship its verification

**Status:** kit surface implemented + CI contract-proven. Per-substrate
maturity is listed honestly below — the SQL and file/SFTP verifiers are
**contract-proven against local fixtures (sqlite, fakes) in CI, not
live-proven against a production database or SFTP endpoint**; the FHIR
verifier additionally has an opt-in live-OpenEMR test
(`tests/test_effect_fhir_live_openemr.py`).

> Without a declared effect and a configured verifier, OpenAdapt falls back to
> screen evidence for a consequential write or read outcome. This kit exists
> so declaring the outcome and configuring the verifier is a **reviewed YAML
> section**, not a bespoke per-deployment integration.

The underlying design (three-valued verdicts, fail-safe-to-HALT, the shared
judge) is [`docs/design/EFFECT_VERIFIER.md`](./design/EFFECT_VERIFIER.md).
This page is the **operator kit**: what to declare, how to configure each
substrate, how run parameters and secrets bind, and two worked examples lifted
from the reference apps.

## Concept

1. **The bundle declares WHAT must be true** — typed `Effect` contracts on
   each consequential step (`record_written` for mutations; `field_equals`
   for a unique persisted field or independently read business outcome;
   `exact_new_set` for the full set of records an action may add),
   at-most-once counts, idempotency keys, and `{param: ...}` references that
   bind to the run's governed parameters. Contracts are substrate-neutral.
2. **The deployment declares WHERE truth lives** — the `effects:` section of
   `deployment.yaml` wires one `EffectVerifier` (REST / GraphQL / FHIR / SQL /
   file / email / document / document-hash, or a registered plugin adapter)
   plus its secret-isolated auth. When more than one reviewed read boundary is
   available, `candidates:` selects the strongest verification class for each
   resolved effect before input. It does not downgrade after input. An unavailable
   selected proof halts or enters reconciliation.
3. **The runtime refuses to guess.** Every verdict is CONFIRMED / REFUTED /
   INDETERMINATE; both non-confirmed verdicts HALT. A step that declares
   effects with no verifier configured HALTs. An escalated failure emits a
   typed `ReconciliationTask` (see below) — halt + evidence, never silent.
4. **Certification measures coverage.** `openadapt-flow lint` reports
   per-consequential-step effect coverage (% of irreversible steps declaring a
   contract) and warns per gap (`missing_effect_contract`; `same_channel_oracle`
   when the declared oracle is the same page session, screenshot, or banner as
   the acting surface). A policy that sets
   `require_effects_for_irreversible: true` turns a missing contract into a
   certification **failure** (warn-vs-fail is the policy's choice). Independent
   API, SQL, second-session, and file reads are channel-disjoint; on-screen
   success is not.

## The selector names the subject

An oracle read returns the whole collection. `VerifierOracle.read` and
`JsonDocumentOracle.read` in `openadapt_flow/reward/oracles.py` stamp the
episode's `oracle_identity` onto the observation and then hand back every
record they can see. Nothing filters those records by patient or by account.

So the effect's own `match` is where the subject enters. A `{param: ...}`
reference resolves against the episode identity before the judge runs, and the
judge keeps only the records that selector picks. Write the subject into
`match` and the contract asks about one patient. Leave it out and the contract
asks about the collection.

Right:

```yaml
effects:
  - kind: record_written
    match:
      patient_id: {param: patient_id}   # binds to THIS episode's patient
      type: Triage
    expected_count: 1
```

Wrong, and `RewardBundle.load` refuses it:

```yaml
effects:
  - kind: record_written
    match:
      type: Triage                      # any patient's triage row satisfies it
    expected_count: 1
```

The second contract still declares `oracle.identity_keys: [patient_id]`, so
its receipt names a patient. Put one triage row in the store belonging to
somebody else and the judge CONFIRMS the effect: the receipt then asserts a
patient whose record was never read. The load-time guard closes that. A bundle
whose required effects select no record by a declared identity key fails to
load, and the error names the missing keys and the selector to add.

`idempotency_key` counts as a selector too, because it filters the matched set
by `key_field`. `value` does not. On a `field_equals` contract `match` chooses
the record and `value` asserts its content, so a subject id in `value`
describes whatever matched instead of choosing what to read.

**`identity_keys` is not `identity_field`.** `identity_keys` is the reward
contract's list of parameters that name the subject of an episode.
`identity_field` is the row id an `exact_new_set` effect reads to tell a record
added by the action from one that was already there, and an
environment-assigned surrogate key is the right choice for it.

## Substrates

| `effects.kind` | Verifier | Probe | Proven how |
|---|---|---|---|
| `onscreen` | `OnScreenReadbackVerifier` | re-OCR the saved value off the live screen — the **no-API default** for GUI-only recordings; auto-derived from the demonstration. **Different-path** (re-open the record) is default-eligible; **same-surface** (re-read the write's own form) is opt-in only | measured in `benchmark/effect_readback/` — different-path false-CONFIRM 0, same-surface > 0. **A read-back CONFIRMED is a consistency signal, NOT transactional proof** (`docs/LIMITS.md`) |
| `rest` | `RestRecordVerifier` | GET a JSON records document (templatable path, secret-isolated auth headers) | live in CI against the MockMed transactional back end; Frappe-shaped read in the reference matrix (PR #131) |
| `fhir` | `FhirEffectVerifier` | FHIR R4 search → flattened resources | CI against a byte-faithful fake FHIR server; **opt-in live test** against a real local OpenEMR |
| `graphql` | `GraphQLRecordVerifier` | ONE read-only GraphQL query (a `mutation`/`subscription` refuses to construct), records extracted at a dotted path; optional freshness window → STALE demotion | **contract-proven in CI against a fake session** -- read-only guard, entity binding, error-body handling, staleness; not live-proven against a production GraphQL endpoint |
| `sql` | `SqlRecordVerifier` | ONE read-only SELECT (enforced whitelist), rows judged like any substrate | **contract-proven in CI against sqlite fixtures only** — the query/whitelist/verdict logic is what's proven, not any specific production database |
| `file` | `FileArrivalVerifier` | directory / SFTP listing → `size_ok` + `fresh` + `content_match` per candidate | **contract-proven in CI against temp dirs and a fake SFTP transport** — not live-proven against a real SFTP server |
| `email` | `MaildirDeliveryVerifier` | maildir / SMTP-capture directory → one record per message (`to`, `subject`, `message_id`, body probe, freshness); verifies delivery TO THE CAPTURE POINT, not end-to-end receipt | **contract-proven in CI against maildir/tmp-dir fixtures** (wrong recipient, duplicate send, leak-to-other-recipient collateral hook) -- not live-proven against a production MTA |
| `document` | `DocumentArrivalVerifier` | report arrival + **parseable-content assertion**: each candidate parses (JSON dotted paths or named-regex groups) into judgeable fields; a corrupt report REFUTEs a `parseable: True` contract | **contract-proven in CI against temp-dir fixtures** (corrupt report, wrong entity, duplicates, stale mtime) |
| `document-hash` | `DocumentHashVerifier` | SHA-256 of each document in a store | live in CI (no external service) |

All substrates share one judge (`runtime/effects/_common.py`), so
at-most-once counting, idempotency-key de-duplication, field read-back,
collateral-loss detection, the duplicate-write guard, and the `exact_new_set`
over-write guard below behave identically everywhere.

### The duplicate-write / idempotency guard (`count_new_only`)

`Effect(kind=record_written, count_new_only=True, expected_count=1)` counts
only records that did **not** exist in the pre-action snapshot: *"exactly one
NEW matching record was created by this action."* Use it when the selector
legitimately matches pre-existing rows (e.g. "an encounter for this patient").
It requires a readable pre-state — an unreachable baseline is INDETERMINATE →
HALT, never a guess. Available on every substrate.

### The over-write guard (`exact_new_set`) — opt-in, and why you want it

Every other kind answers **"is my record there?"**. None of them answers
**"and nothing else?"**. A contract set that declares one `record_written`
per intended new record is silent about the records it never named, so an
actuation that writes the 6 intended rows **and 31 unintended ones** satisfies
every declared contract: the runtime CONFIRMS while the system of record holds
writes nobody asked for. That is a **false pass** — the one error direction
this design must never take. (The 6-vs-37 case is not hypothetical: it was
measured in a 150-trial benchmark study of an agent asked to download 6
records.)

`Effect(kind=exact_new_set, ...)` closes it. One **table-scoped** effect
declares the FULL set of records the action may add:

```yaml
effects:
  - kind: exact_new_set
    # `match` is the SCOPE, not a target selector. Empty = the whole read set.
    match: {user_id: "32"}
    # One selector per intended record. Repeat a selector to declare that many
    # identical additions. Values may be literals or {param: ...} references.
    new_records:
      - {user_id: "32", song_id: "199"}
      - {user_id: "32", song_id: "9"}
    # Must equal len(new_records). Stated explicitly so an edit that drops a
    # member fails loud instead of silently weakening the contract.
    expected_count: 2
    # How a record ADDED by this action is told apart from one already there.
    # A surrogate key is the RIGHT choice here even though it is the wrong
    # thing to pin in a selector.
    identity_field: id
```

It REFUTES: an addition no member names (the guard), a missing or duplicated
member, a wrong cardinality, and collateral loss inside the same scope. It
requires a **real pre-action baseline**: with an unreachable baseline, or a
record on either side carrying no `identity_field` value, the added set cannot
be enumerated and the verdict is INDETERMINATE → HALT. Newness is never
guessed. `new_records: []` with `expected_count: 0` is the meaningful
assertion *"this action adds NOTHING to this read set."*

Available on every substrate (it runs in the shared judge). It is judged
against the pre-action snapshot, so the current-state read-back paths (durable
resume, attended qualified read-back) refuse it rather than judge it against a
synthesized empty baseline.

**Backward compatibility and the honest boundary.** This kind is **additive
and opt-in**. Flow contracts are operator-authored. There is no derivation
step that can turn the guard on for you. Thus, every contract written before
this option judges **exactly** as it did before, and its `contract_hash` is
byte-identical (the new fields enter the digest only on the new kind).
The boundary follows directly: **an existing contract does not detect an
over-write unless the operator declares an `exact_new_set` effect for that
read set.** Declare one on any step where an unintended extra write would
matter.

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
| `graphql` | `base_url` + `graphql_query` | `graphql_variables` (`{param: ...}` entity binding), `graphql_records_path`, `graphql_freshness_field` + `graphql_freshness_window_s`, `auth` |
| `fhir` | `base_url` | `resource_type`, `search_params`, `search_param_exprs`, `field_paths`, `access_token_env`, `verify_tls` |
| `sql` | `sql_query` + (`sqlite_database` or `sql_driver`) | `sql_query_params`, `sql_connect_args`, `sql_password_env` |
| `file` | `root` | `file_pattern`, `file_min_size`, `file_mtime_window_s`, `file_content_probe` |
| `email` | `root` | `mail_pattern`, `file_mtime_window_s` (freshness), `file_content_probe` (body regex) |
| `document` | `root` | `file_pattern`, `document_format` (`json` \| `text`), `document_field_paths`, `document_text_pattern`, `file_mtime_window_s` |
| `document-hash` | `root` | `glob` |

Any kind additionally accepts the evidence-minimization fields
`evidence_redact_fields` / `evidence_keep_fields` (see "Evidence minimization"
below).

### Candidate selection when there is no database connection

A database connection is not required. Configure the strongest qualified
read boundary that the workflow has: REST/FHIR/GraphQL, read-only SQL, a file
or report export, a separately authenticated read-only session through a
plugin, or a persisted-state re-acquisition. Do not configure a same-surface
screen read-back as proof of a consequential write.

For more than one reviewed boundary, use `effects.candidates` instead of
`effects.kind`. Each candidate has the normal `EffectsConfig` fields. Flow
constructs every candidate before actuation, then selects the strongest
`VerificationTier` enum member for each resolved effect; declaration order
resolves a tie. `VerificationTier` is a persisted Flow v1 implementation field.
Do not present its numeric value as a Seal Oracle tier. Public receipts use one
ladder: Oracle tier 0 is visual evidence, tier 1 is a separate read-only
session, tier 2 is a system-of-record read, and tier 3 is a counterparty
acknowledgment. This makes the choice deterministic and reviewable. A missing
secret, invalid config, or invalid plugin verification class refuses the run
before input. The on-screen candidate uses
`PERSISTED_STATE_REACQUISITION` only for that exact effect when its read-back
reopens persisted state through a different path. It uses `IMMEDIATE_SCREEN`
for a same-surface read-back. After the action, Flow does not fall back to a weaker
candidate if the selected verifier is unavailable. It records the unavailable
proof and halts or creates the normal reconciliation task.

```yaml
effects:
  candidates:
    - kind: document             # independent system-of-record export
      root: /secure/exports
      file_pattern: "confirmation-*.json"
      document_format: json
    - kind: onscreen             # persisted-state read-back
```

The single `kind:` form remains the recommended configuration when one
qualified verifier exists and remains fully compatible with prior deployments.

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

## The verifier adapter platform

Every substrate above implements ONE stable interface
(`openadapt_flow/runtime/effects/adapter.py`), so "add a system of record"
means "implement the interface", first-party or as a customer plugin. The
lifecycle every adapter honors:

1. **configure** -- the constructor / registered factory, called with the
   deployment's `effects:` section and the governed run params. Secrets
   arrive as env-var / secret-manager REFERENCES (never literals, isolated
   from the actuation session's credentials) and entity + tenant binding uses
   `ValueExpr({param: ...})`, both resolved here -- fail LOUD on anything
   missing.
2. **test-connection** -- `test_connection()`: a read-only reachability probe
   (never a write, never raises) for operator preflight.
3. **capture-before** -- `capture_pre_state()`: the baseline snapshot for
   delta (`count_new_only`), duplicate, and collateral accounting.
4. **capture-after** -- `capture_post_state()`: a fresh post-action snapshot
   (default: the same read), also fed to collateral-effect hooks.
5. **verdict** -- `verify()`: poll-until-settled within the effect's deadline
   (`Effect.timeout_s` -- an asynchronous write that never settles is a
   failure, not a pass), judge with the shared judge (cardinality
   zero / exactly-one / exact-N via `expected_count`; duplicates via
   `count_new_only` + `idempotency_key`; collateral loss via
   `forbid_collateral_loss` plus substrate-specific `collateral_hooks`),
   optionally enforce a freshness window, then minimize evidence.

### Result classes and the transaction taxonomy

`classify_adapter_result` refines the three-valued verdict into six explicit
result classes; `transaction_outcome_for` maps them onto the terminal
transaction taxonomy. **No non-confirmed class maps to a pass** -- the
refinement tells the operator WHICH failure they are reconciling, it never
softens one:

| Adapter result | Meaning | Transaction outcome |
|---|---|---|
| `confirmed` | effect present, correct, fresh | (step proceeds; run-level `VERIFIED` is decided by the run classifier) |
| `refuted` | affirmatively ABSENT (observed count zero) | `HALTED_BEFORE_EFFECT` (this step; run-level `HALTED_BEFORE_EFFECT` is decided by the run classifier, which additionally requires absence for EVERY declared effect of EVERY consequential step) |
| `conflicting` | a write LANDED but is duplicated / wrong-valued / collateral | `RECONCILIATION_REQUIRED` |
| `unavailable` | system of record unreachable / credential failure | `RECONCILIATION_REQUIRED` |
| `stale` | data read but outside the declared freshness window | `RECONCILIATION_REQUIRED` |
| `indeterminate` | cannot certify for another reason (e.g. unreadable baseline) | `RECONCILIATION_REQUIRED` |

### Confidence tiers (screen read-back is demoted, explicitly)

Every adapter advertises a `VerificationTier` (lower = stronger); execution
profiles gate on the tier, never on prose:

| Tier | Label | What it proves | Adapters |
|---|---|---|---|
| 1 | `independent-system` | a read through the SoR's own API/DB/store -- independent proof | `rest`, `graphql`, `fhir`, `sql`, `file`, `email`, `document`, `document-hash` |
| 2 | `independent-session` | same app, separately authenticated read-only session | customer adapter through the plugin interface |
| 3 | `reacquired-state` | the app's own UI re-navigated to re-fetch persisted state | `onscreen` with a different-path read-back |
| 4 | `screen-consistency` | the same surface the write drove still shows the value | `onscreen` same-surface |

**Same-application screen read-back is a LOWER-CONFIDENCE consistency check,
never independent system-of-record proof** -- an optimistic UI can paint
success while nothing persisted. The onscreen adapter declares
`independent_system_of_record = False` in code, and `docs/LIMITS.md` carries
the measured false-CONFIRM evidence behind the demotion.

### Adapter matrix

| Adapter | Status | Notes |
|---|---|---|
| REST read-back | **supported** | `rest` |
| GraphQL read-back | **supported** | `graphql`; read-only guard, freshness window |
| FHIR R4 | **supported** | `fhir`; a FHIR profile over HTTP read-back with resource/entity binding (`search_param_exprs`); opt-in live-OpenEMR test |
| read-only SQL | **supported** | `sql`; whitelist + read-only role |
| file / SFTP arrival | **supported / programmatic SFTP** | `file`; SFTP today via an injected paramiko-compatible `transport` |
| maildir / SMTP-capture email delivery | **supported** | `email`; delivery to the capture point |
| document / report arrival + parse | **supported** | `document` |
| document store (exact bytes) | **supported** | `document-hash` |
| on-screen read-back | **supported (demoted)** | `onscreen`; tiers 3-4, consistency only |
| customer plugin | **supported (SDK seam)** | any kind via the entry-point group below |

### Evidence minimization (field-level redaction)

Any kind accepts `evidence_redact_fields` (denylist) or
`evidence_keep_fields` (allowlist): the named record fields in every emitted
verdict's evidence (matched records; observed/expected values when the
effect's read-back field is named) are replaced with opaque markers. This
avoids making low-entropy identifiers recoverable through a digest dictionary.
The verdict itself is never altered -- redaction minimizes evidence, it cannot
soften a failure into a pass.

### Shipping your own adapter (plugin SDK)

A customer package implements the interface and registers a factory; no
OpenAdapt fork required. The worked reference is
`tests/example_verifier_plugin.py` (a CSV-ledger adapter exercising every
platform obligation), qualified in `tests/test_verifier_adapter_platform.py`.

1. Subclass `VerifierAdapterBase` (or match the `VerifierAdapter` protocol):
   set `substrate` + `verification_tier`, implement `capture_pre_state`
   (one fresh, fail-safe read -- return `reachable=False`, never raise) and
   `verify` (use the shared `poll_until_settled` + the shared judge so
   cardinality/duplicate/collateral semantics match every other substrate).
2. Write a factory with the `VerifierFactory` signature
   `(cfg, params) -> verifier` that reads its config from the `effects:`
   section and FAILS LOUD on missing fields or secrets.
3. Register it under the entry-point group in your package:

   ```toml
   [project.entry-points."openadapt_flow.effect_verifiers"]
   csv-ledger = "acme_verifiers.csv_ledger:build_csv_ledger_verifier"
   ```

   (or programmatically: `register_verifier_factory("csv-ledger", factory)`).
4. Deploy with `effects: {kind: csv-ledger, ...}` -- `build_effect_verifier`
   resolves built-ins first (a plugin can never shadow a built-in kind), then
   the registry; a plugin that fails to import fails the build loudly.
5. Qualify it with the platform's adversarial fixture set (stale data, wrong
   entity, duplicate rows, settlement timeout, credential failure ->
   UNAVAILABLE) -- copy the per-adapter test modules as the template.

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
