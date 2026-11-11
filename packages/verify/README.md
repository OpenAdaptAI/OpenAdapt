# openadapt-verify

Check whether an agent's write landed in the system of record, and that
nothing else changed. openadapt-verify reads the store before and after the
write through a read path the agent doesn't control (SQL, REST or FHIR), and
returns `CONFIRMED`, `REFUTED` or `UNCERTAIN` with a signed receipt. The
writer's own response is never evidence that the write persisted.

```sh
pip install openadapt-verify
```

Requires Python 3.10, 3.11 or 3.12. The install is 21 distributions and about
41 MB of site-packages, with no vision, OCR, browser, GUI, NumPy or ML stack. It uses the verifier, write ledger, protected journal, SQL guards and
receipt signing from `openadapt-flow`: the same code, not a copy. Flow's
vision stack is its `vision` extra, which this package never installs.

## Five-minute quickstart

```sh
openadapt-verify quickstart
```

This starts disposable synthetic stores on 127.0.0.1, has a scripted agent
make three writes against each, and checks each write through an independent
read path:

| Agent write | Outcome |
|---|---|
| Saves the values it declared | `CONFIRMED` |
| Saves a different value than it declared | `REFUTED` |
| Reports success, but nothing reached the store | `UNCERTAIN` |

It runs the REST, SQLite and FHIR R4 read paths. Every result gets a portable
signed receipt (COSE_Sign1 with a local transparency log), and the quickstart
checks each receipt's signature. All state goes to a temporary directory that
is deleted afterwards. The keys are generated for the run; in a deployment,
pin the issuer and log keys you trust.

Add PostgreSQL or MySQL by pointing the quickstart at a **disposable**
database. It creates and drops its own table. The password comes from
`OPENADAPT_QUICKSTART_DB_PASSWORD`:

```sh
pip install 'openadapt-verify[mysql]'    # PyMySQL, MIT
pip install 'psycopg[binary]'            # LGPL-3.0; not a dependency of this package
openadapt-verify quickstart \
  --postgres '{"host": "127.0.0.1", "port": 5432, "user": "qs", "dbname": "qs"}' \
  --mysql '{"host": "127.0.0.1", "port": 3306, "user": "qs", "database": "qs"}'
```

A MySQL read must return utf8mb4 results. With any other result charset the
verifier refuses to read, so the quickstart reports `NOT SENT` and the agent
writes nothing.

