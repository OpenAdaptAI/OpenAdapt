# Real-RDP multi-window vision campaign

This campaign tests the main pixel-automation risk that the existing complex
API benchmarks do not test. It records, compiles, and replays one back-office
workflow across three separate task windows. Observation is limited to pixels
decoded by a real FreeRDP client. Input returns through the same RDP session.

The workflow reads a referral in **Inbox**, finds the request in a scrollable
**Worklist**, enters the appointment in **Scheduler**, reconciles the worklist,
and sends a confirmation from Inbox. The result contract reads three persisted
surfaces without trusting the UI:

- SQLite: exactly one appointment for the authorized request and record.
- CSV: the exact worklist row has status `Scheduled` and no adjacent row changed.
- Maildir: exactly one confirmation has the expected request correlation key.

`campaign.json` defines the fault campaign. Every condition requires three
trials. The report must count silent incorrect success and over-halt, not only
task completion.

## Scope

This fixture uses deterministic synthetic data. It tests the production visual
resolver and RDP input path without a DOM or accessibility tree. It does not
replace qualification of a named Windows or Citrix application. The separate
task windows exercise window switching and focus behavior, but they are hosted
by one synthetic fixture process.

## Fixture

```bash
docker build -t oaflow-rdp-multiapp:latest benchmark/rdp_multiapp/fixture
docker run --rm --name oaflow-rdp-multiapp \
  -v "$PWD/.tmp/rdp-multiapp-oracle:/opt/rdp_multiapp/oracle" \
  oaflow-rdp-multiapp:latest
```

Run the complete campaign after the container is ready:

```bash
python benchmark/rdp_multiapp/run_qualification.py \
  --oracle-root "$PWD/.tmp/rdp-multiapp-oracle" \
  --output benchmark/rdp_multiapp/results.json
```

The runner uses the same `DockerX11RdpTransport` and `FreeRDPBackend` contract
as `benchmark/rdp_ladder`. It runs all nine conditions in `campaign.json` three
times. The result cannot describe the campaign as complete unless all 27 trials
run and the aggregate acceptance contract passes.
