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
keyboard edge checks focus, context and the delivery deadline. Uncertain calls
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
