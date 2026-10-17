# Fleet drift patches

When a common application changes its UI (a payer portal moves a button, an
EHR renames a control), every tenant that automates it halts on the same
change. A fleet drift patch lets one tenant's governed repair reach the
others without any tenant's data leaving it:

1. A person on the source tenant approves a repair in the usual
   [repair lifecycle](REPAIR_LIFECYCLE.md).
2. If an admin has allowed export for that application family, the tenant
   sends the fleet hub a **signed contribution**: for each changed step, the
   old structural anchor (CSS selector, frame path, role, automation id) and
   the new one, plus the before and after application fingerprints. No text,
   no record value, no screenshot.
3. The hub verifies the contributor, records who sent it in an internal
   audit log, and re-signs the patch with a fleet distribution key. It
   distributes nothing for an application version until at least 3
   independent tenants have registered that version.
4. Another tenant on the same application version, with import allowed for
   that family, imports the patch. The import verifies the hub's signature
   chain and the fleet's revocation list, applies the anchor map to a copy of
   that tenant's own bundle, and registers the result as a `fleet_patch`
   candidate. Nothing activates on arrival.
5. The candidate runs automatically on that tenant's own qualification cases
   (healthy and fault).
6. The tenant's signed promotion policy decides. A patch can be promoted
   without a person only when a registered policy approver signed a policy
   with `auto_promote: true`, the patch changes no identity evidence, the
   canary verified on the tenant's own cases, the signature chain is valid,
   and a fresh signed revocation list revokes nothing in the chain. In every
   other case it waits for a person.

Package: `openadapt_flow/fleet/`. Tests: `tests/test_fleet_drift.py`.
CLI: `openadapt-flow fleet`. Simulator:
`python -m openadapt_flow.fleet.simulator --out <empty dir>`.

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
| Cloud governed repair review (openadapt-cloud #404) | `repair_candidates` and `repair_candidate_events` tables, `src/lib/repairReview.ts` | Append-only review audit with final decisions under row-level security. Cloud never applies a repair. Its `SOURCES` list was `heal`, `teach`, `manual`, `model_suggestion`. Draft PR openadapt-cloud #412 adds `fleet_patch` |

What was missing: a fingerprint of an application version that holds no
tenant data, a patch format that can cross tenants, signatures, a key
hierarchy with revocation, a way to
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

`fleet/patch.py`, schema `openadapt.fleet.structural-patch/v2`.

A tenant sends a `PatchContribution` to the hub:

- `payload` (`ContributionPayload`): the structural patch, the source prior
  and repaired bundle digests, and the time of contribution.
- `contribution_id`, `signature` and `chain`: the contributor's signature and
  certificate chain (purpose `contributor`).

The hub sends receivers a `SignedStructuralPatch`:

- `payload` (`StructuralPatchPayload`): workflow family, `app_before`,
  `app_after`, the anchor maps, and `created_at` truncated to the hour.
  There is no source pseudonym and no source bundle digest.
- `patch_id`: SHA-256 over the canonical payload.
- `signature`: Ed25519 over the domain-separated canonical payload.
- `chain`: the hub's distribution certificate chain.

Each `AnchorMap` has the old and new `StructuralAnchorRef` (selector, frame
path, role, automation id), `name_unchanged`, and
`identity_fields_changed`, which lists the *names* of identity fields the
source repair changed on that step (for example `context_text`). The values
stay with the source tenant.

### Redaction

Export refuses rather than redacts. A selector is accepted only if a
tokenizer can read every part of it as a tag, `#id`, `.class`, positional
pseudo-class, combinator, or an attribute test on one of the test-hook
attributes (`data-testid`, `data-test`, `data-test-id`, `data-cy`,
`data-qa`) or `name`, `type`, `role`, `for`. The sanitizer refuses:

- text engines (`text=`, `:has-text()`) and text or address attributes
  (`aria-label`, `title`, `placeholder`, `value`, `href`, `src`, `alt`);
- any token with four or more consecutive digits or eight or more hex
  characters;
- any id, class or attribute value that looks like a name (see the
  name-like rule below), such as `#patient-Jane-Doe`;
- the value of any `data-*` attribute that is not a test hook, such as
  `[data-patient-id=…]`. Values are refused rather than stripped, because
  stripping changes what the selector matches;
- a test-hook value that looks dynamic: a run of four or more digits, eight
  or more hex characters, a UUID, or a name-like value. A value is
  name-like when it is split by `-` or `_` and any part is a capitalised
  word (`patient-Jane-Doe`, `row_Smith`). `[data-testid=save-note]`,
  `[data-cy=runEligibility]` and `[data-qa=SaveButton]` are accepted;
- a query string, fragment, percent escape or URL (`?`, `&`, `%`, `://`, a
  leading `/`, `=/`) in a selector or in any frame path entry;
- class and id names a build tool generated: CSS modules
  (`Button_primary__3xK9a`), emotion (`css-1q2w3e`), styled-components
  (`sc-bdVaJa`), JSS (`jss12`, `makeStyles-root-12`), MUI, Svelte
  (`svelte-1x2y3z`), and a trailing segment that mixes letters and digits
  (`card-a1b2c`). These change on every release, so a patch that uses them
  would not survive the next one.

Accessible names and window titles are never exported; the receiving tenant
keeps its own recorded name when the source name did not change and clears
it otherwise.

After building the payload, export collects every data value in both
bundles: parameters and their examples and choices, typed text, identity
bands, structured identity, window titles, effect match values, API binding
values, identity-template salts and hashes, the digests the candidate
recorded, and the bundle digests themselves. It refuses if any value
appears as a word run in the patch's free-text fields (after splitting
camelCase, so `rowJaneDoe` reads as `row jane doe`), if a value of two or
more words appears anywhere with its spaces removed (`rowjanedoe`), or if
any digest appears anywhere in the patch. The test
`test_export_redaction_carries_no_fixture_value_or_digest` fills a bundle
with PHI-like values in each of those fields and checks that none of them,
in the spellings the test covers, and no digest of one, is in the signed
patch. The check only knows values the bundle recorded; a name that never
appeared in the bundle is caught only by the name-like rule.

