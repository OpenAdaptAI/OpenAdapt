# openIMIS claims-intake reference environment (synthetic and pinned)

Status: **reference/demo environment — NOT a benchmark**. This directory gives
OpenAdapt an INSURANCE vertical mirror of the healthcare (OpenEMR) and lending
(Frappe Lending) reference environments: a real open-source insurance system,
run locally from digest-pinned images, in which one health-facility claim is
entered through the browser UI (claims intake), recorded once, compiled, and
replayed — with success established only by a direct SQL read of the claim
row, never by pixels or actor self-report.

On 2026-07-21 the **paid computer-use agent arm was run** on this environment,
so the insurance vertical is no longer agent-arm-free. On the pinned synthetic
reference, the `claude-sonnet-5` agent drove the same claim-intake task and was
scored by the same arm-independent effect contract:

| metric | value |
|---|---|
| trials | 3 |
| primary outcome | 3/3 `correct` (exactly one 'Entered' claim for the synthetic policyholder each time) |
| over-halt | 0/3 |
| silent incorrect success | 0/3 (no duplicate / wrong / collateral claim) |
| model cost / run | $0.4793 (list price) |
| total model cost | $1.4380 |
| latency (wall) mean | 69.8 s |
| model | `claude-sonnet-5`, `computer_20251124`, <=30 actions/run, <=$1.50/run cap |

Raw per-run rows, environment fingerprints, application-specific
adapter/oracle wiring, and the detailed cost ledger are retained in the private
`OpenAdaptAI/openadapt-corpus` repository. The public generic cost-capped agent
mechanism remains in
`openadapt_flow/benchmark/agent_baseline.py`. See the
[aggregate report](../agent_arm_verticals/README.md).

There is still no full timing matrix and no publication protocol here: a
publication reliability claim would need the full matched protocol the Frappe
Lending and OpenEMR benchmarks define (compiled + agent + API arms, 10 fresh
trials per cell, snapshot-reset baselines). Nothing in this directory is
publication evidence.

## The application

