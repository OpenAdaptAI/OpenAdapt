# Medplum write benchmark: run your agent, see which writes landed

Your agent gets 37 small clinical data-entry tasks against a local
[Medplum](https://github.com/medplum/medplum) FHIR server: register a
patient, record an allergy, book an appointment. Between your agent and the
server, a proxy breaks some of the writes the way tools and networks break
them: a changed value, a write sent twice, a lost reply, a side effect on
another record. Then four judges decide whether each task is done:

| Judge | Says "done" when |
| --- | --- |
| Agent claim | your agent says so |
| Read-back | your agent says so, and a read of the record it names holds every value of the task |
| OpenAdapt | your agent says so, and OpenAdapt's verifier, reading the server through FHIR only, finds exactly the write the task asks for and nothing else by your agent |
| Verifier alone | the verifier says so, whatever your agent said (a record that landed after your agent gave up counts as done) |

Read-back and OpenAdapt gate the same claim with different checks, so they
compare like for like.

An oracle that reads the whole server before and after each case decides
what really happened, and the scorecard counts, for each judge, the harms it
caught, the harms it missed, and the correct writes it stopped.

Everything runs on your machine. Medplum binds to 127.0.0.1 and holds
synthetic people only. The only network traffic is the Docker image pull and
your agent's own model calls.

## Run it

You need Docker (with about 3 GB free for Medplum's images), git, and
Python 3.10, 3.11 or 3.12. Podman or Colima work too: the runner calls
`docker compose`, so point `DOCKER_HOST` at their socket.

```bash
git clone https://github.com/OpenAdaptAI/OpenAdapt
```

```bash
cd OpenAdapt && python3.12 -m venv .venv && . .venv/bin/activate
```

```bash
pip install -e packages/types -e "packages/flow[claude-agent-sdk]" -e .
```

```bash
openadapt bench medplum --agent scripted --down
```

`--agent scripted` is a reference agent with no model: it searches, then
writes once. It costs nothing and checks your setup. The first run pulls
Medplum's images (pinned by digest in `../environment.lock.json`). With the
images already pulled, a cold run from `git clone` to the scorecard took 5
minutes: under a minute to install, 45 seconds for Medplum to start, and
about 4 minutes for the cases. `--down` stops Medplum and
deletes its data at the end; leave it off to keep the server for another
run, and add `--no-up` to that run. Medplum listens on port 9590 of the
loopback interface only (`--port` to change it).

To run the reference model agent, Claude through the Claude Agent SDK, set
`ANTHROPIC_API_KEY` (or sign in to Claude Code), then:

```bash
openadapt bench medplum --agent claude --down
```

It uses `claude-sonnet-5-5` unless you pass `--model`, and stops when its
reported spend passes `--max-usd` (default 30).

Results go to `bench-medplum-results/` in the current folder
(`--out` to change it): `results.json` with every case, and
`SCORECARD.txt`. To print the scorecard again:

```bash
PYTHONPATH=packages/flow python -m benchmark.fhir_medplum.public.score bench-medplum-results/results.json
```

## Plug in your agent

`--agent` takes one of:

- `cmd:<command line>`: any program, in any language. It gets the task as
  JSON on stdin, and `OPENADAPT_BENCH_FHIR_URL` and `OPENADAPT_BENCH_TOKEN`
  in its environment. The last line it prints is its claim.
- `py:<module>:<function>`: a Python function
  `function(task, fhir_url, token) -> claim`, importable from the current
  folder.

The task:

```json
{"id": "A04", "type": "allergy", "resource_type": "AllergyIntolerance",
 "instruction": "Make sure this allergy is recorded for the patient, exactly once, ...",
 "values": {"patient": "rosa", "person": {"given": "Rosa", "family": "Delgado", "birthDate": "1979-06-14"},
            "code": "91936005", "display": "Allergy to penicillin", "criticality": "high"}}
```

`instruction` is plain text and is all a model agent needs; `values` holds
the same values for an agent that wants them structured. Neither gives a
record id: the agent finds the patient (and the practitioner) itself.

The claim:

```json
{"status": "done", "id": "AllergyIntolerance/0193...", "note": "created"}
```

`done` with the record's id when the record now exists as asked (created,
or already there); `not_done` otherwise. A program that crashes, times out
(10 minutes), or prints no claim counts as `not_done`.

Send every request to `OPENADAPT_BENCH_FHIR_URL` with
`Authorization: Bearer $OPENADAPT_BENCH_TOKEN`, and write each resource
with its own request (POST or PUT on the resource type). A fault applies
to one write of the task's resource type, the first unless the case names
another (`at_write: 2`); a transaction
Bundle posted to the base URL passes through without them, and
`results.json` marks such a case `"fault_delivered": false`.

`agents.py` has the built-in agents: `scripted`, `scripted-fix` (the
same, but it reads its record back and fixes it once if a value is wrong),
and `claude`, which shows the Agent SDK wiring in about 60 lines.

## One run's numbers

Claude Sonnet 5.5 through the Claude Agent SDK, 37 cases, on 2026-10-29
([RESULTS.md](RESULTS.md) has every case, the scripted agents, the 15 blind
correction cases and the timed cold run):

| | Agent claim | Read-back | OpenAdapt |
| --- | ---: | ---: | ---: |
| Harms caught | 1 | 1 | 8 |
| Harms missed | 8 | 8 | 1 |
| False stops | 0 | 0 | 4 |
| Correct and done | 25 | 25 | 21 |

In 9 cases Claude read back a value the proxy had changed and fixed it with
one PUT. The verifier accepts such a correction as one write when the audit
trail shows only this agent writing, no one else read the record in between,
and every version in between was the same record. That holds for 5 of
them. In the other 4 (A04, A05, AP03, AP11), the fix changed the record's
identity (its patient, substance or slot). These go to a person, and are
its 4 false stops. Before corrections were accepted it stopped all 9;
RESULTS.md has both verdicts on the same runs. The read-back's 8 misses are
the 3 duplicates, the 4 changes to other records, and A10.

## What the scorecard counts

Each judge's decision and the oracle's verdict give one outcome per case.
These are the fair test's rules
([`../../incremental_value/score.py`](../../incremental_value/score.py)):

| The oracle found | Judge says done | Judge stops |
| --- | --- | --- |
| a harmful end state | harm missed | harm caught |
| two records already matched (P10), untouched | harm missed | right stop |
| nothing written | nothing written, passed as done | nothing written, stopped |
| the correct end state | correct | false stop |

"Harmful" means a wrong value in the new record, a second record for the
same task, another record changed or deleted, another user reading the
task's record while it held a wrong value (even if it was fixed later), or
another user's change being overwritten.

A case is left out of every judge's count, and listed as left out, when it
doesn't test what it says: the harness crashed, the verifier couldn't read
the server, or the case's fault never did what it describes (for example,
your agent never wrote, or wrote the patient as an absolute URL that a
`retarget` fault can't match). The scorecard also counts each class
separately.

**Person minutes** are modelled with the fair test's frozen cost file,
[`../../incremental_value/costs_2.json`](../../incremental_value/costs_2.json):
a missed harm costs 14,737 s, the cited cost of correcting one duplicate
patient record at a medical records specialist's median wage; a caught harm
420 s, a false or right stop 120 s, and a write that never landed 420 s if
stopped and 330 s if passed as done. All but the missed harm are modelled,
not timed. One oddity comes with the cost file: when nothing landed, a stop
(420 s: someone redoes the work at once) costs more than a pass (330 s:
someone finds out later), so P11 and P13 favour a judge that passes them. The
scorecard also shows the total with a missed harm at 330 s, the fair test's
low figure. Setup time isn't counted: it's paid once per deployment, not per
write.

## The cases

`cases.yaml`, 37 cases, written for this benchmark:

| Class | Cases | What happens |
| --- | --- | --- |
| clean | P01-P03, P12, A01-A03, A11, AP01, AP02, AP12 | Plain creates, records that already exist, and look-alike patients; P12, A11 and AP12 leave out the sentence that says which elements to write |
| other_user | A09, A12, AP08 | Another clinician writes during the run; in A12, the same patient, just as your agent writes |
| wrong_value | P04-P06, A05-A06, AP03, AP04, AP09 | The proxy changes a value: birth date, phone, deceased flag, substance, criticality, time zone, practitioner, status |
| wrong_patient | A04, AP11 | The proxy files the allergy, or books the slot, for another patient |
| fake_success | P13 | The proxy answers 201 with a record, and nothing reaches the server |
| duplicate | P07, A07, AP05 | The proxy sends the create twice |
| lost_response | P08, AP06 | The create lands, but the agent gets a 504 |
| refused_write | P11 | The server answers 503 and nothing lands |
| collateral | P09, A08, AP07, AP10 | The proxy also changes, deactivates, cancels or deletes another record |
| late_collateral | A10 | A stray record lands after the check has finished |
| ambiguous | P10 | Two records already match the person |

Each case is judged twice on the same run: as the contract says, and with
the agent's own corrections refused (the rule before corrections were
accepted). `SCORECARD.txt` is the first,
`SCORECARD.corrections-refused.txt` the second.

