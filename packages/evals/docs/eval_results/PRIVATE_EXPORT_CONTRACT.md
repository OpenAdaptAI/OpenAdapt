# Private-export contract (draft, not approved)

`import_files` refuses every file import until OpenAdapt approves one exact
private-export contract. This document is the template for that approval. It is
a draft. Nothing in it is approved, and the gate stays closed while the values
below are blank.

`PRODUCTION_READINESS.md` sets the bar the approval has to clear:

> The future approval must bind the complete payload schema and digest, the
> destination account, service, container, and prefix, the encryption-key
> identity, the immutable retention mode and period, the authorized importer
> workflow and ref, and the approval authority. A broad enable flag or approval
> of only the admission and campaign digests is not sufficient.

That bar was written when the store was an S3 bucket with Object Lock. The store
is now a signed git commit, so "container and prefix" reads as repository and
path prefix, and "immutable retention mode and period" reads as the transparency
log entry plus a recorded commitment. Every other clause holds unchanged. The
next section explains why the medium changed.

## Why an approval is needed at all, and what it fixes

The certificate already carries a `retention` block, and the importer already
validates its shape: every digest is well formed, the retention commit is an
exact 40-character hash, four verification booleans are true, and the chronology
runs `acceptance_verified_at <= retained_at` and no later than now.

Shape is not identity. Today the certificate supplies its own
`storage_identity_sha256`, `kms_key_identity_sha256`, and
`uploader_identity_sha256`, and the importer checks only that they look like
digests. A certificate that named some other repository, some other key, and
some other uploader would pass every check in the file.

The approval closes that. It supplies the expected digests from outside the
evidence, the way `--trusted-admission-signers` and
`APPROVED_CLOUD_ACCEPTANCE_COMMIT` already do for the qualification authority
and the Cloud commit. After approval the evidence can no longer select its own
destination, its own key, or its own uploader.

## What the store is, and why it is not S3

The retained evidence is a signed git commit in a private repository, not an
object in an S3 bucket with Object Lock. That choice was made deliberately,
before the first write, when changing it was still free.

The reasoning, kept here so nobody has to reconstruct it:

- The Cloud writer already encrypts the envelope client-side with an AES-256-GCM
  data key wrapped by KMS. The store holds ciphertext either way, so GitHub
  cannot read the evidence and confidentiality does not depend on the medium.
- What makes the claim credible to a reader is the signature chain plus the
  public Rekor entry, not the storage. Object Lock stops neither the suppression
  of an unpublished result nor the alteration of a published one; the signature
  and the transparency log already do both.
- Git is content-addressed. A commit hash covers the whole tree and history, so
  it binds the relationships between certificate, admission and campaign. A
  per-object lock binds only each blob.
- Object Lock does buy one thing git does not: if a third party is granted
  direct read access to the store, they can trust nothing was pruned before they
  looked. No buyer has asked for that. If one does, this decision should be
  revisited before the next write rather than patched around.

Object Lock in COMPLIANCE mode is also irreversible for its full term. Choosing
it commits storage for years and cannot be undone by anyone, including the
account root.

## The bindings

The approval fixes six destination fields plus the uploader, the importer, and
the authority. The store and its path are settled: `openadapt-evidence` exists
and is empty by design. The remaining blanks are AWS facts and your signature.

| Contract field | What it fixes | Value |
| --- | --- | --- |
| `destination.account_id` | the AWS account holding the KMS key | **TO BE SUPPLIED** |
| `destination.region` | the region of that key | **TO BE SUPPLIED** |
| `destination.repository` | the private repository holding the ciphertext | `OpenAdaptAI/openadapt-evidence` |
| `destination.ref` | the branch it lands on | `refs/heads/main` |
| `destination.path_prefix` | the path inside it | `production-acceptance` |
| `destination.kms_key_arn` | the key wrapping each data key | **TO BE SUPPLIED** |
| `destination.retention_commitment_days` | how long you commit to keep it | default 2555 |
| `uploader_arn` | the principal allowed to write | **TO BE SUPPLIED** |
| `importer_workflow_ref` | the one workflow allowed to import | pre-filled |
| `approval_authority`, `approved_at` | who approved, and when | **TO BE SUPPLIED** |

