# Local-first, with an optional hosted path

openadapt-flow runs entirely on your machine. Record, compile, lint, certify,
replay, and run are all local, and the healthy replay path makes zero outbound
calls. Model grounding is off by default and only wired in behind an explicit
`--allow-model-grounding` opt-in.

OpenAdapt Cloud is the optional managed control plane at `app.openadapt.ai`.
The public managed subscription covers browser workflows today; desktop and
Citrix / VDI runs are self-hosted or on-prem. The hosted commands below connect
the locally executed compiler and runtime to that control plane for
authentication, governed artifact ingest, and PHI-minimal break reporting.

## Seal a production candidate

Seal a production candidate without modifying the compiled source bundle:

```bash
export OPENADAPT_BUNDLE_KEY='<inject from your secret manager>'
openadapt-flow seal ./bundle-v2 --out ./bundle-prod
openadapt-flow certify ./bundle-prod --policy clinical-write
```

The command copies the complete bundle, encrypts the workflow and retained
template evidence, verifies the sealed digest, and atomically publishes the new
directory. It refuses symlinks and existing destinations. Other bundle files
are copied unchanged; keep sensitive artifacts inside the workflow/templates
boundary or protect them with the deployment's encrypted volume. Sealing
changes the artifact contract, so it expires any prior certification instead
of carrying a stale decision forward. Always certify the sealed destination.
For a bundle with a qualification project, run its representative and fault
cases against the sealed destination before running `qualify certify`. Details:
[`phi_at_rest.md`](phi_at_rest.md).

## Hosted (cloud connectivity)

Hosted commands connect the locally executed compiler/runtime to the launched
control plane at `app.openadapt.ai`: authentication, governed artifact ingest,
and PHI-minimal break reporting. Mint an ingest token in the dashboard
(`<host>/dashboard/settings/ingest`), then:

```bash
pip install 'openadapt-flow[privacy,hosted]'
openadapt-flow login --token oai_ingest_…
openadapt-flow sanitize ./my-recording --kind recording --out ./triage.sanitized
openadapt-flow review-sanitized ./triage.sanitized --original ./my-recording
# add missed redactions locally, then approve in the viewer or CLI:
openadapt-flow approve-sanitized ./triage.sanitized --original ./my-recording \
  --reviewer operator@example.com
openadapt-flow push ./triage.sanitized --kind recording

# Compile only from the approved sanitized recording, then validate locally.
openadapt-flow compile ./triage.sanitized --out ./triage.bundle --name triage
openadapt-flow lint ./triage.bundle --strict
openadapt-flow certify ./triage.bundle --policy permissive
openadapt-flow replay ./triage.bundle --url https://app.example.com/login \
  --run-dir ./triage.run --param patient_id=example

# Privacy-review the executable bytes. A changed executable is refused.
openadapt-flow sanitize ./triage.bundle --kind bundle --out ./triage.bundle.sanitized
openadapt-flow review-sanitized ./triage.bundle.sanitized --original ./triage.bundle
openadapt-flow approve-sanitized ./triage.bundle.sanitized \
  --original ./triage.bundle --reviewer operator@example.com

# Bind exact artifacts and local evidence to a one-time Cloud challenge.
openadapt-flow validate-hosted \
  --recording ./triage.sanitized --bundle ./triage.bundle.sanitized \
  --run-dir ./triage.run --policy permissive --risk-class low \
  --environment staging-v1 --target-kind web \
  --target-url https://app.example.com/login \
  --out triage.validation.json
openadapt-flow push ./triage.bundle.sanitized --kind bundle \
  --validation-attestation triage.validation.json

# Desktop/RDP/Citrix use the same validation command, deriving the signed
# target kind from report.json. Their app/window/host details stay local:
#   --target-kind citrix --environment clinic-citrix-qualified-v1
# (omit --target-url and --allowed-host outside the web substrate).

# To activate this as a new version of an existing workflow, add:
#   --workflow-id 00000000-0000-0000-0000-000000000000
# To bind that replacement to the exact halted run it repairs, also add:
#   --resolves-run-id 00000000-0000-0000-0000-000000000000

openadapt-flow report-break runs/replay-… \          # PHI-free break diagnostic
    --workflow-id <id> --deployment-kind byoc         #   -> POST /api/runs/ingest-report
```

