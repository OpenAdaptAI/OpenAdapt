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

Demo once, get a checked program. After `record` and `compile`, propose the
pins from the demonstration and confirm them in one command:

```bash
openadapt-flow qualify propose bundle --recording rec --out proposal.json
openadapt-flow qualify accept bundle --proposal proposal.json
```

`propose` (also `openadapt-flow propose-qualification`) mines:

- Application identity (origin or native id, plus the recorded version).
- Environment fingerprint (surface, origin, viewport, runtime).
- Identity-gate fields (canonical ladder when the demo armed it; otherwise a
  structured `record_id` quorum bound to a workflow parameter).
- Effect oracle from declared or observed system-of-record writes.
- A starter failure matrix. It always includes the `--break-it` class
  (optimistic write: the screen claims success, the system of record does
  not). If the demo has parameters, it also adds identity-swap or extra-field.

Automatic vs confirm: Flow proposes every pin it can see in the recording or
bundle. You confirm all of them with `qualify accept`. `--refuse-pin identity`
(or `application` / `environment` / `effect`) HALTs. Flow does not fill a
missing system-of-record oracle.

`--policy-pack community|cloud|regulated` picks the shipped policy. Community
allows a MockMed local-dev admission (`--admit-local`). That signer cannot
enter a production trust map. Cloud and regulated require a production signer
and still demand the same identity and effect pins.

If you already know the pins, you can still type them by hand:

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

# Or require two independent retained sources with explicit comparisons:
openadapt-flow qualify set-identity bundle \
  --step save \
  --signal record_id=structured:exact \
  --signal secondary_identifier=captured_context:normalized:unicode_nfkc,collapse_whitespace \
  --signal-region secondary_identifier=40,20,240,48 \
  --signal-extract 'record_id=Record ID:\s*(?P<value>[A-Za-z0-9._-]+)' \
  --signal-extract 'secondary_identifier=DOB:\s*(?P<value>[0-9/-]+)' \
  --signal-param record_id=patient_id \
  --quorum 2

# Dedicated runtime context uses explicit PHI-free expected values:
openadapt-flow qualify set-identity bundle \
  --step save \
  --signal application=application:exact \
  --signal workflow_state=workflow_state:exact \
  --signal-expected application=accuro \
  --signal-expected workflow_state=patient-chart \
  --quorum 2

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

Both `--canonical-ladder` and named signal quorums are runtime-enforced.
Quorum signals must use independent retained sources; giving one source two
labels cannot create two votes, and overlapping pixel regions are refused.
Pixel signals use explicit qualified regions rather than arbitrary broad
context bands. A definitive conflict halts even after the numeric quorum has
been reached, while an unavailable signal can abstain only when the remaining
independent signals still satisfy the quorum.
Comparisons are either byte-exact or use only the explicitly listed
normalizers. Structured and captured-context signals require an explicit
`--signal-extract KEY=REGEX` with exactly one named `value` group, so only the
intended field can cast a vote. Parameter substitution is explicit
(`--signal-param`) and matches only complete values, so `John` cannot bind
inside `Johnson`. Reports retain a closed semantic signal key, source, evidence
class, and verdict, not the observed identity value; arbitrary patient/account
labels cannot enter the bundle or report.
Application, session, and workflow-state signals use dedicated runtime
observers and require an explicit PHI-free `--signal-expected KEY=VALUE`;
session values are lowercase SHA-256 identity digests.

When a qualified consequential action uses an `api_binding`, the binding must
map the qualified semantic identity key to a workflow parameter, reference that
parameter in the outgoing request template, and bind the same parameter in the
declared effect selector. The runtime checks this request/effect identity proof
before sending the request; API actuation cannot bypass the GUI identity
contract.

PHI-free bundles compiled with this runtime carry salted full-source hashes for
the exact and supported explicit-normalization contracts. A bundle compiled
before those hashes existed must be recompiled before its hashed evidence can
be assigned to a signal quorum; qualification refuses an unavailable
comparison rather than silently falling back to different semantics.

## Python API

The models and mutation/evaluation functions live in
`openadapt_flow.qualification`. `project_schema()` returns JSON Schema for
Desktop and other clients, while `run_cases(workflow, executor)` provides a
typed local execution seam with an explicit evidence root. Every edit
invalidates stale certification and
resealing the workflow binds the qualification project into the existing
bundle content digest.
