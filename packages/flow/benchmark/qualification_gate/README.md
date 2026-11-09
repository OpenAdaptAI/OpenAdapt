# Qualification gate campaign (local, deterministic)

A counted qualification campaign rebuilt to the production gate standard
(`AGENTS.md` §2): **three trials per task x condition**, an explicit counted
summary that exposes `silent_incorrect_successes` and `over_halts`, and at
least three expected uncertain-delivery fault conditions whose trials end in
`RECONCILIATION_REQUIRED` — or in a contract-proven `VERIFIED` after
uncertainty — with zero blind retries and zero replay dispatches.

It follows the structure and fail-closed conventions proven by the real-RDP
multi-window campaign (`benchmark/rdp_multiapp`, PR #327): a machine-readable
condition contract (`campaign.json`), a harness whose summary refuses to run
without every required counter, per-trial fault evidence, and one accepted
subset verdict for the whole matrix.

## Honest scope label

This is a **DETERMINISTIC LOCAL STAND-IN substrate**. It drives the unmodified
governed runtime (Recorder -> compiler -> Standard-profile run gate ->
qualification case authority -> Replayer -> independent SQLite effect
verifiers) against Pillow-rendered pixels and a local SQLite system of record.

It does NOT exercise FreeRDP or Citrix transports, real window managers,
hosted browsers, or multi-monitor topologies. Those remain bound to the hosted
campaigns (`benchmark/rdp_multiapp` on hosted Linux with Docker + FreeRDP;
the Citrix ICA/HDX lane's real-protocol acceptance). Hosted-only parts are
exactly the transport-level faults: window/session identity, focus theft
across windows, and codec behavior are qualified by those campaigns, not this
one. What this campaign qualifies is the substrate-independent gate behavior:
verified healthy effects, safe halts before consequential input on identity /
ambiguity / render faults, and exactly-once uncertain-delivery reconciliation.

## Conditions and expectations

See `campaign.json`. Summary of the implemented matrix (3 trials each):

| Condition | Expectation |
| --- | --- |
| `healthy` | verified |
| `row_reordered` | verified |
| `moderate_display_drift` | verified_or_safe_halt |
| `severe_display_drift` | safe_halt |
| `duplicate_save_control` | safe_halt |
| `partial_render` | safe_halt |
| `wrong_record_before_write` | safe_halt |
| `stale_identity_before_write` | safe_halt |
| `uncertain_delivery_write_lost` | reconciliation_required |
| `uncertain_delivery_write_kept_timeout` | verified_after_uncertainty_or_reconciliation_required |
| `uncertain_delivery_oracle_unreachable` | reconciliation_required |

Every delivered input edge carries its own exact one-write effect contract on
an independent persisted event surface (`input_events`) beside the qualified
business surface (`records`), so duplicate or phantom edges fail independent
verification rather than relying on screen state.

## Run

```bash
python3 benchmark/qualification_gate/run_campaign.py \
    --output benchmark/qualification_gate/results.json
```

No Docker, no network, no browser, no model calls. The harness fails closed:
a missing required metric, a diverged condition contract, any wrong-record
write, duplicate effect, silent incorrect success, over-halt, blind retry,
replay dispatch, or model call makes the campaign refuse acceptance
(non-zero exit). `tests/test_qualification_gate_campaign.py` runs the same
campaign once and asserts the full gate standard on the counted results.

## Recorded result

`results.json` retains the counted 33-trial local run (2026-08-22, exact
branch head): 12 verified outcomes, 15 safe halts, 18 reconciliation-required
outcomes, and zeros for silent incorrect successes, over-halts, wrong-record
writes, duplicate effects, model calls, blind retries, and replay dispatches.
