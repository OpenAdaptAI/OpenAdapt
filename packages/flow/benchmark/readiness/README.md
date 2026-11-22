# Readiness: synthetic evidence

What `tests/e2e/test_readiness_e2e.py` checks, in headless Chromium on
synthetic MockMed in front of the `effect_e2e` SQLite store (localhost only):

1. A scripted person does 20 tasks (open a patient, add a Triage or Consult
   encounter with a note, save) with the network capture on. Their saves
   land in the store.
2. `readiness import` turns the 20 recordings into reference episodes; none
   is refused.
3. The store is reset to its "before" state before each agent task.
4. A scripted agent, run as a subprocess through `SubprocessLauncher`, does
   the same tasks on the guarded tab. It has 5 planted field errors (two
   wrong encounter types, a letter O for a zero, an empty note, an extra
   letter) and 2 planted wrong-record cases (it opens the next patient).
5. The shadow hold stops every save. The comparator and the scorecard must
   find exactly the planted errors.

Asserted:

- the app's own store and write counter show no write after the run
  (`landed_writes == 0`);
- precision and recall 1.0 on the planted field errors and on the planted
  wrong-record cases; 13 of 20 matched;
- no case with an uncompared field is counted as matched;
- `scorecard.json` holds none of the notes or patient names.

`results.json` is written by the test with `OPENADAPT_WRITE_EVIDENCE=1`, as
is `sample/`: one matched, one mismatched and one `extra_target` task's
`run.json`, `case.json`, `egress.json`, `usage.json` (and
`proposed_write.json`) with that run's `scorecard.json`, for the consumers'
join tests (J1, J2). All values in them are synthetic.

`tests/e2e/test_readiness_adversarial_e2e.py` runs an adversarial agent
on the same app, one route per task, each trying to POST an encounter from
outside the guarded tab: a `target=_blank` form, `window.open` then `fetch`
from the popup, `Target.createTarget` on the browser endpoint then
`Runtime.evaluate` at once (the race against the hold attaching), a page in
a new browser context, a shared worker and a dedicated worker. Asserted, in
the app's own write counter and store: no write landed; every route reached
the hold (a held record per task); every route outside the tab is
`NOT_CHECKED` with reason `extra_target`; `scope_ok` is false. With the
browser-level hold turned off and auto-attach left on, 5 of the 6 routes
landed a write. The same file covers a service worker in a context the
agent made (held, on a small test app with its own POST counter), a session
carried across tasks with `keep_session`, and form values read from a real
page.

Both e2e tests run their scripted agents with `--egress blocked` (the OS
egress block; no model calls), so each task's cost is metered as 0 and its
`egress.json` is checked; they skip where the block is unavailable (it is
macOS only).

`claude_cu_agent.py` is a real-agent candidate for `readiness run
--agent-cmd`: the leaderboard's Claude computer-use loop on the guarded tab,
capped at 25 model calls and $1.00 per task by default. Its unit tests
(`tests/test_readiness_claude_cu_agent.py`) use a stub upstream only.

Not shown here: contamination on a live screen (MockMed's record header
doesn't show saved encounters; the unit tests cover a form that opens
prefilled), out-of-process iframes, the compiled-program path (unit tests
only), and any real model. Running a real computer-use agent through
`readiness run` is the next evidence step.
