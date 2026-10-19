# Production setup

This page collects the production-control detail that the
[README](../README.md) leaves out: how a bundle is qualified from its
recording, how policies and execution profiles gate a run, how separately
recorded bundles and processes are sequenced, what the local Execute server
signs, and how a release is admitted for production use. The README covers
recording, rehearsal, the record check, and the limits in plain terms; this
page uses the engine's own vocabulary.

## Rehearsal and governed runs

`replay` is the permissive rehearsal path and runs the Demo profile. It stays
available while a bundle has certification gaps. A rehearsal never checks the
record, so it ends `COMPLETED_UNVERIFIED` (finished, not checked) and still
exits 0. Exit 0 from `replay` means the steps finished, not that the save was
confirmed; the output says so, and `transaction_outcome` in `report.json`
records it.

For governed execution, complete the remaining idempotency and postcondition
contracts, then use the gated path:

```bash
openadapt-flow certify bundle --config deploy.yaml
openadapt-flow run bundle --profile standard --config deploy.yaml
```

Before a governed run reports `VERIFIED`, Flow confirms every declared effect
through an independent system-of-record read. A pixel-only Citrix path with no
such read cannot be `VERIFIED`: the run halts, or it ends
`RECONCILIATION_REQUIRED`. When the configured verifier finds no declared
effect after a save, the run stops, whatever the screen shows.

`run --profile standard` enforces the policy again. It still refuses the bundle
if the standalone `certify` command failed. A new recording will usually fail
`clinical-write` until its contracts are complete. Two built-in policies ship:
`permissive` and `clinical-write`. `permissive` is a minimal smoke check: a
bundle that passes it can still be refused by a governed `clinical-write` run.

Compiling a bundle is not the same as clearing it to run. `lint` reports what a
bundle failed to cover and grades each gap. Without `--strict`, warnings don't
make `lint` exit nonzero. `certify` enforces the selected policy and refuses a
bundle that doesn't meet it. The profiles themselves are defined in
[EXECUTION_PROFILES.md](EXECUTION_PROFILES.md).

## Qualify a bundle from its recording

```bash
openadapt-flow record --backend web --url https://your.app --out rec
openadapt-flow compile rec --out bundle --name my-task
openadapt-flow qualify propose bundle --recording rec --out proposal.json
openadapt-flow qualify accept bundle --proposal proposal.json
openadapt-flow lint bundle
openadapt-flow replay bundle --backend web --url https://your.app
```

`qualify propose` fills the production-shaped
pins from the recording: application identity, environment fingerprint,
identity-gate fields, and the effect oracle from the write the demo actually
observed. `qualify accept` confirms every pin in one command. If a pin isn't
there, Flow HALTs. It will not guess.

`qualify accept` also runs that proposed oracle against a `--break-it` fault
before it can succeed. MockMed is the default fixture: the save is
rejected and the store does not change, while the screen looks the same as a
good save. If the oracle would have accepted that run, the proposal stays
draft or halted. Actor bytes and oracle bytes
must be disjoint. Re-reading the acting screen or the same-session banner
HALTs and names the shared channel. An API, SQL, file, or second-session
read is allowed; Flow will not invent an endpoint. No second read in the
recording means HALT: do not automate until a second read exists.

`--policy-pack community` is the local/MockMed pack. `cloud` and `regulated`
bind the stricter shipped policy. They do not skip a pin. On MockMed, add
`--admit-local` to mint a signed local admission. That test key cannot enter a
production trust map. The pin list and the failure matrix (`--break-it`, plus
identity-swap or extra-field when the demo has parameters) are in
[QUALIFICATION_PROJECT.md](QUALIFICATION_PROJECT.md).

## Compose separately recorded bundles

A URL or window flag selects the execution surface, not the number of screens
in the task. One bundle can move through screens and same-origin routes inside
its bound surface. An RDP workflow can also switch among windows inside one
remote desktop. The public [multi-window fixture](../benchmark/rdp_multiapp/README.md)
is designed to exercise that path through one FreeRDP backend. Its three task
windows belong to one synthetic fixture process, and the repository does not
contain a completed campaign result. A real deployment needs its own
qualification.

A bundle that declares several surfaces ([MULTI_SURFACE.md](MULTI_SURFACE.md))
runs governed, qualified, and durable under the same rules as one surface:
the surface map is sealed in the authorized bundle digest, each step result
records its surface, and qualification declares one environment boundary per
surface. Resume re-binds each surface by identity (never by a stored
handle) and still stops at "Check the record" first when a save may have gone
through. Managed Cloud dispatch needs only the deployment config's `surfaces:`
map, and it refuses to start while a backend surface is unbound. A program
(graph) resume of such a bundle is still refused.
Worklists repeat that bundle over input
records, and subflows reuse steps inside it. Neither one switches backends.
`--workers N` enters a worklist's items on N signed-in sessions at once, each
item still fully checked ([THROUGHPUT.md](THROUGHPUT.md)).
Browser recording follows popups and new tabs in a browser Flow launched and,
in attach mode, only pages a followed page opened on the declared origin. macOS and Linux bind one exact app and window, while a
governed Windows run binds its application identity.

