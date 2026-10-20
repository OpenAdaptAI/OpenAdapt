# Offline Seals and no-outbound mode

A Seal is a signed statement about one run. OpenAdapt Cloud can issue one
(see [`RECEIPTS.md`](RECEIPTS.md) and `attach-seal`), but that needs a
network connection. An offline Seal is signed by a key on the machine that ran
the work, so a site with no outbound traffic still gets a Seal for every run.

Offline Seals are on by default. Trusted timestamps are optional and batched.
Strict no-outbound mode is enforced in the process, not just documented.

## What happens on every run

`openadapt-flow run` and `openadapt-flow replay` with `--run-dir` write
`seal.json` next to `report.json` when the run ends. The on-prem queue
always passes `--run-dir`, so every queued run is sealed. Sealing never
changes the run's exit code and calls no model.

`seal.json` (`openadapt.local-seal/v1`) states:

| Field | Meaning |
|---|---|
| `subject_sha256` | SHA-256 of the exact bytes of `report.json` |
| `run_id`, `outcome` | Copied from the report. The outcome is never upgraded. |
| `issued_at` | UTC, to the second |
| `key_id`, `public_key` | The local key that signed it |
| `signature` | Ed25519 over the domain-separated canonical statement |

Unless you name a key (see [Customer-held keys](#customer-held-keys)), the
runner creates one on first use under `$OPENADAPT_SEAL_HOME/keys` (default
`~/.openadapt/seal`), mode 0600. Set `OPENADAPT_LOCAL_SEAL=off` to turn
sealing off.

An offline Seal is weaker evidence than one from an independent party: the
machine that ran the work also signed the statement. It shows the report has
not changed since it was sealed. It does not grade the run and grants no
authority.

## Customer-held keys

In production, sign with a key you control instead of the key the runner
generates. Name it with `OPENADAPT_SEAL_KEY` (or `--key` on any
`local-seal` command):

| Source | Setting | Install |
|---|---|---|
| Key file you provide | `file:/etc/openadapt/seal-key.pem`, or `OPENADAPT_SEAL_KEY_FILE=/etc/openadapt/seal-key.pem` | Base package |
| OS keychain | `keychain:NAME` | `pip install 'openadapt-flow[keychain]'` |
| PKCS#11 token or HSM | `pkcs11:token=TOKEN;object=LABEL?module-path=/path/to/module.so` | `pip install 'openadapt-flow[pkcs11]'` |

- **Key file.** A PKCS#8 PEM Ed25519 private key. Only its owner may have
  access: mode 0600 or 0400. A file open to its group or to other users
  (0640, 0644) is refused. When you mount it as a Kubernetes secret, set
  `defaultMode: 0400`. An encrypted PEM opens
  with `OPENADAPT_SEAL_KEY_PASSPHRASE`.
- **OS keychain.** The key is stored through the `keyring` library: the
  macOS Keychain, the Windows Credential Manager (encrypted with DPAPI), or
  the Linux Secret Service. The key is read into memory to sign.
  `openadapt-flow local-seal key create --key keychain:NAME` creates a key
  there without writing it to disk.
- **PKCS#11.** The key stays on the token, which must support CKM_EDDSA
  (Ed25519). The module can also come from `OPENADAPT_SEAL_PKCS11_MODULE`.
  The PIN comes from `OPENADAPT_SEAL_PKCS11_PIN`, or from another variable
  named with `pin-source=env:VAR`. A PIN written in the URI is refused.
  Create the key with the token's own tools.

When a key is configured and can't be used (missing file, wrong passphrase,
locked keychain, wrong PIN, token unplugged), nothing is signed. The run
still finishes with its own exit code and prints `offline seal not written:
key_unavailable`. OpenAdapt never falls back to a generated key.

Every Seal records the signing key's `key_id`.

### Pinning and rotation

```bash
# Pin the configured key in the registry the verifier uses.
openadapt-flow local-seal key pin site-keys.json

# Rotate: create the new key, pin it, and sign with it from now on.
openadapt-flow local-seal key create --key file:/etc/openadapt/seal-key-2027.pem
openadapt-flow local-seal --key file:/etc/openadapt/seal-key-2027.pem key pin site-keys.json

# Revoke a key that may be compromised.
openadapt-flow local-seal key revoke site-keys.json seal-ed25519-0123456789abcdef
```

To rotate, leave the old key pinned and active, and stop signing with it.
Seals from both keys keep verifying.

Revoke a key only when it may be compromised. After `revoke`, every Seal
that key signed is refused with `revoked_key`, including Seals whose
`issued_at` is earlier than the revocation. `issued_at` is written by
whoever holds the key, so someone with a stolen key can backdate it. The
exception is a Seal with a transparency-log anchor that the log recorded
before `revoked_at` (see [Verify an anchor](#verify-an-anchor)). A
registry that lists one key ID twice is refused.

## Seal records and exports

```bash
openadapt-flow local-seal file export.csv --kind export   # writes export.csv.seal.json
openadapt-flow local-seal file record.json --kind record
```

## Verify

Verification trusts pinned keys only. The `public_key` inside a Seal is never
trusted on its own.
Without `--keys`, `verify` trusts only the configured Seal key and says so.

```bash
# On the site machine: print the public key for an auditor to pin.
openadapt-flow local-seal key > site-keys.json

# Anywhere: verify a run, or a file with its Seal.
openadapt-flow local-seal verify runs/job-0007 --keys site-keys.json
openadapt-flow local-seal verify export.csv --seal export.csv.seal.json --keys site-keys.json
```

`site-keys.json` has the same shape as the Cloud Seal key registry, so a key
can be marked `revoked` with a `revoked_at` time. Refusal reasons are closed:
`unsigned`, `malformed`, `subject_mismatch`, `unknown_key`, `revoked_key`,
`key_mismatch`, `bad_signature`, `ambiguous_key`.

If `attach-seal` later adds a Cloud receipt to `report.json`, the offline
Seal still verifies against `report.pre-seal.json`, which `attach-seal` keeps.
`verify` names the file the Seal matched, and adds a note when the match is
`report.pre-seal.json`: in that case `report.json` itself is not what was
sealed. `--strict` accepts only `report.json`.

A Seal file with a repeated JSON key (for example two `outcome` fields) is
`malformed`. Different JSON parsers would read it differently.

## Optional: anchor a Seal in a public transparency log

An offline Seal is signed on the machine that ran the work, so on its own it
is the site's statement about itself. Anchoring records the Seal in
[Sigstore Rekor](https://docs.sigstore.dev/logging/overview/), a public,
append-only transparency log that OpenAdapt does not run. Anyone can then
check that this exact Seal existed when the log recorded it and has not
changed since, without trusting the site's key or OpenAdapt.

Anchoring is off by default and runs only when a person asks for it:

```bash
openadapt-flow local-seal anchor runs/job-0007          # a run directory
openadapt-flow local-seal anchor export.csv.seal.json   # any Seal file
```

### What becomes public

Each anchor adds one `hashedrekord` entry to the public log. It holds:

- the SHA-512 digest of a short anchor statement: the Seal's SHA-256 and a
  random 32-byte nonce;
- the Seal key's public key;
- an Ed25519ph signature over that digest by the Seal's key;
- the time the log recorded it.

Nothing else is sent: no report, no field values, no screenshots, no run id,
no outcome. The nonce is generated for each Seal and kept only in the local
anchor file, so nobody can confirm a guessed report by hashing the guess and
looking it up. Anyone can see that one key anchored several Seals, and when,
because they share a public key. Log entries can't be deleted.

A deployment that handles health data turns anchoring on deliberately, after
deciding that a public record of "this key sealed something at this time" is
acceptable. Under strict no-outbound mode, `anchor` refuses with
`held_no_outbound` and sends nothing.

### The anchor file

`anchor` writes `seal.anchor.json` next to `seal.json` (or
`NAME.seal.anchor.json` next to `NAME.seal.json`). It holds the nonce and
the log's full answer: the entry UUID, the entry, the signed entry timestamp
and the inclusion proof with the log's signed checkpoint. `anchor` checks
that answer before writing the file. Running it again on an anchored Seal
changes nothing. If the connection drops before the answer arrives, run it
again: it re-sends the same digest and picks up the existing entry.

The Seal key signs the anchor. With a PKCS#11 key, the token signs with
CKM_EDDSA and the prehash flag. With a key file or keychain key, OpenAdapt
computes the Ed25519ph signature itself in pure Python, because the
`cryptography` library has no Ed25519ph. That code isn't constant time:
someone who can time many signatures closely on the same machine might learn
something about the key. Anchoring makes one signature per Seal, on demand.
Where that matters, use a PKCS#11 key.

### Verify an anchor

```bash
openadapt-flow local-seal verify runs/job-0007 --keys site-keys.json --require-anchor
```

`--require-anchor` checks, offline, from the stored file:

1. the Seal itself, against the pinned keys, and against `report.json` only
   (`--require-anchor` implies `--strict`);
2. that the anchored digest is the digest of this Seal with the stored nonce;
3. that the entry is signed by the same pinned key that verified the Seal;
4. the log's signed entry timestamp, against the log's public key;
5. the inclusion proof, from the entry up to the root in the log's signed
   checkpoint;
6. that the checkpoint's origin line and signature line name the pinned log,
   and that the entry ID starts with the checkpoint's tree ID.

A Seal from a revoked key passes only when the log recorded it before the
key's `revoked_at`. That uses the log's time, which the key's holder can't
change.

The log key for the public Sigstore instance is built in (log ID
`c0d23d6a…801d`, origin `rekor.sigstore.dev`). To pin another log, pass
`--log-key PEM`, `--log-url URL` and, when its checkpoints name another
origin than the URL's host, `--log-origin NAME`.

`--live` also fetches the entry from the log and compares it. It is optional
and needs network access. It fetches from `--log-url` (default the public
Rekor), never from a URL written in the anchor file. Offline verification
works in strict no-outbound mode.

Refusal reasons are closed: `anchor_missing`, `anchor_malformed`,
`anchor_mismatch`, `anchor_key_mismatch`, `anchor_bad_signature`,
`anchor_unknown_log`, `anchor_bad_timestamp`, `anchor_bad_inclusion`,
`anchor_bad_checkpoint`, `anchor_live_mismatch`, `anchor_live_unavailable`.
An anchor file in the wrong shape, at any field, is `anchor_malformed`.

Cloud Seals are unchanged and remain the hosted option.

## Run authorizations: who authorized the runs

A Seal names the machine key that sealed a run. A run authorization names
the person who authorized it. The scheme of the `authorizer` handle names
the identity provider. The provider built so far is `ssh-github`
(`github:<user>`): an Ed25519 SSH key the person publishes on GitHub.

The statement (`openadapt.run-authorization/v1`, `run-authorization.json`)
holds:

| Field | Meaning |
|---|---|
| `bundle_content_digest` | The compiled program the person authorizes |
| `scope.items` | The work items, for example `r3`, `r4` |
| `scope.profile` | The execution profile the runs must use, for example `standard` |
| `scope.seal_key_ids` | The Seal keys allowed to seal runs under it |
| `authorizer` | The person's public handle, `github:<user>` |
| `issued_at`, `nonce` | When, and 32 random bytes |

```bash
# 1. Write the statement and a snapshot of https://github.com/<user>.keys.
openadapt-flow local-seal authorize create --out auth/ --bundle-digest <digest> \
    --item r3,r4,r5,r6 --profile standard --authorizer github:<user>

# 2. The person reviews what it allows, then signs with their own GitHub SSH key.
openadapt-flow local-seal authorize sign auth/run-authorization.json \
    --key ~/.ssh/id_ed25519 --bundle path/to/bundle

# 3. Seal each run under it, naming its item.
openadapt-flow local-seal run runs/r3 --authorization auth/ --item r3

# 4. Optional: anchor the authorization in the transparency log, as a Seal.
openadapt-flow local-seal authorize anchor auth/
```

`authorize sign` prints a plain-text summary of about 30 lines, then asks
`Sign this authorization? [y/N]`. Only a "y" answer runs
`ssh-keygen -Y sign -n openadapt-run-authorization`. The summary shows:

- who is signing, the key's fingerprint, and whether the GitHub snapshot
  lists that key (✓ or ✗);
- the program: its name, app, short bundle digest, step count, and the
  qualification result from a `QUALIFICATION.md` next to the bundle;
- an outline, one line per section (compiled sections, or else one per
  field the steps fill), the consequential write and its record read-backs,
  and the stop rules;
- the scope, and what the authorization does not allow;
- the statement's SHA-256 and path.

The program is read from the bundle given with `--bundle` (default: a
`bundle/` folder next to the statement). Its content digest is recomputed
from its files, and a bundle that doesn't match `bundle_content_digest` is
refused before anything is shown. For a key the snapshot doesn't list,
`sign` warns and asks you to type "yes". `--dry-run` prints the summary
and signs nothing. Running `ssh-keygen -Y sign` yourself also works.

`authorize create` reads `https://github.com/<user>.keys` once and stores the
Ed25519 keys it lists in `run-authorization.allowed_signers`, in the format
`ssh-keygen -Y verify` reads. OpenAdapt never holds the person's private
key; only the person runs step 2.

`local-seal run --authorization` refuses to seal unless the authorization's
signature verifies, the run's report has the authorized bundle digest and
profile, the item is in scope, and the Seal key is in scope. The Seal then
records `authorization_sha256` (the SHA-256 of `run-authorization.json`) and
`scope_item`, signed by the Seal key. Seals without an authorization don't
have these fields and keep their exact bytes. A Seal that was already
written, for example by the run hook, is re-signed only with
`--replace-unanchored`, and only if it has never been anchored.

Verify:

```bash
openadapt-flow local-seal authorize verify auth/
openadapt-flow local-seal verify runs/r3 --keys site-keys.json --require-anchor --authorization auth/
```

The verifier checks offline:

- the person's signature over the exact statement bytes, in the
  `openadapt-run-authorization` namespace;
- that the signing key is listed for the `authorizer` in the stored GitHub
  snapshot, or in your own file passed with `--allowed-signers`;
- that the Seal names this authorization and an item in its scope;
- that the run's profile, bundle digest and Seal key match the scope;
- with `--require-anchor`, the authorization's anchor.

The person can sign before the runs or after them. Either way the
authorization is valid, and the verifier states which it was, comparing the
time the transparency log recorded the authorization with the run's own
`started_at`:

- `before_run`: "Authorized by @user before this run".
- `after_run`: "Authorization signed by @user on <date>, after this run".
- `not_anchored`: the authorization has no anchor (or the run states no
  start time), so nothing is said about order.

Re-sealing a run under an authorization signed later does not change this:
the run's own start time decides, never the time of the new Seal.
`started_at` is in the sealed report, so it can't change after sealing, but
it is the runner's own record. The Seal's log time is an upper bound on it.
The verifier also reports the authorization's log time against the Seal's
(`before_seal` / `after_seal`).

`--live` also re-reads `https://github.com/<user>.keys` and refuses a key
GitHub no longer lists (`auth_not_on_github`). The host is always
github.com; nothing in the files chooses where to fetch from.

You can check the signature with OpenSSH alone:

```bash
ssh-keygen -Y verify -f auth/run-authorization.allowed_signers -I github:<user> \
    -n openadapt-run-authorization -s auth/run-authorization.json.sig < auth/run-authorization.json
```

What the snapshot proves: that the key listed for the user at the time of
the snapshot signed the statement. It doesn't prove who controls the GitHub
account. Pin your own allowed-signers file, or use `--live`, when that
matters.

Refusal reasons: `auth_missing`, `auth_malformed` (including a repeated JSON
key), `auth_unsigned`, `auth_bad_signature`, `auth_unknown_signer`,
`auth_not_on_github`, `auth_live_unavailable`, `auth_not_referenced`,
`auth_reference_mismatch`, `auth_scope_mismatch`, `auth_bundle_mismatch`.

## Operator profile

```bash
openadapt-flow local-seal profile runs/* --keys site-keys.json --authorization auth/
```

`profile` writes `operator-profile.json` (`openadapt.operator-profile/v1`),
keyed by Seal key ID. For each key it lists:

- the number of runs in each outcome. Verified, stopped before any effect,
  completed but unverified, and failed always appear, even at zero. Any
  other outcome appears under its own code.
- the apps and programs used: the report's app name, or else the first word
  of the program name;
- the first and last time the log recorded one of the key's Seals, and each
  Rekor log index;
- `run_list`: one line per counted Seal, so a reader can recount. Each line
  holds the Seal's SHA-256, its scope item, the outcome codes, app, program,
  issue time and Rekor entry.
- `linked_identity`, only when an authorization signed with the person's
  GitHub key verifies, names this Seal key in its scope, and the Seals
  reference it. It names the handle, the identity provider (`ssh-github`)
  and the SSH key fingerprint.

Only Seals that verify against the pinned keys and have a valid anchor are
counted. The rest are counted under `excluded` as `unanchored` or
`not_valid`, without detail.

The profile holds no report content: no patient names, parameters, record
IDs, run IDs or folder names.

## Optional: one RFC 3161 timestamp per record

A transparency-log anchor records when Rekor saw a record. An RFC 3161 token
from a timestamp authority is a second, independent time, signed by the
TSA's certificate. Sigstore's TSA (`timestamp.sigstore.dev`) is the default.

```bash
openadapt-flow local-seal timestamp runs/job-0007            # writes runs/job-0007/seal.tsa.json
openadapt-flow local-seal timestamp auth/                    # writes auth/run-authorization.tsa.json
openadapt-flow local-seal verify runs/job-0007 --keys site-keys.json --require-timestamp
```

Each request sends only a 32-byte SHA-256 imprint and a random nonce:

- for a Seal, `seal_sha256`;
- for a run authorization, SHA-256 of
  `canonical_json({"run_authorization_sha256", "signature_sha256"})`, which
  covers the statement and the signature together.

The token file (`openadapt.record-timestamp/v1`) holds the record's digest
fields, `imprint_sha256`, `tsa`, `token` (base64 DER TimeStampToken) and
`gen_time`. `timestamp` checks the token offline before writing it.
`--require-timestamp` checks it again, against the Sigstore TSA root shipped
with OpenAdapt, or the certificates you pass with `--tsa-cert`. Strict
no-outbound mode refuses to send. To cover many Seals with one token instead,
use batched timestamps.

## Optional: batched timestamps

Each new Seal is queued under `$OPENADAPT_SEAL_HOME/batch/pending`. Nothing
leaves the machine until a person submits a batch.

```bash
openadapt-flow local-seal batch     # fold the queue into one Merkle tree (local)
openadapt-flow local-seal submit    # one RFC 3161 request per batch root
openadapt-flow local-seal proof runs/job-0007/seal.json
```

The tree follows RFC 6962 (leaf `sha256(0x00 || seal)`, node
`sha256(0x01 || left || right)`). The batch file holds each Seal's inclusion
proof, and the root is itself sealed with the local key. `submit` sends only
the 32-byte root, so one request covers every run in the batch. Use
`local-seal proof` to show one Seal with its proof and root without revealing
the other Seals in the batch.

`submit` uses the RFC 3161 client from `openadapt_flow.countersign`
(Sigstore's TSA by default, or `--tsa-url`). A build without that module
reports `countersign_unavailable` and changes nothing.

## Strict no-outbound mode

```bash
export OPENADAPT_NO_OUTBOUND=1
export OPENADAPT_NO_OUTBOUND_LOG=/srv/openadapt/audit/outbound-blocked.jsonl
# Optional: the allow list (see below).
export OPENADAPT_NO_OUTBOUND_ALLOW_FILE=/srv/openadapt/no-outbound-allow.json
```

### The allow list

Nothing but this machine is reachable by default. Other machines on the LAN
are refused like any outside host. A deployment that drives an application
over a remote-desktop session allows that session's host, and nothing else,
by deriving it from the session profile:

```bash
export OPENADAPT_NO_OUTBOUND_ALLOW_FILE=/srv/openadapt/no-outbound-allow.json
openadapt-flow no-outbound allow derive /srv/openadapt/session.rdp
# added: 10.20.0.15/32 port 3389  (session host, from /srv/openadapt/session.rdp)
```

`derive` reads a Microsoft `.rdp` file (`full address`, `alternate full
address`, and `gatewayhostname` when a gateway is in use) or a FreeRDP
session file (JSON with a `target` object holding `host` and `port`, and an
optional `gateway` object). A host name is resolved once, when you run
`derive`, and the resulting addresses are stored. Public addresses are
refused. The command prints every address it added and where it came from.
The installer runs it for you when you pass the session profile (see
[`ON_PREM.md`](ON_PREM.md)).

To change the list by hand:

```bash
openadapt-flow no-outbound allow list          # every entry and its origin
openadapt-flow no-outbound allow add 10.20.0.40 --port 5432
openadapt-flow no-outbound allow remove 10.20.0.40
```

`OPENADAPT_NO_OUTBOUND_ALLOW` still works for a quick, unrecorded addition.
If `OPENADAPT_NO_OUTBOUND_ALLOW_FILE` names a file that is missing or holds a
public range, startup stops.

### What the hook does

`openadapt-flow` installs a Python audit hook before any command runs. Every
`connect`, `sendto` and name lookup in the process passes through it. Anything
not on this machine or in the allow list raises `OutboundBlocked` before a
packet is sent. Lookups for outside host names are refused too, because the
DNS query would carry the name off the machine. The hook cannot be removed
for the life of the process. The allow list refuses host names and public
ranges, and a bad allow list stops startup.

In this mode:

- `attach-seal` returns `no_outbound` without contacting Cloud.
- `local-seal anchor` refuses with `held_no_outbound` and sends nothing.
- `local-seal submit` holds each batch as `held_no_outbound` unless the
  timestamp authority declares itself local (`MAY_EGRESS = False`).
- Offline Seals and batches work as usual.
- Each refusal is one JSON line in `OPENADAPT_NO_OUTBOUND_LOG` (event, host,
  port, pid, time; never payload bytes).

What the hook does not cover: child processes such as FreeRDP or `curl`, and
native code that opens sockets without Python's `socket` module. For those,
use the on-prem service's outbound-traffic audit (`openadapt-flow service
egress-audit`, see [`ON_PREM.md`](ON_PREM.md)), and rely on the site
firewall, which remains the primary control.

`tests/test_no_outbound.py` runs each check in a fresh interpreter: public
connect, UDP send, DNS lookups and an HTTPS client call are refused and
logged; loopback and allow-listed LAN addresses still work, other LAN
addresses are refused; the hook stays on
after the environment changes; and the CLI refuses `attach-seal`.
