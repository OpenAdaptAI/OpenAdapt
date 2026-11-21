# Handoff receipts: a receipt that travels with an agent handoff (draft)

| | |
|---|---|
| **Identifier** | `https://openadapt.ai/extensions/handoff-receipt/v1` |
| **Status** | Draft, version 1. Implemented in OpenAdapt, not submitted to any standards body. |
| **Document schema** | `openadapt.handoff/v1` |
| **Verdict schema** | `openadapt.handoff-verdict/v1` |
| **Media type** | `application/vnd.openadapt.handoff+json` |
| **Receipt format** | `https://openadapt.ai/record/run/v1`: an in-toto Statement v1 in a DSSE v1.0.2 envelope |
| **Reference implementation** | [`openadapt_flow/handoff.py`](../openadapt_flow/handoff.py), [`openadapt_types/receipt.py`](../../types/openadapt_types/receipt.py) |

The identifier is a name, not a location; nothing is fetched from it.

## 1. Purpose

When one agent hands a task or a result to another, the receiving agent
usually has only the sender's word for what happened. This extension carries
the signed receipt of the run that produced the result alongside the result.
The receiver can then check, against keys it chose to trust:

- who signed the receipt, and that it is unchanged;
- what the run checked, what it didn't check, and what it couldn't confirm;
- whether any part was confirmed in the other party's own system rather than
  reported by the sender.

The handoff works the same over any channel. Section 6 defines bindings for
MCP tool results, HTTP requests and webhooks, files and email attachments, and
the A2A (Agent2Agent) protocol.

## 2. What a valid receipt does and does not prove

Every verifier MUST tell its user the following, in its own words:

1. A valid signature proves **who signed** the receipt and that it has not
   changed since. It does **not** prove that the receipt's claims are true.
2. The signer's role comes from the **receiver's** pinned key registry, never
   from the handoff document or the envelope.
3. The receipt binds the handoff payload only through a `payload` subject:
   `{"name": "payload", "digest": {"sha256": ...}, "media_type": ...}`.
   The sender adds that subject and signs again with the key that signed the
   run, which never changes who signed. A receiver that holds the payload
   MUST refuse a receipt with no `payload` subject (`payload_not_bound`) or
   with another digest (`payload_mismatch`).
4. A confirmation counts as **independent of the sender** only when its
   signed `read_method` reads the other party's own system (`portal_read_back`
   or `api`). Any other claim rests on the signer's word.

5. A sender-signed receipt can't prove the sender's claim to the receiver.
   Before acting, the receiver SHOULD confirm the claim by reading its own
   record. The reference implementation's `decide()` accepts only when the
   receipt is intact and the receiver's own read is `recorded_as_intended`.
   It rejects a fabricated or differing claim even with a valid signature from
   a trusted sender, and it never accepts without that read (`unchecked`).

## 3. The handoff document

```json
{
  "schema": "openadapt.handoff/v1",
  "payload": {
    "media_type": "application/json",
    "sha256": "<64 hex>",
    "size": 123,
    "name": "result.json",
    "content": {"outcome": "done"}
  },
  "receipt": {"payloadType": "application/vnd.in-toto+json", "payload": "<base64>", "signatures": [{"keyid": "seal-ed25519-<16 hex>", "sig": "<base64>"}]}
}
```

- `schema` is required and is exactly `openadapt.handoff/v1`. Other top-level
  fields are refused.
- `payload.sha256` is required. It covers the exact payload bytes: for
  `content_base64`, the decoded bytes; for `content` (a JSON value), its
  canonical form (sorted keys, `,` and `:` separators, UTF-8).
- Exactly one of `content`, `content_base64`, or neither (**detached**) is
  present. An inline payload is at most 1 MiB.
- `receipt` is a `record/run/v1` DSSE envelope (`receipt.json`) or its public
  card (`openadapt.receipt-card/v1`). The private openings file
  (`receipt.private.json`, `openadapt.receipt-openings/v1`) MUST NOT be sent.
  Senders MUST refuse to attach it, and verifiers MUST refuse a document that
  carries it.
