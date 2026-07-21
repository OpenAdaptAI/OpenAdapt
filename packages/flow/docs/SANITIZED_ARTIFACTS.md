# Sanitized artifacts and approval

Compilation does not make a recording or bundle PHI-free. OpenAdapt permits
artifact upload only through a separate sanitized derivative whose coverage,
review, and exact bytes are verifiable.

## Lifecycle

```bash
pip install 'openadapt-flow[privacy,hosted]'
python -m spacy download en_core_web_sm

openadapt-flow sanitize recording/ --kind recording --out triage.sanitized/
openadapt-flow review-sanitized triage.sanitized/ --original recording/
openadapt-flow approve-sanitized triage.sanitized/ --original recording/ \
  --reviewer operator@example.com
openadapt-flow push triage.sanitized/ --kind recording

openadapt-flow compile triage.sanitized/ --out triage.bundle/ --name triage
openadapt-flow lint triage.bundle/ --strict
openadapt-flow certify triage.bundle/ --policy permissive
openadapt-flow replay triage.bundle/ --url https://example.internal/login \
  --run-dir triage.run/ --param patient_id=example

openadapt-flow sanitize triage.bundle/ --kind bundle \
  --out triage.bundle.sanitized/
openadapt-flow review-sanitized triage.bundle.sanitized/ \
  --original triage.bundle/
openadapt-flow approve-sanitized triage.bundle.sanitized/ \
  --original triage.bundle/ --reviewer operator@example.com
openadapt-flow validate-hosted --recording triage.sanitized/ \
  --bundle triage.bundle.sanitized/ --run-dir triage.run/ \
  --policy permissive --risk-class low --environment staging-v1 \
  --target-url https://example.internal/login --out triage.validation.json
openadapt-flow push triage.bundle.sanitized/ --kind bundle \
  --validation-attestation triage.validation.json
```

For an existing hosted workflow, add `--workflow-id <uuid>` to the bundle push.
When the replacement repairs a specific hosted halt, also pass
`--resolves-run-id <halted-run-uuid>`. Cloud locks that unresolved run in the
same workflow and resolves its halt only after the validated version is active.
The validated archive becomes a new active version; recording uploads cannot
select an existing workflow.

`sanitize` never mutates the source. It inventories every source file, rejects
symlinks, transforms supported files on a new copy, runs a stable second scrub
pass, and writes `.openadapt-sanitization.json`. Source paths are represented by
hash only in the manifest so a PHI-bearing source name is not copied into provenance;
the derivative filename itself is scrubbed and collision-checked.

`review-sanitized` serves a self-contained viewer on `127.0.0.1`. It sends no
remote requests and shows original versus sanitized text/images. Reviewers can
add literal text replacements or black image rectangles. Those additions store
only hashes/coordinates in the manifest, not the removed literal. Every change
deletes any existing approval.

`approve-sanitized` creates a deterministic sibling
`<derivative>.approved.zip`. File ordering, timestamps, permissions, and
compression are fixed. Approval binds reviewer, time, policy, derivative tree,
manifest, archive SHA-256, and archive byte size. `push` verifies all hashes and
sends that exact ZIP without reconstructing it.

## Destination trust

Execution lane and egress destination are independent:

| Destination | Requirement |
| --- | --- |
| `https://app.openadapt.ai` | Recognized as OpenAdapt-managed. Only an approved sanitized archive uploads. |
| Customer-managed / BYOC | `--destination-kind customer-managed`, HTTPS, and an exact `--trusted-host https://…` allowlist entry. |
| Local development | `--destination-kind local` and a loopback hostname only. |
| Unknown/custom host | Refused until explicitly classified and allowlisted. |

The `cloud`, `byoc`, and `regulated` execution labels do not grant or deny
network trust. Sanitized artifacts can upload from any lane after destination
verification. Raw artifacts cannot.

## Coverage and refusals

| Artifact content | Current handler | Result |
| --- | --- | --- |
| UTF-8 JSON/JSONL/text/Markdown/CSV/YAML/TOML/HTML/XML/log | NER/text scrub plus stable second pass | Supported |
| PNG/JPEG/WebP | OCR/image redaction, normalized to PNG, plus stable second pass | Supported; human visual review recommended |
| SQLite/database | None | Entire derivative refused |
| Video | None | Entire derivative refused |
| Audio | None | Entire derivative refused |
| ZIP/nested archive | None | Entire derivative refused; contents are never copied through |
| Encrypted/executable/unknown binary | None | Entire derivative refused |
| Symlink | None | Entire derivative refused |

