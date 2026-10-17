# Matched local OpenEMR benchmark (synthetic and pinned)

Status: **local model-free initial engineering matrix complete; not publication
evidence**. On 2026-07-16, the compiled and direct-API arms each completed three
trials under the pinned baseline and three under `ui_cosmetic_v1`: 12/12 rows
were `correct`, with zero silent incorrect successes, zero over-halts, zero
model calls, and $0 model cost.

On 2026-07-21 the **paid computer-use agent arm was run** (it is no longer
omitted). On a separately provisioned synthetic baseline, the
`claude-sonnet-5` agent drove the same patient-registration task and was scored
by the same arm-independent effect contract:

| metric | value |
|---|---|
| trials | 6 (2 conditions x 3) |
| primary outcome | 0/6 `correct`; 6/6 `missing_write` |
| behaviour | every run exhausted its 35-action budget without saving a patient; the agent never got past OpenEMR's duplicate-search / confirm-create step |
| over-halt | 0/6 (all failures were classified `missing_write`) |
| silent incorrect success | 0/6 (it wrote no wrong or duplicate patient) |
| model cost / run | $0.8901 (list price) |
| total model cost | $5.3408 |
| latency (wall) mean | 113.3 s |
| model | `claude-sonnet-5`, `computer_20251124`, <=35 actions/run, <=$1.50/run cap |

This is an honest negative result: on this heavily framed local OpenEMR task,
the bounded-budget computer-use agent failed to complete the write. The earlier
model-free subset recorded deterministic compiled completion at $0, but the
agent arm used a separately provisioned baseline, so this is not a matched
comparison and no compiled result is claimed on the agent baseline. A fully
matched single-baseline three-arm matrix at 10 trials per cell remains the
publication bar. Raw per-run rows, environment fingerprints,
application-specific adapter/oracle wiring, and the detailed cost ledger are
retained in the private `OpenAdaptAI/openadapt-corpus` repository. The public
generic cost-capped agent mechanism remains in
`openadapt_flow/benchmark/agent_baseline.py`. See the
[aggregate report](../agent_arm_verticals/README.md).

The accepted run used baseline SHA-256
`8d2901490a0a6a6044e94b6a8a1663436b7dacedda4f2fe1fb8c48405165011d`.
Protected raw evidence remains in the ignored local directory
`results-model-free-corrected5-20260716/`; it is not a committed or public
reliability claim. Earlier failed/refused runs remain preserved rather than
being overwritten. The pinned stack ran through a disposable, loopback-only
Podman VM because Docker Desktop's existing backing image remains preserved and
unavailable after its ext4 failure.

This protocol replaces the historical shared public-demo methodology for future
reliability comparisons. It does not alter or regenerate the committed historical
OpenEMR cost/latency result.

## Why patient registration replaces the historical note task

The historical workflow writes a Patient Message/note. In OpenEMR 8.0.0.3, the
official Standard API grants Patient Messages create/update/delete but no read
or search permission. Final-screen OCR therefore cannot be upgraded into the
required separately authenticated system-of-record readback for that exact
write. Reusing it would preserve the methodological defect.

The matched task is:

> From an already authenticated blank form, create exactly one fictional
> patient with the declared name, DOB, sex, address, phone, and reserved
> `example.invalid` email; save it.

OpenEMR's official API exposes both Patient create and read/search, and the
underlying `patient_data` row can be audited independently. This preserves the
useful shape of the Frappe task—one multi-field browser form, one durable record,
one exact cardinality requirement—while enabling a real oracle.

The semantics are not identical. A Frappe Loan Application is a draft lending
transaction tied to an existing applicant/product/company. An OpenEMR Patient
is clinical identity master data. The latter would be PHI in production and has
a different risk profile. Here every value is fictional: `example.invalid`, the
reserved 555-0107 number, and an invented address/name. These results may compare
execution behavior, not business-domain equivalence or regulated readiness.

Primary upstream references:

