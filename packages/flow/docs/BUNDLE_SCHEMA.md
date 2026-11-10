# Bundle schema and sealed identity

`Workflow.load` checks the original bundle's manifest-free canonical payload
before upgrading its schema or constructing the current model. Each supported
schema has one canonical projection. A digest mismatch refuses the bundle.
Schema 2's reviewed default omissions apply only at explicit IR ownership
positions; arbitrary request bodies and mappings remain sealed content.

A missing schema version means legacy schema 1. Upgrades run one registered
step at a time on a copy. Malformed versions, future versions and missing
upgrade steps refuse loading. A breaking format change requires a schema bump,
a canonical format rule and an explicit upgrader.

Loading preserves the original files and content identity. The loaded object
retains the original canonical bytes plus a fingerprint of the upgraded model
and its integrity-manifest fields. Later verification checks both representations
and the sealed assets. Changing the workflow, asset hashes, integrity manifest
or asset bytes requires a new seal. Loading with `verify_integrity=False` does
not make a broken original seal pass a later integrity check.

Admission and authorization use `content_identity_digest` to retain a verified
original identity across model upgrades. It checks the loaded model, original
payload, integrity manifest and supplied asset hashes before returning that
identity. Callers checking live assets must hash their bytes or call
`verify_integrity` as well.

`Workflow.save(..., seal_manifest=True)` creates a seal over the current
serialization; this is also the default save behavior. Saving a loaded sealed
object with `seal_manifest=False` refuses. A format upgrade can change the new
seal's identity, so existing admissions must be evaluated against the new seal.
`compute_content_digest` continues to calculate current-format content for new
seals.

The frozen compatibility fixtures cover synthetic linear and program bundles
produced and self-verified by the pinned 2.2.0, 2.5.0 and 2.6.0 source releases.
Their original bytes and provenance were frozen before testing the current
loader. The fixture README records the historical environment limitations.
Current controls verify loading, integrity, tamper refusal and one exact input
in an owned synthetic headless Chromium page. Content digests provide integrity;
run authorization still applies its policy, identity and effect checks.
