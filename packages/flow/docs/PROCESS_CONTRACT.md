# Process contracts

`openadapt.process-contract/v1` runs admitted GUI work, sealed Python, and
signed human tasks in one durable process. The parent keeps each child's own
admission. It passes files by digest and refuses a terminal success until the
declared verifier confirms them.

The earlier `openadapt.process-contract/v0` remains supported. V0 sequences
admitted Flow bundles and scalar effect-bound handoffs. V1 adds mixed child
types and artifact edges. Existing V0 files don't change.

## The child types

| Child | Bound contract | Successful result |
| --- | --- | --- |
| Flow | Exact bundle plus a live qualification admission | The Flow child returns `VERIFIED` |
| Code | Source archive, lockfile, Python version, typed I/O digests, permissions, qualification, and Ed25519 admission | A verifier child confirms every declared output used by the process |
| Human | A signed `HumanDecisionTaskV1`, with an authentication contract when required | Review and decision produce a signed receipt; authentication needs its live verifier; actuation needs a downstream effect verifier |

A code transform first returns `COMPLETED_UNVERIFIED`. A separate admitted code
verifier reads the produced artifact and emits
`openadapt.artifact-verification/v1`. The parent upgrades the transform only
when that receipt names the exact artifact digest and reaches oracle tier 2 or
3. A verifier after a human actuation must also name the exact human receipt
digest. The actuation cannot verify its own effect.

## Author a V1 contract

Write the complete contract as JSON, then ask Flow to validate and store it:

```bash
openadapt-flow process --spec process-v1.json --out tax-process
```

The public JSON Schema is
[`schemas/process-contract-v1.json`](../schemas/process-contract-v1.json).
The contract names paths relative to the parent directory. It doesn't copy the
code archives, admissions, Flow bundles, or authentication templates.

A minimal code and verifier graph looks like this:

```json
{
  "schema_version": "openadapt.process-contract/v1",
  "name": "document-preparation",
  "children": [],
  "code_children": [
    {
      "name": "parse",
      "kind": "code",
      "manifest": "parse/manifest.json",
      "admission": "parse/admission.json",
      "source_archive": "parse/source.zip",
      "role": "transform"
    },
    {
      "name": "check",
      "kind": "code",
      "manifest": "check/manifest.json",
      "admission": "check/admission.json",
      "source_archive": "check/source.zip",
      "role": "verifier"
    }
  ],
  "human_children": [],
  "after": {"check": ["parse"]},
  "artifact_edges": [
    {
      "from_child": "parse",
      "from_output": "table",
      "to_child": "check",
      "to_input": "table",
      "verifier_child": "check"
    }
  ],
  "handoffs": [],
  "allow_halt": {},
  "inputs": []
}
```

## Run and resume

```bash
openadapt-flow run tax-process \
  --run-dir runs/tax-process-001 \
  --code-trust code-signers.json \
  --code-runtime-environment-digest sha256:... \
  --allow-trusted-code \
  --process-receipt-private-key runner-ed25519.key \
  --qualification-trust qualification-trust.json \
  --config deployment.yaml
```

Run the same command with the same `--run-dir` after a human task completes.
The runner verifies `process-events.jsonl` and `process-execution.json` before
it resumes. A completed child doesn't run twice.

The bundled executor supports the `trusted_local` code profile. That profile
requires the explicit `--allow-trusted-code` flag. The runner uses the exact
admitted archive, checks the lockfile and interpreter version, calls Python
directly without a shell, and installs nothing during the run. Container and
VM profiles need an executor that can prove those isolation contracts; this
runner refuses them.

## Artifact edges

The parent copies each produced file into `artifacts/<sha256>`. The portable
artifact reference has no path. It records the content digest, byte size,
media type, logical output name, producer execution, storage boundary, data
class, and verifier receipt digest. The local state keeps a resolver path
inside the run directory. That path never enters the portable reference.

An artifact can enter its consumer only after the named verifier confirms its
exact digest. A file with the same name and different bytes is a different
artifact. A missing verifier, an extra digest, or a partial verifier result
stops the process.

## Human tasks and authentication