`retention_commitment_days` is a commitment the mechanism records and does not
enforce. A git commit has no expiry. Enforcing a number nothing can hold would
be theatre, so the verifier does not police it, and this document says so
plainly rather than leaving a reader to assume otherwise.

Two things the certificate carries are not fixed by the approval, because they
differ per export: the ciphertext, envelope and candidate digests, and the
commit and locator versions. The approval fixes where evidence may go and who
may put it there, not the content of any one export.

## Where the filled copy lives

This repository is public. The filled contract names an account, a repository, a
path prefix, a key ARN, and an uploader ARN, which are deployment-derived facts
and fall under the source-availability boundary.

So: the shape stays here, the values do not. The approved instance belongs in
`openadapt-internal`, and this repository receives only its digest, supplied to
the importer as an external control input beside the signer registry and the
approved Cloud commit.

## How to fill and approve one

1. Copy `docs/eval_results/private-export-contract.template.json` into
   `openadapt-internal`.
2. Replace every `FILL_` value and delete the `_README` key.
3. Check it against the mechanism:

   ```bash
   python scripts/import_production_acceptance.py \
     --private-export-contract <path> \
     --certificate <path> --campaign <path> \
     --qualification-admission <path> --attestation-bundle <path> \
     --expected-cloud-source-commit <sha> \
     --trusted-admission-signers <path> --output <path>
   ```

   The run still refuses at the import gate. Before it gets there it validates
   the contract, derives all three identity digests from your preimages, and
   checks that this process is the importer workflow the contract names. A
   contract error is reported before any private evidence is read.

4. Approve the filled copy in `openadapt-internal` and record its
   `contract_sha256` here.

## What the mechanism now enforces

The Cloud retention writer already computes these digests in
`scripts/retain-execute-private-evidence.mjs`. The importer derives the same
values from the contract, so one approval governs both repositories:

| Digest | Domain | Preimage |
| --- | --- | --- |
| `storage_identity_sha256` | `retention store` | `destination.repository` |
| `kms_key_identity_sha256` | `retention KMS key` | `destination.kms_key_arn` |
| `uploader_identity_sha256` | `AWS retention uploader` | `uploader_arn` |
| `destination_approval_sha256` | `Execute acceptance retention destination` | canonical JSON of all seven `destination` fields |

Every one is `sha256("OpenAdapt " + domain + " v1\0" + value)`. That separator
is not the one `opaque_binding_sha256` uses, which inserts the word
`acceptance`; the two differ by one word and produce different digests, and a
test pins them apart.

`destination_approval_sha256` is the value the Cloud writer already requires as
`EXECUTE_ACCEPTANCE_RETENTION_DESTINATION_APPROVAL_SHA256`. Approving this
contract produces it.

The contract carries values and never a digest, so an approval cannot assert a
hash whose input nobody can see. `verify_retention_against_contract` compares
all three identities against the certificate. `verify_importer_identity` requires `GITHUB_WORKFLOW_REF` to equal the
approved ref exactly. The destination is checked the way the Cloud writer checks
it, including that the KMS key lives in the approved account and region.
The certificate must also carry an exact 40-character retention commit and prove
that the push, the commit read-back, and the transparency-log entry were all
verified.

## Open items that block approval

1. ~~`provenance_attestation` names a route that cannot be produced.~~
   **Resolved.** The route is now `sigstore-public-good-slsa-provenance-v1`,
   held in one constant and referenced by the policy and both validators.
2. ~~The importer has no authorized-workflow check.~~ **Resolved.**
   `verify_importer_identity` refuses unless the process is the approved
   workflow and ref.
3. ~~The destination and key digests need a stated preimage.~~ **Resolved.**
   See the table above; a reviewer can recompute every value.
4. **The Cloud retention gate is still closed on purpose.**
   `tests/unit/execute-live-acceptance.test.mjs` forbids the signing step until
   this contract exists. That test should stay as it is until the approval
   lands, and it is the reason nothing on the Cloud side has moved.

Items 1 to 3 were code. Item 4 waits on this document being filled and approved.

## What approving this does not do

It does not set `production_acceptance: true` anywhere. It does not promote an
evidence set. It does not authorize exporting an admission or a campaign to this
repository. It opens exactly one path: `import_files` may compose four named
evidence inputs and derive one bounded result, under every check that is already
implemented.
