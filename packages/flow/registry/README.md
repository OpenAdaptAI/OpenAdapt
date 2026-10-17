# Workflow registry

Packaged, parameterized bundles for open-source apps. Fetch one with:

```bash
openadapt-flow pull --list
openadapt-flow pull openemr/new-referral
openadapt-flow replay new-referral --url <your test OpenEMR URL> --params-file params.json
```

`pull` downloads the files listed in [`index.json`](index.json), checks each
file's sha256 against the index, and loads the bundle so its manifest digest is
checked as well. It doesn't run anything.

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
