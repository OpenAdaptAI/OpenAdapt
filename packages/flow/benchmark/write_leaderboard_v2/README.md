# Write leaderboard v2: does the agent's "done" match the record, across five apps?

A computer-use agent gets a data-entry task in an app's own web pages:
register a patient (Medplum, OpenEMR), record an allergy (Medplum), add a
contact (Dolibarr), create an account (EspoCRM), open an issue (Redmine).
When it stops, it says DONE or NOT DONE. An oracle then reads the app's
database and decides whether the record is right. The leaderboard counts how
often the two disagree in the direction that hurts: the agent said DONE and
the record is wrong.

Some saves go wrong on the way to the app: a proxy at the app's front door
changes a value, delivers the save twice, drops the reply, refuses the save,
or changes another record. The protocol, cases, metrics and stop rule were
fixed before any scored run: [PROTOCOL.md](PROTOCOL.md). Results:
[RESULTS.md](RESULTS.md). Changes after the freeze:
[DEVIATIONS.md](DEVIATIONS.md). Version 1 (Medplum only) is in
`../fhir_medplum/leaderboard/`.

## Run it

You need Podman or Docker, Python 3.10 to 3.12 and Playwright's
`chrome-headless-shell`.

```bash
pip install -e packages/types -e packages/flow playwright anthropic openai google-genai
python -m playwright install chromium-headless-shell
export OPENADAPT_TEST_CHROMIUM_EXECUTABLE=<path to chrome-headless-shell>
```

From `packages/flow`, with the providers' keys in the environment:

```bash
python -m benchmark.write_leaderboard_v2.run --agent claude --agent openai \
    --app dolibarr --slot lb --port 9850 --cases benchmark/write_leaderboard_v2/cases.yaml
python -m benchmark.write_leaderboard_v2.score --json results.json
```

`--app` is `medplum`, `openemr`, `dolibarr`, `espocrm` or `redmine`. The app
listens on `--port` and its front door on the next port (Medplum: the web app
on the next, the proxy on the one after). `--agent scripted` checks the
harness without a model: it fills the form by selectors, so each fault's
verdict can be seen without spending anything.

## Plug in your agent

`--agent py:<module>:<function>` calls
`function(instruction: str, screen: Screen, meter: Meter) -> AgentRun`, as in
version 1 (`fhir_medplum/leaderboard/README.md`).
