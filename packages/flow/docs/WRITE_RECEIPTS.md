# Write receipts

A write receipt is a signed, portable statement of one verifier verdict:
what OpenAdapt read, when, how strongly, what it concluded, and what it did
**not** check. A vendor can hand a receipt to a customer's compliance team or
an auditor, who checks it offline with one command:

```bash
openadapt-flow receipt verify write-1.receipt.cbor --issuer-key issuer.pub.pem --log-key log.pub.pem
```

Receipts sign the verifier's result. They don't sign the agent's claim about
its write. The IETF agent-receipt drafts sign the agent's claim; a write
receipt records what the system of record showed.

## What a receipt holds

The payload is canonical JSON (`openadapt.write-receipt/v1`). It holds closed
values, timestamps and digests only. It holds no record values, reasons,
URLs, hostnames or parameters.

| Field | Meaning |
|---|---|
| `verdict` | `CONFIRMED`, `REFUTED` or `UNCERTAIN`, as the verifier gave it ([VERIFIER_API.md](VERIFIER_API.md#one-outcome-vocabulary)). |
| `provisional`, `recheck_due_at` | A `CONFIRMED` that a later recheck can still overturn. |
| `checked` | The verify-run parts (`delta`, `frame`, `source`), each `held`, `refuted` or `not_checked`. |
| `scope` | The verifier's `Scope`: `read_paths`, `stores`, `unread`, `window` (baseline read, read-back, maximum window), `strength`, `not_covered`. |
| `not_checked` | The receipt's own plain list of what it says nothing about: the scope's `not_covered`, every unread store, and fixed lines that always apply (below). |
| `subject` | Opaque ids and digests: the write id, the contract's SHA-256, the verify-run receipt's SHA-256. |
| `evidence_commitment` | A salted SHA-256 over the verifier's evidence. The salt and evidence stay in a private file beside the receipt (mode 0600). The holder can open the commitment to an auditor; nobody else can test a guess against it. |
| `source` | `verifier`, `shadow.sql` or `shadow.fhir`. |
| `supersedes` | For a recheck's receipt: the id of the receipt it replaces (its leaf hash). |
| `issued_at`, `issuer` | When, and the issuer key's id. |

Every receipt's `not_checked` ends with these lines, whatever the verifier
said:

- writes that land after the read-back (or after the recheck, when one ran);
- stores and systems not listed in `scope.stores`;
- whether the write was authorized or clinically correct;
- anything the agent did that didn't go through the checked write.

## Fail closed: a receipt never claims more than the scope

`issue` refuses, and `receipt verify` rejects, a receipt where:

- `verdict` is `CONFIRMED` and the strength is `unchecked`, no store was
  read, a contract store went unread, a `checked` part is `refuted`, or the
  window has no read-back time;
- `verdict` is `CONFIRMED` with no baseline read, or (outside FHIR) without
  `delta` held;
- `not_checked` is empty or lacks the fixed lines, or, for `shadow.fhir`,
  the FHIR read path's gaps;
- the window runs backwards, starts or ends after the receipt was issued
  (300 s of clock skew allowed), or the recheck is due before the read-back;
- the bytes aren't the one canonical CBOR encoding (a re-encoding, a tag or
  trailing bytes would give the same receipt another digest);
- `provisional` and `recheck_due_at` disagree;
- any field is unknown, repeated, or outside its closed set.

An issue failure never changes a verdict. The verifier result says
`write_receipt: "not issued: <why>"`, as `seal` does today.

## Format: COSE_Sign1, SCITT shape

We follow the IETF SCITT architecture
(draft-ietf-scitt-architecture) and COSE receipts
(draft-ietf-cose-merkle-tree-proofs):

- **Signed Statement.** A COSE_Sign1 (RFC 9052), tag 18. Protected header:
  `alg` EdDSA (-8), `content type`
  `application/vnd.openadapt.write-receipt+json`, `kid` (the issuer key id),
  and `CWT_Claims` (label 15) with `iss` (issuer key id), `sub` (write id)
  and `iat`.
- **Transparency receipt.** The local log appends the signed statement and
  returns a COSE Receipt: a COSE_Sign1 with a detached payload (the tree
  root), protected header `vds` (395) = 1 (RFC9162_SHA256), and the
  inclusion proof in `vdp` (396). It is placed in the signed statement's
  unprotected header `receipts` (394), which makes the file a SCITT
  Transparent Statement.
- **Leaf and identity.** `SHA-256(0x00 || signed statement with an empty
  unprotected header)`, RFC 9162 hashing. The leaf hash is also the receipt's
  id: `supersedes` and Rekor anchors name a receipt by it. It covers only
  signed bytes, so nothing in the unsigned header (the log's proof) can give
  a receipt another name. The proof itself must be canonical CBOR too.

Signing builds the RFC 9052 `Sig_structure` with `cbor2` and signs through
the Seal key interface, so a file, keychain or PKCS#11 key works unchanged.
Verifying goes through python-cwt, an independent COSE implementation, and
then through our own strict checks.

**Library.** [python-cwt](https://github.com/dajiaji/python-cwt) 3.3.0
(July 2026), MIT. Its dependencies are `cbor2` (MIT), `cryptography`
(Apache-2.0 or BSD) and `pyhpke` (MIT). pycose (BSD-3) was the other
candidate; its last release was December 2023. The extra is
`openadapt-flow[receipts]`.

## Rechecks

A provisional `CONFIRMED` is superseded by the recheck's receipt
(`supersedes` names the first receipt's id): shadow mode issues it
after the recheck, and `issue_recheck_receipt(result, late)` does it for a
verifier `RecheckResult`. A recheck reads fewer stores than the check, so its
receipt keeps the check's window and adds a `not_covered` line naming what
was read again and until when. `receipt verify` warns when a provisional
receipt's recheck is past due: on its own it doesn't show the verdict held.

A receipt is never written over: issuing to an existing path is refused, so
a receipt already handed out keeps the evidence file that opens it.

## Keys

- **Issuer key:** the offline Seal key (`OPENADAPT_SEAL_KEY`: `file:`,
  `keychain:`, `pkcs11:`, or the generated key under `OPENADAPT_SEAL_HOME`).
  Reused from Offline Seals (#1392), so a customer-held key signs receipts
  too.
- **Log key:** a separate Ed25519 key, generated on first use under
  `$OPENADAPT_RECEIPT_HOME/log/` (default `~/.openadapt/receipts/log/`), mode
  0600.

Keys are generated locally and never printed. `openadapt-flow receipt keys
export DIR` writes the two **public** keys for a verifier to pin.
`receipt verify` needs both public keys and refuses without them: a receipt
checked only against a key it carries proves nothing about who issued it.

## Transparency log

The default log is local: an append-only file of leaf hashes under
`$OPENADAPT_RECEIPT_HOME/log/`, guarded by a file lock. It shows a receipt was
logged in order and not edited since. On its own it is the operator's own
log, so the operator could rebuild it.

For a party the operator doesn't control, `openadapt-flow receipt anchor FILE` puts the receipt's digest in Sigstore Rekor through the
same code that anchors Seals (`seal_anchor.anchor_record`). Only a SHA-512
digest of a domain-separated statement over the receipt's id and a
random nonce leaves the machine, plus the issuer's public key and a
signature. It's off by default, and refused in strict no-outbound mode.
`receipt verify --require-anchor` checks the Rekor inclusion proof and
checkpoint offline.

## Where receipts come from

- **Verifier API:** `verify_write(pending, write_receipt=True)`, or
  `issue_write_receipt(result)` on any `WriteResult`. The result carries
  `write_receipt` and `write_receipt_path`.
- **Shadow mode:** `openadapt-flow shadow ... --receipts`. One receipt per
  checked episode under `OUT/receipts/`. A recheck issues a second receipt
  with `supersedes`. FHIR episodes get a scope from `ensure_record`'s read
  path (searches, `_history`, `AuditEvent`).

## What a receipt proves, and what it doesn't

It proves that the holder of the issuer key stated this verdict over this
scope, and that the statement hasn't changed since the log (and, if
anchored, Rekor) recorded it. With the evidence file, it proves which
evidence the verdict was based on.

It doesn't prove that the issuer's verifier was correct, that the store it
read was the real system of record, that nothing happened outside the listed
stores or after the window, or that the write was the right thing to do.
`receipt verify` prints the `not_checked` list every time.

## Not in this version

- A remote SCITT transparency service (SCRAPI). The local log keeps the
  same receipt shape, so a service can be added later.
- Countersignatures and RFC 3161 timestamps on receipts.
- Receipts for `openadapt verify` CLI runs outside the verifier API.
