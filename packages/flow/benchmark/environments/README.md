# Reproducible, non-gameable system-of-record environments

This directory is the **index** over the pinned, containerized applications the
Silent Wrong-Effect benchmark drives. Each environment exposes an **independent
system-of-record (SoR)** — a SQL database, a REST readback, or an HTTP-JSON
store — that an effect oracle reads through a channel the agent never touches.
Success is judged by the *persisted record*, never by the rendered screen or the
agent's self-report. Reproducibility (exact image digests, deterministic
bring-up, a hashed seed baseline restored before every trial) is what makes the
measured rate trustworthy; a mutable public target would make it meaningless.

The design doc (`.private/benchmark_design_2026_07_20.md`) names *reproducible,
non-gameable environments + fair oracles* as the single biggest risk. This is the
groundwork that retires the reproducibility half of that risk for the first
2–3 apps.

> This index does **not** re-implement the fixtures. Bring-up, snapshot/reset,
> and seed state live in each environment's own directory (and in
> `openadapt_flow.mockmed`). This directory adds the machine-readable registry,
> a verification harness, and this runbook.

## The environments

| Env | App (vertical) | Substrate | Docker | CI-fast | System-of-record (how the oracle reads the true effect) |
|---|---|---|---|---|---|
| `mockmed` | MockMed fault-injection SPA (fixture) | web | no | **yes** | `GET /api/db` on an in-process store behind a real HTTP persistence boundary |
| `openemr_local` | OpenEMR 8.0.0.3 (healthcare EMR) | web | yes | no | MariaDB `patient_data` row-state **+** OpenEMR Standard REST readback via a least-privilege OAuth oracle client (`user/patient.rs`, read/search only) |
| `frappe_lending` | Frappe/ERPNext + Lending v16 (lending ERP) | web | yes | no | MariaDB `` `tabLoan Application` `` row-state **+** Frappe REST readback as a read-only oracle user |
| `openimis_claims` | openIMIS 25.10 (insurance claims) | web | yes | no | PostgreSQL `tblClaim` row-state (status `2` = "Entered") — **AGPL, opt-in / repo-only** |

Three named substrates (EMR / ERP / CI-fast anchor) plus the AGPL insurance
mirror. All data is synthetic; all published ports bind to `127.0.0.1` only.

## Ports and credentials (for the oracle harness)

| Env | UI | SoR endpoint(s) | Credentials |
|---|---|---|---|
| `mockmed` | ephemeral `http://127.0.0.1:<port>/` (returned by `serve()`) | `GET /api/db`, `POST /api/reset`, `POST /api/encounter?fault=<mode>` | none (loopback, no auth) |
| `openemr_local` | `http://127.0.0.1:9301` | REST/OAuth `https://127.0.0.1:9300`; SQL via compose service `db` (MariaDB) | `benchmark/openemr_local/state/runtime.env` (generated, `0600`, gitignored) + `state/oauth-clients.json`; actor user `openadapt_actor` |
| `frappe_lending` | `http://127.0.0.1:8080` | Frappe REST at the UI origin; SQL via compose service `db` (MariaDB) | `benchmark/frappe_lending/state/runtime.env` (generated, `0600`, gitignored); site `frontend` |
| `openimis_claims` | `http://127.0.0.1:9401` | PostgreSQL database `IMIS` (compose service) | `benchmark/openimis_claims/out/state/` (generated, gitignored); demo actor `Admin` |

The oracle **must** use the read-only path (the OpenEMR `user/patient.rs` OAuth
client, the Frappe read-only oracle user, a direct read-only SQL connection) so
its read channel is provably isolated from the write channel the agent drives.
This isolation is asserted in CI
(`test_docker_environments_declare_isolated_record`).

## Verify (deterministic bring-up + SoR queryability)

The verification harness has two fail-closed checks, both CI-fast and needing no
Docker:

```bash
# Both checks (default). Exits non-zero on any failure.
python -m benchmark.environments.verify all --json benchmark/environments/verification.json

python -m benchmark.environments.verify locks    # digest/commit pinning, offline
python -m benchmark.environments.verify mockmed   # live SoR queryable + non-gameable
```

- **`locks`** asserts every environment's `environment.lock.json` pins each
  service image to an exact `@sha256:` digest (never a floating tag), pins
  upstreams to full 40-hex commits, and has a `compose.yml` that refuses to
  start without those pinned inputs (`${VAR:?...}` guards).
