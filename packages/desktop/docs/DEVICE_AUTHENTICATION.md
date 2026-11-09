# Desktop Cloud authentication

OpenAdapt Desktop uses one Cloud bearer format: `oai_ingest_…`. It stores the
bearer in the operating system credential store. The token-paste path remains
available for headless systems and recovery.

## Browser sign-in

`openadapt-desktop login --provider browser_pkce` starts an RFC 8252 loopback
flow:

1. Desktop binds an ephemeral `127.0.0.1` port and serves only `/callback`.
2. Desktop creates an S256 PKCE verifier and challenge plus a random `state`.
3. Desktop opens the OpenAdapt Cloud login page in the system browser.
4. After sign-in, Cloud shows the user a Connect confirmation.
5. Cloud redirects to the exact loopback URL with a five-minute, one-use
   `oap_…` pairing code and the exact `state`.
6. Desktop checks the state and code prefix. It claims the pairing with the
   PKCE verifier through `/api/local-bridge/pairings/claim`.
7. Desktop stages the returned `oai_ingest_…` bearer, validates it, promotes it
   atomically, and confirms the connection. A failure invokes the existing
   acknowledged rollback path.

The code is not a Supabase authorization code. Desktop does not call a
Supabase token endpoint, store a Supabase session, or add a loopback URL to a
Supabase allowlist. `oapp_…` and `oaps_…` credentials belong only to the local
attended-decision portal and cannot authenticate a Cloud request.

Browser sign-in is unavailable on a headless machine, without a display on
Linux, or without an unlocked secure credential store. Desktop checks the
store before it claims the one-use code. Token paste remains available:

```console
openadapt-desktop login --provider paste
```

## Expiry and renewal

New Cloud credentials expire within 90 days. Cloud enforces the deadline on
every authenticated request. Desktop treats its local deadline as a warning,
not as proof that a credential remains valid.

Cloud reports the warning contract through `/api/needs-attention/count`.
Desktop checks that the JSON and the `x-openadapt-credential-*` headers agree.
Cloud sets `expiring_soon` from the exact server timestamp. The
`expires_in_days` value is a floor-rounded display value. Thus, day 14 can
report either state. Desktop uses the server `expiring_soon` value and does not
recompute it from the local clock. A legacy credential can report that it has
no expiry; the operator can rotate it to enter the 90-day policy.

```console
openadapt-desktop credential
openadapt-desktop rotate
```

Rotation creates the replacement first. The old bearer stays valid for at
most seven days. Desktop durably stages the one-time response before it changes
the active keychain entries. It then authenticates one independent
`/api/needs-attention/count` request with the replacement and checks the exact
lifetime contract before promotion. After a process interruption, Desktop
validates and finishes that exact stage. It does not send a second rotation
request. Recovery requires the same stored expiry, but the remaining-day count
can decrease while the machine is offline. A later successful login safely
supersedes an older rejected rotation stage.

If the response is lost before Desktop can stage it, the old bearer remains
usable during the overlap. Cloud cannot replay the raw replacement, so the
operator must sign in again. Do not retry rotation with the same old bearer.
