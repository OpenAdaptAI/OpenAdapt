# The bundled tutorial, end to end

`openadapt-flow tutorial` (which `openadapt quickstart` delegates to) is the
complete free path against the bundled MockMed application, a synthetic
practice-management fixture served through its real transactional backend.

```bash
openadapt-flow tutorial                                  # the whole loop, VERIFIED
openadapt-flow tutorial --break-it                       # then watch it catch a lie
openadapt-flow tutorial --guided                         # perform the demo yourself
```

## What `tutorial` does

It records a demonstration while observing the system of record, mines the
effect contract from the record delta it observed, certifies the bundle against
the shipped `clinical-write` policy, admits the run through the fail-closed gate
under the **Standard** profile, and verifies the write by reading the system of
record out of band — a path the application itself never calls, so the screen
cannot influence it. It ends `VERIFIED` with zero model calls, and writes a
shareable `receipt.png` / `receipt.json` beside the run.

## What `--break-it` does

`--break-it` reruns the **same certified bundle** against a backend that lies:
the server rejects the write *after* the application has painted its success
banner, so every on-screen check passes while nothing lands. The independent
read of the system of record refutes the mined `record_written` contract and the
engine **HALTS** at the consequential step instead of believing the screen. The
caught fault's evidence is a clearly-labeled local `run-broken/REPORT.md`; no
shareable receipt is emitted for it, because only `VERIFIED` runs may use the
success rail.

## What `--guided` does

For a live walkthrough, perform the demonstration yourself and then watch the
compiled replay at a visible pace. The recording browser closes after OpenAdapt
observes the saved record through the separate read-only interface. OpenAdapt
then compiles, certifies, and replays what you demonstrated. If you prefer a
fully automatic presentation, use
`openadapt-flow tutorial --headed --presentation-delay 1`. The delay applies
only to this bundled tutorial. The ordinary `tutorial`, `replay`, and `run`
paths keep their normal execution speed.

The receipt the tutorial emits is generated from a closed allow-list — outcomes,
counts, digests, and validated package versions — so it can carry no screenshot,
OCR text, typed value, parameter, URL, hostname, coordinate, operator text, or
free-form halt reason. It carries the bundle digest, so anyone can run the same
public tutorial and compare. The complete field set is in
[`RECEIPTS.md`](RECEIPTS.md).

## Drive the same stages by hand

```bash
openadapt-flow demo-record --out rec                     # record a demonstration
openadapt-flow compile rec --out bundle --name my-task   # compile it
openadapt-flow lint bundle                               # expected: finds demo gaps
openadapt-flow certify bundle --policy permissive        # smoke-policy pass
openadapt-flow certify bundle --policy clinical-write    # expected: strict refusal
openadapt-flow replay bundle                             # replay: local, $0
openadapt-flow replay bundle --drift theme \
  --save-healed-to healed                                # deterministic repair
openadapt-flow visualize bundle -o graph.html            # see what compiled
```

The command is `openadapt-flow`. If you installed the
[OpenAdapt](https://github.com/OpenAdaptAI/openadapt) launcher, the two-word form
`openadapt flow <args>` is equivalent and forwards every flag, including
`--backend`, to this engine.

## Why the hand-driven bundle is refused

The hand-driven `demo-record` bundle above is intentionally **runnable but not
certified for clinical writes**. `lint` exits nonzero because its irreversible
final click is unarmed, and `clinical-write` refuses additional identity,
system-effect, and idempotency gaps. That is the safety boundary working, not a
setup failure. The permissive policy is only a smoke gate, and `replay` runs the
**Demo** profile, whose contract asks for no effect evidence — so a Demo
completion is `COMPLETED_UNVERIFIED` and is never billable and never a success.
`tutorial` differs precisely by supplying that missing evidence: a real
persistence boundary, a mined effect contract, and an independent verifier.
Nothing in the Demo profile was relaxed to get there.

Replay serves MockMed and writes `report.json`, an illustrated `REPORT.md`, and
reviewable repair patches under `heals/`. A healed bundle written by
`--save-healed-to` is a repair *candidate*, never an implicitly active bundle:
promoting it goes through the governed lifecycle (`openadapt-flow repair`:
reviewed diff, replay + fault campaigns, human approval, staged canary,
one-command rollback). See [`REPAIR_LIFECYCLE.md`](REPAIR_LIFECYCLE.md).

## Packaging and browser provisioning

The base `openadapt-flow` package stays lightweight for native desktop, RDP,
and Citrix runners. The `browser` extra adds Playwright only for web workflows;
the first browser command checks its Linux host libraries and then downloads
the matching Chromium build once (about 150 MB). A minimal Linux host may need
Playwright's one-time system-library install. Flow stops before the browser
download and prints a command bound to the exact Python environment that runs
it. Playwright requests administrator access if the system package manager
needs it. Prefer the canonical `pip install 'openadapt[browser]'` launcher path
for normal use. In air-gapped or CI environments that pre-provision the
browser, set `OPENADAPT_FLOW_NO_AUTO_INSTALL=1` to disable the auto-download.

The weekly clean-machine test runs this complete install-to-uninstall journey
on Linux, macOS, and Windows. See the
[capability and qualification matrix](PRODUCT_STATUS.md) for the accepted scope
of each substrate.
