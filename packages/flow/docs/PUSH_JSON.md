# `push --json` controller contract

Desktop and other local controllers can add `--json` to `openadapt-flow push`.
Flow then writes one compact JSON object to standard output. The schema is
[`openadapt.push-result/v1`](../schemas/push-result-v1.json).

The command without `--json` keeps its existing human-readable output.

## Status and exit code

| `status` | Exit code | Meaning | `next_action` |
|---|---:|---|---|
| `paused_for_review` | 0 | Flow made a local sanitized derivative. It did not upload it. | `review_local` |
| `accepted_for_ingest` | 0 | The server acknowledged the exact approved archive and returned its stable ingest id. | `parameterize`, `validate_runtime`, or `open_dashboard` |
| `failed` | 1 | Flow rejected the request before it had proof of an upload attempt or received a definite rejection. | `null` |
| `delivery_uncertain` | 1 | A transport failure or an incomplete or ambiguous server response occurred after Flow attempted the request. The server can have received it. | `reconcile` |

Do not retry `delivery_uncertain` automatically. Use `artifact_sha256` to
reconcile the request with the hosted control plane first.

A `201` status is not sufficient proof. Flow also requires the exact server
ingest id, artifact kind and hash, governed next action, and, for a bundle, the
complete retained version identity chain. An incomplete or contradictory `201`
response is `delivery_uncertain` in both JSON and human-readable modes.

## Stable fields

V1 always includes these top-level keys:

```text
schema, status, workflow_id, artifact_ingest_id, review, attestation,
binding, next_action, dashboard_url, delivery, error
```

An unused value is `null`. Flow does not omit the key.

`artifact_ingest_id` is the server-owned `artifact_ingests.id`. Flow does not
create it. JSON mode does not return `accepted_for_ingest` if a server response
omits this id or does not echo the exact approved archive hash.

A recording ingest has `workflow_id: null`. It is not a runnable workflow. Its
next action is parameterization or runtime validation. An accepted bundle has
a workflow UUID, the server ingest UUID, a same-origin dashboard URL, and the
exact local runtime-attestation binding.

Push-result V1 accepts runtime-validation attestation schemas V1, V2, and V3.
An unrecognized attestation schema is an uncertain delivery, not an accepted
handoff.

The `binding` object lets Desktop detect a stale handoff. It carries:

- the source tree, derivative tree, approved archive, and acknowledged artifact
  SHA-256 values;
- the exact bundle and source-recording SHA-256 values for a bundle;
- the sanitization and certification policies;
- the certification evidence, parameter schema, governed authorization
  template, and attested run-report SHA-256 values; and
- the halted run UUID when the bundle resolves a governed halt; and
- the server-retained organization, bundle-version, and runtime-validation
  identifiers for an accepted bundle.

The local `review.id` is a domain-separated SHA-256 of the canonical sanitized
manifest. It is stable for that exact review candidate. It is local and
non-authoritative. It is not a hosted approval id. The attestation `id` is the
server challenge id that the local runtime-validation attestation signs.

For `paused_for_review`, `review.action` is the typed local action
`review_sanitized`. `sanitized_path` and `original_path` are separate values.
A controller must pass them as process arguments. It must not construct or run
a shell command from either path.

## Examples

A raw recording normally pauses locally:

```bash
openadapt-flow push recording/ --json
```

```json
{
  "schema": "openadapt.push-result/v1",
  "status": "paused_for_review",
  "workflow_id": null,
  "artifact_ingest_id": null,
  "review": {
    "id": "<sanitized-manifest-sha256>",
    "scope": "local_non_authoritative",
    "sanitized_path": "<local-derivative-path>",
    "action": "review_sanitized",
    "original_path": "<local-source-path>"
  },
  "attestation": null,
  "binding": {
    "kind": "recording",
    "source_tree_sha256": "<sha256>",
    "derivative_tree_sha256": "<sha256>",
    "approved_archive_sha256": null,
    "artifact_sha256": null,
    "bundle_sha256": null,
    "source_recording_sha256": null,
    "sanitization_policy": "outbound-phi-v1",
    "certification_policy": null,
    "certification_evidence_sha256": null,
    "governed_authorization_template_sha256": null,
    "parameter_schema_sha256": null,
    "attested_run_report_sha256": null,
    "resolves_run_id": null,
    "organization_id": null,
    "bundle_version_id": null,
    "bundle_version": null,
    "runtime_validation_id": null
  },
  "next_action": "review_local",
  "dashboard_url": null,
  "delivery": { "attempted": false, "certainty": "not_attempted" },
  "error": null
}
```

After local approval, use the same flag for the approved derivative:

```bash
openadapt-flow push approved-bundle/ --kind bundle \
  --validation-attestation attestation.json --json
```

Flow returns `accepted_for_ingest` only after it checks all required server ids,
the echoed artifact hash, the exact local attestation binding, and the retained
server bundle-version record. The server record must bind its organization,
workflow, artifact hash, version number, and runtime-validation identifier.
For a replacement, the retained version must also bind the exact halted run.
When the request does not resolve a halt, that retained field must be `null`.

## Error privacy

Machine-readable errors use bounded messages of at most 500 characters. They
do not copy a token, raw server body, or local source path into the JSON result.
The human-readable command keeps its existing diagnostic output.
