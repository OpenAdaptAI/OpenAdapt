# Workflow registry

Packaged, parameterized bundles for open-source apps. Fetch one with:

```bash
openadapt-flow pull --list
openadapt-flow pull openemr/new-referral
openadapt-flow skill qualify new-referral --verify my-verify.yaml --certify clinical-write
openadapt-flow run new-referral --params-file params.json
```

`pull` downloads the files listed in [`index.json`](index.json), checks each
file's sha256 against the index, and loads the bundle so its manifest digest is
checked as well. It doesn't run anything.

Before it downloads anything, `pull` checks who published the index:

- The registry release workflow attests `index.json` and every bundle
  archive with GitHub's keyless artifact attestations: a Sigstore signature,
  recorded in Rekor, over the sha256 of each file. The attestation is
  published beside the index as `index.sigstore.json`.
- `pull` verifies it against a pinned identity: issued by GitHub Actions to
  `release-registry.yml` on a `registry-*` tag of OpenAdaptAI/OpenAdapt. The
  attestation must name the exact index it read, and an archive must be one it
  names too. This needs the `registry` extra
  (`pip install 'openadapt-flow[registry]'`, Python 3.11 or later).
- Verification is offline, against the Sigstore trusted root shipped with the
  package. `openadapt-flow pull --refresh-trust` fetches the current one
  through Sigstore's TUF repository into `$OPENADAPT_HOME`.
- The attested index carries `issued_at`, `expires_at` and a `serial`. `pull`
  refuses an expired index, and one whose serial is lower than the highest it
  has seen from the release workflow at any address, mirror or local file
  (kept in `$OPENADAPT_HOME/registry-state.json`).
- A remote index is fetched over https only. For an index you are authoring
  on the local disk, `--allow-unsigned` takes one with no attestation, and
  `--allow-test-key` one attested by the local test signer
  (`openadapt_flow.registry_attest.sign_test`). Both are refused for a remote
  index.

A bundle holds only allowlisted files: the program, its screen templates
(crops of a copy holding made-up data, which each entry attests) and a minimal
record check. The full record check and the fault suites are in a private
tier. `skill qualify` fetches the full check with the token in
`OPENADAPT_REGISTRY_TOKEN`, from an https host the package names, and
otherwise qualifies with the minimal check. A bundle never holds
`verify.full.yaml`, `check_coverage.json` or a fault suite; the index
validator refuses them. A release archive also carries a `NOTICE` naming the
app's license.

A pulled skill doesn't run until it is qualified on your own instance. `pull`
writes `skill.lock.json` beside the bundle and expires any certification it
carries. `run`, `replay`, `emit-skill` and `emit-mcp` refuse it until you
have done this:

```bash
openadapt-flow skill bind new-referral --set APP_URL=https://your-test-copy.example \
    --set RECORD_TOKEN_VAR=MY_EMR_READ_TOKEN
openadapt-flow skill qualify new-referral --certify clinical-write
openadapt-flow skill status new-referral
```

`bind` takes addresses, the names of the environment variables that hold your
credentials (never the secrets themselves), local paths and local commands.
`qualify` runs the qualification cases with the skill's bundled record check
(`verify.yaml`) against your instance. A skill that ships no record check
takes your own with `--verify`; replacing one it ships takes
`--replace-contract` as well.

A shipped record check never runs code it downloaded and never reads your
environment:

- it names no Python hooks and no plugin;
- every address, credential name, path and the reset command in it is a
  `${VAR}` naming one of the skill's declared variables, so your binding
  decides where requests go and what runs;
- `${}` expands only against your binding.

A qualified skill runs only on the instance it was qualified on: `run`
checks the deployment's app address, and an emitted MCP server checks the URL
it is given. See
[`docs/design/SKILLS_REGISTRY.md`](../docs/design/SKILLS_REGISTRY.md).

| Workflow | App | Status |
|---|---|---|
| `openemr/new-referral` | OpenEMR 8.0.0.3 | example, not certified |

## Status labels

- `example`: compiled and run on a synthetic copy of the app. It hasn't passed
  `openadapt-flow certify`. Rehearse it on your own test copy first.
- `certified`: passed `certify` against the policy named in the entry's
  `certification` record. The index validator rejects this label without
  that record.
