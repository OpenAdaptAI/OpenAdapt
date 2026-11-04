# Deviations from PROTOCOL.md

1. **effect_e2e client timeout is 0.6 s, not 2 s.** The protocol said 2 s.
   The record service's `timeout` scenario commits and then hangs for 1.5 s,
   so a 2 s client would get a 200 and the scenario would stop being an
   unknown outcome. The harness uses effect_e2e's own `ACTUATOR_TIMEOUT_S`
   (0.6 s), as the original study does.
2. **The frame contract was fixed on one trial repetition.** Before the
   scored effect_e2e pass, one repetition of all 10 scenarios (not kept)
   showed the first contract flagging the clean save. Two lines were added:
   `views: {encounters: ""}` (the record's own table) and
   `new_row_fields: {encounters: [source, key]}` (columns the app fills on
   every new row). Both describe the app, as an operator would when writing
   the contract; neither names a fault scenario. The banner table was
   declared `skip` from the start (it is the app's on-screen echo, and the
   ground truth leaves it out too).
3. **Medplum: our verifier is scored from an offline replay, not from the
   rows' `verifier` field.** The verifier-specificity session found that 163
   of the 222 recorded `verifier` fields are not verdicts: the verifier's
   FHIR read token expired after an hour, so they say "unreadable: HTTP
   401". Scoring that field would measure a harness fault. The primary
   column is instead the verifier as it ran (contracts `main`), replayed
   offline against the frozen Medplum history of each run
   (`benchmark/fhir_medplum/leaderboard_rescore`, branch
   `claude/verifier-specificity`), which reproduces all 59 live verdicts
   exactly. The secondary column is the same replay with the `operations`
   contracts. The recorded field is reported only to show the fault.
4. **Spend cap raised from $50 to $70** by the coordinating session after
   the port was priced at about $0.21 per judged run, so the judge could also
   run on effect_e2e. The runner stops at $65. Order as pre-registered:
   Medplum, then effect_e2e J1, then J2 if there is room.
5. **Per-run data lives in the private corpus, not in `results/`.** The
   protocol said results are committed under `results/`. This repository's
   release check refuses per-run result rows in public source, and the data
   boundary puts run-level evidence and cost ledgers in `openadapt-corpus`.
   Per-run rows, judge outputs, the replayed verifier verdicts and the spend
   ledger are committed there
   (`paid_agent_evidence/2026-11-03-verifier-baselines/`); aggregates,
   rubrics and the synthetic effect_e2e judge views stay here.
