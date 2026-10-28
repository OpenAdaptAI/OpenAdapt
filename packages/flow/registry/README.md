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

Before it downloads anything, `pull` checks the index against keys that ship
with the package (`openadapt_flow/registry_keys.json`), never keys it fetches:

- The index is signed as a whole (Ed25519ph, the form Rekor accepts), and so
  is each entry.
- The index carries `issued_at`, `expires_at` and a `serial`. `pull` refuses
  an expired index, and one whose serial is lower than the highest it has
  seen from that address (kept in `$OPENADAPT_HOME/registry-state.json`).
- A remote index must have a Rekor anchor (`index.anchor.json`) naming its
  exact digest, signed by a registry key.
- A remote index is fetched over https only. `--allow-unsigned` takes an
  unsigned index from the local disk only, for one you are authoring.

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
| `openimis/claim-intake` | openIMIS | planned |

## Status labels

- `example`: compiled and run on a synthetic copy of the app. It hasn't passed
  `openadapt-flow certify`. Rehearse it on your own test copy first.
- `certified`: passed `certify` against the policy named in the entry's
  `certification` record. The index validator rejects this label without
  that record.
- `planned`: no files yet.

## Releases

`.github/workflows/release-registry.yml` publishes the registry:
1. It builds each bundle as a reproducible archive.
2. It signs every entry and the index, with a 30-day expiry, in the protected
   `registry-signing` environment. That job installs only hash-pinned wheels
   ([`signing-requirements.txt`](signing-requirements.txt)).
3. It anchors the signed index in Rekor (`index.anchor.json`).
4. It releases the archives under a `registry-*` tag.
5. It opens a pull request with the signed index and its anchor.

The index expires 30 days after it is signed, so the workflow runs at least
that often.

After the first signed release, only that workflow writes `index.json`;
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
