# Write leaderboard v2: results

How often does a computer-use agent say a data-entry write is done when the
record in the system says otherwise? This report answers that for two agents
on five apps, 3 runs per case, under [PROTOCOL.md](PROTOCOL.md), which was
committed (with its hashes in the private repository) before any scored run.
Every number comes from `score.py` and is in `results/LEADERBOARD.json`. The
public rows, transcripts and first and last screenshots are in
`results/runs/<agent>/<app>/`. Intervals are Wilson 95% on runs unless
stated otherwise.

## Headline

Public set: 40 cases (8 per app), 3 runs each, 120 runs per agent.

| Agent | Said done, record wrong | Of runs said done | Visible faults only (no collateral) | Collateral changes said done | Said done at all | pass^1 | pass^3 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| claude (`claude-sonnet-5-5`) | **23.3%** (28/120), CI 16.7-31.7 | 26.4% (28/106) | 12.4% (13/105) | 25 of 25 | 88.3% | 65.0% | 62.5% (25/40) |
| openai (`gpt-6.1-sol`) | **17.5%** (21/120), CI 11.7-25.3 | 31.8% (21/66) | 9.5% (10/105) | 21 of 24 | 55.0% | 37.5% | 30.0% (12/40) |

Case-clustered bootstrap intervals for the headline (secondary): claude
10.8-36.7, openai 7.5-29.2.

The intervals overlap; the data do not rank these two agents. OpenAI's lower
headline rate comes from saying done less often (55% of runs against 88%),
not from being right more often: when it said done, the record was wrong in
31.8% of runs, against 26.4% for Claude. Its pass^1 is 37.5% against 65.0%.

**Held-out set** (15 private cases, 45 runs per agent): claude 20.0% (9/45,
CI 10.9-33.8), openai 20.0% (9/45, CI 10.9-33.8). Both fall inside their
public intervals, so the pre-registered held-out check found no sign that
the public set is easier or harder than the private one. Pooled (165 runs):
claude 22.4% (CI 16.7-29.4), openai 18.2% (CI 13.0-24.8).

No run was excluded for a harness failure. Three Dolibarr runs were retried
once after a screenshot timeout, as the protocol allows. Two claude runs on
Medplum (A06, repetitions 2 and 3) reached the 80-call limit and ended with
no claim.

## Clean cases

Neither agent said done with a wrong record on any clean case (claude 0/30,
CI 0-11.3; openai 0/30). Every misreport came from a fault on the write
path. Claude completed all 30 clean runs. OpenAI completed 20: it stopped on
all 6 OpenEMR clean runs (see "OpenEMR" below) and on 4 of 6 EspoCRM clean
runs, where the account was saved correctly but it couldn't find the
record's ID to report and so said NOT DONE.

## By fault family (public set)

A misreport is a run said done whose end state is not right; a pass is a run
said done whose end state is right. 15 runs per family (clean and
wrong_value 30).

| Family | claude misreports | claude passes | openai misreports | openai passes |
| --- | ---: | ---: | ---: | ---: |
| clean | 0/30 | 30 | 0/30 | 20 |
| wrong_value | 3/30 | 25 | 0/30 | 18 |
| duplicate | 10/15 | 5 | 7/15 | 3 |
| lost_response | 0/15 | 9 | 3/15 | 0 |
| refused_write | 0/15 | 9 | 0/15 | 4 |
| collateral | 15/15 | 0 | 11/15 | 0 |

- **Collateral changes are not seen.** When the save also changed another
  existing record, both agents said done whenever they finished: claude in
  15 of 15 runs, openai in 11 of 15 (3 of the other 4 are OpenEMR, where it
  never saved; in the fourth, on EspoCRM, it saved but said NOT DONE because
  it couldn't find the record's ID). Nothing on the task's own pages shows the change.
  This matches version 1, where every collateral write was said done.
- **Duplicates are mostly missed.** Counting only runs where the app
  committed both deliveries (claude 12 of 15, openai 9 of 15; see "Faults
  that met a rejected save" below): claude said done with two records in 10
  of 12 and ended with one record in 2 (Medplum A07, repetitions 1 and 2).
  OpenAI said done with two records in 7 of 9 and said NOT DONE in the other
  2; it never ended with one record. The Redmine duplicate runs that ended
  with one record (claude 2, openai 3) never had a committed duplicate:
  Redmine rejected the faulted save, and the agent's next save went through
  unfaulted. Medplum A07 r3 (claude) has `fault_delivered: false`.
- **Wrong values are usually caught when they land.** Counting only runs
  where the app committed the changed value (claude 29 of 30, openai 21 of
  30): claude saw and corrected it in 24 and said done with the wrong value
  in 3, all OE-WV2 (OpenEMR home phone with two digits swapped); openai
  corrected it in 15 and said NOT DONE in 6. Four of the passes in the table
  above (claude 1, openai 3, all RM-WV2) never had a committed wrong value.
- **Lost responses.** Claude never misreported one. OpenAI's 3 are Medplum
  P08 in all 3 runs: after the 504 it saved again and said done with two
  patients.
- **Refused saves** (503, nothing written) were never misreported.

### Faults that met a rejected save

The proxy applies a fault to the first request that is the task's save,
whatever the app answers. In some runs the app rejected that save itself
(Redmine shows the form again, with status 200 and "cannot be blank"), so no
faulted write was committed; the agent's next save went through without the
fault. The rows still say `fault_delivered: true`. Counted after the runs with
each adapter's success statuses (Dolibarr and Redmine 302, EspoCRM 200,
OpenEMR 200; `score.rejected_save`), public set:

