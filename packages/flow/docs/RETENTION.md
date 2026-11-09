# Retention and wrapping-key rotation

The maintenance Python APIs operate on explicitly supplied sealed sets. They
require caller-provided keys and pinned signing keys. They do not load a local
key, provision a key provider, retire a key, or scan unlisted storage.

`retention.plan_retention` reads a finalized encrypted run and verifies its
exact inventory, signed VERIFIED report, runtime-identity hash, and existing
complete RunReceipt contract. A directory label is not runtime-identity proof.
Unsigned runs and runs whose external checkpoints have not been finalized
refuse. External recording directories are outside this authorization.

`retention_apply.prepare_retention` rechecks that exact source and signs a
preparation capsule with an explicit signer. The preparation time starts the
policy interval, even when the original report is older. This local signed
time is an operator attestation, not an independent timestamp. Durations are
operator policy, not a legal-compliance preset.

The capsule retains the existing closed RunReceipt plus policy, counts, time
and digest commitments. Exact source paths, file identities and the original
report signature bytes are encrypted in its private authorization section.
Only the receipt projection is intended to be shared. Keep the original
signing public keys and the capsule protection key available for verification.

`apply_retention(..., commit=False)` returns a local preview by default.
Explicit commit requires the interval to have elapsed and writes the exact
signed capsule durably outside sealed ownership boundaries before claiming or
removing any payload. It retains the original sealed header unchanged. Normal sealed-set
readers continue to refuse the resulting incomplete raw set; this operation
does not rewrite an old signature to excuse missing files. Interruption may
leave an authorized file under a reserved claim name. Resume with the identical
capsule and audit path. Changed, extra, linked or replaced files refuse. Audit
capsules and source directories are never deleted by this API; audit retention
is a minimum policy deadline, not an automatic cleanup schedule.

`key_rotation.plan_rewrap` authenticates the complete set before wrapping its
existing data key under a distinct explicit passphrase and opaque key id.
`rewrap(..., commit=False)` is also a dry-run. Commit atomically replaces only
the header; payload ciphertext, encrypted index and authenticated set digest
remain unchanged. Encrypted header staging stays outside enclosing sealed sets,
so interruption before publication preserves the old readable inventory.
Cross-filesystem publication refuses. Format v1 remains readable; rewrapped
headers use v2 with a fresh nonsecret set identity. The v2 header excludes the
legacy data-key derivation salt and parameters, so the previous passphrase plus
current header cannot directly derive the data key. Its wrapping envelope has
its own fresh KDF salt and authenticates the set identity and key label.
A retained directory lock and header revision checks prevent stale cooperating
Flow writers from restoring an earlier wrapping header on the tested backend.
Stop other writers before maintenance; independent checkpoint stores and other
tools must stay quiescent. Saved historical headers, recovered data keys and
existing plaintext can still permit access to the unchanged ciphertext.
Rewrapping does not revoke or erase those copies, retire a key, or zeroize
existing memory.

`key_coverage` authenticates only the supplied roots with the supplied keys.
Legacy unlabelled sets, unavailable keys and damaged sets are uncovered.
Sets containing independently encrypted checkpoint stores are also uncovered:
outer ciphertext hashes do not establish new-key access to those stores.
Rewrapping a set does not rotate their separate keys. A partially rotated
batch can contain both generations. Complete coverage of an
explicit list does not prove that no other storage depends on an old key, and
it never authorizes key retirement.

Explicit maintenance commits require POSIX descriptor operations and directory
fsync support. Retention also requires Linux no-replace rename support; it
refuses platforms or filesystems without that operation. Tests use synthetic
sealed data and ephemeral keys. Process-exit and filesystem fault controls do not establish power-loss behavior on every
storage system. These APIs do not protect against a privileged hostile host.
Unlinking is not proof of SSD secure erasure or media sanitization. There is no
maintenance CLI, external KMS integration or automated audit-evidence purge in
this component.
