# Deviations from the pre-registered protocol

Every change made after the protocol was frozen (OpenAdapt commit
22e1c092f, openadapt-internal commit cd7f5a2), with its reason and its effect
on the scores.

**None to the scored runs.** The protocol, harness and case sets ran as
frozen, and the frozen headline numbers are unchanged.

## Changes after review (2026-11-12)

An independent review of the PR found the following. None changes a scored
row's claim or verdict, or the headline.

1. **Faults that met a rejected save.** The proxy applied each fault to the
   first save request whatever the app answered. In 13 public runs (all
   Redmine: claude 4, openai 9) and 1 held-out run (claude, Dolibarr) the app
   rejected that save, so no faulted write was committed, but the row says
   `fault_delivered: true`. Added: a per-app success predicate in the adapter
   data (`save_ok_status`, `App.save_accepted`), a per-agent, per-family
   count of these runs (`fault_hit_rejected_save*` in `LEADERBOARD.json`),
   and a sensitivity check that drops every run whose fault never reached a
   committed write (claude 28/116, openai 21/96), labelled as not planned in
   advance. RESULTS.md's duplicate and wrong-value bullets were corrected.
   For future runs the proxy now counts a fault as delivered, and runs a
   collateral change, only when the app accepted the save; no scored run was
   rerun.
2. **Session tokens.** OpenEMR's `token_main` session token (from throwaway
   local containers) was in the `end_url` of every OpenEMR row. It is now
   redacted (`token_main=REDACTED`) in all rows, and `run.py` redacts URLs
   before writing them. The scorer doesn't read `end_url`, so every number
   reproduces.
3. **Soft deletes.** The oracle now treats a row EspoCRM has deleted softly
   (`deleted = 1`) as not present. The scored verdicts were computed at run
   time and are kept as they are; the review found no public run in which an
   agent deleted an EspoCRM record.

Not deviations, recorded here so nothing is missed:

- Three Dolibarr runs (claude r2 DO-RF, openai r3 DO-LR, claude r3 DO-RF)
  hit a browser screenshot error and were retried once, as the protocol's
  harness-failure rule allows. Each row's `attempts` field says so.
- After the runs, the OpenEMR task's wording ("sex") turned out to be
  ambiguous against the app's labels ("Birth Sex" and "Sex"). The runs are
  scored as frozen. RESULTS.md adds a sensitivity check without OpenEMR,
  labelled as not pre-registered.
- The four new apps' stacks were torn down when their runs ended, while
  Medplum's runs went on. No run was affected.
- The held-out case IDs, fault families and per-run outcomes (no task values
  or seeds) were briefly visible in the PR branch history.
