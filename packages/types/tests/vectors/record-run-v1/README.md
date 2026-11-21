# record/run/v1 test vectors

Shared vectors for every receipt verifier: Python (`openadapt_types.receipt`),
Cloud and the website (TypeScript). Regenerate with `python generate.py`; the
output is byte-stable. The keys come from public labels, so they are test keys,
not secrets.

## Files

- `registry.json`: the pinned key registry (`openadapt.receipt-keys/v1`), one key
  per role: `this_computer`, `customer_key` (org "Example Clinic"),
  `openadapt_cloud`, `demo`. The `stranger` key that signs `unknown-signer*` is
  not in it.
- `registry-unknown-role.json`: a registry with role `notary`. A verifier must
  refuse the whole registry (`unknown_role`).
- `<name>.json`: `{description, registry, document, openings?, expected}`.
  `document` is a DSSE envelope (`receipt.json`) or a public card
  (`openadapt.receipt-card/v1`). `openings` is the private sidecar
  (`receipt.private.json`). `expected` has `ok`, `reason`, the exact
  `signer_lines`, and for a pass `outcome`, `demo` and `disclosed`.

## Rules a verifier implements

1. **Envelope.** Exactly `payloadType`, `payload`, `signatures`. `payloadType` is
   `application/vnd.in-toto+json`, otherwise `unknown_schema`. No signatures:
   `unsigned`.
2. **Signatures.** Each is exactly `{keyid, sig}`, Ed25519 over
   `PAE = "DSSEv1 " + len(type) + " " + type + " " + len(payload) + " " + payload`
   where payload is the base64-decoded bytes, used as-is. A key id not in the
   pinned registry: `unknown_signer`, even beside a valid signature. Bad
   signature: `bad_signature`. Revoked key: `revoked_key`. Demo next to any other
   role: `mixed_demo`. The role comes only from the registry.
3. **Statement.** UTF-8 JSON, duplicate keys refused. `_type` is
   `https://in-toto.io/Statement/v1` and `predicateType` is
   `https://openadapt.ai/record/run/v1`, otherwise `unknown_schema`. Every
   string in `subject` and `predicate` is scanned for embedded content
   (`data:` URIs, `scheme://` URLs, base64 runs of 96 or more characters, image
   magic, `<svg`/`<img`, any string over 256 characters): `embedded_content`.
   Closed schema (see `openadapt_types/receipt.py`); the payload must equal its
   canonical form (sorted keys, no whitespace, no ASCII escaping), otherwise
   `malformed`. Contradictions (counts that disagree with `lines`, a VERIFIED
   receipt with a refuted line) are `inconsistent`.
4. **Commitments.** `sha256("openadapt.receipt-commitment/v1" 0x00 path 0x00
   salt(32 raw bytes) 0x00 canonical_json(value))`, as lower-case hex. Salts are
   64 lower-case hex characters.
5. **Openings.** A card reveals only fields on its `disclosure_version`
   allow-list (`openadapt.public-disclosure/v1`: `program_name`,
   `flow_version`, `github_login`), otherwise `disclosure_not_allowed`. Opened
   values are scanned for embedded content and must match their commitment
   (`opening_mismatch`). A private sidecar names the payload's SHA-256.
6. **Signer lines.** One per verified role, in the order this computer,
   customer key, OpenAdapt Cloud, public log: "Signed by this computer",
   "Signed by <org>'s key", "Countersigned by OpenAdapt Cloud", "Logged
   publicly". A demo-only receipt shows "Demo key: checks only in this demo".
   A refusal shows one "Not verified: ..." line; an unknown key shows "Not
   verified: unknown signer".
7. **Outcome.** Copied from the statement, never upgraded.

A receipt never holds screenshots, typed values, URLs or page text. Evidence
files are referenced only by SHA-256 digest.
