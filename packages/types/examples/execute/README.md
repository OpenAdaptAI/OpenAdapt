# OpenAdapt Execute v1 reference clients

The public contract is the generated
[`execute-v1-openapi.json`](../../openadapt_types/schemas/execute-v1-openapi.json)
document. These small clients show the three stable resource paths.

Use a partner-provisioned endpoint and bearer token. OpenAdapt Cloud uses
`https://app.openadapt.ai/api` as its base URL. The clients add `/v1` paths.

The clients do not expose Cloud internals. They do not create qualifications,
issue authority, select application connectors, or handle webhooks. Generate a
complete client from the OpenAPI document when your integration needs full
schema types or webhook receiver support.
