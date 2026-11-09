# Real-RDP vision-ladder qualification

This directory holds a **real-protocol RDP** end-to-end qualification harness
for the vision-only resolution ladder on a synthetic, no-DOM remote-display
surface. It complements, and must not be confused with, the adjacent proofs:

| Proof | What it exercises | Substrate |
| --- | --- | --- |
| `benchmark/rdp` (PR #142) | RDP **transport + input** (aardwolf) | real Windows RDP (Parallels) |
| `benchmark/windows_uia`, `benchmark/macos_native` | **structural** rungs (UIA / AX) | native a11y |
| **`benchmark/rdp_ladder` (here)** | **vision-only resolution ladder + contract** (`template → template_global → ocr → geometry`), effect verification, halt-under-drift | real RDP **pixels**, no a11y |

## Current evidence status

The accepted RDP result already published by this repository is the separate
Windows transport/input qualification at candidate `82a658a`: exactly three
Win+R/file-oracle trials passed with zero silent incorrect success, over-halt,
or model calls. See
`benchmark/rdp/results_82a658a_20260718.sanitized.json`. That result does **not**
prove this vision-ladder harness and does not establish general RDP application
support.

The committed `results.json` is the accepted v2 artifact from GitHub Actions
run `29978847851`, attempt 2, at mechanism commit
`6031fde559b942a1d8b1a560d8b6cee8a6bfc800` (base
`d952c363d1910f1699c1a4690002879b1990d743`). All three healthy trials and all
three drift trials passed. Healthy replay used only deterministic visual
resolution, verified every governed pixel identity plus the runtime and host
effects, and made zero model calls, with no over-halt or silent incorrect
success. Every drift trial halted with no write or false completion. This
historical artifact uses the previous document fixture. The v3 harness below
uses appointment booking, read-only SQL, and a separate wrong-record campaign.
This remains bounded evidence for the synthetic real-RDP surface described
under [Honest scope](#honest-scope).

## Acceptance contract

`run_rdp_ladder_qualification.py` drives the production `Recorder` →
`compile_recording` → governed `Replayer` classes over a genuine RDP
round-trip, with **no structural backend**. The harness adds explicit recorded
pixel-identity regions, a strict fixture policy, an encrypted/sealed bundle,
and an exactly-one-new-appointment effect contract before replay. It accepts
only:

- **healthy (3 trials)**: three trial-unique parameter values succeed with zero
  model calls; only visual resolution rungs are used; every governed identity
  requirement verifies; the runtime `SqlRecordVerifier` confirms exactly one
  appointment through a separate read-only SQLite connection; and the harness
  confirms the request ID, patient MRN, appointment slot, visit type, and saved
  status;
- **halt-under-drift (3 trials)**: with DPI + theme-inversion + JPEG drift
  injected onto the real session, the governed replay halts with no model call,
  false completion, or appointment write;
- **wrong-record halt (3 trials)**: the active record changes from Ada Lovelace
  / MRN A1001 to Grace Hopper / MRN B2002 immediately before the final write.
  The fresh actuation frame fails identity verification, the workflow halts
  before Save, and the appointment table remains empty.

Evidence is written to `results.json`
(`schema_version: openadapt.rdp-ladder-qualification.v3`) with an `accepted`
gate. Reset acknowledgements, trial-unique request IDs, and the read-only
system-of-record query prevent stale state from satisfying the verifier.

## Why FreeRDP (not the product's aardwolf client) for the Linux surface

The product's RDP transport, the pure-Python `aardwolf` client, **cannot
interoperate with Linux RDP servers** (reproduced):

- **xrdp**: aardwolf blocks forever on an MCS `tokenInhibitConfirm` PDU xrdp
  never sends (aardwolf's own code: "TODO: implement properly").
- **FreeRDP3 shadow**: rejects aardwolf's MCS Erect-Domain Request — "invalid
  TPKT header length 12, 1 bytes too long" (aardwolf non-conformance).
- **FreeRDP2 shadow**: server-side container TCP-listener bug; never binds.

aardwolf works only against real **Windows** RDP (lenient parser + full MCS) —
which is why `benchmark/rdp` uses Parallels Win11. A **CI-viable Linux** RDP
surface for the *ladder* therefore uses a conformant client/server pair. The
`RDPTransport` protocol is explicitly swappable, so this is in-design: the fixture
serves a Tk kiosk over a FreeRDP3 **server** and renders it back with a FreeRDP3
**client**, and the harness observes/injects on the client display — so both the
pixels read and the input injected cross a real RDP exchange.

## Determinism note

The FreeRDP client runs with the RemoteFX / NSCodec / GFX-pipeline lossy codecs
disabled (`-gfx -rfx -nsc`), so it decodes **raw bitmaps** and consecutive frames
are byte-identical. Lossy RDP codec jitter otherwise perturbs template scores and
trips the identity band on write steps (a real, but fixture-induced, over-halt).
DPI/theme/compression realism is injected in software on top of this clean
baseline (the harness `_DriftBackend`) — a deliberate, labeled degradation.

The two-Xvfb fixture waits for the server-side cursor to acknowledge each
client-side XTest move before sending the button edge. The move is still
injected only into the FreeRDP client and crosses the RDP wire; the acknowledgement
prevents the headless laboratory from racing an asynchronous MotionNotify and
clicking at the previous remote cursor position.

## Honest scope

Synthetic Tk task over real FreeRDP-transported pixels + input, raw-bitmap RDP
frames, the production resolver, and governed $0 / identity / effect / policy
gates. **NOT** Citrix ICA/HDX, **NOT** the aardwolf transport (that is
`benchmark/rdp`), **NOT** a Windows application qualification, and the drift is
**simulated on the real protocol session** rather than captured over a WAN.

## Runbook

```bash
# 1. Build the fixture image (multi-arch: amd64 CI + arm64 Apple Silicon)
docker build -t oaflow-rdp-fixture:latest benchmark/rdp_ladder/fixture

# 2. Start the RDP round-trip with an out-of-band host oracle
ORACLE_ROOT="$(mktemp -d)"
docker run -d --name oaflow-rdp-ladder --shm-size=1g \
    -e RDP_FIXTURE_ORACLE_ROOT=/oracle \
    -v "${ORACLE_ROOT}:/oracle" \
    oaflow-rdp-fixture:latest
sleep 20   # let the kiosk + shadow server + client come up

# 3. Run the qualification (needs the flow stack with cv2 + rapidocr on PATH)
python3 benchmark/rdp_ladder/run_rdp_ladder_qualification.py \
    --container oaflow-rdp-ladder \
    --oracle-root "${ORACLE_ROOT}" \
    --output runs/rdp-ladder/results.json \
    --candidate-commit "$(git rev-parse HEAD)" \
    --base-commit "$(git merge-base HEAD origin/main)"

# 4. Tear down
docker rm -f oaflow-rdp-ladder
```

Exit code `0` iff all nine trials are accepted. The run takes a few minutes
(the transport screenshots/injects over `docker exec`, one operation at a
time).

The env-gated pytest wrapper `tests/e2e/test_docker_rdp_vision_ladder_e2e.py`
(`OAFLOW_DOCKER_RDP_E2E=1`) builds the fixture, runs the qualification, and
asserts `accepted`. `.github/workflows/docker-rdp-vision-ladder.yml` is manual
via `workflow_dispatch` and also runs on pull requests that change the bounded
RDP-ladder harness or its exact compiler/policy/runtime dependencies. It is not
scheduled.

## Presentation artifact

The same workflow can retain exact synthetic-fixture frames for a buyer demo:

```bash
python3 benchmark/rdp_ladder/run_rdp_ladder_qualification.py \
  --container oaflow-rdp-ladder \
  --oracle-root "${ORACLE_ROOT}" \
  --output runs/rdp-ladder/results.json \
  --presentation-dir runs/rdp-ladder/presentation \
  --candidate-commit "$(git rev-parse HEAD)" \
  --base-commit "$(git merge-base HEAD origin/main)"

python3 benchmark/rdp_ladder/render_presentation.py \
  --presentation-dir runs/rdp-ladder/presentation \
  --output runs/rdp-ladder/openadapt-rdp-demo.mp4
```

The demonstration recorder uses presentation-only pacing and retains one exact
post-input frame after each input. This path is isolated from production
replay. It does not change the accepted workflow or its qualification result.
The renderer verifies every retained frame hash and reads the exact
`ProgramGraphSpec` exported from the accepted bundle. It does not invent target
names or workflow structure. It shows:

1. The structured appointment request.
2. The recorded human demonstration with paced cursor and keyboard input.
3. The real compiled workflow graph, parameters, resolution evidence, identity
   gates, effects, and bundle digest.
4. A replay with values that differ from the demonstration.
5. The remote application beside the independently read SQL row.
6. The wrong-record halt beside the unchanged empty SQL result.

The renderer streams RGB frames directly to FFmpeg. The qualification JSON,
the compiled graph JSON, and the read-only SQL evidence remain the result
authorities.

## License posture

The fixture apt-installs FreeRDP, Openbox, xdotool, and ImageMagick as external
applications inside an ephemeral test image. No third-party source or binary is
vendored here, the workflow does not publish the image, and none of those
binaries enters the MIT wheel or sdist. Each evidence artifact records the
exact installed package versions.
