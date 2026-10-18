# Headless FreeRDP pool throughput

Synthetic target, loopback only. Each session is a private `Xvfb` display
running `xfreerdp3` against its own `freerdp-shadow-cli3` server, which
shares a second `Xvfb` display running a Tk account lookup with seeded,
made-up accounts (`fixture/ledger_app.py`). All sessions use one fixed
session profile (sha256 `aa9e1140…6728b58`).

One job: click the account field, type an account number, press Enter, wait
for the result area to settle, then read holder and balance with the
repository OCR (native read, then a 2x upscaled read) and compare them with
ground truth.

Same 60 jobs at each pool size. Machine: shared 8-CPU Linux box, load average
11 to 16 from other work during the run. Raw numbers are in `results.json`.

| Sessions | Jobs read correctly | Jobs/min | Median job (s) | Median act (s) | Median OCR read (s) |
| --- | --- | --- | --- | --- | --- |
| 1 | 51/60 | 41.6 | 1.24 | 0.32 | 0.91 |
| 2 | 51/60 | 54.8 | 1.82 | 0.48 | 1.32 |
| 4 | 51/60 | 51.4 | 3.89 | 0.79 | 3.10 |

The 9 failures are the same 9 accounts at every size. In each, the screen was
right and the digits were read correctly, but OCR dropped the decimal point
(`15632.55` read as `1563255`). The check counts these as failures. No job
failed for a transport reason after the display claim fix in this change.

Reading is the bottleneck: OCR is CPU-bound, so more than two sessions on
this machine adds contention instead of throughput. Actions alone (click,
type, settle) take 0.3–0.8 s. These numbers say nothing about a real remote
host, a WAN link, or a Windows server.

Run it again:

```bash
python benchmark/rdp_pool/run_pool_throughput.py --sizes 1 2 4 --jobs 60
```

Needs Linux with `Xvfb`, `xfreerdp3`, `freerdp-shadow-cli3`, `xdotool`, Tk
for the system `python3`, and the `browser` extra's OCR.
