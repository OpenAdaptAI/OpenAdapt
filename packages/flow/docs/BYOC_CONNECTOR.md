# BYOC Connector (bring-your-own-cloud)

The BYOC Connector lets **OpenAdapt Cloud manage governed runs that execute
inside the customer's own environment** — their VM, VPC, or on-prem host. The
customer owns the Windows licensing and the data boundary: the PHI-bearing
compiled bundle and run report stay in *their* storage, and only PHI-free
status/halt metadata ever flows back to the control plane.

It is the tractable path to "hosted across every substrate": desktop / RDP /
Citrix-over-RDP runs the managed browser runner cannot host still get a managed
control plane, a governed engine, and an audit trail — without a single PHI byte
or pixel leaving the customer perimeter.

## Why pull, not push

The managed lane PUSHES work (`enqueueRun -> fetch(runner)`). A BYOC customer
runs the data plane behind their own firewall with **no inbound hole**, so we
cannot push. The Connector inverts this: it makes only **outbound HTTPS**
connections to the control plane and PULLS jobs down them (the shape of a GitHub
self-hosted runner or a Citrix Cloud Connector). **The customer opens zero
inbound ports.**

## The loop (all outbound)

```
create   -> authenticated Cloud settings  mint a revocable org-scoped token once
poll     -> POST /api/connector/poll        long-poll; lease the next queued job
execute  -> openadapt-flow run ...          the governed admission gate + Replayer,
                                             against the CUSTOMER'S own storage
callback -> POST /api/internal/run-callback  PHI-free status/metrics
ack      -> POST /api/connector/ack          release only after callback acceptance
```

## Install and run

The Connector ships with the engine (`openadapt-flow`), so a dispatched run is
the *same* fail-closed `openadapt-flow run` you would run locally.

```bash
pip install 'openadapt-flow[vision]'

# 1. In app.openadapt.ai, open Dashboard → Settings →
#    Customer-controlled connector, create a connector, and copy its token once
#    into this machine's service manager or secret store.
export BYOC_CONNECTOR_TOKEN='<shown-once token>'

# 2. Run the daemon (poll -> execute -> report -> ack, until interrupted).
openadapt-flow connector run \
  --control-plane-url https://app.openadapt.ai \
  --org-id org_your_clinic \
  --profile /opt/openadapt/deployment.yaml \
  --storage-backend local \
  --storage-root /srv/openadapt        # a full-disk-encrypted customer volume
```

Every flag also resolves from an environment variable
(`CONTROL_PLANE_URL`, `BYOC_CONNECTOR_TOKEN`, `BYOC_ORG_ID`, ...).
`connector enroll` remains available for mock/development control planes; live
Cloud token creation is authenticated and organization-scoped in the dashboard.

Set `runtime.profile` explicitly in the local deployment file. Use `standard`
for admitted programs. Qualified jobs can use `standard` or `regulated`,
matching their authorization. Startup checks the local profile before polling.
The child checks the exact authorization again for each run.

```yaml
runtime:
  profile: standard
```

### Container storage and logs

Mount a private directory from the customer's encrypted data volume at
`/var/lib/openadapt/ledger`. The connector user (UID 10001) needs write access.
Keep this directory outside the storage root used for job bundles and reports.
For the synthetic container harness, prepare `$STATE/runner-ledger` with mode
0700 for that user. The image sets `OPENADAPT_FLOW_PLAINTEXT_LEDGER_DIR` to
the mount and flushes Python output to `docker logs` without a TTY.

The plaintext ledger records writes already permitted by the local at-rest
policy. The encryption key keeps its configured location. With read-only
configuration at `/etc/openadapt`, provision any required local key beforehand
or use the operator's existing key configuration. If the service manages token
rotation, its enrollment state also needs its own writable location.

For a refused run, look beside the expected report in the customer store for
`<report-filename>.connector-diagnostics`, even if the child produced no report.
Read it in the connector's environment with
`python -m openadapt_flow.connector.diagnostics /path/to/report.json.connector-diagnostics`.
It contains the local error and up to the final 32 KiB of combined child output.
That output stays local. If the connector reports `local_diagnostics_unavailable`,
check the customer storage, its access permissions and the configured key.