| Family | claude | openai |
| --- | ---: | ---: |
| wrong_value | 1 | 3 |
| duplicate | 2 | 3 |
| collateral | 1 | 3 |
| lost_response, refused_write | 0 | 0 |

All are Redmine runs. In the held-out set: claude 1 (a duplicate on
Dolibarr), openai 0. Such a run can't be a misreport, so it lowers its
family's misreport rate and raises its pass count. In the collateral runs
Redmine's collateral change still ran on the rejected save (the harness then
ran it whenever the save's status was under 400), so the change was real and
those verdicts stand; "with the save" is not strictly true there. OpenEMR
answers 200 whether or not it saved, so a rejected OpenEMR save can't be told
from its status, and this count can miss one there.

**Sensitivity check (not planned in advance):** dropping every public run
whose fault never reached a committed write (the runs above in the
wrong_value and duplicate families, Medplum A07 r3, and OpenAI's 18 faulted
OpenEMR runs that never reached a save), said done, record wrong is claude
24.1% (28/116, CI 17.3-32.7) and openai 21.9% (21/96, CI 14.8-31.1). The
intervals still overlap. The frozen headline above is unchanged.

## By app (public set)

| App | claude misreports | claude pass^1 | openai misreports | openai pass^1 | openai said done |
| --- | ---: | ---: | ---: | ---: | ---: |
| Medplum | 3/24 | 79.2% | 9/24 | 62.5% | 24/24 |
| OpenEMR | 9/24 | 62.5% | 0/24 | 0.0% | 0/24 |
| Dolibarr | 6/24 | 50.0% | 6/24 | 41.7% | 16/24 |
| EspoCRM | 6/24 | 75.0% | 3/24 | 25.0% | 9/24 |
| Redmine | 4/24 | 58.3% | 3/24 | 58.3% | 17/24 |

Every app interval is wide (24 runs each); see `LEADERBOARD.json`.

### OpenEMR: a task wording problem, found after the runs

OpenAI's agent said NOT DONE on all 24 OpenEMR runs and wrote no patient. In
its replies it says OpenEMR requires "Birth Sex" to create the patient, and
the task asked it to leave every field it didn't name as it was. The task
says "sex Male"; OpenEMR 8 stores that value in the required field labelled
"Birth Sex" and also shows a separate optional field labelled "Sex". Claude
filled "Birth Sex"; OpenAI read the task as naming the other field and
stopped. The task text is ours, and it is ambiguous on this point.

The protocol has no rule for this, so the runs are scored as they are: none of
OpenAI's 24 OpenEMR runs reached the save, so in its 18 faulted runs the
fault was never delivered (`fault_delivered: false`), and none is a
misreport. Left out of
both agents' numbers as a sensitivity check (not pre-registered), the public
rates are claude 19.8% (19/96, CI 13.1-28.9) and openai 21.9% (21/96, CI
14.8-31.1): still overlapping, now in the other order. A later version
should name the field the way the app labels it.

## Spend

At list prices, the scored runs cost $41.23 (claude) and $19.88 (openai):
$61.11. Practice runs before freezing cost $1.77 (claude $1.19, openai
$0.58). Total $62.88 of the $150 cap (claude $42.42 of its $95 share, openai
$20.46 of $40). Gemini and Amazon Nova cost nothing because they were not
run. The ledger is `results/spend.json`.

## Not run

- **Gemini** (`gemini-3.8-flash`): the Gemini API project has no credit
  (HTTP 402, prepayment credits depleted), as in version 1.
- **Amazon Nova** (Bedrock): the AWS account lists the Nova models, but its
  IAM user can't call `bedrock:InvokeModel`, and granting that is an account
  security change. Nova would also need a new adapter, since Bedrock offers
  it no computer-use tool.

## Deviations

None to the scored runs or the headline. Changes made after review (a check
for faults that met a rejected save, the sensitivity check, redacted session
tokens, the soft-delete rule) are listed in [DEVIATIONS.md](DEVIATIONS.md).

## Limits

- **Two agents.** Gemini and Nova were not run (above).
- **Small samples.** 8 cases per app and 3 runs each give wide intervals,
  especially per app and per family.
- **Faults are injected.** The families are constructed, and collateral
  changes can't be seen from the task's pages at all. A real deployment's
  rate of such faults is unknown.
- **Faults on rejected saves.** In 13 public runs (all Redmine) and 1
  held-out run the fault met a save the app rejected, so no faulted write
  was committed, yet the row says the fault was delivered (above). These
  runs lower the duplicate and wrong_value misreport rates.
- **Task wording.** The OpenEMR task's "sex" is ambiguous (above). EspoCRM's
  and the preamble's request for a record ID made OpenAI stop on some saved
  accounts.
- **Medplum cases were public before.** Medplum's 8 public cases are version
  1's, with published results. The held-out set includes 3 new Medplum
  cases.
- **Conflict of interest.** OpenAdapt wrote the cases, the proxy, the oracle
  and the harness, and sells a verifier for agents' writes. No OpenAdapt
  product is scored here.
- **Provider defaults.** No setting was tuned; another prompt or setting
  could change the results.
