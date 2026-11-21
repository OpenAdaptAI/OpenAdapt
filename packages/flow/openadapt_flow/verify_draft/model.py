"""The model that drafts a contract: pinned model, pinned prompt, spend ledger.

This module is the only place in the draft path that talks to a model, and
nothing on the run path imports it: an admitted contract is plain
``openadapt.verify/v1`` data, judged by the deterministic verifier.

The model and the prompt are pinned. Each draft records the model id the
request named, the model id the response reports, the prompt's version and
SHA-256, the token counts and the estimated cost, in the contract's
``provenance`` section. The key comes from ``ANTHROPIC_API_KEY`` in the
environment only and is never written or printed.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol

#: The pinned model. A change here is a new prompt version too.
MODEL = "claude-opus-5-5"
EFFORT = "medium"
MAX_TOKENS = 32_000
#: USD per million tokens for MODEL (input, output, cache read, cache write).
PRICE_PER_MTOK = {"input": 4.0, "output": 20.0, "cache_read": 0.2, "cache_write": 5.0}
PROMPT_VERSION = "verify-draft-prompt/1"

SYSTEM_PROMPT = """\
You write record checks for OpenAdapt. A record check is an `openadapt.verify/v1`
contract: after an agent writes to a system of record, OpenAdapt reads the
system's own database and decides whether exactly the intended write landed.
Your check will be tested before anyone trusts it: OpenAdapt plants faults in a
sandbox copy (a wrong value in a written field, the write on the wrong record,
a duplicate record, an edit to a sibling business record, the write reverted
after the check, another user's save lost under the agent's save) and replays
clean writes as two different users with different values. It is admitted only
if it refutes every fault and confirms every clean write. At run time it makes
no model calls, so everything it needs must be in the contract.

Write the contract in YAML with this shape. Leave out every connection key
(sql_driver, sql_connect_args, sql_password_env, sqlite_database): OpenAdapt
fills them in. Never write session, reset, command, python or hook keys.

schema_version: openadapt.verify/v1
verifier:
  kind: surfaces
  surfaces:
    record:                 # one read per surface; plain SELECTs only
      kind: sql
      sql_query: SELECT CAST(t."id" AS TEXT) AS "id", CAST(t."name" AS TEXT) AS "name", ... FROM "table" t
effects:
- kind: record_written      # a record matching `match` must exist expected_count times
  probe: surface=record|what this checks, in a few words
  match:
    name: {param: name}     # a task value; or {literal: "x"} for a fixed value
  expected_count: 1
  count_new_only: true      # only for a write that CREATES the record
frame:
  identity_field: id        # how rows of the surfaces are told apart
  target_fields_by_surface: # columns a pre-existing declared row may change
    record: [name, ...]
  database:                 # the whole database is diffed before and after
    dialect: postgresql     # or sqlite, mysql
    allow_tables:           # tables the app writes on its own on every request
    - {table: audit_log, mode: append_only, reason: why}   # or mode: skip
    ignore_columns:         # columns that change on their own (bookkeeping)
      users: [last_login]
    views: {table: surface}       # the surface reads this table's own columns, no join
    judged_by: {table: surface}   # the surface may join; its id is the table's primary key
    new_row_fields:         # for created rows: columns set beyond the matched ones
      table: [id, created_at]
    settle_s: 1
    settle_attempts: 3
    recheck_after_s: 300    # read the whole database again this long after
  versions:                 # only when the store keeps one row per saved version
    read: {kind: sql, sql_query: SELECT ... AS id, ... AS ver, ... AS author FROM ...}
    record_field: id
    target_field: id
    target_surface: record
    version_field: ver
    author_field: author    # optional: who saved each version
    self: [{param: actor_id}]

Rules:
- Read every column as text: CAST(col AS TEXT) AS "col". Match values are
  compared as text. Use the task's parameter names in {param: ...}; the
  acting user's id is always available as {param: actor_id}.