- The document itself is not signed. Its integrity comes only from the
  receipt and the payload digest.

## 4. Verifying

A receiver MUST perform these steps in order and stop at the first failure.
The result is a refusal, never a partial pass.

1. **Parse** the document as JSON, refusing duplicate keys. Check `schema` and
   the closed field set.
2. **Verify the receipt** as `record/run/v1` requires
   ([`openadapt_types/receipt.py`](../../types/openadapt_types/receipt.py),
   vectors in `packages/types/tests/vectors/record-run-v1/`):
   - the DSSE envelope's `payloadType` is `application/vnd.in-toto+json`;
   - each signature is Ed25519 over `PAE(payloadType, payload)` as DSSE
     defines it, from a key in the receiver's pinned registry
     (`openadapt.receipt-keys/v1`);
   - an unknown key, role or schema, a revoked key, or a demo key next to a
     real one is refused;
   - the payload is canonical JSON, an in-toto Statement v1 with
     `predicateType` `https://openadapt.ai/record/run/v1`, closed, internally
     consistent, and free of embedded content.
3. **Check the payload**:
   - recompute its SHA-256 from the inline content or the detached file;
   - refuse when it doesn't match the document's own `payload.sha256`
     (`payload_mismatch`);
   - check the bytes against the receipt's `payload` subject, as
     `verify_receipt(..., received=bytes)` does in the reference verifier.
     A receipt with no `payload` subject is refused (`payload_not_bound`),
     and one with a different digest is refused (`payload_mismatch`);
   - a detached payload the receiver doesn't have is reported as not
     checked, never as bound.
4. **Report** the verdict (section 5).

## 5. The verdict

```json
{
  "schema": "openadapt.handoff-verdict/v1",
  "integrity": "intact",
  "reason": null,
  "signer_roles": ["this_computer"],
  "signer_lines": ["Signed by this computer"],
  "outcome": "VERIFIED",
  "checks": {"passed": [], "incomplete": [], "not_checked": ["field_values"],
             "writes": {"confirmed": 1, "refuted": 0, "not_checked": 0}},
  "payload": {"bound": true, "media_type": "application/json", "note": "The receipt's signer bound these exact payload bytes: ..."},
  "confirmations": [{"state": "recorded_as_intended", "read_method": "api", "independent": true}],
  "confirmation_lines": ["Confirmed in the other party's system"],
  "independent_confirmations": 1,
  "what_this_proves": ["A valid signature proves who signed this receipt and that it has not changed since. It does not prove that what the receipt says is true.", "..."]
}
```

These rules apply to the verdict:

- **`integrity`** is `intact` or `refused`. A refusal carries `reason`, using
  the `record/run/v1` refusal codes plus `payload_mismatch`.
- **`outcome`** is copied from the receipt and never upgraded.
- **`signer_lines` and `confirmation_lines`** are kept separate. A
  confirmation never strengthens a signer line.
- **`what_this_proves`** always opens with the statement in section 2,
  point 1.
- **`receiver_check` and `decision`** are filled in by `decide()`.
  `receiver_check` is the receiver's own read: a `state` from
  `recorded_as_intended`, `recorded_differently`, `not_found` or `unreadable`,
  the `differing_fields` as field ids only, and the `read_method`. `decision`
  is `accept`, `reject` or `unchecked`.

## 6. Bindings

### 6.1 MCP

`openadapt agent serve --handoffs` adds two read-only tools:

- **`get_handoff {run_id}`** returns the run's public result as the payload,
  with its receipt attached.
- **`verify_handoff {handoff}`** returns the verdict, checked against the
  server's pinned keys.

### 6.2 HTTP and webhooks

The document is the request body, with `Content-Type:
application/vnd.openadapt.handoff+json`. The receiver verifies the body it
received, as is. `openadapt handoff send FILE --run RUN_DIR --post URL`
sends one. It accepts HTTPS, or plain HTTP only to loopback.