- **Token resolution** (all outbound calls): `--token`, then
  `OPENADAPT_INGEST_TOKEN` env, then OS keychain, then an existing
  `~/.openadapt/config.toml` token (migration read). Install the `hosted` extra
  for keychain storage. New plaintext mode-`0600` storage is refused unless
  `login --allow-plaintext-token` makes the insecure fallback explicit.
- **Sanitization never mutates the original.** It inventories every file,
  applies type-specific text/image handlers to a copy, requires a stable second
  scrub pass, and writes per-file source/derivative hashes and coverage to
  `.openadapt-sanitization.json`.
- **Review is local-only by default.** `review-sanitized` binds to `127.0.0.1`,
  loads no remote assets, presents original and derivative side by side, accepts
  additional literal/rectangle redactions, and invalidates prior approval after
  every change. Administrators may opt into policy approval only for fully
  covered, stable derivatives. Automatic hosted approval additionally requires
  a deployment-allowlisted HMAC signing key; an ingest token cannot self-assert
  that policy.
- **Approval freezes exact bytes.** It creates a deterministic immutable archive
  and binds reviewer, policy, timestamp, SHA-256, and byte size. `push` sends
  those exact bytes plus the `openadapt.sanitization/v1` manifest; it never
  re-zips after approval.
- **Destination trust is independent of deployment lane.** OpenAdapt's managed
  origin is recognized explicitly. A customer-managed/BYOC endpoint requires
  HTTPS plus an exact-origin allowlist. Sanitized artifacts may upload from
  cloud, BYOC, regulated, or PHI-mode lanes; unknown destinations are refused.
- **Current coverage is text and still images.** Symlinks and database, video,
  audio, nested archive, encrypted, executable, or unknown files are refused,
  never copied through or reported as covered. See
  [`SANITIZED_ARTIFACTS.md`](SANITIZED_ARTIFACTS.md).
- **Sanitizing a bundle can break execution.** If a load-bearing target, typed
  value, identity crop, or postcondition changes, the manifest marks runtime
  semantics unvalidated and `push --kind bundle` refuses it. Parameterize PHI
  before compilation or execute the original inside its trusted boundary.
- **Runtime validation is separate from privacy approval.** It binds the exact
  approved recording and bundle, compiler configuration, parameter schema,
  strict lint, named certification, derived `low`/`consequential` risk class,
  successful report, and exact HTTPS target/host boundary to a short-lived,
  one-time tenant/token challenge. Cloud also requires exact deployment
  allowlist membership for certification policy, derived risk class, and a
  compiler version actually deployed by the runner. The HMAC proves token
  possession and envelope integrity; it is not independent observation, a
  compliance certification, or a safety SLA.
- **Halt signaling** is read from **`report.json` (`RunReport.halt` /
  `HaltObservation`)**, never from a process exit code (`replay`/`run` return
  `0`/`1` only). `report-break` posts only a schema-minimal descriptor: hashes,
  status, resolver rung, and numeric metrics. Free text, screenshots, DOM, and
  field values never enter the automatic payload. A `422` boundary rejection
  retries the same minimal shape, then falls back to local-only.
- **Opt-in post-run hook:** set `OPENADAPT_FLOW_HOSTED_WORKFLOW_ID` (and
  optionally `OPENADAPT_FLOW_DEPLOYMENT_KIND` / `OPENADAPT_FLOW_ORG_ID`) and a
  halting `replay`/`run` emits the break automatically (best-effort; never
  changes the run's exit code). Off by default.

## Pairing, console, and the operator UI

To pair this machine with a launched Cloud tenant from a desktop deep link, use
`openadapt-flow connect`. The operator console (`openadapt-flow console`, needs
the `console` extra) serves a localhost operator UI over compiled bundles, run
reports, halt evidence, and skill-library lineage. It is read-only by default.
An explicit `--attend --allow-actions --config deployment.yaml` starts the
deployment-bound action service. Add `--remote-decisions` for the outbound
phone lane when `human_decisions.remote.enabled` names the exact tenant and
runner and the authenticated runner token is present. The sanitizer uses the
optional `privacy` extra; hosted transport uses `httpx`.
