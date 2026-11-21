# Draft a write contract with a model, admit it by qualification

Writing a [write contract](VERIFY_WRITES.md) by hand for a new system takes
time and many small decisions: which tables to read, which columns identify
the record, what the app writes on its own, how long to wait for a late
change. `openadapt verify draft` hands that work to a model at setup time,
then tests what the model wrote before anything trusts it:

- the model drafts the contract from the system's description, one trace of
  a correct write, and the task;
- OpenAdapt plants faults in a sandbox copy and replays clean writes there;
- the contract is admitted only if it catches every fault and accepts every
  clean write.

Running an admitted contract makes no model calls. It is an ordinary
`openadapt.verify/v1` contract, judged by the deterministic verifier.

```bash
export ANTHROPIC_API_KEY=...     # read from the environment only
openadapt verify draft \
  --describe openapi.json \      # or a GraphQL schema, a FHIR CapabilityStatement, db, or none
  --trace demo.har \             # one correct demonstration
  --task task.yaml \             # the instruction and the values it used
  --sandbox sandbox.yaml \       # where to test it
  --out contracts/update_recipe
```

## What you give it

**The system's description** (`--describe`). One of:

- an OpenAPI or Swagger document;
- a GraphQL schema, as SDL text or an introspection result;
- a FHIR CapabilityStatement;
- `db`, which reads the database schema through the sandbox (tables,
  columns, keys);
- `none`.

The command cuts the description down to what a check needs before it goes
to the model.

**One trace** (`--trace`). A HAR file of one correct demonstration of the
write. The command replays its write requests in the sandbox. Before anything
is sent to the model, it removes sign-in headers (`Authorization`, `Cookie`,
API-key headers) and masks fields whose names look secret (password, token,
key, session).

**The task** (`--task`):

```yaml
instruction: Update the recipe lentil-soup's description and yield.
params:
  slug: lentil-soup
  description: Red lentils, cumin and lemon.
  recipe_yield: 6 bowls
```

`actor_id` is reserved. The sandbox binds it to the acting user's id.

**The sandbox** (`--sandbox`, `openadapt.verify-sandbox/v1`). It holds
facts, not judgments about the check:

- where the app answers;
- how two users sign in;
- two connections to the app's database: the read-only one the contract
  will read with, and a privileged one that plants faults.

```yaml
schema_version: openadapt.verify-sandbox/v1
app:
  base_url: http://127.0.0.1:9000
actors:
  agent:                     # the user the agent writes as
    id: "87c53c26-..."       # that user's id as the store holds it
    login: {method: POST, path: /api/auth/token, token_field: access_token,
            form: {username: agent@example.org, password: "${AGENT_PW}"}}
  other:                     # a second, ordinary user (optional)
    id: "4be1..."
    login: {method: POST, path: /api/auth/token, token_field: access_token,
            form: {username: cook2, password: "${COOK_PW}"}}
store:
  dialect: postgresql        # or sqlite, mysql
  read:  {sql_driver: psycopg2, sql_connect_args: {host: 127.0.0.1, port: 5432, user: reader, dbname: app}, sql_password_env: APP_READER_PW}
  admin: {sql_driver: psycopg2, sql_connect_args: {host: 127.0.0.1, port: 5432, user: app, dbname: app}, sql_password_env: APP_PW}
```

An actor can instead give fixed `headers` (for example
`Authorization: "Bearer ${TOKEN}"`). The command checks once, before any
model call, that the read account can take a whole-database snapshot. The
sandbox must be a copy you can write to: admission writes to it and puts
it back between cases by restoring the rows it read at the start. You write
no reset hook.

## What it does

1. **Record the demonstration.** The sandbox replays the trace's writes
   once, between two reads of the store, to see which rows they move. The
   model gets the rows and columns that moved, with their values. The
   sandbox holds synthetic data only.
2. **Draft.** The pinned model writes a contract that diffs the whole
   database (`frame.database`). It also writes other valid values for the
   task to test with. Connection keys are filled in from the sandbox. A
   draft that sets a hook, a session, a reset or a command, or whose read is
   not a plain SELECT, is refused without being run.
