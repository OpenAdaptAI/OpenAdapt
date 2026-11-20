# Native keyboard focus leases

Native keyboard input has three entry points: text, a key and a chord. A locator
or earlier focusing click alone cannot show which live control received them.
A form can replace its focused control while keeping its label, geometry and
value, and a frame can change after identity verification but before input.

The recorder passively observes the actual focused UI Automation control for
all three entry points. Compilation uses that event's own before frame and the
same operator-selected identity crop as pointer actions. Missing observation
leaves the action unanchored; malformed observation refuses compilation.

An authenticated agent that advertises `native_keyboard_lease_v1` issues one
15-second lease over a unique live focused control, runtime fingerprint,
application/executable/session/environment and exact PNG bytes. Issuing a lease
invalidates its predecessor. Delivery consumes it before checking bindings.
Unicode clipboard preparation finishes before final checks. Every physical
keyboard edge checks actual focus twice, the current locator, RuntimeId/process,
HWND/class, bounds and hit-test, plus context and the delivery deadline. Full
locator uniqueness census remains mandatory at issuance and final pre-input
handoff. The edge check compares the already-bound runtime fingerprint; it
never re-resolves the desktop or chooses another matching control. Replacement
of a control or context during input becomes uncertain and cannot be retried. Uncertain calls
release held keys and produce no positive acknowledgement. A key that
intentionally changes focus can therefore be reported uncertain.

The agent alone creates a typed delivery receipt after the actual input path
returns. The client requires its operation and native target fingerprint to
match. Replayed leases, changed focus, invalid frames and changed context refuse.
Lost or malformed acknowledgements are uncertain and are never retried.

A stale-frame response permits at most two keyboard-only reacquisitions. This
requires an authenticated response for the consumed lease proving zero keyboard
input, real changed-pixel geometry, full target/identity/context/authorization
revalidation, the same runtime target fingerprint, and an unchanged readable
field baseline for text. Effectful, API-bound and irreversible actions do not
use this retry. The earlier focusing click is never repeated. Exact PNG equality
still gates delivery; no caret or focus pixels are ignored. Reports retain the
refused lease hash, geometry and keyboard phase through checkpoint/resume.
The report before-frame is saved from the actual authorized keyboard frame
immediately before each input attempt, including the first attempt and an
uncertain acknowledgement after partial delivery. Earlier focusing observations
do not stand in for that later input boundary.

## Evidence and limits

The opt-in Windows CI proof uses a synthetic WinForms app and an independent
SQLite reader. It requires text, Enter and a chord; plants stale frames, changed
focus, same-form control replacement, consumed-token replay and a lost actual
acknowledgement; and retains delegated HTTP envelopes and native evidence. A
Mac skip is collection evidence only. The PR records the actual Windows result.
The shared runtime has property and entry-point tests; full Windows
qualification remains a separate draft until its signed campaign passes.

No app names or app-specific branches are added to the engine. The defect class
is temporal attribution of keyboard input across all native entry points.
Same-form replacement, replay and lost acknowledgement are held-out cases beyond
the unreceipted Enter/stale-frame campaign failures that motivated the work.
This advances independently verifiable action receipts and refusal evidence.

## Exact editable-value observation

`text_value_at` and `focused_text_value` consume the same v2 observation at
`/uia/editable-value`. A readable empty string is a value. Password or unknown
password state, read-only or unknown editability, missing/ambiguous focus,
replacement, changed value and oversized values refuse. No accessible Name,
MSAA value, normalized text or truncated prefix substitutes for the exact
value. Current observed, armed and consumed keyboard bindings remain
authoritative for read-back; unknown stays a refusal.

A full-looking ValuePattern string alone does not establish completeness. The
native diagnostic independently measured live stored lengths of 4,097 and
8,196 UTF-16 units while the UIA wire returned 4,096. The canonical
`win_agent/native_value.py` helper now requires two stable Unicode
WM_GETTEXTLENGTH/WM_GETTEXT extent/full reads and two exact UIA reads to agree.
It checks caller scope, UIA HWND/PID/RuntimeId and native HWND/PID/thread around
each read, uses bounded buffers and message deadlines, and preserves exact
UTF-16 bytes. Missing native handles, unknown readability, incomplete copies,
timeouts, changed identities or source disagreement are incomplete. Native text
never silently replaces a disagreeing UIA value.

Positive v2 observations carry a closed completeness proof with source method,
owner, UTF-16 extent/hash and both read counts. Clients require that proof and
the v2 capability; a legacy complete flag cannot authorize a baseline. Empty may
be complete for editable input while remaining unmasked in approval projection.
The same shared helper commit is the prerequisite for #2291's separate approval
consumer. Incomplete or unsupported values cannot fall back to OCR that omits
an unseen suffix. This branch does not import that whole approval change or
claim its qualification.