## Windows service

The `OpenAdaptConnector` wrapper runs the connector loop under the Windows
Service Control Manager. The Windows CI proof covers service lifecycle and
refusal of an ungoverned job, rather than a successful desktop write.

A service runs in session 0 and cannot directly interact with a signed-in
user's desktop. A desktop job needs an execution agent in that user's
interactive session. `WindowsBackend` can call a separate HTTP agent, but the
service-to-agent governed write and read-back path has not been qualified by
this workflow. See [Microsoft's service guidance](https://learn.microsoft.com/en-us/windows/win32/services/interactive-services).

Install, from an elevated (Administrator) prompt:

```powershell
pip install "openadapt-flow[vision,connector-windows]==<version>"
# Put the token and settings in the machine-wide config file:
#   C:\ProgramData\OpenAdapt\connector\connector.toml
openadapt-flow connector service install
openadapt-flow connector service start
openadapt-flow connector service status
```

Install into a regular Python installation, not a virtual environment: the
service runs that installation's `python.exe`, and `install` refuses a venv.

`connector.toml` takes the same keys `connector enroll` writes
(`control_plane_url`, `token`, `org_id`, `profile`, `storage_backend`,
`storage_root`, ...) under a `[connector]` table. Pass `--config-dir` to
`install` to use a different directory.

- **Config location.** `%ProgramData%\OpenAdapt\connector`. The service sets
  `OPENADAPT_HOME` to it.
- **Account.** The service runs as the virtual account
  `NT SERVICE\OpenAdaptConnector`, not as LocalSystem. Give that account
  access to the storage root and anything the deployment profile names.
- **Permissions.** `install` removes inherited permissions from the config
  directory. Only SYSTEM, Administrators and the service account can read
  `connector.toml`, which holds the token. The service account can write only
  to `logs`.
- **Logs.** `%ProgramData%\OpenAdapt\connector\logs\connector.log`, rotated at
  5 MB with five old files kept. The token is never written to the log.
- **Start and recovery.** Delayed automatic start. The service restarts after
  a failure (5 s, 5 s, then 60 s). A lost connection to the control plane is
  retried inside the service with backoff.
- **Network.** Outbound only. `install` adds no firewall rule.
- **Desktop.** The wrapper does not create an interactive user session or
  start an execution agent in one. Its service lifecycle proof does not
  qualify desktop execution.
- **Stop.** A job already running finishes and reports. A lease returned while
  the service is stopping never starts; it is left unacknowledged for the
  control plane to reconcile, rather than reported as completed.
- **Token rotation.** For a file-enrolled connector, the service stores the
  active and pending rotation in its virtual account's Windows Credential
  Manager. The entry is local to the machine. The service retries the pending
  candidate after restart before polling. Clearing an old pending field in
  the enrollment file preserves the committed rotation. The service waits
  between failed confirmations. If Cloud can no longer confirm the candidate,
  replace the enrollment token as an administrator and restart the service.
  An environment override for the token, control-plane URL or organization
  leaves rotation with the operator.

Update by stopping the service, installing the pinned version, and starting
it again:

```powershell
openadapt-flow connector service stop
pip install "openadapt-flow[vision,connector-windows]==<new version>"
openadapt-flow connector service start
```

Uninstall with `openadapt-flow connector service remove`. Removal preserves
the enrollment and its local rotation state for reinstallation. To
decommission the connector, revoke it in Cloud and remove its config directory.
The service requires that enrollment before it can use the stored rotation
state. Starting with a replacement enrollment discards the old credential.

On other operating systems, `connector service` prints a message and exits
with code 2; run `connector run` under systemd or launchd instead.

## Data boundary and safety

* **Down (to the Connector):** a PHI-free job *descriptor* only — run/workflow
  ids, an opaque customer-storage *reference* (never our signed URL), PHI-free
  params, a secret *reference* (never a value), the org's resolved governed
  policy (safety + grounding), a run-scoped callback token, and the immutable
  bundle binding.
* **Up (to the control plane):** PHI-free status/metrics + a storage *path* into
  the customer's own store. **Never** the report body, screenshots, OCR text, or
  a record identifier.
* **The bundle and report bytes** are read from / written to the **customer's own
  storage** (`--storage-root`, a local encrypted volume). Our control plane holds
  no URL to them and signs no access.
* **The staged bundle is the exact approved ZIP archive.** `bundle_sha256` binds
  the dispatch to those archive bytes (separately from the bundle's semantic
  content digest). The Connector copies and hashes that file before extraction,
  refuses a missing/different digest or an unpacked directory, then safe-extracts
  only the verified copy. A pre-verification failure never echoes the unverified
  digest claim in its callback.

A live job requires the exact retained `managed_dispatch_session_id`, a
run-scoped callback capability, a closed managed authority and its bundle
binding. The connector advertises `managed_delivery_authority_v3`; older
qualification-v2 capability alone cannot lease these jobs. Parsing an older
payload does not authorize execution or let it inherit a daemon's session.
The per-run session enters the child environment and must match the signed
v3 delivery response. Each live input needs a fresh signed input-start deadline.

Authority has two declared forms. A qualified workflow carries the signed
admission v2 artifact and its digest. A program carries either the closed
person reference v1 or registered-admitter reference v2. Unknown or mixed
fields, a different run or organization, conflicting digests, an unpinned key,
and an admitter-to-person downgrade are refused before input. Only after these
extensions are consumed does the connector write the exact six-field Flow
envelope to an owner-only local file.

Programs use operator-pinned public admission keys. An admitter reference also
requires the current local principal registry, matching principal/key fields
and the exact signed admission. Configure `admission_key_dir` as a public-only,
read-only directory; keep signing material elsewhere. The shared runtime reads
that live registry again at input edges. A person key staged without that
directory is a snapshot and does not provide live local principal revocation.
Remote v3 permit checks remain mandatory for both forms.

Qualified jobs additionally require an independent operator-owned
`ProductionQualificationAuthority` file. Its wrapper v1 contains the actual
signed admission v2, known expected runtime/deployment contracts and trusted
signer registry. The connector matches its proof and tenant, workflow, version,
validation, archive, content and substrate bindings, then the existing Flow
qualification guard verifies it against the sealed bundle. It never constructs
expected contracts from the remote proof itself. Set the path in
`connector.toml` or `BYOC_QUALIFICATION_AUTHORITY_FILE`:

```toml
[connector]
qualification_authority_file = "/etc/openadapt/qualification-authority.json"
```

That file is owner-only and must match each qualified job exactly. Absent,
changed or unrelated local context refuses the job. Its verified canonical
wrapper is handed to the child through the existing
`--qualification-authority-file` channel; the runtime-local qualification field
stays null on the Cloud wire. The operator's deployment profile and default
per-save approval gate remain authoritative.

A dispatch also refuses a missing safety policy, archive digest or substrate,
an our-owned bundle URL, conflicting substrate hints and a required grounding
key absent from this machine. The governed child rechecks its normal admission,
identity, effect and storage requirements before execution.

The callback is retried a bounded three times. The Connector ACKs a job only
after the control plane accepts its outcome. If delivery still fails, the lease
is left unacknowledged; Cloud marks expiry as `lease_expired_uncertain` and
never blindly re-offers it, so an operator checks the real effect before
authorizing a fresh run.

## Enabling the lane (control plane)

The lane is off by default. An operator enables it with
`BYOC_ENABLED=true` and authenticated run callbacks
(`RUNNER_SHARED_SECRET`), then sets the organization's `deployment_kind` to
`byoc`. See openadapt-cloud `src/lib/byocLane.ts`.

## Qualification and operating scope

The source tests exercise an in-process control plane, actual program and
qualification signatures, exact file parsing, session binding, egress refusal,
rotation, cancellation and uncertain lease handling. These are synthetic source
controls. A complete managed BYOC run on a real system of record has not yet
been established by this change.

The engine connector currently reads exact archives and writes reports through
its `local` customer-storage backend. The local deployment profile governs the
engine posture: the delivered safety block does not materialize every runtime
setting, including grounding endpoints. Operators must qualify that profile and
the installed runtime together. Container/service supervision and separate
customer network controls remain part of the deployment.