Export also refuses a repair that is not approved by a person, a repair
approved under a policy, a repair that weakens the contract, an imported
fleet patch (the original patch is the one to share), a repair that adds
steps or changes only pixels, and any application family the admin has not
allowed for export.

## Keys and custody

`fleet/signing.py`. There are three levels of key:

| Level | Held by | Signs | Lifetime |
|---|---|---|---|
| Fleet root | Offline, M-of-N custodians (threshold at least 2) | Intermediate certificates only | Years |
| Intermediate | The hub, in an HSM in production | Signer certificates | At most 400 days |
| Signer | The hub (distribution, status) or a tenant (contributor) | Patches, contributions, status | At most 7 days |

Each signer certificate has one purpose: `contributor` (a tenant signing a
contribution), `distribution` (the hub signing a patch for receivers), or
`status` (the hub signing revocation lists and kill switches). Contributor
and distribution certificates list the application families they cover.
`verify_chain` checks, in order: the root is pinned in the tenant's
`FleetTrust`, the intermediate's signature and validity, the signer
certificate's signature, validity, purpose and family, and revocation of
the root, intermediate or signer key and of the certificate serial. Any
failure raises `PatchSignatureError` and nothing is written. A contributor
certificate cannot sign a distributed patch, and a distribution certificate
cannot sign a revocation list.

`KeyCustody` is the interface for where a private key lives: it exposes the
public key and a `sign` method, never the private key. `SoftwareKeyCustody`
keeps the key in process and is used by tests and the simulator. An HSM
adapter would implement the same interface; none exists yet.
`OfflineRootCustody` wraps the root key and refuses to issue an intermediate
unless at least `threshold` distinct registered custodians approved it. The
certificate records the ceremony ID and the approving custodians.

### Root ceremony procedure

The code only checks that a quorum is recorded. The procedure the code
assumes:

1. **Generate.** The root key is generated inside an HSM that never exports
   it, or on an air-gapped machine that is then destroyed after the key is
   split. Use Shamir or the HSM's own M-of-N scheme, with at least 3
   custodians and a threshold of at least 2. Custodians should not report to
   the same person.
2. **Store.** Each custodian keeps their share or HSM card in a separate
   safe. Record who holds which share. The root public key and key ID are
   published to tenants, who pin them.
3. **Ceremony.** To issue or rotate an intermediate, at least the threshold
   number of custodians meet, in person or over a recorded call with
   hardware tokens. One person reads the intermediate's public key, purposes
   and validity aloud; each custodian checks them against the request before
   approving. The ceremony gets an ID.
4. **Log.** Write a ceremony record: date, ceremony ID, custodians present,
   the intermediate key ID, its purposes and validity, and the hash of the
   signed certificate. Each custodian signs the record. Keep it with the
   audit evidence.
