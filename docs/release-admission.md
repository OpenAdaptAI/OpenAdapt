# Launcher release admission

A launcher release ships one exact `openadapt-flow` release, selected in
`platform-manifest.json` with its wheel and sdist SHA-256 digests. The release
workflow refuses to tag or publish unless the founder signed that exact Flow
release.

## How it works

- `release-admission/trusted-keys.json` lists the trusted Ed25519 **public**
  keys. Changing it is a reviewed pull request.
- `release-admission/flow-<version>.json` is the signed admission. Its payload
  names the Flow repository, version, tag, source commit, and both file names
  and digests. The signature covers the canonical JSON of that payload.
- The `verify-flow-admission` job in `release-and-publish.yml` runs
  `python scripts/flow_admission.py verify`. It downloads both Flow files from
  PyPI and checks their bytes against the manifest. Then it requires the
  admission payload to equal the manifest's Flow release, and checks the
  signature against a trusted key. Anything missing or different fails the
  release before the tag job.
- The private key lives only on the founder's machine. It never enters CI,
  GitHub secrets, or the repository.

## Signing (founder, on your own machine)

From a launcher checkout whose `platform-manifest.json` selects the Flow release:

```bash
python scripts/flow_admission.py sign --key ~/openadapt-release-admission-ed25519.pem
```

The first run creates the key with mode 0600 and adds its public half to
`trusted-keys.json`. Before signing, the command checks PyPI's digests against
the manifest. Commit the admission file (and `trusted-keys.json` on the first
run) through a pull request. Requires OpenSSL 3 (`brew install openssl@3`; set
`OPENSSL=/opt/homebrew/bin/openssl` if your default is LibreSSL).

## Rotation and revocation

To rotate keys, sign with a new key and remove the old entry in the same pull
request. To revoke, remove a key or an admission file through a pull request.
There is no separate registry, counter, or online service.
