# RDP backend (Aardwolf / pixel remote desktop)

RDP is a substrate adapter beneath the same OpenAdapt compiler and governed
runtime used for local workflows. The adapter reads the remote framebuffer and
delivers pointer, key, and wheel input; compiled bundles, policy, verification,
repair, and audit remain unchanged.

## Accepted scoped qualification

Candidate `82a658a` completed one fixed batch of three trials over a real
Aardwolf 0.2.14 RDP connection into a Parallels Windows 11 guest at 1280×800.
Each trial opened the Windows Run dialog through RDP, entered a command that
created a trial-unique file, and matched the exact value read independently by
the Parallels guest-tools oracle.

| Metric | Accepted result |
| --- | ---: |
| Successful trials | 3/3 |
| Failures | 0 |
| Silent incorrect successes | 0 |
| Over-halts | 0 |
| Model calls | 0 |
| Trial latency | 51.845 s, 10.467 s, 7.477 s |

The retained [accepted-batch report](../../benchmark/rdp/ACCEPTED_BATCH_82A658A.md)
and [sanitized machine result](../../benchmark/rdp/results_82a658a_20260718.sanitized.json)
name the environment, readiness gate, oracle, failure taxonomy, cleanup, and
artifact hashes. This accepts the tested transport and remote-input path. A
customer workflow then qualifies its exact target application, identity and
effect rules, session policy, and display conditions.

## The transport abstraction

Two layers keep the adapter CI-testable without a live RDP server and keep the
RDP library replaceable:

```
Backend protocol  ──implemented by──▶  FreeRDPBackend  ──drives──▶  RDPTransport
(screenshot/click/type/press/scroll)                                     ▲
                                                          ┌──────────────┴───────────────┐
                                                   AardwolfTransport            FakeRDPTransport
                                                   (real, `rdp` extra)          (scripted, tests)
```

**`RDPTransport`** is deliberately tiny and honest — a single framebuffer read
plus three input primitives:

| method | contract |
| --- | --- |
| `connect()` | open the session, block until a frame is available |
| `disconnect()` | tear down (idempotent, never raises) |
| `framebuffer()` | return `(frame, width, height)` — `frame` is a PIL image or raw RGB/RGBA bytes; the dims define the coordinate space |
| `pointer(x, y, button, down)` | pointer button transition at framebuffer pixel `(x, y)` |
| `key(keysym_or_char, down)` | a character to type (`'a'`, `'1'`) or a normalized key name (`'enter'`, `'ctrl'`, `'up'`) |
| `wheel(dx, dy)` | wheel gesture by `(dx, dy)` framebuffer pixels |

**`FreeRDPBackend`** maps the flow `Backend` protocol onto it: `screenshot`
PNG-encodes the framebuffer; `viewport` reports its size; `click` sends pointer
down/up (double = the sequence twice); `type_text` sends per-character key
down/up; `press` decomposes a key/chord into ordered key down-then-reverse-up
events; `scroll` sends a wheel gesture.

### Coordinate space

Everything is in **framebuffer pixels** — the same pixels the resolver emits
and the same pixels `screenshot()` encodes, because both come from
`RDPTransport.framebuffer()`. A transport that downsamples the remote desktop
MUST report the downsampled `(width, height)`, so screenshot pixels and click
pixels stay in one space; no scaling happens in the backend. `AardwolfTransport`
runs 1:1 (PIL video-out at the requested width/height).

### Identity model

RDP is a pure-pixel substrate, so `FreeRDPBackend` does **not**
implement the optional `IdentityBackend.structured_text_at` or the
`StructuralBackend` URL/title/page-count observations because the protocol does
not provide that structured layer.
Identity falls back to the OCR name+DOB-primary tier exactly as documented for
pixel-only substrates (`openadapt_flow/backend.py`, `docs/LIMITS.md`). This
mirrors how `WindowsBackend` omits `StructuralBackend`.

## The `rdp` extra

The real transport uses [`aardwolf`](https://pypi.org/project/aardwolf/), a
pure-Python **async** RDP client. It is lazily imported and gated behind an
optional extra so core installs stay lean and importing `rdp_backend` never
imports aardwolf:

```bash
pip install 'openadapt-flow[rdp]'
```

`AardwolfTransport` owns a private asyncio event loop on a daemon thread and
marshals every operation onto it, presenting the synchronous `RDPTransport`
API the backend expects. Construct it from plain credentials:

```python
from openadapt_flow.backends.rdp_backend import AardwolfTransport, FreeRDPBackend

transport = AardwolfTransport.from_credentials(
    "10.0.0.5", "clinicuser", "password",
    domain=None,          # omit for a local account; set for a Windows domain
    port=3389,
    width=1280, height=800,
)
backend = FreeRDPBackend(transport)   # connects on construction
png = backend.screenshot()
backend.click(640, 400)
backend.close()
```

(Or pass a full aardwolf URL:
`rdp+ntlm-password://DOMAIN\\user:password@host:port`.)

## Running the tests

Mock tests (no RDP server, run in CI):

```bash
pytest tests/test_rdp_backend.py -q
```

They cover screenshot→PNG (PIL and raw-bytes framebuffers), viewport, click /
double-click, per-character `type_text`, `press` key/chord mapping, `scroll`,
protocol conformance (`isinstance(backend, Backend)`), the pixel-only
capability omissions, and a full **record → compile → replay** run over the
`FreeRDPBackend` against a stateful fake desktop (proving zero compiler/replayer
changes).

## Live qualification harness

The gated live test connects to a configured RDP target via
`AardwolfTransport`, reads a framebuffer, validates it, and disconnects. It is
skipped unless all three environment variables are set and needs the `rdp`
extra:

```bash
pip install 'openadapt-flow[rdp]'
export OPENADAPT_FLOW_RDP_TARGET=host_or_ip[:port]
export OPENADAPT_FLOW_RDP_USER=username
export OPENADAPT_FLOW_RDP_PASS=password
# optional: OPENADAPT_FLOW_RDP_DOMAIN, OPENADAPT_FLOW_RDP_WIDTH, OPENADAPT_FLOW_RDP_HEIGHT
pytest tests/test_rdp_backend.py -k live_smoke -s
```

### Local Parallels qualification target

The dev Mac has a Parallels "Windows 11" VM. To point the live test at it, the
guest must be running an RDP host (this is a mutating guest change — snapshot
first with `prlctl snapshot "Windows 11" -n rdp-smoke`, and never delete the
user's snapshots):

1. Resume the VM: `prlctl resume "Windows 11"`.
2. In the guest (Windows 11 **Pro** — Home cannot host RDP): Settings → System
   → Remote Desktop → **On**; ensure the login account has a **non-blank
   password** (RDP rejects blank-password accounts by default).
3. Find the guest IP: `prlctl exec "Windows 11" ipconfig` (the shared-network
   IPv4, not `169.254.x`).
4. `export OPENADAPT_FLOW_RDP_TARGET=<guest-ip>` plus `_USER` / `_PASS` and run
   the command above.

## Qualification boundary

The adapter contract, real network transport, frame decode, input delivery,
independent effect verification, and cleanup have all passed in the accepted
batch above. That fixed task establishes a working RDP product path, not a
statistical reliability claim for every Windows application.

For a production workflow, record and qualify the customer's exact application
under its real account/session policy, DPI and scaling, disconnect/reconnect
behavior, latency envelope, identity evidence, and independent effect oracle.
Citrix ICA/HDX is a separate design-partner qualification; the RDP batch is not
used as Citrix acceptance evidence.