[openIMIS](https://openimis.org/) (AGPL-3.0) is the open-source insurance
management system used by national health-insurance and social-protection
schemes. Its claims module is a real payer-side claims-intake surface: a
health-facility claim references an insuree (policyholder), visit dates, an
ICD diagnosis, and tariffed services/items, and is saved into claim status
"Entered" — the step before checking and adjudication.

It is a browser UI over a supported GraphQL API, so — exactly as the Frappe
Lending README notes for lending — a real openIMIS deployment should prefer
the API control arm. The browser demonstration isolates the value of compiled
replay when a UI path is required, and stands in for the many commercial
claims platforms that expose no such API.

## Synthetic data only

* The stack loads the upstream openIMIS **demo dataset**, a fictional fixture
  shipped by the openIMIS project (invented regions such as "Tahida",
  facilities such as "Vida District Hospital", fictional insurees, tariffs,
  and the demo actor credential `Admin`).
* `bootstrap` adds one more synthetic policyholder — **Avery Doe, insuree
  no. 999000001**, with an in-force policy through 2027-12-31 — so the
  recorded claim shows active coverage. All values are invented.
* Everything binds to `127.0.0.1` only; generated stack secrets live in the
  ignored `out/state/` directory.

Any published screenshot or clip must be captured from this synthetic fixture,
labelled "openIMIS reference environment", and must not imply a customer
deployment, production readiness, or a completed benchmark.

## The demonstrated workflow

Authentication and opening the blank claim form (fixing the health facility
`VIHOS001` and claim administrator context) are declared unmeasured setup,
mirroring the Frappe Lending setup boundary. The recorded task is:

> On the blank Health Facility Claim form, enter insuree no. `999000001`
> (which resolves the synthetic policyholder and her active policy), a claim
> number (parameter), main diagnosis `A000`, an explanation note (parameter),
> and service `A1` (General Consultation, auto-tariffed); save the claim.

The compiled bundle parameterizes the three values a claims clerk varies per
claim: `insurance_no`, `claim_no`, and `explanation`. Replay substitutes a
fresh `claim_no` (the claim-form input accepts at most 8 characters). The SQL
oracle then requires **exactly one** non-voided `tblClaim` row with that code,
in status 2 ("Entered"), for the demonstrated insuree and health facility — a
duplicate or missing row fails the run loudly.

## Pinning

`environment.lock.json` pins every image by digest (openIMIS 25.10 backend /
frontend / demo-dataset PostgreSQL, plus Redis and RabbitMQ). `compose.yml`
refuses to start without those digests supplied, and the driver script
supplies them from the lock file. The compose topology and the vendored
`conf/nginx/` templates come from openIMIS's own distribution repo
([openimis-dist_dkr](https://github.com/openimis/openimis-dist_dkr) commit
`cd6220d1f0578e56a589c47953250c2ad3d0caa5`), trimmed to the services the
claims workflow needs (no OpenSearch/dashboards/certbot). openIMIS source and
configuration remain attributed to the upstream project under AGPL-3.0.

The 25.10 images publish `linux/amd64` only; on Apple Silicon they run under
Docker Desktop's Rosetta emulation (verified 2026-07-17: first bring-up
including migrations and demo-dataset load completed in a few minutes).

## Mixed-license boundary

OpenAdapt Flow's original code remains MIT-licensed under the repository-root
`LICENSE`. The local `compose.yml` topology and four configuration files under
`conf/nginx/` are adapted from the openIMIS Docker distribution at exact
commit `cd6220d1f0578e56a589c47953250c2ad3d0caa5` and remain licensed under
`AGPL-3.0-only`:

* `compose.yml` (from upstream `compose.base.yml`, `compose.postgresql.yml`,
  and `compose.cache.yml`)
* `conf/nginx/openimis.conf`
* `conf/nginx/locations/backend.loc`
* `conf/nginx/locations/frontend.loc`
* `conf/nginx/variables/var.conf`

Each file carries its exact upstream path and SPDX identifier. The complete
license is included at
[`conf/nginx/LICENSE-AGPL-3.0.md`](conf/nginx/LICENSE-AGPL-3.0.md), and the
aggregate Git-checkout/GitHub-generated-source-archive boundary is recorded in
the root
[`THIRD_PARTY_NOTICES.md`](../../../THIRD_PARTY_NOTICES.md). The MIT license
does not relicense those adapted files.

## Reproduction commands

Requirements: Docker-compatible API with Compose v2, network for the one-time
pinned image pull, and the repo's `.venv`.

```bash
# Start the pinned stack (loopback-only; first run pulls + migrates + loads
# the synthetic demo dataset) and create the synthetic policyholder.
.venv/bin/python scripts/openimis_claims_demo.py up
.venv/bin/python scripts/openimis_claims_demo.py bootstrap

# Record the scripted claims-intake demonstration, compile it, replay it
# with a fresh claim number. Every replay is SQL-oracle-verified.
.venv/bin/python scripts/openimis_claims_demo.py record \
  --out benchmark/openimis_claims/out/recording --headed
.venv/bin/python scripts/openimis_claims_demo.py compile \
  --recording benchmark/openimis_claims/out/recording \
  --bundle benchmark/openimis_claims/out/bundle
.venv/bin/python scripts/openimis_claims_demo.py replay \
  --bundle benchmark/openimis_claims/out/bundle --headed

# Optional website-media capture (opt-in, unchanged recording/replay):
#   ... record --record-video benchmark/openimis_claims/out/video
#   ... replay --record-video benchmark/openimis_claims/out/video

.venv/bin/python scripts/openimis_claims_demo.py down            # stop
.venv/bin/python scripts/openimis_claims_demo.py down --volumes  # full reset
```

## Primary upstream references

- [openIMIS](https://openimis.org/) and the
  [openimis GitHub organization](https://github.com/openimis)
- [openimis-dist_dkr](https://github.com/openimis/openimis-dist_dkr) — the
  Docker distribution this compose file is adapted from
- Image provenance: [ghcr.io/openimis/openimis-be](https://github.com/openimis/openimis-be_py/pkgs/container/openimis-be),
  [ghcr.io/openimis/openimis-fe](https://github.com/openimis/openimis-fe_js/pkgs/container/openimis-fe),
  [ghcr.io/openimis/openimis-pgsql](https://github.com/openimis/database_postgresql/pkgs/container/openimis-pgsql),
  [redis](https://hub.docker.com/_/redis),
  [rabbitmq](https://hub.docker.com/_/rabbitmq)
