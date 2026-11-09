# Authentication handoffs

An authentication handoff stops sensitive source retention while a person,
password manager, passkey provider, SSO page, or MFA device completes a login.
Capture keeps the recording open. It stores a small timeline marker and resumes
normal recording only after it has retained a new exact frame.

Authenticate before recording when that produces a complete demonstration.
Use a handoff when authentication must occur inside the workflow or when a
session can expire during a long recording.

## What Capture suppresses

The protected interval covers every source owned by the native recorder:

| Source | Behavior during the handoff |
| --- | --- |
| Screen and video | Capture retains no frame. The encoded video holds its prior visual state until the fresh resume frame. |
| Mouse and keyboard | The native observer drops the event before journal reservation and before any structural lookup. |
| Accessibility | UIA, AX, and AT-SPI observations do not run for protected input. |
| Window metadata | Capture does not retain active-window titles, bounds, or state. |
| Audio | The audio process replaces microphone chunks with generated silence so the audio clock stays aligned. It does not retain the protected waveform. |
| Browser category | The marker declares browser data suppressed. The supported Playwright browser recorder remains in `openadapt-flow` and needs its own matching source boundary. |

The recorder does not add black frames or synthetic action rows. A black frame
can look like a real application state. An action row without pixels can look
replayable. The sidecar names the gap directly.

## What the marker contains

`authentication-handoffs.json` uses
`openadapt.capture.authentication-handoffs/v1`. The capture seal inventories
its exact bytes. Each interval contains:

- a random interval UUID;
- one or more method classes from `password_manager`, `passkey`, `sso`, `mfa`,
  `device_unlock`, and `other`;
- `requires_user_presence` and `saved_account_selected` booleans;
- start and end times on the capture clock;
- an outcome: `completed`, `cancelled`, `failed`, or `aborted`;
- the fixed list of suppressed source categories; and
- the timestamp, source ordinal, pixel digest, capture source, and optional
  window geometry generation of the clean entry frame;
- for a normal close, the same proof fields for the fresh resume frame.

The API has no field for a provider name, account identifier, username,
password, passkey material, OTP, recovery code, vault item, or free-form note.
Method classes describe the handoff without describing the credential.

An autofill login fits this contract. Set `methods="password_manager"` and
`saved_account_selected=True`. The click on the saved account, the account
chooser, and any filled values stay inside the protected interval.

## Recorder API

The owner can control a recorder in the same process:

```python
from openadapt_capture import Recorder

with Recorder(
    "./capture",
    task_description="Download the monthly statement",
    window={"owner": "Google Chrome", "title": "American Express"},
) as recorder:
    if not recorder.wait_for_ready():
        raise RuntimeError("Capture did not become ready")

    handoff = recorder.begin_authentication(
        methods="password_manager",
        requires_user_presence=True,
        saved_account_selected=True,
    )

    # The person selects the saved account and completes any OS or MFA prompt.
    # Wait until the application no longer shows credential UI before ending.

    marker = recorder.end_authentication(
        handoff,
        outcome="completed",
        timeout=10,
    )
    assert marker.resume_frame is not None
```

`begin_authentication()` returns only after in-flight screen, input, window,
and structural operations finish. When audio is active, it also waits for the
microphone process to acknowledge suppression after its callback lock is clear.
The screen worker retains one clean entry frame before the open marker is
written. This closes the after-frame binding for an action that occurred just
before the handoff. New input stays blocked across that cut.

If that begin barrier times out or the open marker cannot be written, the
recorder stays protected and fails the recording. Resuming would create an
unmarked evidence gap.

`end_authentication()` first changes the interval to a resuming state. Input,
window metadata, structural data, and audio remain protected. The screen worker
then acquires and journals one new frame. For window-scoped recording, both the
entry and resume frames use the same source ordinal as their exact geometry.
Capture writes the resume proof to the sidecar before it reopens the other
sources.

If the timeout expires, the interval stays protected and the caller can repeat
the same end request. A source-frame error follows Capture's fail-loud media
rule and ends the recording. Capture never resumes input because a timer
expired or a frame failed.

## Authenticated process control

A launcher, Desktop, or Flow process can use the owner-only local control
channel:

```python
from openadapt_capture import (
    begin_authentication_handoff,
    end_authentication_handoff,
)

handoff = begin_authentication_handoff(
    session_id=session_id,
    methods=("password_manager", "mfa"),
    requires_user_presence=True,
    saved_account_selected=True,
)

# Complete the attended login, then close the protected interval.
marker = end_authentication_handoff(
    handoff,
    session_id=session_id,
    outcome="completed",
)
```

These requests use the same loopback capability, process identity, message MAC,
clock bound, and replay checks as `status_recording()` and `stop_recording()`.
The begin client selects the interval UUID. A retry with the same UUID and the
same parameters returns the same handle. A retry with different parameters is
refused. End is idempotent for the same interval and outcome.

`RecorderStatus.authentication_protected` lets an owner show a local privacy
indicator. It does not expose method classes or account data.

## Stop and failure behavior

Stopping a recorder during a handoff closes the interval as `aborted`. It does
not take a terminal screenshot from the credential UI and does not claim a
resume frame. The rest of the capture can still finish and seal if its retained
evidence is valid.

Stopping during the entry cut, before the open marker exists, fails the
recording. Capture cannot seal a source gap that has no interval marker.

Nested handoffs are refused. An end call with the wrong handle is refused. A
method outside the fixed vocabulary is refused before the interval starts.
Current captures cannot contain an open interval at the completion seal.

`CaptureSession.load_verified()` checks each interval against the immutable
database. Both proofs must name retained screen rows with the same timestamps
and source ordinals. The entry proof must be the last pre-handoff frame. No
action, browser, window, or other screen row can occur inside the protected
range. Window-scoped proof must match the frame's capture source and geometry
generation. When PNG pixels are present, their source-pixel digest must match
the marker.

## Ownership boundary

Capture proves observation behavior. It proves that the named sources were not
retained and that a fresh frame existed before normal observation resumed.

Capture does not prove that the application accepted the login. The
`completed` outcome means that the owner completed the handoff. A compiler or
runtime must verify the application state before it treats the user as
authenticated. That contract belongs outside `openadapt-capture`.

Capture also does not store credentials or ask a password manager to release a
secret. Password managers, passkey providers, operating-system account choosers,
and MFA devices keep that authority. OpenAdapt can coordinate the attended
gate while those systems retain credential custody.

The native handoff does not turn the repository-only Chrome extension into a
supported recorder. The Playwright path must suppress DOM values, page frames,
network-derived observations, and browser input at its own source boundary.
The two recorders can share the schema after that browser contract exists.
