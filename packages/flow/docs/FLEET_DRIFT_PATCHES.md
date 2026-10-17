# Fleet drift patches

When a common application changes its UI (a payer portal moves a button, an
EHR renames a control), every tenant that automates it halts on the same
change. A fleet drift patch lets one tenant's governed repair reach the
others without any tenant's data leaving it:

1. A person on the source tenant approves a repair in the usual
   [repair lifecycle](REPAIR_LIFECYCLE.md).
2. The tenant exports a **signed structural patch**: for each changed step,
   the old structural anchor (CSS selector, frame path, role, automation id)
   and the new one, plus the source bundle digests and the before and after
   application fingerprints. No text, no record value, no screenshot.
3. Another tenant on the same application version imports the patch. The
   import verifies the signature chain, applies the anchor map to a copy of
   that tenant's own bundle, and registers the result as a `fleet_patch`
   candidate. Nothing activates on arrival.
4. The candidate runs automatically on that tenant's own qualification cases
   (healthy and fault).
5. The tenant's promotion policy decides. A patch can be promoted without a
   person only when the tenant pre-approved that, the patch changes no
   identity evidence, the canary verified on the tenant's own cases, and the
   signature chain is valid. In every other case it waits for a person.

Package: `openadapt_flow/fleet/`. Tests: `tests/test_fleet_drift.py`.
Simulator: `python -m openadapt_flow.fleet.simulator --out <empty dir>`.

## What existed before this package

