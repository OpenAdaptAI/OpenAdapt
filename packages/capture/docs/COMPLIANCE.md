# Compliance and data handling

This page is for operators, security reviewers, and packagers. It covers what
a recording can contain, where it may go, the licensing boundaries of the
package, and how a Capture release gets its product state. The
[README](../README.md) covers installation and use.

## Release state

An exact Capture release reaches Production only through an active signed,
expiring, revocable release admission. If the admission is missing, expired,
revoked, mismatched, or unverifiable, the release is
**not actively admitted**. The validator doesn't fall back to an older
admission or hand out a lifecycle label instead. A passing unit test or an
installable package doesn't admit a recording path.

The current state is machine-readable in the
[signed production record](https://docs.openadapt.ai/production-lifecycle.json).
What each substrate can claim, and the evidence behind it, is in the
[repository lifecycle registry](https://github.com/OpenAdaptAI/.github/blob/main/REPOSITORY_LIFECYCLE.md)
and [Flow's product status](https://github.com/OpenAdaptAI/openadapt-flow/blob/main/docs/PRODUCT_STATUS.md).
The qualification a release must pass is described in
[DESIGN.md](DESIGN.md#release-qualification).

## What a capture contains, and where it must stay

A raw capture can hold everything visible on screen and everything typed:
credentials, personal data, and protected health information. Keep the whole
directory inside its approved local boundary and give it a retention policy.

An authentication handoff keeps the named sign-in interval out of the raw
capture. Content retained before or after that interval remains sensitive.
See [AUTHENTICATION_HANDOFF.md](AUTHENTICATION_HANDOFF.md).

Capture doesn't upload a session. The sharing command (`capture share`) and the
profiling transfer (`send_profile`) are the only two uploads, and both run
only when the operator asks for them. Installing the `privacy` extra doesn't
scrub anything by itself.

Structural observations carry control names, window titles, and accessibility
ancestry, so they stay inside the same local boundary. Accessibility text is
capped at 512 characters per field, and a longer value is omitted rather than
truncated, so a consumer never treats half a string as exact identity
evidence. Flow refuses desktop `--secret` authoring until its source-time
field-redaction contract can prove the sensitive value was never retained.

## Microphone narration

Microphone narration (`capture record --audio`, or `RECORD_AUDIO`) is off by
default, and it's the riskiest thing this package can observe. Speech has no
structure to scrub the way a screen field does, and a voice is itself
biometric identifying data (HIPAA Safe Harbor identifier 16).
`openadapt-privacy` has no audio modality and `openadapt-flow` refuses audio
artifacts, so a waveform has no sanitized derivative and can never be cleared
for egress. These boundaries are in code:

- Transcription runs on the device. There is no remote backend. With no local
  backend installed, `--audio` and `capture transcribe` refuse with an install
  hint instead of reaching for a network recognizer.
- `--audio` refuses before the microphone opens if it can't transcribe
  locally, so you never capture a session you then have to throw away.
- The waveform is discarded after transcription unless
  `RECORD_AUDIO_RETAIN_WAVEFORM` is set.
- The transcript is never logged. Narration carries names, dates of birth, and
  diagnoses.
- The HTML viewer doesn't embed audio by default, because that file is
  self-contained and easy to forward.
- `--audio` prints a microphone notice before recording starts.

Transcript text is unscrubbed free text. Treat it as at least as sensitive as
the rest of the capture. Nothing downstream reads it today.

## Recorder control channel

`capture status`, `capture stop`, and the Python control functions talk to a
running recorder over an endpoint that listens on IPv4 loopback only. Every
request carries an authenticated session capability stored in an owner-only
runtime file (`0700`/`0600` on macOS and Linux, a protected current-user DACL
on Windows). On macOS the recorder also removes extended ACL entries and
verifies they are absent. The capability never reaches command arguments,
logs, or the capture directory.

`capture stop` is bound to the exact session ID, process ID, and process start
identity. It waits for the producer and the writer to shut down, reconciles
producer counts against committed rows, checks database integrity, validates
the replay-relevant events, and writes atomic terminal metadata before it
returns success. A timeout, a worker failure, an invalid capability, a stale
process, or an ambiguous set of active sessions all return non-success. Repeat
calls share one finalization result.

## Licensing

This package is MIT. Shipping FFmpeg inside it would relicense the package, so
the wheel and the source distribution carry no FFmpeg bytes, and
`scripts/verify_distribution.py` refuses a release archive that does.
`capture install-ffmpeg` downloads nothing until the operator runs it. The
pinned build is LGPL-2.1-or-later, configured with `--disable-gpl`,
`--disable-nonfree`, and `--disable-version3`; FFmpeg's own `LICENSE.md`
states that the GPL components it leaves out are not used by default. The
install puts that licence text beside the binaries and writes a receipt that
names the archive, its digest, and the matching upstream source tarball.

Neither transcription extra is vendored into the MIT wheel; users install them
into their own environment. `transcribe-fast` pulls PyAV, whose published
wheels bundle GPL-licensed `libx264` and `libx265` binaries. That's fine for a
user-side `pip install`, but not inside a first-party installer, sidecar, or
image. Packaging work that bundles a transcription backend should prefer an
MIT backend with a clean dependency tree and should inspect the built archive.

## Browser extension prototype

The Chrome extension and its WebSocket bridge live in
[`experimental/`](../experimental/), outside the package. They have no
source-time secret exclusion, and the bridge accepts unauthenticated local
connections. The published wheel and source archive exclude both. Why the
supported browser recorder lives in Flow instead:
[BROWSER_EXTENSION_BOUNDARY.md](BROWSER_EXTENSION_BOUNDARY.md).
