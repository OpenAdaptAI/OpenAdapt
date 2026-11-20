# Local form copilot runtime

`CopilotFormRuntime` connects a live `FormReconciler` session to the Chrome side panel through the installed native host. The browser owner calls `start()` once, then calls `resume_pending()` on the browser's owning thread while waiting for an operator.

Each published halt has a run ID, random session nonce, generation, pack and dossier digests, operator ID, and expiry. The native host accepts only a decision matching that state and an allowed current halt action. It refuses stale decisions, changed payloads under the same idempotency key, secret-field value entry, and messages without a bound run. The background script accepts copilot state/decision requests only from `sidepanel.html`.

The mailbox uses owner-only local files on POSIX and a SQLite transaction to serialize state publication with decision acceptance. A decision is claimed durably before applying it. If the process crashes after claiming it, the runtime does not replay that decision; the operator must reconcile the live application. This is a safe-stop policy, not exactly-once execution in the external system. The operator ID is a local assertion, not authenticated identity proof. Windows ACL handling has not been qualified.

## Attach to an existing browser

Use a dedicated browser session already signed in through the customer's approved login process. Install the native host using the existing extension installer, with its recording/session directory set to the same private directory supplied to this command. This command attaches to exactly one existing page by its complete URL and does not launch a browser:

```sh
python -m openadapt_flow.copilot_runtime \
  --pack ./approved-pack.json \
  --dossier ./local-dossier.json \
  --profile ./approved-ui-profile.json \
  --session-dir ./private-copilot-session \
  --operator-id local-operator \
  --cdp-endpoint http://127.0.0.1:9222 \
  --page-url https://portal.example.test/authorized-draft
```

The profile and pack IDs must match. CDP must use a credential-free loopback HTTP endpoint. An existing POSIX session directory must be owned by the current user with mode `0700`; decision storage must have owner-only access. The owner refreshes decision capabilities while it is polling. Stopping the owner or an uncertain application error requires reconciliation before starting a replacement run.

The caller remains responsible for authorization, admitted pack selection, permitted actions, the network/data boundary, and qualification of saved-state reads. Local transport does not prove those properties. Secret fields are resolved on screen; they are not entered into the side panel. Legacy unbound JSONL methods remain offline compatibility helpers and grant no authority to this runtime or native host.

Tests cover native subprocess framing, scope and replay refusal, installed headless Chrome side-panel messaging, and a live reconciler filling a synthetic local form with persisted HTTP read-back. These tests do not establish compatibility with a real third-party portal or production qualification.