| Piece | Where | What it gives fleet patches |
|---|---|---|
| Governed repair lifecycle | `openadapt_flow/repair/` and [REPAIR_LIFECYCLE.md](REPAIR_LIFECYCLE.md) | The state machine, `RepairStore` (the only writer), human review and approval, approval bound to exact content digests, staging by digest, the atomic `ACTIVE.json` pointer, and a live canary window that reverts on regression |
| Candidate registration | `repair/registration.py` | `build_candidate` digests each changed binding and marks identity fields (`IDENTITY_FIELDS` from `runtime/healing/patch.py`); `enforce_contract_invariants` refuses a weakened contract |
| In-run heals and `--save-healed-to` | `runtime/replayer.py`, `runtime/healing/` | Each heal writes `heals/<step>/patch.json` and a screenshot; `--save-healed-to` writes the healed bundle and registers a `heal` candidate |
| Repairs taught by an agent (#504) and branches from several halts or demos (#514, #521) | `learning/teach.py`, `agent_teach` | More ways to produce a candidate a person then approves. A fleet patch can come from any of them |
| Cloud review projection (#502) | `repair/cloud_review.py` | A digest-only projection of a candidate for review in Cloud. A Cloud approval counts as the human review step only |
| Web session identity (#498) | `qualification_web_session.py`, `QualificationEnvironmentObservation` | The target kind, the application's origin as its identity, the confirmed application version, and a session digest |
| DOM page-structure identity (#503) | `_FIELD_GROUP_IDENTITY_JS` in the Playwright backend | Reads label text and other controls' values into `structured_identity`. This is identity evidence and record data, so the fingerprint does not use it |
| Workflow registry and pull (#517) | `registry.py` | Shared workflow ids (`availity/eligibility-check`), per-file SHA-256 on pull, statuses `example`, `certified`, `planned`. A patch names a registry workflow family, never a tenant's bundle name |
| Cloud governed repair review (openadapt-cloud #404) | `repair_candidates` and `repair_candidate_events` tables, `src/lib/repairReview.ts` | Append-only review audit with final decisions under row-level security. Cloud never applies a repair. Its `SOURCES` list is `heal`, `teach`, `manual`, `model_suggestion`; it would reject a `fleet_patch` candidate until that is added |

What was missing: a fingerprint of an application version that holds no
tenant data, a patch format that can cross tenants, signatures, a way to
apply a patch to a different tenant's bundle, an automatic canary on that
tenant's own cases, and a promotion rule that is not a person.

## Application-version fingerprint

`fleet/fingerprint.py`, `AppVersionFingerprint`:

- `app_family`: a registry-style name such as `sim-payer/portal`.
- `target_kind`: `web`, `desktop`, and so on, from the #498 observation.
- `version_sha256`: a domain-separated digest of the family and the version
  the qualification environment confirmed.
- One or more structural digests:
  - `dom_skeleton_sha256`: element tags, the values of `type` and `role`, and
    sorted attribute *names*. Text, attribute values (ids, hrefs, values),
    scripts and styles are dropped, `data-*` names that contain digits are
    collapsed, and runs of identical siblings collapse to one, so the
    number of records on a page does not change the digest.
  - `window_hierarchy_sha256`: control types and class names only. Window
    titles and automation ids are dropped.
  - `executable_sha256`: the application binary.

`fingerprint_from_observation` builds it from the existing #498 observation
and leaves out the origin and session digest: an origin such as
`https://clinic-name.portal.example` names the tenant.

Two fingerprints match when the family, kind and version digest are equal and
at least one structural component they both carry is equal. A tenant on a
different version, or on the same version with a different page structure,
does not receive the patch.

## Patch format

`fleet/patch.py`. A `SignedStructuralPatch` holds:

- `payload` (`StructuralPatchPayload`): workflow family, the source prior and
  repaired bundle digests, the source tenant's pseudonym, `app_before`,
  `app_after`, the anchor maps, and a timestamp.
- `patch_id`: SHA-256 over the canonical payload.
- `signature`: Ed25519 over the domain-separated canonical payload.
- `certificate`: the signer's certificate.

Each `AnchorMap` has the old and new `StructuralAnchorRef` (selector, frame
path, role, automation id), `name_unchanged`, and
`identity_fields_changed`, which lists the *names* of identity fields the
source repair changed on that step (for example `context_text`). The values
stay with the source tenant.

### Redaction

Export refuses rather than redacts. A selector is accepted only if a
tokenizer can read every part of it as a tag, `#id`, `.class`, positional
pseudo-class, combinator, or an attribute test on one of `data-testid`,
`data-test`, `data-qa`, `data-cy`, `name`, `type`, `role`, `for`. Text
engines (`text=`, `:has-text()`), text attributes (`aria-label`, `title`,
`placeholder`, `value`) and any token with four or more consecutive digits or
eight or more hex characters are refused, because those are how a record
key or a name gets into a selector. Accessible names and window titles are
never exported; the receiving tenant keeps its own recorded name when the
source name did not change and clears it otherwise.

After building the payload, export collects every data value in both
bundles: parameters and their examples and choices, typed text, identity
bands, structured identity, window titles, effect match values, API binding
values, identity-template salts and hashes, and the digests the candidate
recorded. It refuses if any value appears as a word run in the patch's
free-text fields, or any digest appears anywhere in the patch. The test
`test_export_redaction_carries_no_fixture_value_or_digest` fills a bundle
with PHI-like values in each of those fields and checks that no value, in any
spelling, and no digest of one, is in the signed patch.

Export also refuses a repair that is not approved by a person, a repair
approved under a policy, a repair that weakens the contract, an imported
fleet patch (the original patch is the one to share), and a repair that adds
steps or changes only pixels.

## Signature chain

`fleet/signing.py`. A fleet root key issues each tenant's signer a
`SignerCertificate`: the tenant's pseudonym, the signer's public key, the
application families it may sign for, a validity window, and the root's key
id. Each tenant pins the roots it trusts in `FleetTrust` and can list revoked
signer key ids. `verify_patch` checks, in order: the root is pinned, the
certificate signature, the validity window, revocation, that the
certificate covers the patch's application family, that the patch names the
certificate's tenant, that `patch_id` matches the payload, and the patch
signature. Any failure raises `PatchSignatureError` and nothing is written.

## Import

`fleet/intake.py`, `import_patch`:

1. Verifies the signature chain.
2. Checks that the tenant's own fingerprint matches `app_after`.
3. Finds the tenant's steps whose structural locator equals an old anchor
   exactly, copies the tenant's bundle, and replaces only those structural
   locators. Templates, identity bands, identity templates, effects and
   parameters are the tenant's own and are not touched.
4. Registers the copy with `build_candidate(source="fleet_patch")`, so the
   binding digests and identity flags come from the same code as every other
   candidate, and records `FleetPatchProvenance` (patch id, digests,
   fingerprint, signer and root key ids, source pseudonym, whether the patch
   flags identity evidence, and the patched step ids).

If anything fails, the copy is removed and the store is unchanged.

## Canary on the tenant's own cases

`fleet/canary.py`. A tenant supplies a `TenantCanaryRunner` that runs one of
its `QualificationCase`s against a bundle. `canary_patch` runs every case
and records the results as the candidate's replay campaign (healthy cases)
and fault campaign (fault cases), with case labels that are digests of the
case ids. A healthy case passes only if the run is `VERIFIED` with zero model
calls and zero heals. A fault case passes only if the run did not succeed,
made zero model calls and observed no written effect. A campaign with no
cases fails.

## Promotion policy

`fleet/policy.py`, `FleetPromotionPolicy`: who approved the policy and when,
`auto_promote`, the application families it covers, the minimum healthy and
fault case counts, and the live canary window length. Its SHA-256 is
recorded on any approval it grants.

`process_imported_patch` always runs the canary, then returns one of:

- `blocked`: a campaign failed. The candidate cannot be approved by anyone
  until it passes.
- `needs_person`: `auto_promote` is off, the policy does not cover the family,
  the patch changes identity evidence, the contract is weaker, or there were
  fewer cases than the policy requires. The candidate waits in the normal
  lifecycle for a person.
- `auto_promoted`: the store reviewed and approved it under the policy, staged
  it, and started the live canary window. The live window still reverts the
  pointer automatically on any regression.

The rule is enforced in `RepairStore` itself, not only in the policy module:
`review_under_policy` and `approve_under_policy` accept only `fleet_patch`
candidates with no identity change and no weakening, and approval also
requires both campaigns passed on cases of kind `qualification_case`. The
approval record carries `pre_approval_policy_sha256` and `pre_approved_by`
(the person who approved the policy), so the audit shows which policy, and
whose, promoted the repair.

## Distribution

`fleet/feed.py`. `PatchFeed` is the interface: `publish(patch)` and
`list_patches(app_family, app_fingerprint_sha256)`. `FilePatchFeed` stores
one `<patch_id>.json` per patch in a directory and skips malformed files.
`CloudPatchFeed` is a stub that raises `NotImplementedError`; there is no
server.

## Simulator

`fleet/simulator.py` runs three tenants against a simulated payer portal.
Each tenant has its own bundle, member data, system of record, signing key and
policy; `north` and `east` allow auto-promotion and `west` does not. Every
run uses the real `Replayer` under the Standard execution profile, with
independent effect verification against the tenant's system of record and an
identity band on each click.

The scenario: the portal moves from 7.1 to 7.2 and renames two controls.
`north` halts, a person repairs and approves, and `north` exports the patch
to the file feed. `east` imports it; the canary verifies 3 healthy cases and
refuses 3 faults (wrong member on the row, duplicate target, missing control);
the policy promotes it; the live canary completes to `active`. `west` is
still on 7.1, so the import is refused; after `west` upgrades, the import and
canary succeed and the candidate waits for a person because `west`'s policy
is off. Every replay in the scenario makes zero model calls.

The simulator keeps its durable-run records in a database under the output
directory rather than in `~/.openadapt/durable-authority`.

## Limits

- Encrypted bundles cannot be patched yet: import reads the bundle as plain
  files.
- Only structural locators are remapped. A repair that changes pixels only,
  adds steps, or changes effects is not exportable.
- There is no CLI subcommand; the API and simulator are the entry points.
- Cloud review (#404) does not accept `source = 'fleet_patch'` yet.
- The policy is a local file the tenant's own process trusts. It is hashed
  into the approval but not signed.

## Open privacy and legal questions

These need answers from counsel and compliance before any patch crosses a
real tenant boundary:

- **Is a structural patch PHI-derived data?** It is computed from bundles
  recorded on screens that showed PHI. The code keeps text and values out,
  but whether selectors and page-structure digests derived from a covered
  entity's environment are covered by its BAA, and whether a Business
  Associate may share them with other customers, needs a legal reading of
  each BAA.
- **Consent to share.** Does a tenant's contract allow us to export
  structure from its environment to other tenants, and does it need an
  explicit opt-in per application family?
- **Tenant-specific customizations.** A DOM skeleton or selector can reveal
  that a tenant runs a custom module or configuration, which may itself be
  confidential.
- **Pseudonym linkability.** The source pseudonym is stable so certificates
  can be revoked, which also lets every receiver link patches to one source.
- **Root key custody and revocation.** Who holds the fleet root key, how
  revocation reaches tenants, and how a tenant learns its own key was
  compromised.
- **Liability.** If a promoted patch causes a wrong write on a receiving
  tenant, which party is responsible: the source tenant, us, or the tenant
  whose policy promoted it?
- **Policy authority.** Whether the person who approves an auto-promotion
  policy must hold a specific role, and whether the policy must be signed or
  countersigned.