The contracts the quickstart uses are in
[`quickstart.py`](src/openadapt_verify/quickstart.py). Each is an
[`openadapt.verify/v1` contract](https://github.com/OpenAdaptAI/OpenAdapt/blob/main/packages/flow/docs/VERIFIER_API.md);
copy one and point it at your store.

## One line per write

Plain Python, as a decorator. The function's arguments are the contract's
values. The call returns the verifier's reply instead of raising, and a write
the verifier can't read first is never sent:

```python
from openadapt_verify import verified

@verified("verify.yaml", pinned={"account": "account-17"})
def save_item(code, quantity, account):
    api.post("/items", json={"code": code, "quantity": quantity})

reply = save_item(code="item-17", quantity="3", account="account-17")
reply["outcome"], reply["seal_path"]
```

Or as a context manager, with a portable signed receipt:

```python
from openadapt_verify import checked_write

with checked_write("verify.yaml", values) as write:
    api.post("/items", json=values)
write.result.outcome, write.result.write_receipt_path
```

Agent frameworks, each SDK behind its own extra:

```python
from openadapt_verify.adapters.claude_sdk import verified   # [claude]
tool = verified(save_order, contract="verify.yaml")

from openadapt_verify.adapters.openai_agents import verified  # [openai]
tool = verified(save_order, contract="verify.yaml")
```

```sh
openadapt-verify serve --contracts ./contracts --state ./state   # [mcp]
```

| Extra | Adds |
|---|---|
| `mysql` | PyMySQL, for a MySQL read path |
| `claude` | Claude Agent SDK tool adapter |
| `openai` | OpenAI Agents SDK tool adapter |
| `mcp` | The MCP server behind `openadapt-verify serve` |

SQLite is in the standard library. For PostgreSQL, install `psycopg`
yourself: the PostgreSQL drivers whose session the verifier can make
read-only are LGPL-3.0, so this package declares none.

## Wrap a write

Use an existing [`openadapt.verify/v1` contract](https://github.com/OpenAdaptAI/OpenAdapt/blob/main/packages/flow/docs/VERIFIER_API.md)
that reads your store through SQL, HTTP or another supported independent path.
Pin record and tenant identifiers that the agent must not choose.

```python
from openadapt_verify import WriteGuard

guard = WriteGuard("verify.yaml", pinned={"account": "account-17"})
reply = guard.run(values, lambda: existing_agent_write(values))
print(reply["outcome"], reply["seal"], reply["seal_path"])
```

The canonical guard reads before the write, refuses an unreadable baseline,
and reads again after the write, including an error or cancellation. It keeps
the existing uncertain-write ledger and retry protections. `arun` accepts an
async write. Only calls through the guard are checked; other tools and direct
calls remain outside it.

`CONFIRMED`, `REFUTED` and `UNCERTAIN` describe the store result. A confirmed
write can still be provisional when its contract requires a later recheck.
The contract's scope states what was read and what was not checked. It does
not establish that the agent chose an authorized or appropriate write.

## Two signed formats

`WriteGuard` and the SDK/MCP adapters keep their existing result format:
a confirmed effect receipt can have a **local Seal** at `seal_path`.
`seal: "sealed"` reports issuance; a store result can remain confirmed when
signing is off or fails. `verify_local_seal` checks that format against an
independently pinned key registry and the receipt file's SHA256.

For a **portable COSE/SCITT write receipt**, use the canonical verifier API:

```python
from openadapt_verify import Actor, Contract, begin_write, verify_write

pending = begin_write(Contract.load("verify.yaml"), values,
                      actor=Actor("agent", "existing-agent"), workdir="./state")
try:
    existing_agent_write(values)
finally:
    result = verify_write(pending, write_receipt=True)
print(result.outcome.value, result.write_receipt, result.write_receipt_path)
```

Check `write_receipt == "issued"` before using its path. `verify_receipt`
checks portable receipts with pinned issuer and log public keys and returns
`PortableReceiptVerification`; `verify_local_seal` returns
`LocalSealVerification`. A valid signature reports integrity and the pinned
issuer, separately from the payload's verdict. Never trust a key merely because
the receipt carries it. No receipt format or cryptography is defined here.

## Existing agents

The existing `WriteGuard` compatibility adapter emits Flow's
`DeprecationWarning` when first requested. Plain `import openadapt_verify`
does not load that adapter.

OpenAI and Claude use the existing tool adapters:

```sh
pip install 'openadapt-verify[openai]'  # or [claude]
```

Import `verified` from `openadapt_verify.adapters.openai_agents` or
`openadapt_verify.adapters.claude_sdk`, and pass an existing tool and
`contract="verify.yaml"`. The Claude module also exports the existing `only`
helper for restricting the agent's built-in tools.

For browser-use, wrap a typed async action **before** registering it with
`Tools.action`:

```python
from openadapt_verify.adapters.browser_use import verified

checked_save = verified(save_action, contract="verify.yaml")
tools.action("Save the item")(checked_save)
```

Parameter names and annotations, including `browser_session`, are preserved.
The action returns the verifier reply as tool text. Other browser actions are
unchanged. See the [official custom-action API](https://docs.browser-use.com/open-source/customize/tools/add).

For a caller-owned Stagehand Python session:

```python
from openadapt_verify.adapters.stagehand import verified

checked = verified(session, contract="verify.yaml")
reply = await checked.act(values=values, input=observed_action)
```

This checks that single `act` call. It does not replace the original session
or wrap its other methods. See the [official Python session API](https://docs.stagehand.dev/v3/sdk/python).
Tests use scripted SDK callbacks and local stores; they do not establish live
model/provider behavior.

## Commands and MCP

```sh
openadapt-verify run --contract verify.yaml --params values.json --state ./state -- python write.py
openadapt-verify verify write.receipt.cbor --issuer-key issuer.pub.pem --log-key log.pub.pem
openadapt-verify recheck --contract verify.yaml --params values.json --recheck-state ./state/WRITE_ID/recheck.json
pip install 'openadapt-verify[mcp]'
openadapt-verify serve --contracts ./contracts --state ./state
```

`run` executes an explicit argv without a shell. It reads back and requests a
portable receipt even when the command fails. Exit zero requires a final
confirmed result, issued portable receipt and command exit zero. A provisional
result exits 5 and reports `recheck_state_path`. Command diagnostics go to stderr.

`verify` checks the portable signature and log inclusion against the pinned
keys. Its JSON `valid` field reports those integrity checks; the exit code
also requires a final `CONFIRMED` payload. Both commands use 0 for final
`CONFIRMED`, 3 for `UNCERTAIN`, 4 for `REFUTED` and 5 for provisional confirmation.
Code 1 reports a signature, signing or command failure; 2 reports a usage,
configuration or read error. Inspect the JSON outcome and signing fields
separately before deciding what happened.

At or after `recheck_due_at`, `recheck` reads the store using the canonical
verifier and the private state saved by `run`. Supply the original contract,
params and `--agent-id`, and retain the same verifier authority and ledger
directories. The ledger directory must not be writable by the checked agent.
Before the due time it exits 5 without ending the window.
A confirmed recheck exits 0, an uncertain one 3 and a refuted one 4. It reports
the current result and read window as unsigned JSON; the earlier signed
receipt remains provisional. A write after the recheck read is outside its
window. A refusal requires a person to check the record before any retry.

`serve` exposes the existing `verify_contracts`, `verify_begin`, `verify_check`
and `verify_recheck` MCP tools. Their contracts remain operator-owned, and a
refusal never authorizes retrying the write. The `openadapt_verify.shadow`
module also exposes the canonical shadow verifier without changing its scope.

To configure the same protected journal for `run`, `recheck`, and `serve`,
see [Use the protected journal from installed tools](../flow/docs/VERIFIER_API.md#use-the-protected-journal-from-installed-tools).
An enrolled journal refuses if a process opens it without its authority.
The legacy default provides no tamper evidence when the checked agent can
write to its journal directory.

A recheck exits zero only after the exact ledger attempt records confirmation for the same receipt. A prior refutation remains held for person review even if the unexplained change is later removed. A transient unreadable-store recheck can recover after a fresh read of the same originally confirmed write.
