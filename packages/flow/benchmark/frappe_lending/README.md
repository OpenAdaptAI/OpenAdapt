# Frappe Lending benchmark (synthetic and pinned)

Status: **local model-free initial engineering matrix complete; not publication
evidence**. On 2026-07-16, the compiled and direct-API arms each completed three
trials under the pinned baseline and three under `ui_cosmetic_v1`: 12/12 rows
were `correct`, with zero silent incorrect successes, zero over-halts, zero
model calls, and $0 model cost. The agent arm was intentionally omitted, so the
result records `full_matrix_complete: false` and `publication_ready: false`.
Publication still requires the paid agent arm and 10 fresh trials per cell.

The accepted run used baseline SHA-256
`7fd6c965f6b7a11f54e451cdc73fdf65f88d9883dc5f8eb5b2055b3cd4be8b83`.
Protected raw evidence remains in the ignored local directory
`results-model-free-corrected3-20260716/`; it is not a committed or public
reliability claim. Earlier failed/refused runs remain preserved rather than
being overwritten. The exact custom image was built with native Podman and its
pinned source labels were verified. Docker Desktop's existing data remains
preserved and unavailable after its backing ext4 failure; it was not deleted
or repurposed.

This is the reproducible lending reference environment for OpenAdapt. It is
not evidence that Frappe Lending is a legacy Windows/Citrix application. In
fact, Frappe exposes a good REST API, so a real deployment should prefer the
API control arm. The browser arms isolate the value of compiled replay and
independent effect verification when a UI path is required.

## Task and fair arms

All data is synthetic. Authentication and opening a blank form from the pinned
synthetic Customer are declared unmeasured setup for both browser arms; API
authentication is likewise unmeasured. The browser setup uses the same route
options and full-form branch as pinned Lending's supported
`frappe.new_doc("Loan Application", {applicant_type, applicant})` Customer
integration. It calls that branch directly because pinned Frappe constructs an
unused QuickEntry dialog and leaves an orphan backdrop when quick entry is
disabled; setup refuses any remaining backdrop or modal-open body. That route supplies
`Customer`, `OpenAdapt Synthetic Applicant`, and the exact prepopulated Company
`_Test Company` before recording and timing; the resulting Applicant control
must be read-only. None is a demonstrated edit or caller-selectable workflow
parameter, and the actor does not receive broader Customer or Company write
permissions. Every reported wall time uses the same effect boundary: actuation
start through independent REST, SQL, and exact table-delta verification. The
measured task is:

> On the Loan Application already opened for fixed Customer
> `OpenAdapt Synthetic Applicant`, enter `OpenAdapt Synthetic Term Loan`,
> amount `125000`, 18 repayment periods, and the declared
> contact fields; save exactly one application.

The compiled bundle parameterizes only the five values the recorded browser
task actually enters: email, phone, loan product, amount, and repayment
periods. Applicant, applicant type, the prepopulated Company, and the pinned
repayment-method default are literal parts of the effect contract. The API
control writes those same literals and accepts the same five task parameters,
so it cannot silently substitute a different Customer or Company.
The recorded browser task preserves the explicit wheel scroll from the last
top-of-form contact input to the lower Loan Product, amount, and repayment
controls. It also waits for the exact visible Loan Product suggestion before
recording Enter; neither navigation step is hidden from the compiled program.

Every trial restores the same SHA-256-bound SQL snapshot. The three arms are:

| Arm | Actuation | Model calls | Success oracle |
|---|---|---:|---|
| compiled | one recorded browser demonstration after fixed-Customer/Company setup, compiled and replayed | 0 | read-only REST + SQL delta |
| agent | intent-only computer-use prompt after the same fixed-Customer/Company setup | measured | read-only REST + SQL delta |
| API control | Frappe REST write with the same fixed Customer and Company through `ApiActuator` | 0 | read-only REST + SQL delta |

The REST oracle authenticates as `openadapt.oracle@example.invalid`, a fixture
user with a custom read-only `Loan Application` permission. It is not the UI/API
writer. Direct MariaDB target-field read-back is a second oracle, supplemented
by an exact arm-specific per-table row-count contract for collateral
inserts/deletes. The pinned baseline pre-creates naming-series metadata, so the
only accepted count change is exactly `tabLoan Application: +1`; every other
table must remain `+0`. A legitimate subscriber discovered in a live run fails
closed until reviewed and added with an exact count—there is no broad allowlist.
Pixels and actor self-report never establish success. The table inventory does
not claim to detect a same-row, same-count update outside the target table; a
future higher-assurance deployment should bind explicit effects for each
business-relevant collateral record.

