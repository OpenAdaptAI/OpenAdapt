# Publish a wrapped task as an API

`openadapt wrap` runs an agent task once, compiles it into a checked program
and reuses that program. `openadapt wrap publish` uploads the program to
OpenAdapt Cloud, which runs it for you behind an HTTPS endpoint. Each call
returns a receipt that says whether the write was read back from the system
of record.

Publishing needs one credential, `OPENADAPT_API_KEY`. The first `wrap` run
also needs a model key (`ANTHROPIC_API_KEY` or `OPENAI_API_KEY`), because an
agent does the task that one time.

## Before you start

- Python 3.12.
- A synthetic web app at a public HTTPS address, with a REST read of the
  records it saves. OpenAdapt Cloud opens the app itself, so `publish`
  refuses `localhost`, private and plain-HTTP addresses. Use made-up data
  only.
- An OpenAdapt Cloud API key, from **Settings > API keys** in the dashboard.

## 1. Install

```bash
pip install 'openadapt[browser]' 'openadapt-flow[privacy]'
playwright install chromium
```

The `privacy` extra scrubs the program before upload.

## 2. Set the keys

```bash
export OPENADAPT_API_KEY=...      # publish and call the endpoint
export ANTHROPIC_API_KEY=...      # first wrap run only
```

`publish` asks the cloud which organization the key belongs to. To name it
yourself, pass `--org <org id>` or set `OPENADAPT_ORG_ID`.

## 3. Describe the task

Save this as `task.json`, with your app's address:

```json
{
  "schema": "openadapt.wrap-task/v1",
  "instruction": "Add a note with the text {note}",
  "parameters": ["note"],
  "url": "https://notes.example.com/",
  "verifier": {
    "kind": "rest",
    "base_url": "https://notes.example.com",
    "records_path": "/api/notes",
    "records_key": "records"
  },
  "expect": {"list": "demo"},
  "on_screen": ["Notes"],
  "canary_params": [{"note": "canary note 1"}]
}
```

`expect` names the fields that every record this task saves shares (here,
the demo list). `verifier` is the read that checks the save independently.

## 4. Wrap the task

```bash
openadapt wrap --task task.json --request-id first-run \
  --param note="hello from the agent" --max-cost-usd 2 --person "Your Name"
```

The agent does the task once. Confirm the proposed read-back checks, review
the steps and attend the canary when the terminal asks. The program is then
admitted, and later calls run it with no model calls.

`--person` needs an interactive terminal: the person's typed confirmations are
the admission evidence, so `wrap` refuses `--person` when input is piped or
the run is a script. To admit without a person at the terminal, pass
`--admit-key` with the encrypted key of a registered admitter instead.

If the model key for `--provider` is not set, `wrap` stops before the agent
starts with `[MISSING_MODEL_KEY]` and the variable to set. Nothing is recorded.

## 5. Publish

```bash
openadapt wrap publish --task task.json
```

`publish` checks the program, sanitizes it, and shows a short summary of what
will be uploaded:

```text
Ready to publish to https://app.openadapt.ai:
  org         <org id>
  workflow    skill-1a2b3c4d5e6f7a8b (new workflow)
  program     1 saving step(s), each read back by the task's verify.yaml; inputs: note
  upload      the sanitized program: 9 files, 41 KB, 0 changed by the scrubber, 0 unresolved findings
  recording   stays on this machine; only the hash of its sanitized copy is sent
  validation  the attended canary (VERIFIED) on https://notes.example.com/
  admission   this machine's admission public key is registered with the organization (once), so the cloud can check the signature
  inspect     ~/.openadapt/skills/publish/skill-1a2b3c4d5e6f7a8b/sanitized-bundle
Upload it? [y/N]
```

Answer `y`. With the same key, `publish` then:

1. registers this machine's admission public key (`keys/ed25519.pub`) with the
   organization, if the cloud does not already list it;
2. gets a validation challenge for the organization and attests the program
   from the wrap run's own recording and canary;
3. uploads the program as a new workflow, or as a new version of the workflow
   named by `--workflow-id`;
4. prints the endpoint and a `curl` example from the path and qualification
   the cloud returned.

Nothing about the program leaves the machine before you answer. Every run
asks again, even after an earlier upload, because the answer covers this
run's host and organization. In a script, pass `--yes`; the summary is still
printed. `--json` prints one object and needs `--yes` to upload; without it
`publish` stops and uploads nothing.

The key goes only to a destination the upload policy trusts:
`https://app.openadapt.ai`, a loopback host for local development, or a
customer-managed HTTPS origin that is classified and in the trusted-host
allowlist. Any other `--host` is refused before the first request.

For local development against a cloud on this machine, classify the
destination as local. Only `localhost`, `127.0.0.1` and `::1` are accepted:

```bash
export OPENADAPT_FLOW_DESTINATION_KIND=local
openadapt wrap publish --task task.json --host http://127.0.0.1:3000
```

The program is attested as `openadapt.runtime-validation/v2` when it has no
governed authorization template. That is allowed only for a Standard canary;
a Regulated canary needs the template. A v2 attestation does not state the
execution profile, so the cloud cannot tell a Standard canary from a demo one
by the attestation alone; the profile is in the canary's run report.

### What authorizes a hosted run

A wrap program has no qualification campaign, so it has no governed
authorization template. Its authority is its signed admission
(`admission.json`), the same one every local run of it already checks. The
upload's `openadapt-wrap-publish.json` carries the program's Standard run
shape (`run_authorization`): the policy and its digest, the profile, the
effect tier and requirements, the identity steps, and the admission's
SHA-256. It carries no values. Cloud builds each call's run authorization from
it and names the admission in `approval_source`. The managed runner refuses the
run before any action unless all of these hold:

