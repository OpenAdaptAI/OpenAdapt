# Chrome extension evidence: native host registration

Part of [OpenAdapt#1872](https://github.com/OpenAdaptAI/OpenAdapt/issues/1872).
The optional OpenAdapt Chrome extension sends evidence to a local
native-messaging host (`packages/flow/chrome_extension/native_host`). Chrome
starts that host only when a manifest named `com.openadapt.extension.json` is
in a folder Chrome reads, and only for the extension ids the manifest allows.

Desktop can set this up for you. It is off by default.

## Turn it on

Load the unpacked extension (`chrome://extensions` → Developer mode → Load
unpacked → `packages/flow/chrome_extension`), copy its id, then start Desktop
with:

```bash
OPENADAPT_CHROME_EXTENSION_ID=<32-letter id>
# optional: chromium instead of Google Chrome
OPENADAPT_CHROME_EXTENSION_BROWSER=chromium
# optional: one profile instead of the per-user folder
OPENADAPT_CHROME_NATIVE_HOSTS_DIR=/path/to/profile/NativeMessagingHosts
# optional: the host script, when openadapt_flow is not an editable install
OPENADAPT_CHROME_EXTENSION_HOST_SCRIPT=/path/to/openadapt_extension_host.py
```

## What Desktop does

- **Recording starts** (native recording): Desktop writes the manifest, a
  launcher, an "active" marker holding `active` and an empty `<marker>.gate`
  file. The manifest allows exactly `chrome-extension://<id>/`. The launcher
  runs the host with
  `--recording <capture folder> --active-file <marker> --active-lease 30 --active-lock`.
  Desktop holds an exclusive lock on the marker for the whole recording and
  touches it every 5 seconds. If the lock can't be taken, nothing stays
  registered and recording continues without extension evidence.
- **Recording stops**: Desktop stops touching the marker and closes the
  locked file, then checks (with a non-blocking shared lock) that no
  exclusive holder remains. The kernel also drops the lock if Desktop exits.
  A host that is already running then refuses its next write. Desktop next
  takes the gate exclusively: each host write holds the gate (shared) from
  its check until the evidence is on disk, so a write that passed its check
  before the stop finishes before stop returns. Stop waits at most 10
  seconds and reports the recording as not revoked if a write is still
  running. Then it removes the manifest, the launcher, the marker and the
  gate. The host checks the lock, the marker and the capture folder before
  every write, never creates the capture folder, and so never recreates a
  deleted one.
- **Known limits of the lock**: a flock belongs to the open file
  description, so a child process forked while recording (without exec)
  keeps it held; POSIX leaves a descriptor's state unspecified when `close`
  reports EINTR (Linux always releases it); flock over network filesystems
  may be emulated or refused. Desktop's stop-time check catches a lock that
  is still held and reports the recording as not revoked; the 30-second
  lease then ends the host's writes. Where locking is refused, registration
  fails and recording continues without extension evidence.
- **Cleanup fails**: if the marker can't be removed, Desktop overwrites it
  with `revoked` and backdates it. If even that fails, the released lock
  normally already stops the host, and the lease is a second guard: the marker goes
  stale after 30 seconds because nothing renews it. Desktop logs an error and
  makes no new registration until the old marker is revoked; recording itself
  continues without extension evidence.
- **Engine start and shutdown**: no recording is active, so Desktop removes
  any registration it wrote earlier (after a crash, or after you turned the
  setting off).

Fail-closed rules:

- The id must be 32 letters `a`–`p`. Anything else (including `*`) is refused
  and nothing is written. Recording still works without extension evidence.
- Desktop never overwrites or removes a registration it did not write (for
  example one made by hand with `install_host.py`). Its launcher carries an
  ownership marker line.
- The manifest is `0600`, the launcher `0700`. All files are local; there is
  no network.

## Not yet

- **Packaged app.** The frozen Desktop build does not ship the host script or
  a Python that can run it, so registration is skipped there. It works from a
  source or editable install.
- **Windows.** Chrome reads host registrations from the registry. Desktop
  skips registration on Windows.
- **Flow-target recordings** (web or desktop targets driven by
  `openadapt-flow record`) do not register the host. Only native recordings do.
- **Store listing.** The extension id is the unpacked id, which depends on the
  load path.