5. **Rotate.** Issue a new intermediate before the old one expires (at most
   400 days) and whenever a hub host changes. Signer certificates last at
   most 7 days, so the hub renews them from the intermediate and a lost
   signer key expires within a week even if revocation fails.
6. **Compromise.** If an intermediate is compromised, add its key ID to the
   revocation list (signed by a status certificate from a different
   intermediate), issue a kill switch for any patch it signed, and hold a
   ceremony to issue a replacement. If the root is compromised, every tenant
   must remove the pin by hand; there is no automatic recovery.

## Fleet status: revocation list and kill switch

`fleet/status.py`. The hub publishes two kinds of signed status, both signed
with a `status` certificate:

- **Revocation list** (`SignedRevocationList`): a sequence number, the
  revoked key IDs and certificate serials, `issued_at`, and `next_update`,
  which is at most 24 hours later.
- **Kill switch** (`SignedKillSwitch`): a sequence number, whether
  auto-promotion is disabled fleet-wide, the patch IDs to roll back, and a
  reason (`contributor_revoked`, `key_compromise`, `bad_patch`, `incident`,
  `resume`). The latest entry decides whether auto-promotion is disabled;
  rollback IDs from every entry accumulate.

`read_fleet_status` never raises. It returns a `FleetStatus` with a list of
problems. Any of these makes it not fresh: no list, a list that does not
verify, a list signed by anything but a status certificate, a list past
`next_update`, or a list older than the policy's `max_status_age_hours`
(at most 24). A kill-switch entry that is malformed or does not verify is
also a problem. Each entry is parsed and verified on its own, so a bad
entry does not hide the rollback IDs in the others.

Auto-promotion fails closed. Before reviewing under a policy, and again
before approving, `process_imported_patch` reads status. If status is not
fresh, if it revokes any key or serial in the patch's chain, or if a kill
switch disabled auto-promotion, the candidate waits for a person
(`needs_person`). A patch named by a kill switch is rejected (`blocked`).
`apply_fleet_kill_switches` rolls back named patches already in `canary` or
`active`, which restores the prior bundle, and rejects them in earlier
states. A person can still review and approve a candidate when status is
stale; only automatic promotion stops.

**Replay protection.** Each tenant keeps `status-state.json` in its fleet
directory (`StatusHighWater`), with the highest revocation-list sequence and
the highest kill-switch sequence it has accepted. `read_fleet_status`
refuses a revocation list with a lower sequence than the recorded one, and
refuses kill-switch history whose newest entry is lower than the recorded
one, which is what a feed that dropped a recent kill switch looks like. It
raises the recorded numbers only for status it accepts, and never lowers
them. The file is replaced atomically. If it exists but cannot be read or
parsed, or cannot be written, status is not fresh and auto-promotion stops
until an admin repairs it; the damaged file is not overwritten. A missing
file means the tenant has accepted nothing yet. `process_imported_patch`
requires the state (`status_state`); without it, the candidate waits for a
person. `openadapt-flow fleet status` shows the recorded sequences.

## Fleet hub

`fleet/hub.py`, `FleetHub`.

**Re-signing.** `accept` verifies a contribution's chain (purpose
`contributor`), refuses a contributor whose certificate subject has been
revoked, and refuses a contributor that has not registered the base
fingerprint itself. It then writes an entry to `audit/contributions.jsonl`
with the contribution ID, the contributor's certificate subject, serial and
key IDs, and the source bundle digests. Only the hub reads this log; it is
how the hub finds every patch from a contributor when that contributor is
revoked (`revoke_contributor` returns a new revocation list and a kill
switch naming those patches). The distributed patch is signed with the
hub's distribution certificate, has `created_at` truncated to the hour, and
carries nothing from the contribution's envelope. Two patches from the same
tenant share no field that names the source. A receiver can still guess
from what a patch contains (the same selector style, for example) or from
when it appears in the feed; the hub publishes as soon as it accepts, and
does not batch.

**Three-tenant rule.** Tenants register the fingerprints they run with
`register_fingerprint`, signed with their contributor certificate. The hub
distributes a patch only when at least `MIN_INDEPENDENT_TENANTS` (3)
distinct contributor certificate subjects have registered the patch's
`app_before` fingerprint. Revoked subjects do not count. Earlier
contributions are held, and
`release_held` distributes them once the third tenant registers. The hub
maps each contributor certificate subject to an organisation at onboarding,
so two subjects of one organisation do not count as two tenants. This keeps
a patch for an application only one or two tenants run, which could reveal
who they are, from leaving the hub.

## Import

