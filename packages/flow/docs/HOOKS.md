# Hooks for coding agents

Preview. The file format and the commands may change before a release.

When Claude Code or Codex works in a project, OpenAdapt can check each tool
call that writes to a system of record you declare: a database, an HTTP API,
or an MCP server. For each such call:

1. **Pre-check.** OpenAdapt matches the call against the writes you declared
   and their approval mode. It lets the call run, asks a person, or blocks it.
   Before a write runs, it reads the system of record, so that it can tell a
   new record from an old one afterward.
2. **Read-back.** After the call, OpenAdapt reads the record back and checks
   it against the write's contract.
3. **Receipt.** OpenAdapt writes a signed `record/run/v1` receipt for the
   call, whatever the outcome.

The read-back is the same check that [`verify-run`](VERIFY_RUN.md), the
[guarded save](GUARDED_SAVE.md) and `openadapt verify` use
([`VERIFIER_API.md`](VERIFIER_API.md)), with the readers in
[`EFFECT_KIT.md`](EFFECT_KIT.md). The hooks make no model calls.

**The hooks are advisory.** They see the agent's tool calls, not the
network. An agent that holds its own credentials and has a shell can reach a
system in ways no hook recognizes: a script that opens the database, a
connection string in an environment variable, an encoded command. Treat the
hooks as a check on a cooperative agent and a record of what it did, not as a
security boundary. For a boundary, the agent must not hold the credentials;
see [Strict mode](#strict-mode).

```bash
openadapt hooks install --agent claude-code   # or: --agent codex
openadapt hooks status
openadapt hooks approve ap-1a2b3c4d5e6f       # a call waiting for a person
openadapt hooks uninstall
```

## Declare the systems

Put `openadapt.systems.yaml` in the project root. Each system lists how a
call reaches it, the writes that are in scope, and how each write is read
back and approved:

```yaml
schema_version: openadapt.systems/v1
unknown_write: block           # or ask: a write to a declared system that no entry covers
systems:
  - name: clinic-db
    kind: database
    url: sqlite:///data/clinic.sqlite    # relative to the project; or postgresql://host/db
    commands: [sqlite3]                  # the CLIs that reach it
    aliases: ["$DATABASE_URL"]           # other strings that name it in a command
    writes:
      - name: add-note
        sql: "INSERT INTO notes \\(patient_id, note\\) VALUES \\('(?P<patient_id>[^']*)', '(?P<note>[^']*)'\\)"
        contract: contracts/note-db.yaml
        approval: allow

  - name: notes-api
    kind: http
    base_url: http://127.0.0.1:8080
    writes:
      - name: add-note
        method: POST
        path: "/api/notes"
        contract: contracts/note-api.yaml
        approval: ask

  - name: crm
    kind: mcp
    tools: ["mcp__crm__.*"]              # the server's tools
    read_tools: ["mcp__crm__get_.*"]     # tools that only read
    writes:
      - name: update-contact
        tool: mcp__crm__update_contact
        contract: contracts/contact.yaml
        approval: ask
```

Patterns are Python regular expressions that must match the whole text. A
write's values come from the call itself:

- **SQL:** the named groups of the `sql` pattern.
- **HTTP:** the named groups of the `path` pattern and the top-level values
  of a JSON body.
- **MCP:** the tool's top-level arguments.

Each `contract` is an `openadapt.verify/v1` file, the one `verify-run`
reads. It names the reader (`sql`, `rest`, `fhir`, `graphql` and the others)
and the record the write must produce. A call that doesn't supply every value
its contract needs is blocked.

The values come from the agent's own call, so the read-back confirms that
the call did what it said, and that nothing else in the read set changed.
It doesn't confirm that the agent chose the right values. The scope (which
writes are declared) and the approval mode are what limit the choice.

### Approval modes

| `approval` | Claude Code | Codex |
|---|---|---|
| `allow` | The call runs. OpenAdapt sends no decision, so Claude Code's own permission rules and prompt still apply. Set `auto_approve: true` to skip that prompt for this write. | The call runs. |
| `ask` | Claude Code asks the person, with OpenAdapt's reason. | Codex can't ask from a hook, so the call is denied. The reason tells the agent to ask the person to run `openadapt hooks approve <id>` in their own terminal. The same call is then allowed once. |
| `block` | Denied. | Denied. |

### What counts as a write

OpenAdapt fails closed. A call that names a declared system and isn't
provably a read is a write. If no entry covers it, it's an unknown write,
handled by `unknown_write`.

