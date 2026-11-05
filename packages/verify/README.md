# openadapt-verify

Check an agent's write through an independent system-of-record read path.
This package exposes OpenAdapt's existing verifier, signed receipts and agent
tool wrappers. The operator supplies the read path and verification contract;
the writer's response is never evidence that its write persisted.

```sh
pip install openadapt-verify
```

This is a small facade over `openadapt-flow[receipts]`. Its installation includes
Flow's dependencies, including its vision stack.

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
pip install 'openadapt-verify[mcp]'
openadapt-verify serve --contracts ./contracts --state ./state
```

`run` executes an explicit argv without a shell. It reads back and requests a
portable receipt even when the command fails. Exit zero requires a confirmed
result, issued portable receipt and command exit zero; inspect those fields
separately before deciding what happened. Command diagnostics go to stderr.

`serve` exposes the existing `verify_contracts`, `verify_begin`, `verify_check`
and `verify_recheck` MCP tools. Their contracts remain operator-owned, and a
refusal never authorizes retrying the write. The `openadapt_verify.shadow`
module also exposes the canonical shadow verifier without changing its scope.
