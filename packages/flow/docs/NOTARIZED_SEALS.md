# Notarized Seals

A Seal is a receipt that OpenAdapt Cloud signed with an Ed25519 key
(`openadapt.seal-signature/v1`). It says who made a statement about one write.
It does not say when the statement existed, and it can't let a holder prove a
patient's MRN to an auditor without publishing it. `openadapt-flow notary`
adds both, plus a dossier file for disputes:

- **Trusted timestamps** (RFC 3161) over the receipt hash, from any TSA.
- **Salted field commitments** so the holder can reveal chosen fields later
  and keep the others hidden.
- **A countersignature** by a notary issuer, using the Seal scheme as is.
- **A dossier**: one HTML or zip file a third party can check offline.
- **`notary verify`**: the Seal check with the executor, the verification
  tier, the timestamps, and the disclosures printed every time.

Nothing here runs during replay, and nothing here calls a model. Only digests
leave the machine: the TSA receives one 32-byte hash.

## Install

```bash
pip install 'openadapt-flow[notary]'
```

This adds `asn1crypto` (pure Python, no dependencies) to parse and encode
the CMS structures in RFC 3161. Signatures and X.509 checks use
`cryptography`, which Flow already requires. `rfc3161ng` was the
alternative. It pulls in `pyasn1`, `pyasn1-modules`, and `requests`, and it
leaves certificate path checks to the caller, so it saves no code here.

## Where the data goes

The receipt and its evidence manifest are unchanged. Everything new lives
beside them in the same wire document:

```json
{
  "schema_version": "openadapt.effect-receipt/v1",
  "receipt": { "...": "unchanged, still carries seal_signature" },
  "evidence": { "...": "unchanged, still bound by evidence_digest" },
  "extensions": {
    "openadapt.notary/v1": {
      "schema": "openadapt.notary/v1",
      "statement": {
        "schema_version": "openadapt.notary-statement/v1",
        "receipt_schema": "openadapt.effect-receipt/v1",
        "receipt_sha256": "…",
        "execution_id": "…",
        "nonce": "…",
        "evidence_digest": "sha256:…",
        "baseline_sha256": "sha256:… or null",
        "baseline_read_at": "… or null",
        "commitments_sha256": "sha256:… or null"
      },
      "commitments": {"schema": "openadapt.field-commitments/v1", "algorithm": "sha256-salted-256", "fields": {"f-1a2b3c4d": "sha256:…"}},
      "receipt_timestamp": {"tsa": "…", "imprint_sha256": "…", "token": "base64 DER TimeStampToken"},
      "baseline_timestamp": {"binding": {"…": "…"}, "tsa": "…", "imprint_sha256": "…", "token": "…"},
      "countersignature": {"attestation": {"…": "…"}, "seal_signature": {"schema": "openadapt.seal-signature/v1", "…": "…"}}
    }
  }
}
```

Every part of the block is optional except `statement`. Verifiers that
predate this block look for `receipt` and ignore the rest, so
`openadapt seal verify` and `openadapt_types.seal.verify_sealed_receipt`
still give the same answer.

The timestamp covers SHA-256 of the canonical `statement`. The statement
contains `receipt_sha256`, so the token covers the receipt hash, and it also
covers the commitments and the run's baseline. A token over the receipt hash
alone would leave the commitments untimed, and a holder could write new ones
after a dispute started.

## Commands

```bash
# 1. Before the executor runs: timestamp the baseline read for this run.
openadapt-flow notary timestamp-baseline --baseline baseline.json \
  --read-at 2026-10-17T04:58:00Z --nonce "$RUN_NONCE" -o baseline-ts.json

# 2. After the receipt exists: commit fields, timestamp, countersign.
openadapt-flow notary notarize receipt.json -o notarized.json \
  --fields-file fields.json --baseline-timestamp baseline-ts.json

# 3. Check it. Exit 0 only for a genuine Seal on a verified outcome.
openadapt-flow notary verify notarized.json

# 4. In a dispute: reveal one field, then export a dossier.
openadapt-flow notary disclose notarized.json --field f-1a2b3c4d -o mrn.disclosure.json
openadapt-flow notary dossier notarized.json --disclosure mrn.disclosure.json \
  -o dossier.html
```