- `planned`: no files yet. A release refuses an index with a planned entry
  (`build_registry_index.py --release`), so planned entries stay out of
  `sources.json`.

## Releases

`.github/workflows/release-registry.yml` publishes the registry:
1. It builds each bundle as a reproducible archive.
2. It tags the commit `registry-<suffix>`. The tag push stamps the index
   with a 30-day expiry and attests the index and every archive, in the
   protected `release-identity` environment. Then it checks its own
   attestation with the pull verifier, using only hash-pinned wheels
   ([`verify-requirements.txt`](verify-requirements.txt)).
3. It releases the archives, the index and `index.sigstore.json` under that
   tag.
4. It opens a pull request with the attested index and its attestation.

There is no signing key to keep. The index expires 30 days after it is
stamped, so the workflow runs at least that often.

To release, dispatch the workflow from `main` with a plain suffix:

```bash
gh workflow run release-registry.yml --ref main -f suffix=2026-11-18
```

To renew the expiry of the index on `main` without changing it, dispatch a
re-stamp. It tags `registry-restamp-<YYYYMMDD>`, and the build refuses an
index that differs from the committed attested one in anything but
`issued_at`, `expires_at`, `serial` and the archive URLs (which name the tag):

```bash
gh workflow run release-registry.yml --ref main -f restamp=true
```

`pull` reads the index from `main`, so a release or re-stamp takes effect
when its pull request is merged. The workflow has a commented-out schedule
that runs re-stamps twice a month; it runs only once that block is
uncommented. A failed scheduled run or registry tag run opens a "Registry
release failed" issue.

After the first attested release, only that workflow writes `index.json`;
`build_registry_index.py --check` confirms it still matches `sources.json`.

## Adding a bundle

1. Put the bundle under `bundles/<app>/<task>/`. Use synthetic data only.
2. Add an entry to [`sources.json`](sources.json).
3. Run `python scripts/build_registry_index.py` and commit `index.json`.

Bundles live in the repository and are not part of the PyPI wheel or sdist.
Don't add files copied from an AGPL application; see `AGENTS.md`.

`openemr/new-referral` starts on OpenEMR's Patient > New form
(`/interface/new/new_comprehensive.php`), signed in, at 1280x800.
[`benchmark/openemr_local`](../benchmark/openemr_local) brings up a matching
synthetic copy. `openadapt-flow intake new-referral referral.pdf` can fill
`params.json` from a referral document.

## Catalog proof for consumers

Listing and pulling use one publisher-verified index snapshot. Local unsigned
source inspection also needs `--allow-unsigned`; it cannot establish a
published catalog. The CLI reports metadata from the same snapshot whose
entry was installed. Publisher high-water updates share a cross-process lock;
an unreadable existing rollback ledger is refused rather than reset.

A catalog consumer can retain a portable proof and verify it offline:

```bash
python -m openadapt_flow.registry_catalog export --index INDEX_URL --out index.proof.json
python -m openadapt_flow.registry_catalog verify index.proof.json
```

Export requires the production registry publisher. It retains the exact public
index and attestation bytes, plus indexed workflow and manifest JSON. Verification
rechecks the pinned publisher, expiry, serial, file hashes, sealed asset membership
and canonical program digests. Its summary distinguishes the entry digest,
`workflow.json` file hash, bundle content digest and workflow contract digest.
A catalog must match the latter identities to the signed evidence for that exact
program. A different program's Seal, a matching name or a caller's `verified`
field supplies no such evidence.

This proof grants no execution, admission or instance qualification. Existing
pulled-skill refusal still applies to emitted MCP tools and agent runs. The
committed example index is not an attested release; the proof command does not
sign, release, publish or configure a publisher.


Publisher acceptance and final bundle publication share the rollback-state lock.
Downloads are prepared in a private directory on the destination filesystem;
currentness is checked again before the prepared directory is published. A
newer listing or catalog observation cannot overtake an installation in that
critical section. Failed publication restores an existing destination, and an
unexpected destination is not overwritten without `--force`. The rollback
ledger uses the same unambiguous JSON decoder as the index: duplicate keys and
non-finite numbers refuse without resetting or rewriting the existing bytes.
These are artifact-currentness checks, not runtime or instance qualification.