If a task crosses a browser and a native app, or otherwise changes backend,
record one bundle per surface. `openadapt-flow compose` sequences the compiled
bundles:

```bash
openadapt-flow compose \
  --child intake=./intake-bundle \
  --child posting=./posting-bundle \
  --handoff intake.patient_id=posting.patient_id \
  --out composed
openadapt-flow certify composed --policy clinical-write
openadapt-flow run composed --config deploy.yaml
openadapt-flow visualize composed -o composed.html
```

Child A must end `VERIFIED` (or a halt class you named with `--allow-halt`)
before child B starts. Handoffs copy parameter values that A's confirmed
effect contract already bound. The parent will not guess a window title or a
URL. Missing evidence stops the run. Qualify each handoff and the end-to-end
result verifier before deployment.

`visualize` on that parent draws one node per child, on the surface that child
was recorded on. Edges follow `--after`. Handoff edges label the effect-bound
parameters they copy. The parent ends at "End of declared steps", which isn't
a live `VERIFIED` verdict. See [VISUALIZE.md](VISUALIZE.md).

Compose will not retarget one recording onto a second backend. Program authors
can bind individual steps to different HTTP systems, but that is API actuation
rather than recorded GUI backend switching. If you installed the OpenAdapt
launcher, `openadapt flow compose` is the same command.

## Process contracts

A V0 process contract sequences Flow children that already have signed
qualification admissions. The parent names the order or DAG and the confirmed
effect-bound facts that may copy as handoffs:

```bash
openadapt-flow process \
  --child intake=./intake-bundle \
  --admission intake=./intake-admission.json \
  --child posting=./posting-bundle \
  --admission posting=./posting-admission.json \
  --handoff intake.patient_id=posting.patient_id \
  --out process
openadapt-flow certify process --policy clinical-write
openadapt-flow run process --config deploy.yaml
```

`openadapt-flow replay process` refuses; governed `run` is the path, same as
compose. `openadapt flow process` is the launcher form. Compose still sequences
recordings. Don't wrap a `composition.json` and call it admitted. Operator
detail lives in [PROCESS_CONTRACT.md](PROCESS_CONTRACT.md).

ProcessContract v1 adds sealed Python children, signed human tasks, and
verified content-addressed artifact edges to that same parent:

```bash
openadapt-flow process --spec process-v1.json --out process
openadapt-flow run process \
  --run-dir runs/process-001 \
  --code-trust code-signers.json \
  --code-runtime-environment-digest sha256:... \
  --allow-trusted-code \
  --process-receipt-private-key runner-ed25519.key \
  --config deploy.yaml
```

An admitted transform doesn't prove its output. The named verifier must confirm
the exact artifact digest before another child can read it. Human completion
records authority and intent; the declared verifier still proves the effect.
`RECONCILIATION_REQUIRED` stops the parent and is never retried as a general
halt. The [V1 design](design/PROCESS_CONTRACT_V1.md) records the execution,
authentication, portability, and isolation boundaries.

## Local Execute server receipts

`openadapt-flow serve-execute` hosts the public Execute HTTP contract on one
machine, in one process. It binds loopback, generates an Ed25519 key on first
start, and keeps it under `~/.openadapt/execute-ref/`. Health is
`GET /health`. Submit `openadapt.execute-request/v1` to `POST /v1/executions`,
poll `GET /v1/executions/{id}` until `terminal`, then read
`GET /v1/executions/{id}/receipt`. The same process also speaks MCP at
`POST /mcp`. A request must match exactly one admitted bundle in the data
directory's `admissions/` folder by qualification id, workflow version,
workflow digest, environment id, and minimum effect strength.

Receipts are self-signed with that local key. They aren't OpenAdapt
production Seals. `GET /seals/{id}` is the local verify page; it shows the
key fingerprint and a $0 meter. Counterparties that require an OpenAdapt Seal
still use Cloud.

## Production admission

Flow reports `Production` only while a signed, expiring, revocable release
admission is valid for the exact build. Workflow admission is separate: a
release admission doesn't qualify a customer's bundle, application,
environment, or verification contract. The current admission state is
machine-readable at
[openadapt.ai/production-lifecycle.json](https://openadapt.ai/production-lifecycle.json).

The registry in [`claims.yaml`](../claims.yaml) assigns each registered public
capability claim a tier and names the tests or benchmark artifacts behind it.
The build fails when a registered claim outranks its evidence. See
[CLAIMS_AND_QUALIFICATION.md](CLAIMS_AND_QUALIFICATION.md) and the generated
[VERIFICATION.md](VERIFICATION.md). For security review, see
[ENTERPRISE_ARCHITECTURE.md](ENTERPRISE_ARCHITECTURE.md).