Refusal is deliberate: `coverage.complete=true` must mean every input byte was
handled by a known transform. Database cell-level sanitization, media
transcription/frame redaction, and safe recursive archive traversal need their
own bounded handlers and adversarial tests before support is claimed.

## Human versus policy approval

Human review is the default because OCR/NER can miss contextual, handwritten,
or non-textual PHI. An administrator may configure policy approval only when:

- every file has a supported handler;
- the second pass is stable;
- no unresolved finding remains; and
- the organization accepts the policy's residual-risk threshold.

Policy approval is recorded as `method=policy`; it is not disguised as human
review. Both modes bind the same exact archive hash. Managed Cloud refuses
policy approval by default; only a deployment operator can explicitly enable a
reviewed automatic policy, and an upload request cannot enable it. Automatic
ingest also requires `OPENADAPT_SANITIZATION_POLICY_KEY_ID` and a base64 HMAC
key of at least 32 bytes in `OPENADAPT_SANITIZATION_POLICY_KEY`. The matching
key ID must be present in Cloud's deployment-controlled
`SANITIZATION_POLICY_KEYS_JSON` allowlist. The signature covers the exact
artifact hash, size, semantics flags, scrubber/policy identity, media types,
approver, and approval time; possession of an ingest token is insufficient.

The approval is an **operator attestation**. Cloud can account for every archive
byte, verify the manifest and exact hash, and identify the ingest token that
submitted it; it does not independently witness the local viewer session or
rerun OCR/NER. A compromised or dishonest operator can mislabel data, so
regulated deployment policy must control reviewer identity, separation of
duties, retention, and evidence export. Human approval reduces detector risk;
it is not third-party proof of de-identification.

## Execution semantics and runtime PHI

Sanitizing recorded typed values, selector evidence, target crops,
postconditions, or identity bands can change behavior. A changed recording is
marked `requires-parameterization-validation`: replace patient-specific values
with runtime parameters and revalidate the compiled program. A changed compiled
bundle is marked `not-preserved` and cannot upload as an executable bundle.

Sanitization covers the design-time derivative only. Live values, screenshots,
identity crops, model requests, and system-of-record checks can reintroduce PHI.
They must remain inside the deployment's declared trusted runtime boundary.

## Schemas

- `schemas/sanitized-artifact-manifest-v1.json`: rich local per-file provenance.
- `schemas/sanitized-artifact-approval-v1.json`: local approval and immutable
  archive binding.
- `schemas/sanitization-ingest-v1.json`: public multipart
`sanitization_manifest` contract (`openadapt.sanitization/v1`).

The public envelope carries `execution_semantics`,
`runtime_semantics_validated`, and `trusted_boundary_required_at_runtime` next
to the archive hash. The control plane must not treat privacy/integrity approval
as proof that the artifact can execute. The current sanitizer leaves changed
recordings in `requires-parameterization-validation`; compile and validate them
with runtime parameters in the customer/BYOC boundary. Changed compiled bundles
remain `not-preserved` and are refused by `push`.

`validate-hosted` is deliberately later than privacy approval. It recomputes
strict lint and policy certification, requires a successful non-halted report
bound to the same bundle/source recording/parameter schema, derives the
bundle's `low` or `consequential` risk class, and signs the exact HTTPS target
origin and host allowlist against a short-lived one-time Cloud challenge. The
Cloud admission also requires exact membership in its policy, risk-class, and
deployed compiler-version allowlists. Managed hosted targets must use public DNS
names; literal IP, loopback, private/link-local resolution, wildcard, and
special-use hostnames are refused. The HMAC is operator evidence: it proves
token possession and envelope integrity, but does not mean Cloud or an
independent auditor witnessed the local replay. `--environment` must name the
exact runner boundary qualified by the deployment operator. Cloud hashes that
identifier, binds it during activation, and the runner refuses a job whose
boundary ID/hash differs; a descriptive label that was never qualified is not
accepted as evidence by configuration alone.
