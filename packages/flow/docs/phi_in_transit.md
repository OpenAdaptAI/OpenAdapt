# PHI in transit — the desktop control channel

The companion to [`phi_at_rest.md`](phi_at_rest.md). Where that page covers the
compiled **bundle on disk**, this page covers PHI **on the wire**: the live
control channel between the runtime (client) and the in-guest `win_agent`
(server) that drives the Windows desktop during a recording or a replay.

## The channel and what it carries

`WindowsBackend` (client) talks to `openadapt_flow.backends.win_agent` (server,
running in the VM's interactive session) over HTTP:

| Endpoint | Direction | PHI on the wire |
| --- | --- | --- |
| `GET /screenshot` | server → client | **A PNG of the live patient chart** (name, DOB, MRN, clinical data as pixels). |
| `POST /execute_windows` | client → server | The commands that read/write the chart — and the **UIA text echoed back** (row identity: name/DOB/MRN as characters). |
| `GET /health` | both | Liveness only — no PHI, unauthenticated. |

The 2026 HIPAA Security Rule makes **encryption in transit mandatory**. Before
this change the channel was token-authed but **plaintext HTTP** over
localhost/LAN — a passive listener on the LAN segment (or between Parallels host
and guest) could read every screenshot. This change encrypts and authenticates
it, and makes the client **fail closed** rather than silently downgrade.

## Trust model — per-run self-signed cert + certificate pinning

```
control plane                      guest (win_agent)          client (WindowsBackend)
─────────────                      ─────────────────          ───────────────────────
generate_self_signed_cert()  ──►   serves HTTPS with          pins the fingerprint:
  cert.pem + key.pem   (provisioned into guest)               TLS accepted ONLY if the
  fingerprint  ───────────────────────────────────────────►  server presents THAT cert
```

1. **Mint per run.** The control plane calls
   `win_agent.tls.generate_self_signed_cert(hostnames=[...])`, producing a
   short-lived (1-day default) self-signed cert + key and its SHA-256
   fingerprint. The SAN covers loopback and any host/IP the agent binds.
2. **Provision into the guest.** The cert + key are placed in the VM; the agent
   is started with `--certfile/--keyfile` (or `OAFLOW_AGENT_CERTFILE/…KEYFILE`)
   and serves **HTTPS**.
3. **Pin on the client.** The control plane hands `WindowsBackend` the
   **fingerprint** (`pin_fingerprint=...`). The client accepts the TLS session
   **only** if the peer certificate's SHA-256 equals that pin — via urllib3's
   `assert_fingerprint`. Any other certificate, **including a valid CA-signed
   one**, has a different fingerprint and the handshake is **rejected**. That is
   the MITM defense a bare self-signed cert would otherwise lack.

**Why pinning (not a CA, not mutual-TLS).** The cert is self-signed and per-run,
so there is no CA to trust — pinning is precisely what makes a self-signed cert
safe. Mutual-TLS also works but requires provisioning *and* verifying a second
(client) certificate; pinning reuses the exact out-of-band channel that already
delivers the per-run bearer token to deliver one 64-hex-char fingerprint. It is
the simpler robust option for a per-run trust root.

**Two independent factors.** TLS + pinning give **encryption** and **server
identity**; the per-run **bearer token** gives **caller authorization**. They
are checked independently — a correct cert with no/ wrong token still gets `401`,
and a correct token over a wrong cert is rejected at the handshake. Neither
silently opens the channel.

## What is enforced (fail-closed) vs residual

**Enforced**

- **Encryption in transit** when the agent is given a cert/key (HTTPS); the
  PHI-bearing screenshot + command channel is then ciphertext on the wire.
- **Server identity by exact-match pin** — a wrong/unpinned/MITM cert is
  rejected at the TLS handshake (`requests` raises `SSLError`; the client's
  action path re-raises `RuntimeError`, never a silent no-op).
- **No silent downgrade.** `WindowsBackend(require_tls=...)` defaults to
  **required for any non-loopback host**: a plaintext `http://` URL to a
  non-loopback host **raises at construction**. Loopback dev may use plaintext
  but only with an explicit warning.
- **Fail-closed config.** A half-configured TLS pair (cert without key, or vice
  versa) raises rather than falling back to plaintext. An `https://` client with
  no pin warns loudly (system-CA validation will reject the per-run cert) rather
  than appearing to work.
- **Second factor retained.** The bearer token still gates `/screenshot` and
  `/execute_windows` over TLS.

**Auto-provisioned on the Parallels launch path**

`ParallelsVM.launch_agent()` closes the manual step end to end: it mints the
per-run cert on this control plane, provisions the cert + key into the guest,
starts the agent serving **HTTPS**, deletes the host-side key, and returns an
`AgentEndpoint` carrying the URL, token, and **pin fingerprint**. Call
`endpoint.backend()` (or splat its fields into `WindowsBackend`) and the client
is encrypted + pinned + fail-closed with **no manual provisioning**. The
documented dev escape is `launch_agent(tls=False)`, which serves plaintext and
returns `require_tls=False` — never for real PHI.

**Residual / operator responsibility**

- **Cert/key distribution & lifecycle.** On the Parallels launch path this is
  automatic (above). For other control planes, minting is provided but
  **provisioning the cert into the guest and delivering the fingerprint to the
  client** is the control plane's job. Treat the private key like any secret
  (never commit it — `generate_self_signed_cert` writes the key `0600`;
  keys/certs live in tmp/run dirs, not git).
- **Rotation.** Certs are per-run and short-lived by design; there is no
  long-lived cert to rotate, but a long-running agent should be restarted with a
  fresh cert per session.
- **The guest endpoint is still RCE by contract.** TLS + token reduce *who* can
  reach `/execute_windows`; they do not change that it executes arbitrary Python
  by design. Keep the default loopback bind unless a host→guest path is needed.
- **`cryptography` on the control plane.** Cert minting needs it (already a
  dependency). The guest needs only stdlib `ssl`.

## Remote display (RDP / Guacamole) — already transport-encrypted

The `win_agent` channel is one substrate; the other is the **remote-display**
path (`openadapt_flow.backends.rdp_backend`, the L1/Retinology RDP wedge). That
stream does **not** need this work: **RDP is encrypted at the protocol level**
(TLS/CredSSP since modern Windows), so the pixel stream and input are already
protected in transit by the RDP layer itself. If RDP is fronted by
**Apache Guacamole** (browser-based access), the **Guacamole web endpoint must
itself be fronted by TLS** (HTTPS/WSS at the reverse proxy) — the guacd↔RDP hop
is RDP-encrypted, but the browser↔Guacamole hop is only as secure as the proxy
in front of it. That is a deployment configuration, not a code control in this
repo.

## Verifying it

`tests/test_win_agent_tls.py` proves the controls end to end against a real
loopback HTTPS listener (fake PNG grabber, no VM): a pinned client completes the
handshake and drives both endpoints; a **wrong-fingerprint** client is rejected
(`SSLError` at the transport, `RuntimeError` at the action path); `require_tls`
**refuses plaintext** to a non-loopback host; and the bearer token is still
enforced over TLS.
