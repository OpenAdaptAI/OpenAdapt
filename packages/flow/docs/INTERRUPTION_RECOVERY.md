# Interruption recovery

Production apps interrupt a run: a session warning, an "Update available"
banner, a feature tour, a notification, a modal from background polling.
Without help, each one stops a compiled run for a person. An *interruption
class* lets the run clear a known interruption itself, but only when the
dismissal is provably read-only. Anything else stops the run as before.

## The policy

A run clears an interruption only when all four of these hold:

1. **It matches a class by identity, on the whole dialog.** Every text the
   class lists is on screen, and the dismiss control is found by its role,
   accessible name, selector, automation ID or window. Position is never
   used. The button word never decides: the backend reads the whole
   interruption (its full text, accessible title and owning window). The
   run halts unless all of these hold:
   - the dialog shows every class text;
   - it carries the class's title and window, if the class declares them;
   - it holds no business word (`BUSINESS_WORDS`: a patient, a record, a
     chart, an order, or saving, applying, changing, discarding, deleting
     or confirming anything);
   - it says at most 240 characters beyond the class texts and the
     control's label.

   "Keep my changes? [Yes]" and "Apply template to patient? [OK]" can look
   like "Stay logged in? [OK]", but they never match a class. A backend
   that can't read the whole dialog can't prove the class, so the run
   halts. This applies to pixel surfaces, and to native desktop backends
   until they implement `interruption_text`.
2. **The dismissal does not commit anything.** The dismissal is Escape, or a
   control whose label is in a closed set: close, dismiss, not now, maybe
   later, skip, got it, continue session, stay signed in, and similar
   (`NON_COMMITTING_LABEL` in `openadapt_flow/interruptions.py`). "OK",
   "Continue", "Yes", "Accept" and any write word are outside the set, so a
   class that uses one is refused when it loads.

   The label checked is the label of the control that gets clicked. A
   control class declares its role and accessible name, and a class without
   both is refused when it loads. A selector or automation id may resolve to
   any control, so before the click, and again at the last moment before
   input, the backend must show that the resolved element is the one
   control with the declared role and exact accessible name. If the class
   says "Close" and its selector finds "Sign out", the run halts with no
   click. So does a backend that can't read the live control
   (`structural_control_is`), which today means every backend except the
   browser.

   The label must also hold for the whole click. In the browser, a click
   guard is installed at the start of every document, ahead of any page
   script, so its capture-phase listeners on `window` run before any page
   listener. It is dormant until a dismissal arms it on the bound control.
   While armed, it watches the control, the nodes its `aria-labelledby` and
   `aria-describedby` name, its labels, and the page's stylesheets. At
   every press and click it also re-reads how each of them renders: the
   computed `::before` and `::after` content, their own computed content,
   and `display` and `visibility`, so a stylesheet rule added through the
   CSSOM (`insertRule`, `adoptedStyleSheets`) that relabels or hides the
   text counts as a change. If any of them changed before a press or click
   arrives, or the input is not on
   the control, the guard cancels that input before the page sees it. The
   run then halts with the dismissal reported as delivered and its outcome
   uncertain (`LabelChangedDuringClick`), so it is reconciled, never
   retried. A label that turns "Close" into "Sign out" as the pointer
   arrives is caught this way. A document that loaded before the guard was
   installed, a guard that can't arm, or one whose listeners are gone after
   the click (for example, erased by `document.open`) halts too. Learning a
   class from a halt uses the same guard.

   The report agrees with the fence. Every request made while the fence is
   up is listed in the dismissal's `requests_seen`, reads included, and a
   write in that log that no write attempt names is added as one. So a
   report never says "no write attempts" while the fence saw a write.
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

## A popup that steals the click

A toast or modal can appear after the last check and before the click
lands, so the input reaches the popup instead of the target. On the web,
every structural click is checked twice:

- **Before the input.** Playwright's actionability trial and the
  existing target and context checks refuse a covered target before any
  input is sent.
- **After the input.** A witness armed just before the click records,
  from the first moment the page sees each trusted pointer event,
  whether it reached the verified element. If any part of the click
  (press, release or click) reached something else, the outcome is
  reported as delivery uncertainty (`InputLandedElsewhere`). The run then
  stops and reconciles; it never retries. If the click navigated away,
  the witness is gone with the old document, and the checks before the
  input stand alone.

The same check after dispatch on native desktop and RDP/Citrix backends
(the foreground window, the focused element, and a frame diff of the
target region) is not built yet.

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
| Session text plus "Apply template to patient?" behind "Stay signed in" | halt before any input | passes |
| A popup covers Save as the pointer arrives | refused before any input (pre-dispatch trial) | passes |
| A popup covers Save between press and release | delivery uncertainty `InputLandedElsewhere`, no retry, nothing written | passes |
| Dialog mentions "Keep my changes?" or "Unsaved record" | halt before any input | passes |
| Backend can't read the whole dialog | halt before any input | passes |
| Dialog says much more than the class texts; title mismatch | halt | passes |
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
  the native continuity check after dispatch, and real-app runs on
  OpenEMR and Dolibarr (including behind a chaos proxy) are not covered
  here.