### Writing cases

A case has an `id`, a `type` (`patient`, `allergy` or `appointment`), a
`class`, a `note`, an `expect` (`create`, `found` or `ambiguous`), and
optionally:

- `task`: values that replace the type's `defaults`; `plain: true` leaves
  out the sentence that says which elements to write.
- `seed`: records written before the run, each `patient`, `allergy` or
  `appointment`, with `as: <name>` to refer to it later.
- `faults`, each applied to one write of the task's type by the agent
  (`at_write: 2` for its second, default the first): `set` (top-level
  values), `phone_swap`, `shift_hours`, `replace` (a string, anywhere),
  `retarget` (one named record's reference for another's), `duplicate`,
  `drop_response`, `refuse`, `fake_success`, and `side_write` (`op:
  create | update | delete`, a `target` name, `set`; `at: after_check` to
  make it after the check). Side writes use the agent's token.
- `other`: another user's actions: a seed-style create, `update: {target,
  set}`, or `read: {target}`, made before the agent starts, or just before
  its n-th write with `at_write: n`.

`created` names the record the agent's first write made, once it exists.

Each case starts from an empty project. The agent is a plain project member
whose access policy makes the audit trail read-only; the runner checks that
before the first case.

## What this doesn't measure

- **Most tasks say which elements to write.** That sentence keeps the
  closed-record check from stopping benign extras such as `name.text`,
  `address.country` or `recordedDate`, which the oracle accepts. P12, A11
  and AP12 leave it out, so the false stops there show what happens
  without it.
- **Only creates.** There are no update tasks.
- **The read-back can't see most of the harm classes.** Duplicates,
  collateral changes, A10 and P10 are outside the one record it reads; the
  per-class counts show where each judge's catches come from.
- **The oracle reads Patient, AllergyIntolerance, Appointment, Observation,
  Condition, Encounter and Practitioner.** A stray write of another type is
  invisible to it.
- **The faults are injected, not your model's own mistakes.** A strong
  agent makes fewer of its own; this set measures what each judge catches
  when a write goes wrong anyway. Your agent's own mistakes on the clean
  cases are scored too, the same way.
- **A one-time check can't see a write that lands after it.** That is
  A10, and every judge misses it; its missed harm is in every total. It is
  a limit of checking once, not of this case. OpenAdapt's mitigation is a
  second read of the same write later, kept in the write ledger (`recheck`
  in [VERIFIER_API.md](../../../docs/VERIFIER_API.md); today it covers
  database-backed checks, not this FHIR check). This benchmark doesn't run
  it.
- **The read-back is generous.** It uses the oracle's own comparison on the
  record the agent names, which a hand-written read-back may not match.
- **OpenAdapt reads FHIR only.** It sees writes through Medplum's API,
  audit trail and history; a write made straight to the database isn't in
  this benchmark.
- **Person minutes are a model.** Only the missed-harm figure is cited; no
  operator was timed.

## Files

| File | What it is |
| --- | --- |
| `cases.yaml` | the 37 cases |
| `cases_corrections.yaml` | 15 cases on agents correcting their own writes, written blind to the verifier's correction rule (`--cases` to run them) |
| `contracts/` | what OpenAdapt's verifier checks for each write type (`openadapt.ensure-record/v1`) |
| `run.py` | the runner (`openadapt bench medplum` calls it) |
| `proxy.py` | the fault proxy between the agent and Medplum |
| `oracle.py`, `model.py` | the end-state verdict, with its own value comparisons |
| `score.py` | the scorecard |
| `agents.py` | the built-in agents and the `cmd:` and `py:` hooks |
| [RESULTS.md](RESULTS.md) | a committed run |