`openadapt-flow seal` already encrypts bundles, so these commands live under
`notary`.

`fields.json` is a JSON object of field name to value, such as
`{"mrn": "004217"}`. Values never go on the command line, because shell
history and process lists would keep them. Delete the file once the vault
copy is in place.

Timestamps come from Sigstore's public timestamp service
(`https://timestamp.sigstore.dev/api/v1/timestamp`) unless you pass
`--tsa-url` for another RFC 3161 service, such as a paid eIDAS-qualified one.
It is free, its tokens state 1-second accuracy, and it receives only the
32-byte digest and a nonce. `--no-timestamp` skips the timestamp.

`--tsa-local DIR` uses an offline TSA that keeps its key in `DIR`. Its time
is this machine's clock, so its tokens prove nothing about time to anyone
else. It exists for tests and trials without network access.

## What `notary verify` shows

```
SIGNATURE GENUINE, NOT A FLOW REPLAY (AGENT). Pass --accept-non-flow to accept a receipt from another executor

  Executor            AGENT: a computer-use agent acted. This is not a Flow replay (declared by the run record)
  Verification tier   independent_readback (the verifier re-read the system of record or a separate session; oracle_tier 2)
  Effect verdict      CONFIRMED
  Outcome             verified
  ...
  Seal signature      VERIFIED against a pinned key (seal-ed25519-…, issued 2026-10-17T05:00:00Z)
  Evidence manifest   bound: digest and labels match the receipt
  Notary statement    matches the receipt
  Receipt timestamp   VERIFIED 2026-10-17T05:20:40Z by CN=DigiCert SHA256 RSA4096 Timestamp Responder 2026 1,… (chains to pinned CN=DigiCert Trusted Root G4,…)
  Baseline timestamp  absent
  Countersignature    absent
  Field commitments   2 committed; bound by timestamp
  Disclosure 1        f-1a2b3c4d (mrn) OK
```

The executor line always comes first. An `openadapt.execute-evidence-receipt/v1`
reads as a Flow replay because Cloud issues that schema only for a Flow runner
whose callback evidence it verified. In an effect receipt, `executor_kind` is
what the run record declared. A declared `flow_replay` there is labelled
"DECLARED by the run record and not attested by an issuer; treat as unknown".

Exit codes follow `openadapt seal verify`:

| Situation | Exit |
|---|---|
| Genuine Seal, outcome `verified` or `rolled_back_verified`, every present part checked and passed | 0 |
| Genuine Seal on `REFUTED` or `INDETERMINATE` (`halted_before_effect`, `reconciliation_required`) | 1 |
| Effect receipt from anything but an issuer-attested Flow replay, without `--accept-non-flow` | 1 |
| A timestamp is present but no TSA certificate was pinned (`not_checked`) | 1 |
| Any failed check: signature, evidence, statement, timestamp, countersignature, disclosure | 1 |
| A disclosure checked against commitments that nothing binds | 1 |

`--json` prints the same report as data.

## Trust

Verification reads only what the holder pinned:

- Seal keys come from the same places `openadapt-agent seal` reads: `--keys`,
  `OPENADAPT_SEAL_KEYS_PATH`, `OPENADAPT_SEAL_KEYS_JSON`, or
  `~/.openadapt/seal-keys.json`. Nothing is fetched.
- TSA trust comes from `--tsa-cert`: the TSA's certificate or a CA above it.
  Without it, the verifier pins Sigstore's TSA root, shipped in
  `openadapt_flow/notary/trust/sigstore-tsa-root.pem` (SHA-256 fingerprint
  `2A:CA:8F:EA:...:D6:33`, valid until 2035-04-06). A token from another TSA
  then fails until you pin that TSA's certificate.
  A certificate inside the token or the document is used to build the path,
  never as an anchor. Each link must be valid at the token's `genTime`, and
  the TSA certificate must carry the critical `timeStamping` extended key
  usage.

The countersignature is a `openadapt.seal-signature/v1` over an
`openadapt.notary-attestation/v1` object. It reuses the Seal key registry,
pinning, and `openadapt_types.seal.verify_seal_signature` unchanged. The
attestation's `schema_version` is inside the signed hash, which keeps an
attestation from being read as a receipt. Cloud has no notary endpoint yet;
`CloudNotaryIssuer` is an interface stub that raises, and
`LocalNotaryIssuer` signs with a local key for tests and self-hosted trials.

