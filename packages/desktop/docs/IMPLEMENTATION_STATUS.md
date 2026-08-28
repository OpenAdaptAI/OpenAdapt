# Implementation status, area by area

What is checked in, what evidence stands behind it, and where the boundary of
that evidence is. The README carries the short version; this is the long one.

## Substrates

Substrate roles and qualification evidence:

| Substrate | Role | Evidence boundary |
| --- | --- | --- |
| Browser (web) | Managed browser recording and execution | Qualification is task- and environment-specific; the signed lifecycle ledger selects the active Production default |
| Native desktop (Windows, macOS, Linux) | Customer-controlled native recording and execution | Qualification is task- and environment-specific |
| Remote display (RDP) | Customer-controlled remote-display recording and execution | Qualification is task- and environment-specific |
| Citrix / VDI | Customer-controlled remote-application recording and execution | ICA/HDX qualification is deployment-specific |

## Areas

| Area | Checked-out implementation | Evidence and admission state |
| --- | --- | --- |
| Python capture CLI | Record, list, inspect, scrub, review, approve, local storage, health, and cleanup commands | Covered by tests; native capture comes from the canonical `openadapt-capture` component |
| Local review gate | Persisted states and egress checks for the capture pipeline | Separate from the `openadapt-flow` certification system |
| Tauri/React cockpit | Login, onboarding, workflows, target-aware record/review/replay/governed run, teach, and settings calling the engine through Tauri commands | Browser and customer-controlled native/remote targets are available as scoped above; the shell renders an engine-offline state when the sidecar binary is absent |
| Rust commands | Generic `engine_invoke` bridge plus typed commands, sidecar spawn/watchdog/shutdown, and event re-emission to the WebView | Compiled and bundled in CI |
| Python sidecar IPC | JSON-lines handler backed by a shared `EngineDispatcher` (recording, compile/replay/run/teach, auth, sync/push, review, config) | Unit and end-to-end tests use mocked external boundaries |
| Tray IPC socket server | Token-authenticated loopback TCP server plus a `~/.openadapt/desktop_ipc.json` discovery file for `openadapt-tray` | Desktop and the shipped tray are not yet validated together end to end |
| Desktop-to-flow handoff | `FlowBridge` launches the pinned Flow runtime embedded in the frozen sidecar as an isolated subprocess | Self-contained; no separate Python or Flow installation |
| Hosted auth and governed handoff | Browser-PKCE and paste-token sign-in; host-bound keychain credentials; exact `openadapt.push-result/v1` review, accepted-ingest, and uncertain-delivery state; local handoff retention; and halted-run break reports | Distribution requires a release-qualified Flow build and live Cloud acceptance before Desktop updates its exact runtime pin |
| Attended phone decisions | One-use QR pairing, protected local evidence, typed allowed actions, runner revalidation, receipts, device revocation, and an optional outbound hosted lane | Device pairing does not replace the deployment's authenticated operator principal |
| Build artifacts | Wheel/sdist, a self-contained PyInstaller engine+Flow runtime, and DMG/MSI/NSIS/DEB/AppImage native jobs | Native jobs prove the frozen browser lifecycle, structurally install/uninstall, and label every platform, architecture, and signing state |
| Native installers | Distinct `desktop-v*` prerelease workflow with final-byte checksums and GitHub provenance | Unadmitted release-candidate lane; signing state is encoded in every filename and workflow qualification remains specific |
| Code signing and updater | Apple Developer ID/notarization and Windows Authenticode are credential-gated and fail closed on partial configuration; the updater feed is disabled | Candidate publication requires the complete platform trust set; the updater is outside the current channel |

CI builds the self-contained `openadapt-engine` freeze. Candidate publication
requires the external code-signing and notarization controls. Production
selection also requires an active central admission for the exact artifacts.

## What the governed push path guarantees

The governed `push` implementation delegates to Flow's exact-hash sanitized
derivative contract. It consumes the closed `openadapt.push-result/v1` schema
and retains the exact review or ingest handoff locally. A recording acceptance
requires the server-owned `artifact_ingest_id` and a governed next action. A
bundle acceptance additionally requires the server-owned workflow identity,
the runtime-attestation binding, and the exact trusted dashboard path. An
unknown child or delivery outcome requires reconciliation and never becomes an
automatic retry. The command never falls back to a direct Desktop upload when
Flow is missing or returns an error. The former direct hosted-ingest backend
now refuses every upload.

This path does not enter a native release until the exact pinned Flow artifact
and the managed Cloud runtime pass the same live acceptance contract. The legacy
customer-owned adapter queue remains paused for this release; its exit
condition is a Flow-owned complete inventory, image-capable scrub, and exact
in-app review. The dormant queue also selects the reviewed scrubbed path again
immediately before egress; a dismissed raw capture is not uploadable.