- Every row the write adds or changes anywhere in the database must be
  explained: by a declared effect (its surface, `views`/`judged_by`,
  `target_fields_by_surface`, `new_row_fields`), or as bookkeeping
  (`allow_tables`, `ignore_columns`). Anything else refutes the write, which
  is what catches a sibling edit. Exclude a column only when it changes on
  every request regardless of the values written (a clock, a counter, the
  acting user's last-seen time). Never exclude a column that holds a value
  the task writes.
- How a table's moved row counts as declared: with `views`, the effect's
  match fields must all be columns of that table. When the match needs a
  value from another table (an audit row tied to the record's business key),
  read it with a join, select the table's primary key as the surface's
  identity field, and use `judged_by` for that table instead of `views`.
- Choose match columns so a write on the wrong record, a wrong value or a
  second copy changes the count.
- The check must hold for any record the task names, not only the
  demonstrated one: never write a value of the demonstrated record (its id,
  its name, its other fields) as a literal. Bind it to a task parameter, or
  join to it in the read (for example an audit row's record id joined to the
  record's own key).
- Always set recheck_after_s so a write reverted after the check is caught.
- For an update of an existing record, catch a lost update: if the store
  keeps an audit or version row per save, declare frame.versions (with the
  author when the store records it), or count the save's own audit row as an
  effect and let the frame refute any other.

Answer with one JSON object:
{"contract_yaml": "<the YAML>",
 "payloads": [ [{"param": "<name>", "value": "<value>"}, ...], ... ],
 "notes": "<one paragraph: what the check reads and why>"}
`payloads` are 8 other valid sets of the task's VALUE parameters (not the
parameters that pick the record), each different from the demonstration and
from each other, in the formats the app accepts. OpenAdapt replays the
demonstrated write with them.
"""

#: The JSON object the model must return.
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "contract_yaml": {"type": "string"},
        "payloads": {
            "type": "array",
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "param": {"type": "string"},
                        "value": {"type": "string"},
                    },
                    "required": ["param", "value"],
                    "additionalProperties": False,
                },
            },
        },
        "notes": {"type": "string"},
    },
    "required": ["contract_yaml", "payloads", "notes"],
    "additionalProperties": False,
}


def prompt_sha256() -> str:
    payload = json.dumps(
        {
            "version": PROMPT_VERSION,
            "system": SYSTEM_PROMPT,
            "schema": OUTPUT_SCHEMA,
            "model": MODEL,
            "effort": EFFORT,
        },
        sort_keys=True,
    )
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


class BudgetExceeded(RuntimeError):
    """The spend ledger's cap would be passed; no request was sent."""


@dataclass
class Call:
    request_model: str
    response_model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    usd: float
    seconds: float
    request_id: str = ""
    stop_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class Draft:
    contract_yaml: str
    payloads: list[dict[str, Any]]
    notes: str
    call: Optional[Call] = None
    raw: str = ""


class Drafter(Protocol):
    def draft(self, messages: list[dict[str, Any]]) -> Draft: ...


def cost(usage: Any) -> float:
    def n(name: str) -> int:
        return int(getattr(usage, name, 0) or 0)

    return (
        n("input_tokens") * PRICE_PER_MTOK["input"]
        + n("output_tokens") * PRICE_PER_MTOK["output"]
        + n("cache_read_input_tokens") * PRICE_PER_MTOK["cache_read"]
        + n("cache_creation_input_tokens") * PRICE_PER_MTOK["cache_write"]
    ) / 1_000_000


@dataclass
class SpendLedger:
    """Every model call's cost, appended to a JSONL file. Refuses a call
    when the total has reached ``cap_usd`` minus the worst case of one call."""

    path: Path
    cap_usd: float
    #: The most one call can cost: MAX_TOKENS of output plus a long prompt.
    worst_call_usd: float = MAX_TOKENS * PRICE_PER_MTOK["output"] / 1e6 + 0.5
    entries: list[dict[str, Any]] = field(default_factory=list)

    def total(self) -> float:
        if not self.path.is_file():
            return 0.0
        return sum(
            float(json.loads(line).get("usd", 0))
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )

    def check(self) -> None:
        spent = self.total()
        if spent + self.worst_call_usd > self.cap_usd:
            raise BudgetExceeded(
                f"model spend {spent:.2f} USD plus one more call could pass the "
                f"{self.cap_usd:.2f} USD cap"
            )

    def add(self, call: Call, label: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"at": time.time(), "label": label, **call.as_dict()}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        self.entries.append(entry)


class ClaudeDrafter:
    """The pinned model through the Anthropic SDK."""

    def __init__(self, ledger: SpendLedger, label: str = "") -> None:
        if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
            raise RuntimeError("set ANTHROPIC_API_KEY to draft a contract")
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "drafting needs the Anthropic SDK: pip install anthropic"
            ) from exc
        self._anthropic = anthropic
        self.client = anthropic.Anthropic(max_retries=3)
        self.ledger = ledger
        self.label = label

    def draft(self, messages: list[dict[str, Any]]) -> Draft:
        self.ledger.check()
        started = time.monotonic()
        with self.client.messages.stream(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=messages,
            output_config={
                "effort": EFFORT,
                "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
            },
        ) as stream:
            response = stream.get_final_message()
        call = Call(
            request_model=MODEL,
            response_model=str(getattr(response, "model", "")),
            input_tokens=int(response.usage.input_tokens or 0),
            output_tokens=int(response.usage.output_tokens or 0),
            cache_read_tokens=int(
                getattr(response.usage, "cache_read_input_tokens", 0) or 0
            ),
            cache_write_tokens=int(
                getattr(response.usage, "cache_creation_input_tokens", 0) or 0
            ),
            usd=round(cost(response.usage), 6),
            seconds=round(time.monotonic() - started, 2),
            request_id=str(getattr(response, "_request_id", "") or ""),
            stop_reason=str(response.stop_reason or ""),
        )
        self.ledger.add(call, self.label)
        if response.stop_reason == "refusal":
            return Draft("", [], "the model declined to draft", call)
        raw = "".join(
            b.text for b in response.content if getattr(b, "type", "") == "text"
        )
        return parse_answer(raw, call)


def parse_answer(raw: str, call: Optional[Call] = None) -> Draft:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{") :]
    try:
        data = json.loads(text[text.find("{") : text.rfind("}") + 1])
    except ValueError:
        return Draft("", [], "the answer was not the JSON object asked for", call, raw)
    payloads = []
    for p in data.get("payloads") or []:
        if isinstance(p, dict):
            payloads.append(p)
        elif isinstance(p, list):
            payloads.append(
                {str(i.get("param")): i.get("value") for i in p if isinstance(i, dict)}
            )
    return Draft(
        str(data.get("contract_yaml") or ""),
        payloads,
        str(data.get("notes") or ""),
        call,
        raw,
    )
