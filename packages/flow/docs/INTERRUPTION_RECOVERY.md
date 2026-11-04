# Interruption recovery

Production apps interrupt a run: a session warning, an "Update available"
banner, a feature tour, a notification, a modal from background polling.
Without help, each one stops a compiled run for a person. An *interruption
class* lets the run clear a known interruption itself, but only when the
dismissal is provably read-only. Anything else stops the run as before.

## The policy

A run clears an interruption only when all four of these hold:

1. **It matches a class by identity.** Every text the class lists is on
   screen, and the dismiss control is found by its role, accessible name,
   selector, automation ID or window. Position is never used.
2. **The dismissal does not commit anything.** The dismissal is Escape, or a
   control whose label is in a closed set: close, dismiss, not now, maybe
   later, skip, got it, continue session, stay signed in, and similar
   (`NON_COMMITTING_LABEL` in `openadapt_flow/interruptions.py`). "OK",
   "Continue", "Yes", "Accept" and any write word are outside the set, so a
   class that uses one is refused when it loads.
3. **No watcher sees a write.** The dismissal runs under every write watcher
   the run has (`openadapt_flow/runtime/effect_watch.py`):
   - in a browser, the write fence. It is the same hold that guards a
     checked save, with nothing allowed through. Every write request, new
     WebSocket, worker and socket message is stopped before it leaves the
     page, and each is reported.
   - on any backend, the system-of-record read-back that the run's writes
     are checked with. It is read before and after the dismissal; any
     difference is a write. This is the watcher for desktop and RDP or
     Citrix runs, which have no network to fence.

   A run with no watcher, a fence that can't go up, or a read-back that
   can't be read does not dismiss. A dismissal that a watcher saw write
   stops the run as a safety halt.
4. **The expected screen comes back.** After one dismissal the screen must
   settle, the class's text must be gone, and the step then resolves its
   target through its normal identity check. If the screen isn't the one
   the step expects, the run stops there.

A run makes at most one dismissal per class per step, and never retries one.

## The record

Each dismissal is recorded in the step's `interstitial_actions` in the run
report (`report.json`, which the run seal signs). The record includes the
class, the control's label, the frames before and after, the watchers that
were up (`effect_watchers`), and any write they saw (`write_attempts`).

## Declaring classes

Classes are data. The engine has no rule for any particular app. Put a
per-app file next to the deployment and point `runtime.interruptions_path`
at it:

```yaml
interruptions:
  - name: session expiry warning
    texts: ["Your session will expire"]
    dismiss: {control: {role: button, name: Stay signed in}}
  - name: update banner
    texts: ["Update available"]
    dismiss: {control: {role: button, name: Close}}
  - name: product tour
    texts: ["Take a quick tour"]
    dismiss: {key: Escape}
```

`texts` must all be present. `dismiss.control` takes `role`, `name`,
`selector`, `automation_id` and `window`. `gone` names the text whose
absence clears the interruption; it defaults to the first text. If any
entry fails the policy, the run refuses to start.

## Learning a class from a halt

When a person clears a new interruption during a halt,
`openadapt_flow.interruptions.learn_interruption` can learn it as a class.
It builds the class from the interruption's texts and the control the
person chose, and checks that class against rules 1 and 2. It then clicks
the control on the live screen under the run's watchers. It keeps the class
only when no watcher saw a write and the text is gone. If the dismissal
writes, the class is not learned. `save_interruption_class` adds the class
to the app's file.

## Cases

These cases were registered before the runs. The real-browser cases are in
`tests/test_interruptions_browser.py`, which uses headless Chromium, the
real replayer and OCR, and a local synthetic app that saves with
`POST /save`. The fake-backend cases are in `tests/test_interruptions.py`.

| Case | Expected | Result |
|---|---|---|
| Session warning, "Stay signed in" (sends only a GET) | cleared, save lands | passes |
| "Update available" banner, "Close" | cleared, save lands | passes |
| Session warning whose "Stay signed in" POSTs /logout | safety halt; the logout is stopped in the browser, nothing saved | passes |
| Unknown dialog | halt, no click | passes |
| Class whose control reads "OK", "Continue", "Accept", "Confirm" | refused at load | passes |
| Class found only by position | refused at load | passes |
| Desktop run: Escape, read-back unchanged | cleared (`record` watcher) | passes |
| Read-back changes during the dismissal | safety halt | passes |
| Run with no watcher; fence refused | no dismissal, halt | passes |
| Dismissal leaves a different screen | halt at the step's identity check | passes |

## Limits

- A `GET` request with a side effect gets through the browser fence. On
  desktop backends, the read-back sees only the records that the run's
  verifier reads. Configure a read-back that covers the records the run
  touches.
- A session keep-alive that POSTs is a write, so the run halts on it. Use
  a session warning whose control only closes it, or re-authenticate.
- Consent prompts (cookie "Accept" or "Reject") are decisions, not
  dismissals, so they stay outside the closed set.
- Session re-authentication, a bounded re-resolve after late page loads,
  and real-app runs on OpenEMR and Dolibarr are not covered here.