Human work uses the existing portable attended-task contract. V1 names four
presentation kinds: `authenticate`, `actuate`, `decide`, and `review`. They use
one signed task and receipt path. The kind doesn't create another approval
engine.

An authentication child also issues `AuthenticationTaskContractV1`. Its
receipt contains keyed principal and session bindings, the exact process and
step, the application version, the surface, a verifier result, freshness, and
a capture-exclusion receipt digest. It contains no credential, account label,
cookie, token, or provider item name. Clicking Done doesn't prove a login. The
trusted local verifier must confirm the authenticated session.

The receipt format alone doesn't prove that Capture withheld the protected
interval. Capture, Desktop, and the attended runtime still need a qualified
source-time integration before an authentication child can be used in a
recorded login path. For current tax work, authenticate before recording. Do
not record password or MFA entry.

The CLI writes a task request and exits with code 3 when no trusted human-task
adapter is attached. The adapter writes
`human/<child>/human-completion.json`, with schema
`openadapt.process-human-completion/v1`. That local envelope contains the
signed `HumanDecisionReceiptV1` and, for authentication, the exact run binding
and authentication receipt. Run the same CLI command to resume. The runtime
checks the signatures, task revision, pause, request digest, action, validity
window, process, and surface. It has no flag that turns an unauthenticated Done
action into success.

## Ledger and terminal receipt

The process package digest binds the contract and each referenced admission,
code manifest, source archive, and authentication template. A referenced file
change refuses resume, even when `process-contract.json` did not change.

`process-events.jsonl` is an append-only hash chain. Every record binds the
previous record. `process-execution.json` stores the current projection and
the expected journal head. A changed contract, deleted tail, reordered event,
or altered digest refuses resume.

Terminal runs emit a signed `ProcessEvidenceReceiptV1`. It binds the process
digest, child receipts, human receipts, artifact graph, environment, runner,
model use, network use, oracle tier, and the exact Execute outcome.

The parent derives the oracle tier from its children. It does not assign a
fixed tier. A terminal `VERIFIED` result requires tier 2 or 3 on every completed
child. A human actuation without its verifier ends without a success result.

The parent preserves these terminal outcomes:

- `VERIFIED`
- `HALTED_BEFORE_EFFECT`
- `RECONCILIATION_REQUIRED`
- `REJECTED_POLICY`
- `FAILED_PLATFORM`
- `ROLLED_BACK_VERIFIED`

`RECONCILIATION_REQUIRED` is sticky. The parent doesn't absorb it as a halt,
and it doesn't dispatch the child again. An operator must reconcile the live
effect.

## Model portability

The package has no chat ID, assistant ID, prompt, tool-call trace, or vendor
storage handle. Codex, Claude, ChatGPT, a local model, or a person can author
the JSON and sealed source archive. The runner evaluates the same digests and
receipts in every case.

A healthy run makes zero model calls. A repair creates a new archive, admission,
and process digest. It doesn't edit an active run.

## V0 commands

The V0 authoring command still accepts two or more `--child` and `--admission`
pairs:

```bash
openadapt-flow process \
  --child intake=./intake-bundle \
  --admission intake=./intake-admission.json \
  --child posting=./posting-bundle \
  --admission posting=./posting-admission.json \
  --handoff intake.record_id=posting.record_id \
  --out process
```

`openadapt-flow replay process` refuses both versions. Use the governed `run`
path.

V0 supports `--after`, scalar `--handoff`, and `--allow-halt`. Its `certify`
command evaluates every admitted Flow child against one policy. Its `run`
command verifies each live qualification envelope and then uses governed local
run or Cloud Execute. V1 has no parent certification shortcut. Each Flow child
must be certified, and each code manifest must have its own live admission.

Signer trust comes from `qualification-trust.json` beside the parent or from
`OPENADAPT_QUALIFICATION_TRUST`. A test key must not enter a production trust
map.

Both versions use the parent visualizer:

```bash
openadapt-flow visualize process --out process.html
openadapt-flow visualize process --format mermaid
openadapt-flow visualize process --format json
```

V1 nodes state `flow`, `code`, or `human`. Artifact edges name the output,
consumer input, and verifier. The terminal remains **End of declared steps**.
The visualizer never labels the declared end as success.