- [OpenEMR 8.0.0.3 release](https://github.com/openemr/openemr/releases/tag/v8_0_0_3)
- [OpenEMR official Docker image](https://hub.docker.com/r/openemr/openemr)
- [OpenEMR Standard REST API](https://github.com/openemr/openemr/blob/v8_0_0_3/Documentation/api/STANDARD_API.md)
- [OpenEMR OAuth authentication](https://github.com/openemr/openemr/blob/v8_0_0_3/Documentation/api/AUTHENTICATION.md)
- [OpenEMR authorization scopes](https://github.com/openemr/openemr/blob/v8_0_0_3/Documentation/api/AUTHORIZATION.md)

## Exact fair-arm protocol

The OpenEMR and Frappe drivers import the same result schema and enforce the
same matrix:

| Condition | compiled | computer-use agent | direct API control |
|---|---:|---:|---:|
| pinned baseline | 3 initial / 10 publication | 3 / 10 | 3 / 10 |
| `ui_cosmetic_v1` | 3 / 10 | 3 / 10 | 3 / 10 |

The complete three-arm initial matrix is 18 trials. The completed model-free
engineering subset is 12 trials; publication is 60 fresh trials. A failed trial
is recorded and never retried or discarded. Authentication and
opening the blank form are identical, unmeasured setup for browser arms. The API
arm is still reset and measured under the drift condition as the UI-independent
control.

- **compiled:** one browser demonstration, compiled and replayed; zero model
  calls and $0.
- **agent:** intent-only prompt over the same browser surface; calls, input,
  output, cache tokens, latency, and list-price cost recorded. It is impossible
  to start without explicit opt-in, a positive per-run cap, and a positive total
  cap. There is no unmeasured paid preflight call. Before every Messages call,
  Anthropic's free token-counting endpoint sees the exact messages, images,
  tools, and model. The guard adds a 20,000-token margin, refuses any request
  that could enter long-context pricing, and reserves the worst standard-tier
  cache-write input plus the full 4,096-token output before dispatch. If a
  provider exception makes the last attempt's billing ambiguous, that row is
  saved as spend-indeterminate and all subsequent paid calls stop. The control
  follows Anthropic's official [token-counting](https://platform.claude.com/docs/en/build-with-claude/token-counting)
  and [pricing](https://platform.claude.com/docs/en/about-claude/pricing)
  documentation; both must be rechecked if the pinned model/prices change.
- **api:** OpenEMR Standard REST `POST /apis/default/api/patient` through
  `ApiActuator`; zero model calls and $0.

In OpenEMR, `ui_cosmetic_v1` changes only page and header paint colors without
changing labels, values, layout, or task semantics.

The OpenEMR UI's first `#create` click does not save. It opens the pinned
duplicate-search iframe. Recording explicitly clicks `#confirmCreate`, waits
for a durable SQL row, and requires the independent oracle to pass before the
recording is accepted. A sidecar binds that exact event index to the effect
contract, because a top-level browser backend sees the iframe rather than the
button's inner DOM selector. Compilation refuses a missing, malformed, or
non-click marker; it does not attach effects to an assumed last step.
The marker is created only after that oracle passes. Compilation also seals a
benchmark-contract sidecar into bundle provenance. Matrix preflight requires
the expected workflow name, confirmation step, exact effect list and risk,
every required synthetic parameter, and recording/marker digests before the
first paid request; a merely parseable unrelated bundle is refused.

## Independent oracle and reset

Every trial restores the same byte-for-byte MariaDB dump and verifies its
SHA-256 first. The OpenEMR writer container is stopped during restore. The
fixture then obtains two bearer tokens from distinct OAuth clients:

- actor client: exactly `openid api:oemr user/patient.crus`;
- oracle client: exactly `openid api:oemr user/patient.rs`.

The oracle client cannot create/update/delete. Its filtered Patient readback is
compared field-for-field and identifier-for-identifier with direct SQL from
`patient_data`. Exact before/after row counts across every table provide a
collateral-write inventory. There is no broad allowlist. The exact arm-specific
contract covers every observed subscriber table, not only the target row. For
the accepted pinned run, the API arm included `api_log:+2`, paired
`log`/`log_comment_encrypt:+16`, `patient_data:+1`, `uuid_mapping:+1`, and
`uuid_registry:+2`. The compiled arm included `api_log:+13`, paired
`log`/`log_comment_encrypt:+311`, the target patient and history rows, and the
exact reviewed contact, clinical-rule, recent-patient, settings, and UUID
deltas. Every other nonzero table delta and every negative delta fails.
The SHA-256 of a deterministic dump of every *non-target* `patient_data` row is
also required to remain unchanged. This catches same-table collateral churn
that could hide behind an expected net count. For browser arms, the one new
`history_data` row must also carry the newly created target PID, while a dump of
all non-target history rows must equal the full pre-write dump. The API arm
requires zero history rows for that PID and unchanged history content. The
accepted contract was derived only after three consecutive pre-trial table
inventories agreed, then remained exact across all six compiled trials and all
six API trials. A future legitimate subscriber write still fails as
`collateral_write` until its exact arm-specific cardinality is reviewed. That
is the safe direction.

The shared taxonomy is:

- `correct`
- `missing_write`
- `partial_write`
- `duplicate_write`
- `collateral_write`
- `rest_db_disagreement`
- `oracle_indeterminate`
- `execution_error`

`silent_incorrect_success` and `over_halt` are orthogonal counters. Actor
self-report and pixels never decide success. An unreadable REST/SQL/delta oracle
is indeterminate, not successful.

Every trial writes a protected evidence directory containing raw synthetic
REST/SQL records, every before/after table count and delta, non-target digests,
oracle errors, observed image/source identity, and artifact hashes. Agent trials
also retain the final screenshot and action log; compiled trials bind the full
replayer artifact tree. Files are created atomically as mode 0600 under mode
0700 directories. Malformed oracle identifiers become a persisted
`oracle_indeterminate` row, including already-incurred model usage, rather than
terminating the matrix. `wall_s` has one meaning for all arms: actuation start
through common REST, SQL, non-target, and table-delta verification. The narrower
actuation time is retained separately in metadata.
The common timer stops immediately after those oracles, before browser teardown,
fallback screenshot capture, or evidence serialization. Every row's evidence
locator is relative to the matrix output root and resolves under
`evidence/<trial>/`; artifact hashing refuses symlinks and non-regular files.

## Environment identity and limitations

`environment.lock.json` pins:

- OpenEMR tag `v8_0_0_3` to commit
  `7c96c8eefe460d6fadbccbe93d0fa6bf819acd69`;
- official OpenEMR image index
  `sha256:0aa4d3d52b22fa69986c087e7c99e9854d8dfd70440634eb7c8af0e08f19f3ab`;
- MariaDB 11.8 image index
  `sha256:efb4959ef2c835cd735dbc388eb9ad6aab0c78dd64febcd51bc17481111890c4`;
- SHA-256 proofs for 13 files governing release identity, REST routes and
  controller, the actual form/duplicate-confirm/save path, Patient service and
  validator, social-history auxiliary write, login, OAuth test-client command,
  and GPL license as installed in the running image.

The remote tag, all proof bytes fetched from the exact commit, and image
identities are checked during `prepare`; local RepoDigest and all 13 installed
source-file hashes are checked on every start/reset. This is materially stronger
than trusting a mutable tag, but it is not a complete reproducible-build proof
for every byte in the official image. The results must state that limitation.
The built-in OpenEMR test-client command is used only to create local client
identities; their registered scopes are immediately narrowed and no generated
secret is logged or persisted by this driver.

Both HTTP and HTTPS bind only to `127.0.0.1`. HTTP is used for browser replay so
Playwright need not trust the fixture's self-signed certificate; OAuth/REST uses
the official HTTPS endpoint. The synthetic credentials are generated into a
0600 file ignored by Git. Runtime secrets, OAuth identities, the SQL dump and
its hash are created with exclusive 0600 file descriptors—never written
permissively and chmodded afterward. Recording always resets to the verified
baseline before launching a browser.

## Reproduction commands and optional spend gate

The plan is safe and offline:

```bash
PYTHONPATH="$PWD" .venv/bin/python scripts/openemr_local_demo.py plan --profile initial
```

With a responsive Docker-compatible API, provider/source verification and
fixture setup are:

```bash
PYTHONPATH="$PWD" .venv/bin/python scripts/openemr_local_demo.py preflight
PYTHONPATH="$PWD" .venv/bin/python scripts/openemr_local_demo.py prepare
PYTHONPATH="$PWD" .venv/bin/python scripts/openemr_local_demo.py up
PYTHONPATH="$PWD" .venv/bin/python scripts/openemr_local_demo.py bootstrap
PYTHONPATH="$PWD" .venv/bin/python scripts/openemr_local_demo.py snapshot

PYTHONPATH="$PWD" .venv/bin/python scripts/openemr_local_demo.py record \
  --out benchmark/openemr_local/out/recording --headed
PYTHONPATH="$PWD" .venv/bin/python scripts/openemr_local_demo.py compile \
  --recording benchmark/openemr_local/out/recording \
  --bundle benchmark/openemr_local/out/bundle
```

The full three-arm matrix will not run without acknowledging the paid arm.
Obtain explicit spend approval first and choose a new output directory.

Before spend approval, run the explicit model-free engineering subset. It uses
the same baseline and drift conditions, per-cell trial count, reset, timing
boundary, oracle, and evidence retention for the compiled and direct-API arms:

```bash
PYTHONPATH="$PWD" .venv/bin/python scripts/openemr_local_demo.py plan \
  --profile initial --model-free
PYTHONPATH="$PWD" .venv/bin/python scripts/openemr_local_demo.py run \
  --profile initial --model-free \
  --bundle benchmark/openemr_local/out/bundle \
  --out benchmark/openemr_local/out/model-free-initial-YYYYMMDD
```

`--model-free` rejects paid-agent flags and makes no model call. Its 12 initial
rows are compiled-versus-API engineering evidence only. Results explicitly
mark the agent arm omitted and keep `full_matrix_complete` and
`publication_ready` false, including a 10-per-selected-cell run; do not present
this subset as the complete three-arm comparison.

The paid three-arm command remains:

```bash
PYTHONPATH="$PWD" .venv/bin/python scripts/openemr_local_demo.py run \
  --profile initial \
  --bundle benchmark/openemr_local/out/bundle \
  --out benchmark/openemr_local/out/initial-YYYYMMDD \
  --allow-paid-agent \
  --max-cost-per-run-usd 1.50 \
  --max-total-agent-cost-usd 9.00
```

The $9 initial total is six agent cells times the fully reserved $1.50 per-run
envelope. Actual recorded spend will normally be lower. The matched Frappe run
must use the same pre-call token-count guard; do not compare against a run that
labels a post-call threshold as a hard cap.

`publication_ready: true` means only that all 60 protocol cells are present with
one shared baseline hash. It is not certification and does not authorize a site
claim without human review of every row, DB delta, screenshot, environment
identity, and the matched Frappe run on the same machine/model/cost policy.
