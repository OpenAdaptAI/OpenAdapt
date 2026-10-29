# Verify your agent's writes

Your agent saves something in another system: an order, a patient, a ticket. `openadapt verify` reads that system's own records before and after the write and returns a verified receipt, or a refusal. It never returns an unverified success. It works with any agent framework, because it wraps the write you already have.

## 1. Say what a right write looks like

A contract is a small YAML file. It says how to read the system of record, and what must be there after the write, with `{param}` placeholders for the values your agent writes:

```yaml
# contracts/order.yaml
schema_version: openadapt.verify/v1
verifier:
  kind: rest                       # also: fhir, sql, graphql
  base_url: http://127.0.0.1:8765
  records_path: /api/orders
  records_key: records
effects:
  - kind: record_written
    match:
      customer: {param: customer}
      product: {param: product}
      units: {param: units}
    expected_count: 1              # exactly one new record
    count_new_only: true
```

The contract belongs to whoever runs the agent. The agent can't change what it is checked against.

## 2. Wrap the write

In Python, wrap your agent's own write function. The same code works as a tool in the Claude Agent SDK, OpenAI Agents, LangGraph, or a plain script:

```python
from openadapt.verify import Actor, Contract, WriteBlocked, verified_write

contract = Contract.load("contracts/order.yaml")
AGENT = Actor("agent", "orders-agent")

def save_order(customer: str, product: str, units: str) -> dict:
    values = {"customer": customer, "product": product, "units": units}
    try:
        with verified_write(contract, values, actor=AGENT) as write:
            my_orders_api.create(**values)    # however your agent writes
    except WriteBlocked as stop:              # held, or the store can't be read
        return {"written": False, "reason": stop.reason}
    return write.result.to_dict()             # CONFIRMED, or a refusal
```

For the Claude Agent SDK, OpenAI Agents SDK, LangGraph and CrewAI, `verified(tool, contract=...)` wraps an existing tool in one line: [AGENT_FRAMEWORKS.md](AGENT_FRAMEWORKS.md).

From any MCP client, run the server with your contracts:

```bash
openadapt verify serve --contracts ./contracts
```

```bash
claude mcp add openadapt-verify -- openadapt verify serve --contracts /abs/path/to/contracts
```

The agent calls `verify_begin` before the write, makes the write its own way, then calls `verify_check`.

## 3. Act on the result

| Result | What it means | What to do |
| --- | --- | --- |
| `outcome: CONFIRMED` (`verified: true`) | The system of record shows exactly the declared write. The result carries a receipt (`receipt_sha256`). | Nothing more. |
| `outcome: REFUTED` | The store shows a different write: a wrong value, another record, or a duplicate. | Don't retry. A person fixes the record. |
| `outcome: UNCERTAIN` (`verdict: RECONCILIATION_REQUIRED`) | The store doesn't show the write, or couldn't be read. It may still land. | Don't retry, or it could save twice. The ledger holds it until a read-back finds it or a person checks. |
| `ok_to_write: false` (from `verify_begin`), or `WriteBlocked` | The store couldn't be read before the write, or the ledger holds an earlier attempt of the same write. | Don't make the write. |

Every result also says what it establishes (`scope`): the read path (database, API, FHIR, audit log or screen), the stores read, the time window, and what it doesn't cover, such as email, other services, and writes that land after the read. The ledger keeps a write that may have landed from being sent again, also after a restart or by another worker (`openadapt verify ledger list`). See [VERIFIER_API.md](VERIFIER_API.md).

A verified result proves the system of record holds what was declared. It doesn't prove the write was authorized: `values_declared_by` says who declared the values (the agent), and `authorization` is `unbound` unless you pass an operator's authorization reference to `begin_write`. The receipt is sealed with this computer's Seal key (`seal_path`; manage the key with `openadapt-flow local-seal key`, or turn Seals off with `OPENADAPT_LOCAL_SEAL=off`), so someone else can check it was issued here and not written by the agent. `seal` says `sealed`, `off`, or `unsealed:` with why the key couldn't be used.

## Teach a write as a tool

`openadapt-agent serve --teach-browser --contracts ./contracts` lets your agent teach a browser write once, against a contract, and call it again as a `taught_<name>` tool. Each call makes the write and returns the same verified receipt or refusal.

For a taught write, the contract also lists the requests the write may send:

```yaml
writes: ["POST /login", "POST /orders"]   # the sign-in and the save
```

The browser stops any other write before it is sent, while teaching and on every call. The tool sends exactly the writes it was taught, in order and with the same fields, and a call where the page tries anything else ends in `needs_review`, never `done`.

Sign-in values stay with the server. They go only into a password field or another field of a sign-in form, never into an address, and no form of them (encoded or not) reaches the agent:

```bash
openadapt-agent serve --teach-browser --contracts ./contracts \
  --sign-in username=PORTAL_USER --sign-in password=PORTAL_PASSWORD
```

The example app has a sign-in and a New Order form to try it on: `openadapt agent example-app`, with user `clerk` and password `portal-demo`.
