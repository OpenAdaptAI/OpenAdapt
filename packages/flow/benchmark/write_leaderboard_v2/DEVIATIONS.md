# Deviations from the pre-registered protocol

Every change made after the protocol was frozen (OpenAdapt commit
22e1c092f, openadapt-internal commit cd7f5a2), with its reason and its effect
on the scores.

**None.** The protocol, harness and case sets ran as frozen.

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
