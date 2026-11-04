# Parallel smoke test: N=100 (AWS spot, 2026-11-03)

One OpenEnv server ran the MockMed environment (public sample) with room for
100 sessions. 100 OpenEnv WebSocket clients played 2 scripted episodes each, all
at once, on one c7i.8xlarge spot instance (32 vCPU, 61 GiB, Ubuntu 24.04).
Branch `feat/openenv-benches` (#1780).

| Measure | Value |
|---|---|
| Sessions / episodes | 100 / 200 |
| Episodes at the maximum reward | 200 of 200 |
| Errors | 0 |
| Reset time | median 4.6 s, p95 5.6 s |
| Episode time | median 12.5 s, p95 17.8 s |
| Wall time for all 200 episodes | 30.8 s |
| Throughput | 390 episodes per minute |
| Setup (boot to ready: install, clone, browsers) | about 60 s |

For comparison, 8 sessions on the shared Mac took 3.5 s per reset and 9.5 s per
episode (median), with a load average of about 40.

Each session holds its own app copy and Chromium browser, so sessions don't
share state. With 100 at once, every episode still reached the maximum reward.
That shows nothing leaked between them and nothing was dropped.

Raw output is in
`last-run.json`.

Not tested: a model-driven run, other environments (OpenEMR and Frappe use one
app database per server, so they scale by running more app instances, not more
sessions), and sustained runs longer than a few minutes.