`fleet/intake.py`, `import_patch`:

1. Checks that the admin allowed import for the patch's application family,
   or for the tenant's local origin host. The origin host is read locally
   and never sent anywhere.
2. Verifies the distribution chain, then checks fleet status: a revoked
   chain raises `PatchSignatureError`; a patch named by a kill switch
   raises `PatchImportError`.
3. Checks that the tenant's own fingerprint matches `app_after`.
4. Finds the tenant's steps whose structural locator equals an old anchor
   exactly, copies the tenant's bundle, and replaces only those structural
   locators. Templates, identity bands, identity templates, effects and
   parameters are the tenant's own and are not touched.
5. Registers the copy with `build_candidate(source="fleet_patch")`, so the
   binding digests and identity flags come from the same code as every other
   candidate, and records `FleetPatchProvenance`: patch ID, family,
   fingerprint, the distribution signer's key ID and serial, the
   intermediate and root key IDs, whether the patch flags identity
   evidence, the patched step IDs, and when the chain was verified.

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

## Promotion policy and roles

`fleet/policy.py`, `FleetPromotionPolicy` (schema v2):

- `approver`: the person ID, name and title of the policy approver.
- `security_official`: the name and title of the organisation's security
  official, when they authorised the policy, and a fixed attestation
  statement: "Our organisation's security official authorised this fleet
  promotion policy, including its auto_promote setting." The official must
  be a different person from the approver.
- `approved_at`, `auto_promote`, the application families it covers, the
  minimum healthy and fault case counts, the live canary window length, and
  `max_status_age_hours`.

The policy is signed (`SignedPromotionPolicy`) with the approver's key. A
local role registry, `roles.json`, lists workflow authors and policy
approvers with their public keys. The two lists must not overlap, so a
person the admin lists as a workflow author cannot also be listed as an
approver. The code checks the lists, not who actually recorded or repaired
a workflow; keeping `roles.json` accurate is the admin's job. `verify_policy`
refuses a policy whose signer is not a
registered approver, whose approver is also an author, or whose signature
does not match. An unverified policy never auto-promotes.

`process_imported_patch` always runs the canary, then returns one of:

- `blocked`: a campaign failed, or a kill switch names the patch. The
  candidate cannot be approved by anyone until it passes; a killed patch is
  rejected.
- `needs_person`: the policy does not verify, `auto_promote` is off, the
  policy does not cover the family, the patch changes identity evidence, the
  contract is weaker, there were fewer cases than the policy requires, or
  fleet status is not fresh, revokes the chain, or has auto-promotion
  disabled. The candidate waits in the normal lifecycle for a person.
- `auto_promoted`: the store reviewed and approved it under the policy, staged
  it, and started the live canary window. The live window still reverts the
  pointer automatically on any regression.

The rule is enforced in `RepairStore` itself, not only in the policy module:
`review_under_policy` and `approve_under_policy` accept only `fleet_patch`
candidates with no identity change and no weakening, and approval also
requires both campaigns passed on cases of kind `qualification_case`. The
approval record carries `pre_approval_policy_sha256` and `pre_approved_by`
("Name (Title)" of the approver), so the audit shows which policy, and
whose, promoted the repair.

## Opt-in and the fleet CLI

Export and import are off by default. `fleet/optin.py` reads
`~/.openadapt/fleet/opt-in.json` (or the directory in `OPENADAPT_FLEET_DIR`
or `--fleet-dir`). It has separate export and import allowlists. Each entry
is an application family (`sim-payer/portal`) or a domain, with the admin
who added it and when. A missing file means both lists are empty.

`openadapt-flow fleet`:

| Command | What it does |
|---|---|
| `status [--json]` | Shows the allowlists, roles, policy, recorded status sequences and trust settings |
| `allow --export\|--import --app-family F\|--domain D --admin NAME` | Adds an allowlist entry |
| `disallow ...` | Removes one |
| `roles --author ID` / `roles --approver ID --public-key KEY` | Registers a workflow author or a policy approver; refuses a person in both roles |
| `policy keygen --out FILE` | Writes an approver key (PEM, mode 0600) and prints its public key |
| `policy sign --draft FILE --key FILE --approver-id ... --approver-name ... --approver-title ... --security-official-name ... --security-official-title ... --attest-security-official` | Signs a draft policy; refuses without the attestation flag or if the signer is not a registered approver |
| `policy verify [--policy FILE]` | Verifies the signed policy against the role registry |
| `simulate --out DIR` | Runs the simulator |