## Field commitments

Each field gets its own 32-byte random salt. The commitment is

```
sha256("openadapt.field-commitment/v1\0" || canonical({label, name, salt, value}))
```

with `salt` in base64 and canonical JSON as in RFC-0002 (sorted keys, compact
separators, UTF-8). Labels are `f-` plus random hex, so the block names no
field. A disclosure bundle (`openadapt.field-disclosure/v1`) gives the label,
name, value, and salt of each revealed field, and anyone can recompute it.

Per-field salts are what make selective disclosure work. A per-run salt,
which RFC-0002 allows for identity digests, has to be revealed to check any
one field, and once it is out every other low-entropy field from that run can
be found by enumeration again. The commitments sit beside the receipt's own
identity digests and do not replace them.

A disclosure counts only when the commitments are bound by a verified
timestamp or countersignature. Otherwise anyone could have written the block
after the fact, and `notary verify` reports `commitments_unbound`.

## The vault

`notarize` writes the salts and values to
`$OPENADAPT_NOTARY_VAULT/<receipt_sha256>/field-secrets.json` (default
`~/.openadapt/notary-vault`), mode 0600 in a 0700 directory, together with a
copy of the notarized document. The vault holds PHI. Back it up and encrypt
it the way you do the system of record. Losing it loses the ability to
disclose; it never weakens the commitments. On Windows, the mode bits do not
apply; rely on the profile directory's ACL.

## The dossier

`notary dossier` writes one HTML page, or a zip with the page plus the raw
token files and an `openssl` recipe. The page contains the receipt, the
evidence manifest, the notary block, the report computed at export, and the
pinned keys used. When opened, it checks in the browser the receipt hash,
the Ed25519 Seal signature against the embedded key, the evidence digest, and
each disclosed commitment. RFC 3161 tokens need `openssl ts -verify` or
`openadapt-flow notary verify dossier.html`; the page prints the command.

By default the dossier has no PHI. Disclosed values appear only when you pass
a disclosure bundle. Screenshots appear only with `--screenshot` and only
after automatic redaction by openadapt-privacy. Without the `privacy` extra
they are left out, and the page says so. Automatic redaction can miss text,
and screenshots are not bound to the receipt, so review a dossier before you
send it.

## What a timestamp proves, and what it does not

A verified RFC 3161 token proves that the TSA saw this exact statement hash
no later than `genTime`, within the TSA's stated accuracy. So the receipt,
its evidence digest, and its commitments existed by then and have not
changed since.

It does not prove:

- that the receipt is true, or that the write happened. The read-back and
  the Seal carry that claim; the timestamp only fixes when the claim existed.
- when the write happened. A receipt can be timestamped long after the run.
- that the baseline preceded the executor. A baseline token proves the
  snapshot for this run's nonce existed by its `genTime`. Comparing that
  with the earliest executor action needs another clock, such as the EHR's
  audit log.
- anything about who asked for the timestamp. TSAs stamp any hash.
- that the TSA's clock was right, or that its key was not compromised.
  Revocation (CRL or OCSP) is not checked offline, and a token signed after
  the TSA key's compromise date verifies the same. Long-term validity needs
  archive timestamps (RFC 4998 or CAdES-A), which this does not implement.
- that the time is precise. Sigstore and the local TSA claim one
  second; DigiCert's tokens state no accuracy.

A token from `--tsa-local` proves only what this machine's clock said.

## Tests

`tests/test_notary.py` runs offline. It covers brute-force resistance over a
small MRN domain, wrong disclosed values, a real Sigstore token
(`tests/fixtures/notary/sigstore-token.der`) checked against the bundled
root, a real DigiCert token
(`tests/fixtures/notary/digicert-token.der`, captured on 2026-10-17 with
`openssl ts -query`) checked against DigiCert Trusted Root G4, `openssl ts
-verify` on local tokens, tampering after notarization, baseline nonce
binding, exit codes for REFUTED and INDETERMINATE Seals, and a dossier with
no PHI by default.
