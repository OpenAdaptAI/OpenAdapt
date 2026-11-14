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

The program is attested as `openadapt.runtime-validation/v2` when it has no
governed authorization template. That is allowed only for a Standard canary;
a Regulated canary needs the template. A v2 attestation does not state the
execution profile, so the cloud cannot tell a Standard canary from a demo one
by the attestation alone; the profile is in the canary's run report.

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
| `ADMISSION_INVALID` | The admission signature does not verify with the local key, or `keys/ed25519.pub` is missing. |
| `NO_READ_BACK` | The program has no save, a save with no effect contract, or no confirmed `verify.yaml` that verify-run accepts. |
| `NO_WRAP_EVIDENCE` | The signed recording or passing canary from the wrap run is missing. |
| `TARGET_NOT_PUBLIC` | The task's app URL is not public HTTPS. |
| `BUNDLE_UNSAFE` | The program contains a symbolic link. |
| `SANITIZATION_CHANGED_PROGRAM` | The scrubber had to change the program's own files. |
| `VALIDATION_FAILED` | Strict lint, certification or the canary report did not pass. |
| `VALIDATION_REJECTED` | The cloud refused the runtime validation. |
| `CLOUD_RESPONSE_INVALID` | The upload's acknowledgment did not name this organization's endpoint and qualification. |
| `DELIVERY_UNCERTAIN` | The upload may have arrived. Don't run `publish` again; check the dashboard. |

Exit codes: 0 published, 1 refused, 2 setup missing, 3 waiting (the upload was
not confirmed, or the endpoint is not callable yet).