There are no `fleet export` or `fleet import` commands yet; those go through
the API.

## Distribution

`fleet/feed.py`. `PatchFeed` is the interface: `publish(patch)` and
`list_patches(app_family, app_fingerprint_sha256)`. `FilePatchFeed` stores
one `<patch_id>.json` per patch in a directory and skips malformed files. It
also stores the revocation list and numbered kill-switch files under
`status/`. `CloudPatchFeed` is a stub: listing raises `NotImplementedError`
and its status methods raise `FleetStatusUnavailable`, so a tenant pointed at
it never auto-promotes. There is no server.

## Simulator

`fleet/simulator.py` runs three tenants and a hub against a simulated payer
portal. Each tenant has its own bundle, member data, system of record,
contributor key and certificate, opt-in file, role registry and signed
policy; `north` and `east` allow auto-promotion and `west` does not. Every
run uses the real `Replayer` under the Standard execution profile, with
independent effect verification against the tenant's system of record and an
identity band on each click.

The scenario: the portal moves from 7.1 to 7.2 and renames two controls. All
three tenants register 7.1 with the hub. `north` halts, a person repairs and
approves, and `north` sends the contribution to the hub, which re-signs and
publishes it. `east` imports it; the canary verifies 3 healthy cases and
refuses 3 faults (wrong member on the row, duplicate target, missing
control); the revocation list is fresh; the policy promotes it; the live
canary completes to `active`. `west` is still on 7.1, so the import is
refused; after `west` upgrades, the import and canary succeed and the
candidate waits for a person because `west`'s policy is off. Finally the hub
issues a kill switch for the patch: `east` rolls back to its prior bundle and
`west` rejects the candidate. Every replay in the scenario makes zero model
calls.

The simulator keeps its durable-run records in a database under the output
directory rather than in `~/.openadapt/durable-authority`.

## Limits

- Encrypted bundles cannot be patched yet: import reads the bundle as plain
  files.
- Only structural locators are remapped. A repair that changes pixels only,
  adds steps, or changes effects is not exportable.
- `RepairStore.review_under_policy` and `approve_under_policy` check the
  candidate, but trust their caller for the policy: a `PolicyPreApproval` is
  a digest and a name, and the store does not verify the policy signature
  or fleet status itself. `process_imported_patch` does both. Any code
  that runs in the tenant's process could call the store directly.
- If fleet status goes stale between the first check and the approval, the
  candidate stays reviewed under the policy and waits for a person to
  approve it. That person's approval is the only human step.
- Two processes updating `status-state.json` at once can race; the file is
  replaced atomically but not locked.
- There is no HSM adapter and no hub server. The hub runs in process over a
  file feed.
- The security official's authorisation is asserted by the approver, not
  signed by the official. Planned follow-up: the security official
  co-signs the policy with their own key, registered in the role registry,
  and `verify_policy` checks both signatures.
- The role registry is a local admin file and is not signed.
- The name-like check is a heuristic. A single PascalCase word such as
  `[data-qa=Smith]` cannot be told apart from a control name and is
  accepted; the export leak check still refuses it if the bundle's own data
  contains that word.
- The code counts distinct contributor certificate subjects as independent
  tenants. That two subjects are two organisations rests on the onboarding
  mapping, not on anything the code can check.

## Open privacy and legal questions

These need answers from counsel and compliance before any patch crosses a
real tenant boundary:

- **Is a structural patch PHI-derived data?** It is computed from bundles
  recorded on screens that showed PHI. The code keeps text and values out,
  but whether selectors and page-structure digests derived from a covered
  entity's environment are covered by its BAA, and whether a Business
  Associate may share them with other customers, needs a legal reading of
  each BAA.
- **Consent to share.** Export is now opt-in per application family or
  domain. Whether that opt-in is enough under each tenant's contract, and
  who at the tenant may give it, is open.
- **Tenant-specific customizations.** A DOM skeleton or selector can reveal
  that a tenant runs a custom module or configuration, which may itself be
  confidential.
- **Hub audit log.** The hub can link every patch to its source. Who may
  read `audit/contributions.jsonl`, and how long it is kept, is open.
- **Root key custodians.** Who the custodians are, and how a tenant learns
  its own contributor key was compromised.
- **Liability.** If a promoted patch causes a wrong write on a receiving
  tenant, which party is responsible: the source tenant, us, or the tenant
  whose policy promoted it?
- **Policy authority.** Which roles may act as policy approver and as
  security official at each tenant. (A co-signature by the security
  official is a planned follow-up; see Limits.)
