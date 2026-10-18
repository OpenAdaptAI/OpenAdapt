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

The key is created on first use under `$OPENADAPT_SEAL_HOME/keys`
(default `~/.openadapt/seal`), mode 0600. Set `OPENADAPT_LOCAL_SEAL=off` to
turn sealing off.

An offline Seal is weaker evidence than one from an independent party: the
machine that ran the work also signed the statement. It shows the report has
not changed since it was sealed. It does not grade the run and grants no
authority.

## Seal records and exports

```bash
openadapt-flow local-seal file export.csv --kind export   # writes export.csv.seal.json
openadapt-flow local-seal file record.json --kind record
```

## Verify

Verification trusts pinned keys only. The `public_key` inside a Seal is never
trusted on its own.

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
`key_mismatch`, `bad_signature`.

If `attach-seal` later adds a Cloud receipt to `report.json`, the offline
Seal still verifies against `report.pre-seal.json`, which `attach-seal` keeps.

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
