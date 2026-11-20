# Export a local program candidate

`openadapt wrap export` prepares an **untrusted local candidate** for offline
review. Pass the exact plaintext bundle directory yourself:

```bash
openadapt wrap export --bundle /path/to/program/bundle --out /path/to/new-candidate
```

The output parent must already exist. The destination must be new and disjoint
from the input. Capture needs directory-relative, no-follow file access; a
platform without those filesystem primitives refuses. Positive filesystem
validation currently covers macOS; other supported platforms need their own
checks before a portability claim.

The directory contains:

- `candidate.json`: untrusted status, original bundle content digest, exact file
  hashes and archive hash.
- `bundle/`: exact captured workflow JSON, its effective sidecar manifest when
  the workflow has no embedded manifest, and the complete sealed PNG template
  set.
- `bundle.tar.gz`: a reproducible archive of those same captured bytes, checked
  through the registry's archive reader before the handoff succeeds.

The exporter checks the original persisted integrity seal before model
migration, workflow structure, referenced assets and the complete template
inventory. It compares the captured files again before writing. Later changes
to the source cannot replace the captured occurrence in the output. Unknown
source files, executable workflow source, admission records, keys, private-tier
checks and run evidence are not copied or read. Encrypted or sealed-set input,
unsafe links, missing assets and unsupported asset paths refuse. The exporter
does not decrypt or create a replacement seal.

The output is private local data. It may still contain sensitive values or
screen crops: this command does not sanitize it or establish a license to share
it. File hashes show identity, not privacy, safety or trust. Existing embedded
provenance and qualification claims remain the original input bytes and are
not accepted as authority by this export. No registry index, registry origin,
qualification, admission or publication is created. The candidate is not an
accepted registry entry and cannot be passed directly to `pull`.

## MCP handoff

A candidate grants no permission to execute. `--emit-mcp` always refuses with
`qualification_required` before reading input or writing output:

```bash
openadapt wrap export --bundle /path/to/program/bundle \
  --out /path/to/new-candidate --emit-mcp
```

Complete the original bundle's existing local certification and intended-instance
qualification first, then use the existing `emit-mcp` command on that qualified
bundle. The exporter never looks up an admitted cache program or reuses hosted
publication staging. It does not attest that the existing emitter enforces every
ordinary local qualification gate; that consumer is a separate source concern.
Published and pulled programs retain their existing target/instance requirements.

This is an offline handoff, with synthetic validation only. It is not evidence
of a signed registry release, a qualified MCP execution or a real-system effect.
