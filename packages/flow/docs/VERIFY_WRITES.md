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
from openadapt.verify import WriteContract, verified_write

contract = WriteContract.load("contracts/order.yaml")

def save_order(customer: str, product: str, units: str) -> dict:
    values = {"customer": customer, "product": product, "units": units}
    with verified_write(contract, values, "~/.openadapt/verify",
                        executor={"kind": "agent", "id": "orders-agent"}) as write:
        my_orders_api.create(**values)        # however your agent writes
    return write.result.to_dict()             # verified, or a refusal
```

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
| `verified: true` | The system of record shows exactly the declared write. The result carries a receipt (`receipt_sha256`). | Nothing more. |
| `REFUTED` | The store shows a different write: a wrong value, or a duplicate. | Don't retry. A person fixes the record. |
| `RECONCILIATION_REQUIRED` | The store doesn't show the write, or couldn't be read. It may still land. | Don't retry, or it could save twice. A person checks. |
| `ok_to_write: false` (from `verify_begin`) | The store couldn't be read before the write. | Don't make the write. |

## Teach a write as a tool

`openadapt-agent serve --teach-browser --contracts ./contracts` lets your agent teach a browser write once, against a contract, and call it again as a `taught_<name>` tool. Each call makes the write and returns the same verified receipt or refusal. Sign-in values stay with the server:

```bash
openadapt-agent serve --teach-browser --contracts ./contracts \
  --sign-in username=PORTAL_USER --sign-in password=PORTAL_PASSWORD
```

The example app has a sign-in and a New Order form to try it on: `openadapt agent example-app`, with user `clerk` and password `portal-demo`.