- the admission verifies against the admission key registered with the
  organization. The runner stages only that key, at
  `<key_dir>/keys/ed25519.pub`, because it trusts whatever key is there;
- the authorization names that exact admission;
- the bundled `verify.yaml` has the SHA-256 the admission binds;
- the run shape matches what the runner derives itself;
- the profile is Standard. Regulated needs the qualification campaign.

Only then does the run read back through the bundled `verify.yaml`, and it is
VERIFIED only when that read-back confirms the write. A run that rests on a
qualification admission or a production authority file ignores the bundled
file and reads back through its own deployment config.

The run's receipt says what its authority was:
`authority basis: customer-attested-program-admission`. Receipts of runs
under a qualification admission or a production authority leave the field
out, as receipts did before it existed.

### What the cloud learns about people

The uploaded `admission.json` and `verify.yaml` are copies that `publish`
writes. A principal stays in plaintext only when it is a machine handle
(`ci:`, `agent:` or `service:` followed by `[a-z0-9._-]`) or a key
fingerprint. Any other name, such as `jane.doe`, `user:jane.doe` or a record
number, becomes `hmac:<hex>`: an HMAC-SHA256 of the name keyed with a random
per-install key at `<key_dir>/keys/principal-hmac.key`. That key never leaves
the machine. The same name gives the same value on one machine, so the cloud
can still see when two roles are the same principal.

`publish` signs the copy again with the same admission key, after the
original admission verifies, and adds the read-back digest to it. The local
admission keeps the plaintext names. An admission signed by a registered
admitter key cannot be rewritten that way, so `publish` refuses it with
`ADMISSION_INVALID` unless its admitter and recorder are machine handles.
The scrubber replaces any other principal it finds in these files with
`<PRINCIPAL>`, and the upload is then refused.

| Step | Request |
| --- | --- |
| Organization | `GET /api/v1/whoami` (skipped with `--org` or `OPENADAPT_ORG_ID`) |
| Admission key | `GET`, then if missing `POST /api/v1/org/{org}/admission-keys` |
| Challenge | `POST /api/v1/org/{org}/validation-challenges` |
| Upload | `POST /api/v1/org/{org}/workflow`, or `PUT /api/v1/org/{org}/workflow/{id}/definition` |
| Check | `GET /api/v1/org/{org}/workflow/{id}/definition?qualification_id=...` |

## 6. Call it

```bash
curl -sS -X POST "https://app.openadapt.ai/api/v1/org/<org>/workflow/<workflow>" \
  -H "Authorization: Bearer $OPENADAPT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"parameters": {"note": "hello from the API"}, "idempotency_key": "req-1", ...}'
```

Copy the full command that `publish` printed: it carries the exact binding
fields. The response has an execution id. Read the result with:

```bash
curl -sS "https://app.openadapt.ai/api/v1/org/<org>/workflow/<workflow>?execution_id=<id>" \
  -H "Authorization: Bearer $OPENADAPT_API_KEY"
```

Its status says `VERIFIED`, `HALTED` or `UNCERTAIN`.

## What publish refuses

Each refusal is one line that ends with a code in brackets. Nothing is
uploaded.

| Code | Meaning |
| --- | --- |
| `NO_API_KEY` | `OPENADAPT_API_KEY` is not set. |
| `DESTINATION_REFUSED` | `--host` is not a trusted destination. The key was not sent. |
| `API_KEY_REJECTED` | The cloud did not accept the key. |
| `ORG_UNKNOWN` | The cloud did not report the key's organization. Pass `--org`. |
| `ORG_INVALID` | `--org` or `OPENADAPT_ORG_ID` is not an organization id. |
| `WORKFLOW_API_UNAVAILABLE` | The workflow API is not enabled for the organization. |
| `ADMISSION_KEY_UNREGISTERED` | The cloud did not list or register the admission key. |
| `ADMISSION_KEY_REVOKED` | The organization revoked this admission key. Admit the program again with a new `--key-dir`. |
| `NOT_ADMITTED` | The task has no admitted program yet. Run step 4. |
| `TASK_HELD` | The last run failed or was interrupted. Reconcile it first. |
| `ADMISSION_INVALID` | The admission signature does not verify with the local key, `keys/ed25519.pub` or `keys/ed25519.pem` is missing, or the cloud copy of the admission cannot be written (see "What the cloud learns about people"). |
| `NO_READ_BACK` | The program has no save, a save with no effect contract, or no confirmed `verify.yaml` that verify-run accepts. |
| `RUN_SHAPE_UNAVAILABLE` | The task's policy is unknown, or the program has no Standard run shape under it. |
| `NO_WRAP_EVIDENCE` | The signed recording or passing canary from the wrap run is missing. |
| `TARGET_NOT_PUBLIC` | The task's app URL is not public HTTPS. |
| `BUNDLE_UNSAFE` | The program contains a symbolic link. |
| `SANITIZATION_CHANGED_PROGRAM` | The scrubber had to change the program's own files. |
| `VALIDATION_FAILED` | Strict lint, certification or the canary report did not pass. On an RDP or Citrix target, a missing identifier crop also fails strict lint. |
| `VALIDATION_REJECTED` | The cloud refused the runtime validation. |
| `CLOUD_RESPONSE_INVALID` | The upload's acknowledgment did not name this organization's endpoint and qualification. |
| `DELIVERY_UNCERTAIN` | The upload may have arrived. Don't run `publish` again; check the dashboard. |

Exit codes: 0 published, 1 refused, 2 setup missing, 3 waiting (the upload was
not confirmed, or the endpoint is not callable yet).
