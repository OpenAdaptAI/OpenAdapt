# Job attribution in bundle schema 3

Schema 3 binds a program to a job, organization, owner identity and exact intent
hash. These values participate in the program's content digest. They do not
approve a program, qualify a runtime or authorize an input.

Ordinary workflows still default to schema 2. Existing schema 1/2 serialization,
digests and migration from 1 to 2 stay unchanged. No migration adds job identity
to an old program or run. A job-bound program must be authored explicitly in
schema 3 before its normal sealing, qualification and admission.

The `job_authoring` object has exactly five required fields:

```json
{
  "schema": "openadapt.job-authoring-binding/v1",
  "job_id": "11111111-1111-4111-8111-111111111111",
  "org_id": "22222222-2222-4222-8222-222222222222",
  "owner_principal_id": "33333333-3333-4333-8333-333333333333",
  "intent_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
```

Identifiers must be lowercase, hyphenated canonical UUIDs. The intent digest is
bare lowercase SHA-256. It hashes `openadapt.private-job-intent/v1`, one NUL byte,
and the exact UTF-8 intent bytes, up to 8192 bytes. Whitespace and Unicode are not
normalized. The server must supply the actual identities and intent hash;
parsing these strings does not authenticate a person.

Unknown fields, omitted fields and malformed values refuse. Schema 3 requires a
non-null binding. Schemas 1/2 refuse a non-null binding; their owning Workflow
serializer omits an absent or null value.

## Canonical content

The existing digest envelope is retained: `workflow` contains the manifest-free
workflow and `files` contains its asset hashes. JSON keys are sorted by Unicode
code point, separators are compact, and the result is UTF-8 with non-ASCII text
retained. Schema 3 applies no schema-2 historical omission rules.

Schema 3 uses Python's number representation consistently in its writer and
digest. For example, `1.0` differs from `1`, and exponent forms include `1e-07`
and `1e+20`. Its JSON parser refuses other numeric spellings, non-finite values,
duplicate object keys, invalid Unicode and byte-order marks. The checks apply to
structured request data as well as attribution fields. They do not reinterpret
strings containing JSON or remove user payload keys named `job_authoring`.

Workflow and schema-3 manifest JSON writers share this rule. Legacy writers and
canonical projections remain unchanged. The [vectors](../tests/fixtures/job-authoring-v3/vectors.json)
include ordinary numeric data, negative zero, Unicode key order and nested
job-named payloads. Their generator never touches legacy vectors.

## Supported consumers

`Workflow.model_validate` and `Workflow.model_validate_json` support authoring.
`Workflow.save` seals the resulting program, and `Workflow.load` verifies the
original content and assets. Schema-3 execution additionally requires that
actual verified load through the supported consumer. Copying serialized metadata
or attaching a manifest cannot grant its process-local load capability.

`openadapt_flow.job_authoring.verified_job_authoring(workflow, bundle_dir,
expected_binding=...)` returns the closed binding only after checking the actual
source, current content and optional exact expected identity. The expected value
must come from an authoritative caller. This helper does not issue an admission
or decide who may execute the program.

CLI replay and deployment-created replayers share the same early runtime guard,
including resume paths and preconstructed models. Unsupported or changed input
refuses before runtime output or actuation. A valid schema number or advertised
package version is insufficient: deployments must pin and qualify the actual
supported consumer source and retain normal admission and per-save checks.

Every binding-field change produces a different content identity. Existing
admission signatures therefore cannot cover the changed program. Explicit
verified derivation invalidates the prior certification; it does not update or
re-sign a past run. Owner approval, the exact planned request, current principal
and pre-run timing remain separate checks in the authoritative execution system.

The included tests and vectors use synthetic data. They do not establish an
owner-approved managed execution or a real-system outcome.
