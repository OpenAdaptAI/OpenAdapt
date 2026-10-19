# Compliance and release status

This page is for the people who approve a deployment: security, compliance,
and IT. It says what this repository is responsible for, which releases are
trusted, what the CI evidence covers, and where data can leave the machine.
The [README](../README.md) covers what the app does and how to run it.

## Role of this repository

> **Role: installed authoring and operation companion.** The compiler and the
> governed runtime are `openadapt-flow`; this repository publishes release
> candidates around them. A release becomes the active Production default only
> while the canonical signed lifecycle ledger holds a current admission for its
> exact artifacts. If that admission is absent, inactive, expired, or revoked,
> the release is **not actively admitted**.

None of the compiler or the runtime lives here. This repository is the desktop
shell, the local engine that wires it to Flow, and the release machinery for
both. [RELEASES.md](../RELEASES.md) defines the release lanes, the
admission-driven Production channel, and the machine-readable selection rule.

## Release state

There is no trusted signed native build to download yet. Releases from the
OpenAdapt monorepo ship macOS ad-hoc and Windows unsigned until Developer ID or
SignPath signing is configured, and say so in every file name and the release
notes. The older `desktop-v0.15.0` native packages in openadapt-desktop keep
their original ad-hoc and unsigned labels; don't install them.

Each version ships as one `openadapt-desktop-vX.Y.Z` release: the PyPI wheel and
sdist and the native packages built from the same commit. The native version is
synchronized to each engine release, so the native packages are packaging
evidence for that engine, and not a separate supported desktop release. Every
filename encodes its platform, architecture, and signing state:
`adhoc` or `developer-id-notarized` on macOS, `unsigned` or `authenticode` on
Windows, and `github-attested` for the exact Linux bytes.

Native packages are unadmitted release candidates until the central Production
lifecycle activates an exact release. A partial set of signing credentials
stops a native release rather than shipping some platforms unsigned. The
updater and rollback stay disabled until there is an independent signing-key
lifecycle.

On macOS, ad-hoc CI uses a non-hardened overlay, because an identity-less
hardened launcher can't load PyInstaller's identity-less embedded libraries.
Developer ID builds keep the hardened runtime and pass the same Apple identity
into both PyInstaller and Tauri. The installed-app smoke test runs bundled Flow
after the final signing pass, so a structurally valid but unloadable app can't
ship.

- [RELEASE_CANDIDATE_INSTALLERS.md](RELEASE_CANDIDATE_INSTALLERS.md): artifact
  names, verification scope, and provenance.
- [CODE_SIGNING.md](CODE_SIGNING.md): the activation runbook, what to buy,
  which secrets to add, and what each surface may then claim.
- [third_party/README.md](../third_party/README.md): third-party licenses,
  pinned sources, hashes, and modification status. Licenses sit beside the
  components they cover and are checked against the frozen archive.

## What the CI evidence covers

CI builds the frozen `openadapt-engine` sidecar and proves its browser record,
compile, and replay loop on Windows, macOS, and Linux. It installs, launches,
and uninstalls every package on clean runners. That is packaging evidence. It
isn't workflow qualification, which stays specific to each workflow and each
environment.

CI also runs the exact `openadapt-tray` IPC client against this engine's
loopback socket server. The two applications haven't been run together end to
end.

Area-by-area evidence boundaries are in
[IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md).

## Data handling

Recordings stay on the local machine by default. Any egress path still needs a
review of the selected adapter, the configuration, the logs, and your
data-classification policy. Nothing in this repository by itself makes a
deployment HIPAA-compliant.

- `push` delegates to Flow's exact-hash sanitized derivative contract. It
  never falls back to a direct upload when Flow is missing or returns an
  error.
- The former direct hosted-ingest backend refuses every upload.
  `upload --backend hosted_ingest` runs `push` instead.
- Upload to customer-owned storage (S3, R2, MinIO) is paused behind a
  fail-closed gate. Its exit condition is in
  [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md#what-the-governed-push-path-guarantees).
- Run-list item values reach Flow only through an owner-only params file that
  exists for the length of one call. They never enter a process argument list,
  an event, a log line, a notification, or a Cloud payload.
- In the local index database, an item's values, its label, and the expected
  and observed text of a right-record check are stored encrypted (AES-256-GCM).
  The key is in the OS keychain, never in the database. A closed item's values
  are deleted. Without the keychain key, Desktop doesn't run new items.
- Desktop starts without downloading a browser. The first browser workflow
  pulls the Chromium revision pinned by the bundled Playwright runtime into
  `~/.openadapt/browser-runtime`. Air-gapped packages point
  `PLAYWRIGHT_BROWSERS_PATH` at a version-matched prebundle. Native, RDP, and
  Citrix workflows never use that path.

## Shadow mode

Shadow mode is off by default and has no remote, admin, policy, or
configuration-file switch. A person turns it on in the Desktop window after
accepting a versioned notice; the engine checks the notice hash and writes the
acceptance time to an owner-only local file. The header and the tray show
when it's on. Its record is an encrypted buffer in the platform's app-data
folder (Application Support on macOS, LocalAppData on Windows), and shadow
mode refuses to start if that folder resolves inside a known sync root such as
Documents, Desktop, iCloud, OneDrive, or Dropbox. The key is in the OS keychain
(or in memory) and is destroyed, with the buffer, at least every 24 hours,
when shadow mode is turned off, and when the app quits; a buffer left by a
crash is erased at the next start. Shadow mode makes no network connections,
and its commands aren't accepted on the local socket. OpenAdapt never sends
shadow mode data off the computer. These controls stop OpenAdapt, an
employer, and other apps from turning it on or reading it through OpenAdapt;
they don't stop an OS administrator, software running as the user, or on a
managed computer IT administrators and endpoint security or backup tools, from
inspecting or copying local files or memory while shadow mode is on. The
notice says so, says the record may include personal, confidential, or health
information, and advises pausing before viewing sensitive material. **Pause**
is in the header, the tray, and Settings. **Turn off** erases the buffer, the
key, and drafts; **Delete everything and turn off** also removes the ignore
list and withdraws the
acceptance. [SHADOW_MODE.md](SHADOW_MODE.md) has the details.

## Phone decisions

A paired phone receives a request for a decision. A tap doesn't approve the
screenshot the phone showed, and it doesn't prove that anything was saved. The
runner goes back to the live application, repeats the state, identity, and
target checks, and returns a typed result. If a check fails, the run stays
paused where it was.

Pairing authenticates the device session, not the named operator principal
that a qualification contract may require for attribution. Design approval
policies with that in mind. The local portal can serve an approved retained
frame over the customer's own HTTPS origin behind a reverse proxy or VPN; the
hosted outbound lane carries a signed, remote-safe task with no pixels.
[DECISION_PORTAL.md](DECISION_PORTAL.md) has the full contract, including
pairing, evidence handling, reconciliation, escalation, and terminal receipts.
