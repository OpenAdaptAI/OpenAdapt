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

`results.json` is written by the test with `OPENADAPT_WRITE_EVIDENCE=1`.

Not shown here: contamination on a live screen (MockMed's record header
doesn't show saved encounters; the unit tests cover it), the compiled-program
path (unit tests only), and any real model. Running a real computer-use
agent through `readiness run` is the next evidence step.
