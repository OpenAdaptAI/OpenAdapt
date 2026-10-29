# Medplum write benchmark: run your agent, see which writes landed

Your agent gets 31 small clinical data-entry tasks against a local
[Medplum](https://github.com/medplum/medplum) FHIR server: register a
patient, record an allergy, book an appointment. Between your agent and the
server, a proxy breaks some of the writes the way tools and networks break
them: a changed value, a write sent twice, a lost reply, a side effect on
another record. Then three judges decide whether each task is done:

| Judge | Says "done" when |
| --- | --- |
| Agent claim | your agent says so |
| Read-back | your agent says so, and a read of the record it names holds every value of the task |
| OpenAdapt | OpenAdapt's verifier, reading the server through FHIR only, finds exactly the write the task asks for and nothing else by your agent |

An oracle that reads the whole server before and after each case decides
what really happened, and the scorecard counts, for each judge, the harms it
caught, the harms it missed, and the correct writes it stopped.

Everything runs on your machine. Medplum binds to 127.0.0.1 and holds
synthetic people only. The only network traffic is the Docker image pull and
your agent's own model calls.

## Run it

You need Docker (with about 3 GB free for Medplum's images), git, and
Python 3.10, 3.11 or 3.12.

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
Medplum's images (pinned by digest in `../environment.lock.json`) and waits
for the server, which takes a few minutes. `--down` stops Medplum and
deletes its data at the end; leave it off to keep the server for another
run, and add `--no-up` to that run.

To run the reference model agent, Claude through the Claude Agent SDK, set
`ANTHROPIC_API_KEY` (or sign in to Claude Code), then:

```bash
openadapt bench medplum --agent claude --down
```

It uses `claude-sonnet-5-5` unless you pass `--model`, and stops when its
reported spend passes `--max-usd` (default 30).

Results go to `bench-medplum-results/` in the current folder
(`--out` to change it): `results.json` with every case, and
`SCORECARD.txt`. `python -m benchmark.fhir_medplum.public.score
results.json` (from `packages/flow`) prints the scorecard again.

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
with its own request (POST or PUT on the resource type). The faults apply
to the agent's first write of the task's resource type; a transaction
Bundle posted to the base URL passes through without them, and
`results.json` marks such a case `"fault_delivered": false`.

`agents.py` has the three built-in agents; `claude` shows the Agent SDK
wiring in about 60 lines.

## What the scorecard counts

Each judge's decision and the oracle's verdict give one outcome per case.
These are the fair test's rules
([`../../incremental_value/score.py`](../../incremental_value/score.py)):

| The oracle found | Judge says done | Judge stops |
| --- | --- | --- |
| a harmful end state | harm missed | harm caught |
| nothing written | nothing written, passed as done | nothing written, stopped |
| the correct end state | correct | false stop |

"Harmful" means a wrong value in the new record, a second record for the
same task, another record changed or deleted, or (case P10) two records that
already matched, which a person has to merge.

**Person minutes** are modelled with the fair test's frozen cost file,
[`../../incremental_value/costs_2.json`](../../incremental_value/costs_2.json):
a missed harm costs 14,737 s, the cited cost of correcting one duplicate
patient record at a medical records specialist's median wage; a caught harm
420 s, a false stop 120 s, and a write that never landed 420 s if stopped and
330 s if passed as done. All but the missed harm are modelled, not timed. The
scorecard also shows the total with a missed harm at 330 s, the fair test's
low figure. Setup time isn't counted: it's paid once per deployment, not per
write.

## The cases

`cases.yaml`, 31 cases, written for this benchmark:

| Class | Cases | What happens |
| --- | --- | --- |
| clean | P01-P03, A01-A03, AP01-AP02 | Plain creates, records that already exist, and look-alike patients |
| other_user | A09, AP08 | Another clinician writes a different record during the run |
| wrong_value | P04-P06, A05-A06, AP03, AP04, AP09 | The proxy changes a value: birth date, phone, deceased flag, substance, criticality, time zone, practitioner, status |
| wrong_patient | A04 | The proxy files the allergy on a look-alike patient |
| duplicate | P07, A07, AP05 | The proxy sends the create twice |
| lost_response | P08, AP06 | The create lands, but the agent gets a 504 |
| refused_write | P11 | The server answers 503 and nothing lands |
| collateral | P09, A08, AP07, AP10 | The proxy also changes, deactivates, cancels or deletes another record |
| late_collateral | A10 | A stray record lands after the check has finished |
| ambiguous | P10 | Two records already match the person |

Each case starts from an empty project. The agent is a plain project member
whose access policy makes the audit trail read-only; the runner checks that
before the first case.

## What this doesn't measure

- **The faults are injected, not your model's own mistakes.** A strong
  agent makes fewer of its own; this set measures what each judge catches
  when a write goes wrong anyway. Your agent's own mistakes on the clean
  cases are scored too, the same way.
- **A10 is there to show a limit.** A write that lands after the verifier
  has read the server is missed by every judge.
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
| `cases.yaml` | the 31 cases |
| `contracts/` | what OpenAdapt's verifier checks for each write type (`openadapt.ensure-record/v1`) |
| `run.py` | the runner (`openadapt bench medplum` calls it) |
| `proxy.py` | the fault proxy between the agent and Medplum |
| `oracle.py`, `model.py` | the end-state verdict, with its own value comparisons |
| `score.py` | the scorecard |
| `agents.py` | the built-in agents and the `cmd:` and `py:` hooks |
| [RESULTS.md](RESULTS.md) | a committed run |
