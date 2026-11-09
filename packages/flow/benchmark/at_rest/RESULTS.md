# Cost of encrypting a recording

`run.py` records the same synthetic 150-action session twice through the real
`Recorder`, once in plaintext and once with an encryption key, and times the
parts that change. Frames are synthetic 1280x800 application screens with
table text (82-90 KB PNGs). The settle wait is disabled so the numbers show
the write path, not the screen.

Measured 2026-10-19 on an Apple-silicon Mac (12 cores, macOS 15.7, Python
3.12), with a 1-minute load average of 11 from other work on the machine.
Raw numbers: [`results.json`](results.json).

| | Plaintext | Encrypted | Difference |
| --- | --- | --- | --- |
| Persist one frame (median) | 0.12 ms | 0.25 ms | +0.14 ms |
| Persist one frame (p95) | 0.51 ms | 0.55 ms | +0.05 ms |
| Record one action: 2 frames, 1 event (median) | 5.2 ms | 5.8 ms | +0.6 ms |
| Same, last 15 actions of 150 (median) | 5.1 ms | 6.1 ms | +0.9 ms |
| Key derivation, once per recording (scrypt) | none | 183 ms | +183 ms |
| Open and verify the finished recording | n/a | 221 ms | |
| Disk, 303 files (25.98 MB of content) | | +62.6 KB | +0.24% |

The disk overhead is 44 bytes per file (format marker, nonce, GCM tag) plus
the sealed index in the header (49 KB here: one SHA-256 per file). The cost
per action grows slowly with the recording's length because the event log is
resealed whole on each append; at 150 actions it adds under 1 ms.

In a real recording the screenshot and the settle wait take tens to hundreds
of milliseconds per action, so encryption adds about 1% or less to recording
time.

Reproduce:

```bash
python benchmark/at_rest/run.py --actions 150 --out benchmark/at_rest/results.json
```
