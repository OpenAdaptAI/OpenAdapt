# Offline update & rollback — on-prem (air-gapped) runbook

How an operator patches a regulated, air-gapped clinic box and reverts instantly
if a release misbehaves. **Nothing here touches the network.** No PyPI, no
phone-home, no telemetry: a release is prepared off-site, copied in on removable
media, verified locally, and applied atomically.

The design goal: an update is a **single atomic symlink flip** after the new
release has been **verified and smoke-tested**, and a rollback is the **reverse
flip** — so a bad patch is a seconds-long, zero-data-loss event.

---

## On-disk model

Under `storage_root` (from `onprem.yaml`):

```
releases/<version>/venv/     an immutable release, built in place (its venv's
releases/<version>/RELEASE   absolute paths never move — only `current` moves)
releases/HISTORY             append-only ledger of activate/rollback events
current  -> releases/<version>    the ACTIVE release   (atomic symlink)
previous -> releases/<version>    the rollback target  (atomic symlink)
venv     -> current/venv          convenience alias for humans/docs
rollback-forward -> ...           transient crash-recovery pointer during rollback

bundles/ runs/ jobs/ audit/ keys/ deployment.yaml   DATA — NEVER touched by an
                                                     update or a rollback.
```

The systemd unit runs the engine via `storage_root/current/venv/bin`. A flip or
rollback therefore takes effect on the **next job** — no unit edit, no
`daemon-reload`, no service interruption of an in-flight run (it finishes on the
release it started with; `Type=oneshot` picks up the new one next time).

Customer data lives **outside** `releases/` and is never read, moved, or deleted
by update/rollback. Config, keys, the hash-chained `audit.log`, and queued jobs
all survive across any number of updates.

---

## Build a signed release bundle (off-site, on a connected host)

The bundle is a tarball containing a `VERSION` file and an offline pip
wheelhouse, plus a required detached signature and an optional checksum:

```bash
# 1. Assemble the payload
mkdir -p release/wheels
echo "1.8.1" > release/VERSION
python -m pip download --only-binary=:all: \
  'openadapt-flow[privacy]==1.8.1' -d release/wheels

# 2. Archive it
COPYFILE_DISABLE=1 tar czf release-1.8.1.tar.gz release

# 3. Optional transport-corruption check
shasum -a 256 release-1.8.1.tar.gz > release-1.8.1.tar.gz.sha256

# 4. Authenticity: detached signature with your PINNED vendor key (REQUIRED)
#    minisign (ed25519):
minisign -Sm release-1.8.1.tar.gz            # -> release-1.8.1.tar.gz.minisig
#    or openssl (RSA/ECDSA PEM key):
openssl dgst -sha256 -sign vendor_priv.pem \
  -out release-1.8.1.tar.gz.sig release-1.8.1.tar.gz
```

The `VERSION` value must exactly match the single `openadapt_flow` wheel in the
wheelhouse. `COPYFILE_DISABLE=1` prevents macOS metadata sidecars from entering
the release layout.

Copy the archive, signature, and optional `.sha256` onto removable media. Keep the
**private** signing key off the clinic box forever; only the **public** key is
staged on the box (`updates.vendor_pubkey`).

---

## Apply an update (on the clinic box, offline)

```bash
cd deploy/on-prem

# Option A — point the config at the staged files (updates: block in onprem.yaml)
sudo ./install.sh --update --config onprem.yaml

# Option B — pass paths explicitly
sudo ./install.sh --update --config onprem.yaml \
  --release   /media/usb/release-1.8.1.tar.gz \
  --checksum  /media/usb/release-1.8.1.tar.gz.sha256 \
  --signature /media/usb/release-1.8.1.tar.gz.minisig \
  --pubkey    /srv/openadapt/keys/vendor.pub \
  --sig-tool  minisign
```

What `--update` does, in order — **it never flips `current` until every gate
passes**:

1. **Verify authenticity**: the detached signature must verify against the
   pinned public key. GPG verification uses an isolated temporary keyring, not
   the host's trusted-key set. If a checksum is present, it must also match.
2. **Extract and build safely**: traversal paths, links, devices, duplicates,
   oversized payloads, ambiguous layouts, and version/wheel mismatches are
   refused. The new `releases/<version>/` uses offline, wheel-only `pip
   --no-index`; the running release is untouched.
3. **Smoke-test** the new release: the engine CLI must run and the air-gap
   acceptance gate (`verify-airgap.sh`) must pass.
4. **Atomically flip** `current` -> the new release and record the outgoing one
   as `previous`. Release changes are serialized. PHI-free prepared/completed
   records are appended to the same locked, hash-chained audit log as runs.

If step 1, 2, or 3 fails, the half-built release dir is removed and the box stays
on the release it was already running. **There is no window in which a part, or
unverified, release is live.**

---

## Roll back (instant, no rebuild)

```bash
sudo ./install.sh --rollback --config onprem.yaml
```

Flips `current` back to `previous` atomically and restores a roll-forward pointer.
A transient `rollback-forward` link keeps the outgoing release reachable across
power loss between pointer changes; the next update or rollback reconciles it.
A `rolledback` record is appended to `audit.log`. Because both releases already
exist on disk, rollback is a pointer change with zero effect on customer data.

> **Existing-layout migration:** a box installed with the earlier single
> `storage_root/venv` layout is migrated automatically after the signed new
> release passes verification, build, and smoke. The old venv moves into a
> versioned release, remains reachable through the original absolute shebang
> path, and becomes the first `previous` rollback target.

---

## Verify state / audit

```bash
readlink /srv/openadapt/current      # which version is live
readlink /srv/openadapt/previous     # the rollback target
cat      /srv/openadapt/releases/HISTORY
OPENADAPT_ONPREM_AUDIT_LOG=/srv/openadapt/audit/audit.log \
  ./bin/verify-airgap.sh --config onprem.yaml --audit    # walk the hash chain
```

`audit.log` records prepared and completed update/rollback events (versions only,
PHI-free), hash-chained to surrounding run records. Writers serialize the
read-previous-hash plus append transaction, so concurrent runner and updater
events retain one valid chain. If completion logging fails after a pointer flip,
the command returns non-zero and tells the operator to reconcile; it never
reports an unlogged state change as successful.

---

## Prune old releases (disk hygiene)

Keep the exact targets of both `current` and `previous`. Pruning is intentionally
manual: resolve both pointers with `readlink`, compare each candidate's absolute
path, and remove only an older root-owned release that matches neither pointer.
Do not use an age-only `ls | xargs rm` command; activation order and file mtime
are not equivalent.

---

## Test it yourself

`bin/test-update.sh` proves signed update/rollback, authenticity and extraction
refusals, legacy migration, pointer/lock safety, data preservation, and concurrent
audit-chain integrity in a throwaway temp dir, fully offline:

```bash
./bin/test-update.sh        # 43 baseline; 45 with GPG keygen available; exit 0 = pass
```

---

## Vendor visibility

By design the vendor is **blind** to a pilot's health — nothing phones home (see
`COMPLIANCE.md`). Update outcomes live only in the local `audit.log` and
`releases/HISTORY`. Support is out-of-band: the operator emails a scrubbed
`audit.log` slice. Applying and rolling back releases is entirely the operator's
local action.
