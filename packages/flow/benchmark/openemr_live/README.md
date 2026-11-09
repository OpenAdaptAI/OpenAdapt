# Live OpenEMR for the FHIR EffectVerifier end-to-end test

This stands up a **real, local OpenEMR** (OpenEMR + MariaDB, via
`docker-compose`) with the **REST + FHIR R4 APIs and OAuth2 enabled**, so the
FHIR `EffectVerifier` in
[`openadapt_flow/runtime/effects/fhir.py`](../../openadapt_flow/runtime/effects/fhir.py)
can be exercised against an actual system of record: a real write through
OpenEMR's API, then an **independent FHIR read-back**.

It closes the one honest caveat from the EffectVerifier PR (#63): the verifier
was only ever run against a faithful in-repo *fake*
([`tests/_fhir_fake.py`](../../tests/_fhir_fake.py)), because the repo's
OpenEMR harness targets the public demo (`demo.openemr.io`) *vision-only* and
never stands up a FHIR endpoint to write to and read back. Here it runs live.

## One-command bring-up

```bash
# 1. start OpenEMR + MariaDB (first boot auto-installs; several minutes)
docker compose -f benchmark/openemr_live/docker-compose.yml up -d

# 2. wait for the install, enable the APIs + OAuth2, register a client,
#    mint a bearer token, and export the env the test consumes
eval "$(benchmark/openemr_live/setup.sh)"

# 3. run the live end-to-end test (writes a real Patient, reads it back
#    through FHIR, asserts CONFIRMED / REFUTED / INDETERMINATE)
.venv/bin/pytest tests/test_effect_fhir_live_openemr.py -v
```

Tear down (removes the containers and their volumes):

```bash
docker compose -f benchmark/openemr_live/docker-compose.yml down -v
```

## What `setup.sh` does

1. Waits for OpenEMR's unattended install to finish.
2. Enables the REST + FHIR R4 APIs and the OAuth2 **password grant** by setting
   the OpenEMR globals (`rest_api`, `rest_fhir_api`, `rest_system_scopes_api`,
   `oauth_password_grant`).
3. Registers a confidential OAuth2 client via **dynamic registration**
   (`POST /oauth2/default/registration`).
4. **Enables** that client (newly-registered clients start disabled).
5. Obtains a bearer token via the password grant
   (`POST /oauth2/default/token`, `grant_type=password`, `user_role=users`,
   `admin`/`pass`).
6. Prints shell-evalable env exports (all logging goes to stderr):

   ```
   export OPENEMR_FHIR_BASE_URL='https://localhost:9390/apis/default/fhir'
   export OPENEMR_FHIR_TOKEN='<bearer access token>'
   export OPENEMR_FHIR_VERIFY_TLS='0'   # self-signed localhost cert
   ```

The test module (`tests/test_effect_fhir_live_openemr.py`) is **skipped unless
`OPENEMR_FHIR_BASE_URL` is set**, so normal CI never touches it; CI keeps
running the fake-backed contract tests in `tests/test_effect_fhir.py`.

## Ports

Offset to avoid colliding with any other local OpenEMR / MySQL:

| service | container | host |
|---|---|---|
| OpenEMR (HTTP)  | 80  | 8390 |
| OpenEMR (HTTPS) | 443 | 9390 |
| MariaDB         | 3306 | (internal only) |

FHIR base: `https://localhost:9390/apis/default/fhir`. Credentials are the
stock OpenEMR docker defaults (`admin` / `pass`) — **local, throwaway,
fake-data instance only. Never point this at a real OpenEMR install.**

## What the live test proves (and its honest limits)

Against the REAL FHIR server, the verifier under test returns:

- **CONFIRMED** — a Patient POSTed through OpenEMR's FHIR API is independently
  read back and matches (both `record_written` and a `field_equals`
  read-back of the given name).
- **REFUTED** — a deliberately-wrong field expectation against a real record,
  and an expected write that never happened (the real server returns zero
  matches).
- **INDETERMINATE → HALT** — a bad/expired bearer token makes OpenEMR return
  `401`; the verifier reads that as *unreadable* and halts, **never** as
  "record absent". (An unreachable host is covered by the fake-backed suite.)

**The write is an API write, not a GUI-driven one.** It is a **FHIR Patient
POST** — a real clinical resource in OpenEMR's system of record — read back
over FHIR. OpenEMR's FHIR API exposes **Observation as read-only** (there is
no `user/Observation.write` scope), so the note-as-Observation write the fake
models cannot be created over FHIR on a stock OpenEMR. Driving the OpenEMR
*GUI* to make the write and reading it back over FHIR would be the most honest
path of all; it is not done here (reliably browser-driving the dense OpenEMR
UI is a separate effort). The property this test establishes is exactly the
one the fake could not: the verifier's verdicts are correct against a **real
FHIR server**.

Verified end-to-end against `openemr/openemr:7.0.3` on 2026-07-13 (6/6 live
tests: 2 CONFIRMED, 2 REFUTED, 1 INDETERMINATE, 1 reachability).
