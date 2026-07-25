# Qualification projects

A qualification project turns a compiled workflow into a versioned,
machine-checkable production contract. It is sealed inside the existing
`workflow.json`; it references the workflow's canonical steps, identity
evidence, and effects rather than copying a second executable manifest.

The v1 contract records:

- The application, runtime, capability, and environment boundary.
- Operator-confirmed read-only, state-changing, consequential, and
  irreversible classifications.
- Exact or explicitly normalized identity signals and their quorum.
- Effect-verification strength:
  1. Independent system interface.
  2. Independent session.
  3. Persisted-state reacquisition.
  4. Immediate screen confirmation.
- The minimum acceptable effect tier.
- Representative cases and deterministic ambiguity, wrong/stale identity, and
  weak/missing effect cases.
- Trusted-runner attestations bound to the exact project contract and revision,
  executable workflow, environment, runtime, capabilities, and on-disk evidence
  hashes.
- Machine-readable certification refusals.
- Semantic revisions and requalification conditions.

Unknown or unconfirmed action risk always refuses certification. Confirmed
state-changing actions require effect coverage. Consequential and irreversible
actions additionally require executable identity coverage, and immediate
screen confirmation alone cannot qualify them. An action declared irreversible
by the executable Flow contract cannot be down-classified during qualification.

## CLI

Initialize the contract without editing bundle JSON:

```bash
openadapt-flow qualify init bundle \
  --target citrix \
  --application "Example application" \
  --application-version "1" \
  --environment-digest "<sha256>" \
  --require-capability pixel_observation \
  --minimum-tier 3
```

Configure existing identity evidence and an effect:

```bash
openadapt-flow qualify set-risk bundle \
  --step save \
  --classification irreversible \
  --explanation "Submits a source-of-record write"

openadapt-flow qualify set-identity bundle \
  --step save \
  --canonical-ladder

openadapt-flow qualify set-effect bundle \
  --step save \
  --effect-index 0 \
  --tier 1

openadapt-flow qualify trust-runner bundle \
  --key-id clinic-runner-1 \
  --public-key "<raw-ed25519-public-key-base64>"
```

Add a representative case, import signed results produced inside the declared
execution boundary, and explain or persist the decision:

```bash
openadapt-flow qualify add-case bundle \
  --case-id representative-1 \
  --kind representative \
  --expected-outcome verified \
  --input-ref fixtures/representative-1

openadapt-flow qualify run bundle \
  --results case-results.json \
  --evidence-root qualification-evidence
openadapt-flow qualify explain bundle \
  --policy clinical-write \
  --evidence-root qualification-evidence \
  --json
openadapt-flow qualify certify bundle \
  --policy clinical-write \
  --evidence-root qualification-evidence \
  --json
```

`qualify run` imports typed results from Desktop or another local,
customer-controlled executor. Unsigned, stale, missing, symlinked, or
hash-mismatched evidence is refused. Raw parameters, screenshots, credentials,
and system-of-record records do not belong in the qualification project.

The current runtime-enforced identity choice is `--canonical-ladder`.
Signal/quorum policies remain representable for qualification UI work, but
certification refuses them until the runtime consumes those exact semantics.
This prevents reviewed field/quorum intent from being mistaken for executable
behavior.

## Python API

The models and mutation/evaluation functions live in
`openadapt_flow.qualification`. `project_schema()` returns JSON Schema for
Desktop and other clients, while `run_cases(workflow, executor)` provides a
typed local execution seam with an explicit evidence root. Every edit
invalidates stale certification and
resealing the workflow binds the qualification project into the existing
bundle content digest.
