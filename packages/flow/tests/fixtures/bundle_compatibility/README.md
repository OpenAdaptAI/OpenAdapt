# Frozen bundle compatibility fixtures

These synthetic linear and program bundles were emitted by the actual Flow
2.2.0, 2.5.0 and 2.6.0 serializers at the source commits recorded in each
provenance file. Each producing release loaded and verified its own output
before the bytes were frozen. The current serializer does not generate the
expected bundles or digests.

`canonical.json` contains the producing release's manifest-free canonical
payload and sealed asset hashes. Its SHA-256 is the original manifest content
digest. This is content-integrity evidence, not a claim of cryptographic signing.
No signing credential or private data is used.

The generator refuses another package version or source tree, checks pinned
source hashes, and writes only a new output directory. Run it in the exact
historical release's locked environment, then freeze provenance and every output
hash before running the current loader. The tests copy fixtures to temporary
storage, verify original seals, reject tampering and replay one exact button
click in an owned synthetic headless Chromium page.

Changes to a fixture require new release provenance. Do not regenerate a golden
with the current runtime to make a compatibility test pass.

Flow 2.2.0's original lockfile failed uv 0.13.7's freshness check. Its fixture
run used `uv sync --frozen` with that unchanged lockfile; it did not resolve or
update dependencies. The 2.5.0 and 2.6.0 runs passed `uv sync --locked`. Each
provenance file pins the producing source, project metadata, lockfile, Python
and Pydantic version. These runs reproduce the pinned release source serializers;
they do not claim to reproduce the original release build environment.