3. **Qualify.** Each case restores the store, takes the contract's
   baseline, replays the write through the app, and then plants one fault
   with the privileged connection:

   | Case | What the store holds after the write | Must be |
   |---|---|---|
   | `wrong_value` | one written column of the record holds another value (one case per column that holds a task value as written; a lower-cased copy only for an email address) | refuted |
   | `wrong_row` | the write on another record: an update moved to a sibling, or a create filed under another parent | refuted |
   | `duplicate` | a second copy of the record (unique columns changed so the store accepts it) | refuted |
   | `sibling_edit` | another business record edited beside the write | refuted |
   | `late_revert` | the write undone after the check confirmed it | refuted at the recheck |
   | `lost_update` | updates only: the other user saves the record first, then the agent's save overwrites it, both through the app | refuted, or prevented by the app |
   | 5 clean writes | the write with other values, and a recheck of each | confirmed |
   | attribution probe | the write by two users with two sets of values (2 x 2), and the first of them again | confirmed |

   The **causal-attribution probe** sorts every column that moved by cause:

   - on its own (`clock` or `fresh`): it differs between two identical
     writes, like a timestamp or a new row id;
   - the values written (`payload`);
   - who wrote (`actor`);
   - nothing (`constant`): it moves the same way every time.

   The contract's bookkeeping exclusions (`allow_tables`, `ignore_columns`)
   must agree with that sorting:

   - excluding a column that follows the values written hides the write
     itself, so it fails;
   - excluding a column that never moved has nothing to excuse and can only
     hide a fault, so it fails too.

   A literal in an effect that pins the demonstrated record also fails, for
   example its id or its name. The check must hold for any record the task
   names.
4. **Feed back.** A failed admission goes back to the model with each
   failing case, its reasons and the rows that moved (names only). This
   happens at most 3 rounds in all. Then the command stops and prints why
   (`report.json`).
5. **Ask once, only when needed.** Sometimes a planted fault could go only
   into columns the draft excludes. Then the command asks a person, once,
   whether those columns are bookkeeping the app sets on its own or data
   people edit. Answer from a file with `--answers answers.yaml`
   (`table.column: bookkeeping` or `business`), or at the terminal. With
   `--no-input`, or no terminal, the draft is refused rather than admitted
   on a guess.

Admission runs with the contract's recheck window shortened to 2 seconds.
The check is otherwise the same, and the admitted contract keeps its own
window.

## What it writes

- `contract.yaml`, only when the draft was admitted. Its `provenance`
  section records:
  - the model and the prompt (version and SHA-256);
  - the inputs' digests;
  - the admission: the check digest, each fault's result, the clean writes
    and probe writes confirmed, and any column a person confirmed as
    bookkeeping.

  `verify-run` and the verifier ignore `provenance`.
- `rounds/round-N.yaml` and `rounds/round-N.admission.json`: every draft and
  every case's outcome.
- `report.json`: admitted or not, rounds, person decisions, seconds and
  model cost.

`openadapt verify draft --check contract.yaml` tells you whether the file is
still the check that was admitted. Any edit to its effects, its reads or its
frame after admission makes the check fail.

## The model and its cost

The model is pinned in `openadapt_flow/verify_draft/model.py` (`MODEL`,
`EFFORT`, `PROMPT_VERSION`). A refusal, or a model that falls back to
another, never changes the check silently: the response's model id is
recorded too. Every call is appended to a spend ledger (`--spend-ledger`,
default `~/.openadapt/verify-draft/spend.jsonl`). The command refuses to
send a call that could pass `--spend-cap-usd` (default 30).

## Limits

- The store must be SQLite, PostgreSQL or MySQL/MariaDB, reachable from the
  machine that runs the command. A system you can only reach through its API
  can't be qualified this way yet: the late-revert case needs the
  whole-database recheck.
- The clean writes and the attribution probe vary the task's values, not
  the record. A check that only works on the demonstrated record is caught
  by the literal-pin rule, not by a write to another record.
- Lost updates need a second user in the sandbox. Without one, that case is
  reported as not applicable.
- Faults are planted in the database, not made by the app. An app-made
  fault the store can't express this way, such as an email sent, is outside
  the contract's read.

Measured on three apps: [benchmark/verify_draft/RESULTS.md](../benchmark/verify_draft/RESULTS.md).
