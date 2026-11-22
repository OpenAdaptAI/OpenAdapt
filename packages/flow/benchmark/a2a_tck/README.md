# A2A TCK run against `openadapt-agent serve --a2a`

This is the official A2A Technology Compatibility Kit
([a2aproject/a2a-tck](https://github.com/a2aproject/a2a-tck), Apache-2.0,
commit `5e6f37c2d38eb3d7684686243c34d365c83df49b`, 2026-11-18). It was run on
2026-11-21 against the sandbox server, started with this command:

```bash
openadapt-agent serve --a2a --a2a-port 9999 --sandbox-engine simulated
./run_tck.py --sut-host http://localhost:9999
```

- **Python.** The TCK declares Python 3.15. Its `grpcio` wheel crashed on
  3.15.0b3 (exit 139), so it ran on Python 3.14.9 with its own dependencies
  installed.
- **Transports.** The card declares only JSON-RPC and HTTP+JSON, so the gRPC
  tests were skipped.

## Result

These numbers come from `2026-11-21/compatibility.json`:

| | |
|---|---|
| Automated checks | 110 passed, 11 failed, 142 skipped |
| Agent Card | 9 of 11 passed, 0 failed (2 skipped) |
| MUST | 76.0% |
| SHOULD | 57.1% |
| MAY | 100% |
| Overall | 75.0% |

**What failed, and why:**

- **`DM-ART-001` and `DM-MSG-001`** (10 tests). These tests send messages
  whose `messageId` starts with a scenario prefix such as `tck-artifact-text`,
  and expect the SDK's generated test agent to answer with that exact
  artifact or message. OpenAdapt is not that test agent. It answers every
  message with its own skills: a workflow run, or `verify_handoff`. The tests
  therefore get a failed task with usage text, not the scripted artifact.
- **`HTTP_JSON-SVC-001`** (1 test). HTTP+JSON error responses come from
  `a2a-sdk` 1.3.1 with `Content-Type: application/json`. Specification 1.0.1
  expects `application/a2a+json`. That behaviour is in the SDK, not in
  OpenAdapt.

Before two fixes in this pull request, the TCK found two more failures:

- JSON-RPC posts to the interface URL plus `/`, which now has a route.
- `ContentTypeNotSupportedError` was not returned, which input-mode
  validation now handles.

The Agent Card also now sends `Cache-Control`.

The TCK also lists checks it can't automate, which it marks for manual
verification. Two of them don't apply to this server:

- **Agent Card signing.** The card is not signed.
- **TLS.** The server listens on loopback over HTTP. Off loopback it needs a
  bearer token, and TLS is the deployment's job.