- **`mockmed`** stands up the fault-injection server and proves the record is
  (a) *queryable* — a clean write reads back verbatim through `GET /api/db` —
  and (b) *non-gameable* — a `partial` fault the SPA still paints as "saved"
  shows up in the record as a dropped note, so a screen-only check would score
  it success while the oracle catches it.

The committed `verification.json` is a captured run of `verify all`;
`environments.json` is the registry snapshot with digests resolved from the live
locks. CI regenerates and diffs both (`tests/test_benchmark_environments.py`).

## Bring up a containerized environment

Each Docker environment is driven by its existing fixture script. Requirements:
a responsive Docker-compatible API with Compose v2, network for the one-time
digest-pinned pull (Frappe also builds a custom image from pinned upstreams),
and free space on the state filesystem (OpenEMR ≥15 GiB, Frappe ≥40 GiB).

```bash
# OpenEMR (EMR)
python scripts/openemr_local_demo.py preflight
python scripts/openemr_local_demo.py prepare      # verify remote source + image pins
python scripts/openemr_local_demo.py up           # pull by digest, start, health-check
python scripts/openemr_local_demo.py bootstrap    # enable APIs, create oracle client
python scripts/openemr_local_demo.py snapshot     # hashed baseline for per-trial reset
# teardown: docker compose -p openadapt-openemr-benchmark down -v && rm -rf benchmark/openemr_local/state

# Frappe Lending (ERP)
python scripts/frappe_lending_demo.py preflight
python scripts/frappe_lending_demo.py prepare
python scripts/frappe_lending_demo.py build       # build image from pinned upstreams
python scripts/frappe_lending_demo.py up
python scripts/frappe_lending_demo.py bootstrap
python scripts/frappe_lending_demo.py snapshot
# teardown: docker compose -p openadapt-frappe-lending-benchmark down -v && rm -rf benchmark/frappe_lending/state

# openIMIS (insurance) — AGPL, opt-in
python scripts/openimis_claims_demo.py up
python scripts/openimis_claims_demo.py bootstrap
# teardown: python scripts/openimis_claims_demo.py down --volumes
```

Each fixture restores a **SHA-256-verified baseline** (SQL dump / volumes)
before every trial, so an oracle's pre-state is deterministic and cross-trial
contamination is detectable. See the per-environment `README.md` for the exact
task, oracle contract, and per-table collateral-write audit.

## Programmatic access

```python
from benchmark.environments import all_environments, get

emr = get("openemr_local")
emr.service_digests()      # {"openemr": "openemr/openemr:8.0.0.3@sha256:...", ...}
emr.sor.channels           # ("sql", "rest_api")
emr.sor.read_recipe        # how to read the true effect off-screen
emr.bringup                # deterministic bring-up commands
```

`registry_snapshot()` returns the JSON the task pack and oracle harness consume
(also committed as `environments.json`).

## Licensing (the hard rule)

**Running a pinned upstream container is not redistribution. Vendoring copyleft
source into an MIT wheel/sdist is.** This groundwork stays on the safe side:

- **OpenEMR (GPL-3.0)** and **Frappe/ERPNext/Lending (GPL-3.0)** — the official
  images are **pulled (Frappe: built from pinned bases) at runtime by digest**.
  No OpenEMR/Frappe source is vendored into this repo. The `compose.yml` files
  are OpenAdapt's own (adapted, attributed).
- **openIMIS (AGPL-3.0)** — images pulled at runtime by digest; the adapted
  `compose.yml` + `conf/nginx/` templates are **AGPL-3.0-only, repo-only**, carry
  SPDX headers, and are recorded in the root `THIRD_PARTY_NOTICES.md`.
- **Package boundary — enforced, not just asserted.** The wheel packages only
  `openadapt_flow/`, so top-level `benchmark/` (every containerized environment)
  is excluded by construction. The sdist additionally `exclude`s
  `/benchmark/openimis_claims`, `/scripts/openimis_claims_demo.py`,
  `/tests/test_openimis_claims_fixture.py`, and `/THIRD_PARTY_NOTICES.md`. Both
  boundaries are checked in CI (`test_agpl_openimis_is_excluded_from_sdist`,
  `test_copyleft_environments_never_ship_in_artifacts`) and should be re-verified
  against the *built archives* at release time.

**Do not** relocate any environment under `openadapt_flow/`, and do not add a new
copyleft environment without updating the registry's `LicenseStatus`, the sdist
`exclude` list, `THIRD_PARTY_NOTICES.md`, and these tests.