## Trial matrix

The initial engineering run is 3 trials in every cell; publication requires 10
fresh trials in every cell. A failed row is never retried or discarded.

| Condition | compiled | agent | API control |
|---|---:|---:|---:|
| pinned baseline | 3 initial / 10 publication | 3 / 10 | 3 / 10 |
| deterministic cosmetic UI drift (`ui_cosmetic_v1`) | 3 / 10 | 3 / 10 | 3 / 10 |

The drift CSS changes theme, spacing, borders, and control radius. The API arm
is intentionally UI-independent but is still reset and measured under the same
named condition as the control. Publication therefore requires 60 trials total
(3 arms × 2 conditions × 10), not a single showcase run.

Each row records end-to-end verified latency, actuation-only latency, actions,
model calls, all token buckets, list-price cost, baseline snapshot hash,
REST/SQL record hashes, complete table deltas, and this exact taxonomy:

- `correct`
- `missing_write`
- `partial_write`
- `duplicate_write`
- `collateral_write`
- `rest_db_disagreement`
- `oracle_indeterminate`
- `execution_error`

`silent_incorrect_success` and `over_halt` are orthogonal flags. This preserves
root cause (for example `duplicate_write`) while still counting an actor that
claimed success incorrectly. An unreadable oracle never becomes success.

## OpenEMR pairing: matched engineering subset, not publication evidence

The pinned local OpenEMR and Frappe drivers now use the same result schema,
baseline restore discipline, baseline/cosmetic-drift conditions, three trials
per selected cell, independent REST/SQL oracle boundary, failure taxonomy, and
silent-incorrect/over-halt counters. Each model-free subset completed 12/12
rows correctly on 2026-07-16: compiled and direct-API actuation under both
conditions, with zero model calls and $0 model cost.

| Property | pinned local OpenEMR | pinned Frappe Lending |
|---|---|---|
| synthetic task shape | browser patient-registration form and one durable record | browser Loan Application form and one durable record |
| reset | exact hashed SQL snapshot before every trial | exact hashed SQL snapshot before every trial |
| oracle | separate read-only REST client + direct SQL + exact table deltas | separate read-only REST user + direct SQL + exact table deltas |
| completed subset | compiled/API, baseline + drift, 3/cell: 12/12 correct | compiled/API, baseline + drift, 3/cell: 12/12 correct |
| agent arm | omitted | omitted |
| publication ready | **No** | **No** |

This is matched local engineering evidence, not a cross-application reliability
claim. The applications have different business semantics and risk profiles;
neither subset includes the paid agent arm, 10 fresh trials per cell, a clean-
machine replication, independent review, or a design-partner environment.
Frappe is an API-rich browser reference rather than a legacy Windows/Citrix LOS,
and synthetic OpenEMR is not production-regulated evidence. Do not compare
their latency as if the workflows were identical or promote 12/12 as a broad
success-rate claim.

## Exact environment

`environment.lock.json` pins:

- Lending v16.2.0: `caed066b6636075634418f4f0382798b60c0e188`
- Frappe Framework v16.27.0: `73decbb00106a12c4e854c98dce8c0e3f42f514e`
- ERPNext v16.27.0: `9d5c7605b8eae7fb5aaf9efd00a778adae2daeb1`
- frappe_docker: `c004361e790125ed13aaa933d11f7838711a8960`
- official `frappe/build:v16.27.0`, `frappe/base:v16.27.0`, MariaDB 11.8,
  and Redis 6.2 Alpine OCI manifest-list digests.

