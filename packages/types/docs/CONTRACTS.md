# What the versioned wire contracts may carry

Version 1 remains the control-state contract. Version 2 adds an optional
normalized top-level viewport rectangle, the exact source viewport and DPR, and
an exact observation or decoded-media-frame binding, without changing V1.

Overlay schemas reject unknown fields and contain only closed presentation
labels and canonical statuses. Screenshot payloads, action-target selectors,
accessible names, text and values, typed input, identities, URLs, logs, report
bodies, and user-authored workflow names remain outside this public contract.
V2 may carry only normalized target geometry from a browser top-level CSS
viewport. Native and RDP device-pixel geometry is not part of this V2 schema.

Target geometry never carries locators, accessible names, values, URLs, or
screenshots. A private live observation uses a run/export-scoped HMAC reference
instead of a linkable raw frame hash. Published media uses the exact media
SHA-256 and decoded frame index. A renderer draws tracking only when that
binding matches; it omits the rectangle rather than replaying selectors,
interpolating movement, or inferring a missing target from adjacent events.
The runtime does not guess a future viewer transform. Desktop, Cloud, and media
renderers map the normalized rectangle through their actual content box.
If multiple runtime states land in one decoded media frame, the producer must
coalesce them deterministically; it must not invent extra media frames or
approximate their timing.

`openadapt_types/schemas/execute-v1-openapi.json` is the public OpenAdapt
Execute v1 contract. It defines asynchronous execution submission, lifecycle
status, terminal evidence receipts, and signed decision and terminal webhooks.
It keeps `waiting_for_reconciliation` as a lifecycle state and
`reconciliation_required` as a terminal outcome.

The separate `business-decision-*-v1.json` files define a finite human branch
that the workflow declared before execution. They do not reuse the operational
halt actions. They carry only opaque bindings, option IDs, digests, counts, and
closed status values. The separate presentation artifact classifies each
question and option label as `local_only` or `reviewed_remote_safe`. Remote
delivery requires a positive egress-review digest for every field. The signed
delivery policy binds the exact presentation and review digest. Screenshots,
free-form notes, and live record values stay on the customer runner. An
accepted answer only selects a compiled branch. The next action must still pass
its live-state, identity, policy, and effect contracts.

## OpenAdapt Execute v1

OpenAdapt Execute is the public asynchronous contract for a qualified
workflow. A partner sends an authorized request, receives an execution ID, and
then reads a terminal receipt or receives a signed webhook. The contract does
not expose a runner, customer data, evidence bytes, application recipes, or
Cloud control-plane internals.

The generated OpenAPI document is packaged at
`openadapt_types/schemas/execute-v1-openapi.json`. The package also includes a
small Python client. The reference Python and TypeScript clients are in
[`examples/execute`](examples/execute/). Use `https://app.openadapt.ai/api` as
the OpenAdapt Cloud base URL. It uses a partner-provisioned bearer token and
the client appends the stable `/v1` paths.
The client requires an HTTPS base URL and rejects redirects before it can send
the bearer token to another endpoint.

```python
from openadapt_types import ExecuteClient

client = ExecuteClient(
    base_url="https://app.openadapt.ai/api",
    bearer_token="partner-provisioned-token",
)
```

The client does not poll forever. A workflow can wait for a human decision or
reconciliation. Use the terminal receipt or signed webhook as the completion
signal.