- **Shell** (`Bash` in both agents): a single `sqlite3`, `psql` or `mysql`
  command with one SQL statement on the command line, or a single `curl`
  call. A `SELECT` (with no write keyword outside string literals) is a read,
  and so is a `curl` GET or HEAD, also when piped into a filter such as `jq`.
  Any other command that names the system is an unknown write: a script, a
  redirect, `;` or `&&`, `$(...)`, a body read from a file, a form upload.
  The parsers read an allow-list of flags (`curl -X -d -H -s ...`, `psql -c
  -h -U ...`), and any other flag makes the call an unknown write. SQL that
  holds a comment, an escape or dollar quoting (`--`, `/*`, `#`, `\`, `$$`)
  is never a read and never matches a declared write, because it can hide a
  second statement. A note value with `#` in it is therefore blocked or asked
  about.
- **File tools** (`Write`, `Edit`, `MultiEdit`, `NotebookEdit`, Codex
  `apply_patch`) on a declared SQLite file or its journal: an unknown write.
- **WebFetch** on a declared HTTP base: a read, because it sends GET only.
- **MCP tools**: a tool that matches `tools` and not `read_tools` is a
  write.

A system is "named" when the command contains its URL, its SQLite path or
file name, its host and port, or one of its `aliases`.

The agent can't change its own scope. A call that edits the systems file,
the contracts, `.openadapt/hooks`, or the agents' hook settings is blocked.
So is running `openadapt hooks approve`, `install` or `uninstall` through the
agent's shell.

## Outcomes

| Outcome | Meaning | Receipt `outcome` |
|---|---|---|
| `VERIFIED` | The record reads back as declared, and nothing else in the read set changed. | `VERIFIED` |
| `REFUTED` | The store shows a different write: a wrong value, another record, a duplicate. | `FAILED` |
| `UNCERTAIN` | The read-back failed, showed nothing, or couldn't run. The write may still land. | `UNKNOWN` |
| `BLOCKED` | The pre-check refused the call. Nothing was sent. | `STOPPED` |

`UNCERTAIN` is never a pass. After `REFUTED` or `UNCERTAIN`, the agent is
told not to retry, and the same write is refused until a person checks the
record and runs `openadapt hooks approve <write id>`. "The same write" is the
declared write with the same values, however the call is worded. For an
undeclared write it is the same command or tool input, ignoring Claude Code's
free-text `description`. When the store
can't be read before a write, the write is blocked, because nothing could
check it afterward.

If a pre-checked call never gets its `PostToolUse` (the person declined it,
the agent was interrupted, the tool failed in a way the agent didn't report),
the `Stop` hook reads it back when the agent stops. A call that never ran
then comes out `UNCERTAIN`, because the read-back can't tell "not sent" from
"not landed yet".

## Receipts

Each checked call gets a folder `.openadapt/hooks/runs/<id>/`:

- `report.json`: the outcome, the reasons, the system and write names, and a
  digest of the call. No values from the call.
- `receipt.json`: a DSSE envelope over a `record/run/v1` statement, signed with
  this computer's key ([`openadapt_types.receipt`](../../types/openadapt_types/receipt.py)).
  It covers `report.json` by digest and holds no value, path or URL from the
  call.
- `receipt.private.json`: the salts behind the receipt's commitments (mode
  0600).
- `effect-receipt.json`: the verifier's own `openadapt.effect-receipt/v1`,
  when a read-back ran.

Two fields of the receipt need reading with care, because `record/run/v1`
was made for OpenAdapt's own runs:

- `model_calls` is 0: it counts OpenAdapt's check, which makes none. The
  agent's own model calls are not counted, and the receipt doesn't say so.
- A declared write whose read-back didn't settle (`UNCERTAIN`) has a line
  with `not_checked: approved_unverified`, the closest code the schema has.
  An undeclared write that a person allowed has `effect_not_declared`.

The signing key is this computer's receipt key, the same one `openadapt run`
uses (`OPENADAPT_SEAL_HOME`, default `~/.openadapt/seal`). It is created on
the first receipt if it doesn't exist yet. `.openadapt/hooks/` holds a
`.gitignore` that keeps state, snapshots and receipts out of the repository.

## What install writes

`install` writes only project files, and never `~/.claude` or `~/.codex`. It
refuses to run in your home folder.

| Agent | File | Events |
|---|---|---|
| Claude Code | `.claude/settings.local.json` | `PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `Stop` |
| Codex | `.codex/hooks.json` | `PreToolUse`, `PostToolUse`, `Stop` |

Both entries run `.openadapt/hooks/run-hook`, a launcher that pins the
Python that ran `install` and the project folder. Existing settings are kept.
`uninstall` removes only the entries that run this launcher, and removes a
file that `install` created if it ends up empty.

Claude Code's `settings.local.json` is not shared with the team, because the
launcher path differs on each machine. Claude Code holds back project hooks
until you accept the folder's workspace trust dialog. Codex loads project
hooks only after you trust the project and the hook (`/hooks` in the Codex
CLI), and a changed hook must be trusted again. `openadapt hooks status`
can't see Codex's trust.

## Agent facts this relies on

Checked against the agents' documentation on 2026-11-21.

**Claude Code** ([hooks reference](https://code.claude.com/docs/en/hooks)):

- Input on stdin: `session_id`, `transcript_path`, `cwd`, `permission_mode`
  and `hook_event_name`. `PreToolUse` adds `tool_name`, `tool_input` and
  `tool_use_id`. `PostToolUse` adds `tool_response`. `Stop` adds
  `stop_hook_active` and `last_assistant_message`.
- `PreToolUse` answers with `hookSpecificOutput.permissionDecision`: `allow`,
  `deny`, `ask` or `defer`, with `permissionDecisionReason`. `allow` skips
  the permission prompt. Deny and ask rules in settings still apply whatever
  the hook returns. The older top-level `decision` field is deprecated for
  this event.
- `PostToolUse` and `Stop` use a top-level `decision: "block"` and `reason`.
  After a tool call, `block` adds the reason next to the result. It can't undo
  the call.
- Exit 2 blocks, with stderr as the reason. Any other non-zero exit is a
  non-blocking error, and the tool runs.

**Codex** ([hooks](https://learn.chatgpt.com/docs/hooks)):

- Codex has hooks, configured in `.codex/hooks.json` or `[hooks]` in
  `config.toml`, with the same event, matcher and handler structure. Matchers
  are regular expressions. Shell calls match as `Bash` and patches as
  `apply_patch`, both with `tool_input.command`. MCP tools match by their
  `mcp__server__tool` name. Hosted tools such as web search are not covered.
- Input adds `model` and `turn_id` to Claude Code's fields.
- `PreToolUse` supports `deny` and `allow`. `ask` is parsed but not
  supported: Codex marks the hook as failed and runs the tool. That is why
  OpenAdapt never sends `ask` to Codex.
- Exit 2 blocks. Errors, timeouts and malformed output let the tool run.

### Failing closed

Both agents run the tool when a hook fails in any way other than exit 2. So
the launcher turns every failure before a tool call into exit 2: a missing
Python, a crash, or an unreadable systems file. It also blocks every hooked
call while the hooks are broken. Run `openadapt hooks status` to see why.
After a tool call, a hook failure is reported to the agent and doesn't block.

The launcher can't catch a timeout: the agent kills the hook at its
`timeout` (120 seconds here). For Claude Code 2.1.295 and later, the
`PreToolUse` entry sets `onFailure: "block"`, so a timeout, or a launcher
that can't start, blocks the call. Older Claude Code versions and Codex let
the call run after a timeout. Keep each contract's own read timeouts
(`timeout_s`) well under 120 seconds, because the pre-check reads the store
before the write.

The launcher runs Python from `.openadapt/hooks`, which holds no Python
files, so a project file named like a module Python imports can't shadow
it.

## Strict mode

`openadapt hooks install --strict` is refused in this release. Strict mode
needs a broker that holds the credentials and gives the agent scoped tools
only, so that the agent has no direct path to the system. `openadapt wrap
-- <command>` is that broker for browser tasks: the agent gets an MCP endpoint
and no browser handle. A broker for databases, HTTP APIs and MCP servers
doesn't exist yet. Until it does, `--strict` refuses instead of installing
advisory hooks under a strict name.

## Limits

- Hooks are advisory (see the top of this page).
- Only the commands listed under [What counts as a write](#what-counts-as-a-write)
  are parsed. Anything else that names a system is blocked or asked about,
  and a call that doesn't name a system isn't seen at all.
- The read-back covers the contract's read set. A change outside it, such as a
  trigger writing another table, is seen only if the contract's `frame`
  reads the whole database (`frame.database`, see
  [`VERIFY_RUN.md`](VERIFY_RUN.md)).
- The hooks keep their own hold on a call that came out `REFUTED` or
  `UNCERTAIN`. They don't use the uncertain-write ledger, so another process
  writing the same record isn't held.
- Tested with a scripted harness that sends each agent's documented hook
  input, against a local SQLite file and a local HTTP API. Not yet run in a
  live Claude Code or Codex session.