The official `frappe/erpnext` image is not used because it omits Lending. A
custom image is built from the pinned official build/base images and exact app
tags, after `git ls-remote` proves those tags still resolve to the locked
commits. The image build then re-checks each checked-out app HEAD against the
same locked commit before stripping Git metadata, closing the fetch/build race.
The upstream [`frappe_docker` build setup](https://github.com/frappe/frappe_docker/blob/c004361e790125ed13aaa933d11f7838711a8960/docs/02-setup/02-build-setup.md)
documents the BuildKit-secret `apps.json` path. Base/service image provenance is
available from the official Docker Hub repositories for
[`frappe/build`](https://hub.docker.com/r/frappe/build/tags),
[`frappe/base`](https://hub.docker.com/r/frappe/base/tags),
[`mariadb`](https://hub.docker.com/_/mariadb), and
[`redis`](https://hub.docker.com/_/redis).

Container digests make image inputs immutable; the completed run must also
record the built custom image ID and baseline SQL hash. The GPL-3.0 Lending and
ERPNext source and MIT frappe_docker source remain attributed to their upstream
projects. The exact pinned Customer integration is
[`custom_customer.js`](https://github.com/frappe/lending/blob/caed066b6636075634418f4f0382798b60c0e188/lending/public/js/custom_customer.js).
Any website screenshot must be captured from this synthetic fixture,
labelled “Frappe Lending reference environment,” and must not imply Encompass,
Windows, Citrix, customer use, or a completed full three-arm/publication
benchmark.

## Reproduction commands

Requirements: responsive Docker-compatible API with Compose v2 for runtime,
Docker or native Podman for the image build, network for the one-time verified
source/image fetch, and at least 40 GiB free on the state filesystem.

```bash
# Read-only plan; safe now, no Docker or model call.
.venv/bin/python scripts/frappe_lending_demo.py plan --profile initial

# Preflight/build/up fail closed on daemon/disk/image prerequisites; prepare
# verifies source pins without starting Docker.
.venv/bin/python scripts/frappe_lending_demo.py preflight
.venv/bin/python scripts/frappe_lending_demo.py prepare
.venv/bin/python scripts/frappe_lending_demo.py build

# Rootless Podman can build natively when Docker Buildx cannot run inside its VM.
# Runtime orchestration still uses the same Docker-compatible API/Compose file.
OPENADAPT_BUILD_ENGINE=podman \
OPENADAPT_PODMAN_CONNECTION=openadapt-benchmark \
.venv/bin/python scripts/frappe_lending_demo.py build
.venv/bin/python scripts/frappe_lending_demo.py up
.venv/bin/python scripts/frappe_lending_demo.py bootstrap
.venv/bin/python scripts/frappe_lending_demo.py snapshot

# Record and bind the Save step's typed effect contract.
.venv/bin/python scripts/frappe_lending_demo.py record \
  --out benchmark/frappe_lending/out/recording --headed
.venv/bin/python scripts/frappe_lending_demo.py compile \
  --recording benchmark/frappe_lending/out/recording \
  --bundle benchmark/frappe_lending/out/bundle
```

The full matrix is deliberately not copy-paste runnable without acknowledging
spend. It refuses unless `--allow-paid-agent`, a per-run hard cap above the
fixed worst-one-call reserve, and a total hard cap covering the full equal
matrix are supplied. There is no separate paid preflight request: the first
measured agent response is the auth/credit check and is accounted in its row.
The driver subtracts the one-call reserve from the agent loop's post-response
stop threshold, preventing either hard cap from being crossed by one final
response. Obtain explicit spend approval first.

An explicit model-free engineering run is available before spend approval. It
runs exactly the compiled and direct-API arms with the same baseline and drift
conditions, trial count, reset, timing boundary, oracle, and evidence retention:

```bash
.venv/bin/python scripts/frappe_lending_demo.py plan \
  --profile initial --model-free
.venv/bin/python scripts/frappe_lending_demo.py run \
  --profile initial --model-free \
  --bundle benchmark/frappe_lending/out/bundle \
  --out benchmark/frappe_lending/out/model-free-initial-YYYYMMDD
```

`--model-free` rejects paid-agent flags and never loads agent credentials. Its
12 initial rows can support compiled-versus-API engineering diagnosis only.
The result always records the omitted agent arm, keeps `full_matrix_complete`
and `publication_ready` false, and must never be presented as the complete
three-arm comparison—even with 10 trials per selected cell.

```bash
.venv/bin/python scripts/frappe_lending_demo.py run \
  --profile initial \
  --bundle benchmark/frappe_lending/out/bundle \
  --out benchmark/frappe_lending/out/initial-YYYYMMDD \
  --allow-paid-agent \
  --max-cost-per-run-usd 2.00 \
  --max-total-agent-cost-usd 12.00
```

Use a new output directory for the publication run. The driver fsyncs every
finished row immediately to a mode-0600 `rows.jsonl`, writes bounded protected
per-trial oracle/action/screenshot evidence, binds run artifacts by an
in-tree-only hash manifest, and refuses to mix runs into an existing directory.
It emits `publication_ready: true` only for a complete 10-per-cell matrix.
Actual commercial/site claims require human review of the rows, DB deltas,
screenshots, and environment identity; the boolean is only protocol
completeness, not certification.
