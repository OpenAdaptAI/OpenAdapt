# Named-person binding fixtures (test doubles)

These files are **synthetic test doubles**, not live WebAuthn ceremonies or
IdP traffic. They record the deterministic shapes produced by
`DeterministicPasskeyDouble` in `openadapt_flow.approval_mode.named_person_binding`.

Do not treat them as production attestation material. A later slice will add
`py_webauthn`-backed verification against a virtual authenticator; until then
every assertion here is HMAC-domain-separated fixture data.