### 6.3 Files and email attachments

The payload stays as the file. `FILE.handoff.json` beside it is a detached
document: it holds the payload digest and the receipt, and no content.
`openadapt handoff verify FILE.handoff.json` finds `FILE` next to it and
checks it. If `FILE` is missing, the verdict says the payload was not
checked.

### 6.4 A2A protocol (Agent2Agent, specification 1.0)

**Agent Card declaration.** The extension is declared in
`capabilities.extensions`:

```json
{
  "uri": "https://openadapt.ai/extensions/handoff-receipt/v1",
  "description": "Task results carry the run's record/run/v1 receipt ...",
  "required": false,
  "params": {
    "handoff_schema": "openadapt.handoff/v1",
    "verdict_schema": "openadapt.handoff-verdict/v1",
    "media_type": "application/vnd.openadapt.handoff+json",
    "receipt_predicate": "https://openadapt.ai/record/run/v1",
    "envelope": "dsse"
  }
}
```

The extension adds data only and is never `required`, so a client that
doesn't know it still gets every result.

**Activation.** A client MAY send `A2A-Extensions:
https://openadapt.ai/extensions/handoff-receipt/v1`, and the server echoes the
header when it activates the extension. Artifacts are the same either way.

**Artifact shape.** A run's task carries one artifact, `run_result`, with
`extensions: ["https://openadapt.ai/extensions/handoff-receipt/v1"]` and two
parts:

1. a `data` part (`application/json`) holding the run result. `outcome` and
   `safe_to_retry` are unchanged;
2. a `raw` part (`mediaType` `application/vnd.openadapt.handoff+json`,
   `filename` `<run_id>.handoff.json`) holding the handoff document's exact
   bytes.

The handoff travels as `raw` bytes, not as a `data` part, because a `data`
part is a protobuf `Value`, which stores every number as a double and would
change the digested bytes.

**Task states.**

| Outcome | Task state |
|---|---|
| `done` | `TASK_STATE_COMPLETED` |
| `needs_review` | `TASK_STATE_INPUT_REQUIRED` |
| Anything else | `TASK_STATE_FAILED` |

For `needs_review`, a person at the serving computer decides. A message on
that task only re-checks the run and never approves it.

**Verifier skill.** The `verify_handoff` skill accepts a message with either
part:

- a `raw` part of the handoff media type;
- a `data` part `{"skill": "verify_handoff", "handoff": {...}}`.

It returns the verdict as a `data` part artifact, `verdict`, marked with the
extension. Over a `data` part, whole-number doubles are restored to integers
before verifying. Send the `raw` part when the payload contains numbers that
are not whole.

## 7. Security considerations

- **Trust comes from the receiver.** Pin keys you chose. The default registry
  of `openadapt handoff verify` trusts only this computer's key and pinned
  OpenAdapt Cloud keys.
- **Replay.** A receipt can be re-sent. Bind it to the payload, as in
  section 4, step 3, or check the run identity yourself before acting.
- **Privacy.** A `record/run/v1` statement holds only digests, enums, counts
  and salted commitments. A handoff never carries the openings.
- **Transport.** `openadapt agent serve --a2a` listens on 127.0.0.1 by
  default and needs a bearer token on any other address. It refuses requests
  that carry a browser `Origin` or an unknown `Host`.

## 8. References

- A2A Protocol Specification 1.0 (latest release v1.0.1, 2026-05-28):
  <https://a2a-protocol.org/latest/specification/>. Proto:
  <https://github.com/a2aproject/A2A/blob/v1.0.1/specification/a2a.proto>.
- A2A extensions: <https://a2a-protocol.org/latest/topics/extensions/>.
- in-toto Attestation Framework, Statement v1:
  <https://github.com/in-toto/attestation/blob/main/spec/v1/statement.md>.
- DSSE v1.0.2, envelope and protocol (PAE):
  <https://github.com/secure-systems-lab/dsse/blob/master/protocol.md>.
