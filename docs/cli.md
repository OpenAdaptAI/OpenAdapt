# OpenAdapt CLI reference

> **Canonical reference.** The complete current option list is at
> [docs.openadapt.ai/reference/cli](https://docs.openadapt.ai/reference/cli/).
> Run `openadapt <task> --help` for the installed command's options.

The launcher command is `openadapt`. It delegates the product lifecycle to the
installed `openadapt-flow` engine.

## Command tasks

`openadapt --help` lists 15 shared engine tasks and the launcher's `wrap` task:

`demo`, `verify`, `record`, `teach`, `compile`, `run`, `approve`, `undo`,
`qualify`, `repair`, `inspect`, `workflow`, `services`, `setup`, `research`, and `wrap`.

Run `<task> --help` to see its arguments or related operations. For example:

```bash
openadapt demo quickstart --break-it
openadapt verify effect --help
openadapt inspect lint bundle
openadapt run --replay bundle --run-dir run
openadapt services agent serve --bundles bundle --mode production
openadapt setup doctor --backend web
openadapt wrap --help
```

`workflow` manages reusable libraries: pull, push, skills, and generated skill
or MCP exports. Execution variants belong under `run`; reports belong under
`inspect`.

The old names, including `openadapt flow ...`, remain callable. An alias notice
appears only on interactive stderr; redirected streams and machine output keep
their existing format. Commands that accept paths remain leaves. For example,
`openadapt run replay` still names a bundle called `replay`. Select an alternate
operation immediately after the task name: `openadapt run --replay bundle`.
A `--` before the task ends launcher options: `openadapt -- run --replay bundle`.
After the operation, `--` protects a literal path beginning with `-`:
`openadapt run --replay -- --bundle` and `openadapt flow replay -- --bundle`
pass the same literal path to Flow.

`openadapt-flow` presents the 15 shared engine tasks. Launcher operations such
as `demo quickstart`, `record capture`, `services agent`, `setup doctor`, and
`wrap` stay in `openadapt`. `wrap` runs an agent task once and reuses its checked
compiled tool. Run `openadapt wrap --help` for its installed options. The
`openadapt-agent` command and other module entrypoints keep their existing grammar.

## First run

```bash
uvx --python 3.12 openadapt demo tutorial [--headed] [--break-it] [--out NEW_DIRECTORY]
```

uv fetches Python 3.12 for the run. With `openadapt` installed, run
`openadapt demo tutorial` directly.

`openadapt demo tutorial` is the command that writes a `VERIFIED` receipt. It
runs the bundled synthetic MockMed workflow from recording through an
independently verified Standard-profile result. `openadapt demo quickstart --break-it`
is the halt demo. It reruns the same certified bundle against a backend that
rejects the write after the app shows success. The independent read finds no
saved record, and the run halts with transaction outcome
`RECONCILIATION_REQUIRED`, so nothing is retried until a person checks the
record.

The engine runs on Python 3.10 through 3.12. On Python 3.13 or newer, pip
installs only the launcher and every engine command prints the fix:
`uv tool install --python 3.12 openadapt`. The `uvx --python 3.12` form above
needs no install step.

To serve compiled programs to an MCP client, use the agent bridge.
`pip install openadapt` (1.19.0) installs it as `openadapt-agent` (2.2.1):

```bash
openadapt-agent serve --bundles <bundle or directory of bundles> --allow-run
```

## Flow lifecycle

For shared engine tasks, the launcher and engine forms are equivalent:

```bash
openadapt <task> [options]
openadapt-flow <task> [options]
```

Use one form for the complete command. `demo record` is a Flow command path. It
is not a standalone executable.

| Command | Purpose |
| --- | --- |
| `record` | Record a human demonstration on a declared surface. |
| `demo record` | Create the bundled synthetic demonstration. |
| `compile` | Compile a recording into a deterministic bundle. |
| `inspect lint` | Report identity, action-risk, effect, and policy gaps. |
| `qualify --certify` | Evaluate a bundle against a named policy. |
| `run --replay` | Run a bundle in the explicit Demo posture. |
| `run` | Admit and run a bundle with a deployment configuration. |
| `run --resume` | Resume a verified durable pause. |
| `inspect visualize` | Render the compiled program for inspection. |
| `inspect report-run` | Create a closed-schema receipt from a verified run. |
| `compile --sanitize` | Create a sanitized derivative without changing the source. |
| `approve --review-sanitized` | Compare a derivative with its local source. |
| `approve --sanitized` | Bind approval to the exact derivative bytes. |
| `repair` | Review, test, promote, or roll back a repair candidate. |

The manual synthetic lifecycle is:

```bash
openadapt demo record --out rec
openadapt compile rec --out bundle --name my-task
openadapt inspect lint bundle --strict
openadapt qualify --certify bundle --policy permissive
openadapt run --replay bundle --demo-app --run-dir run
```

The strict lint step refuses the unarmed demo write. The permissive policy is a
smoke gate. The replay uses the Demo profile and returns
`COMPLETED_UNVERIFIED`.

## Record and replay one surface

```bash
openadapt record --backend <web|windows|macos|linux|rdp|citrix> <target> --out rec
```

The target is `--url` for a web app and `--window` (plus `--window-title`) for
a desktop app or a local RDP or Citrix client window. Network RDP is recorded
inside the remote session, and Linux records the local desktop. Side by side:

```bash
# A web app
openadapt record --backend web --url https://your-app.example --out rec
openadapt run --replay bundle --url https://your-app.example --run-dir run

# A desktop app (macOS here; Windows and Citrix take --window too).
# Desktop recording and macOS replay are separate extras.
python -m pip install --upgrade 'openadapt[capture,macos]'
openadapt record --backend macos --window "Your App" --out rec
openadapt run --replay bundle --macos-app "Your App" --run-dir run
```

`run --replay` performs every recorded action, including real saves; point it
at a test or staging copy. `--backend` can be left out: with `--url` it records
the browser, and without it records this computer's desktop. The commands for
every surface are in
[SURFACES.md](../packages/flow/docs/SURFACES.md#first-workflow-per-surface).

Supported selectors are `web`, `windows`, `macos`, `linux`, `rdp`, and
`citrix`. The required target flags differ by surface. Run these commands for
the installed option contract:

```bash
openadapt record --help
openadapt run --replay --help
```

## Launcher diagnostics

```bash
openadapt setup version
openadapt setup doctor --backend web
openadapt setup deploy --backend web
```

`doctor` checks the required launcher packages. For the default and `web`
checks, it also uses Flow's Chromium library probe. A missing core package or
Chromium system library returns a nonzero exit status. On Linux, install missing
browser libraries with:

```bash
python -m playwright install-deps chromium
```

Playwright asks for administrator access when the package manager needs it.
The command printed by `doctor` uses the exact Python interpreter that runs the
launcher, including an interpreter inside an isolated tool environment.

An absent Chromium binary does not fail the check when its host libraries are
ready. OpenAdapt will ask Playwright to download the matching build on the first
browser action, so that action needs network access.

`deploy` performs a read-only deployment preflight and prints the applicable
Flow and Desktop path. Neither command certifies a customer workflow.

## Hosted connection

```bash
openadapt setup connect
openadapt setup login --token oai_ingest_...
openadapt workflow push APPROVED_DERIVATIVE --kind recording
```

Do not upload a raw recording. The push path accepts an approved sanitized
derivative and validates its exact bytes.

## Capture and research commands

The optional `record capture` group exposes the supported low-level Capture
component for raw native sessions. Use `openadapt record` when the output must
enter the compiler directly. The `research train` and `research eval` groups
are research surfaces and are not required for the record-compile-replay
product path.

## Exit status

An exit status of zero means that the requested command completed. It does not
by itself prove a business effect. Read `report.json` and the transaction
outcome. A production success requires a `VERIFIED` outcome from the complete
configured contract.

## Check a Seal

A Seal is OpenAdapt Cloud's Ed25519 signature on an Execute receipt. Check one offline against keys you pinned:

```bash
openadapt verify seal keys pin                        # once; compare the fingerprint before you type the key id
openadapt verify seal verify runs/<run_id>/report.json
openadapt verify seal verify https://example.com/receipt.json --json
```

`verify` never downloads keys. It exits 0 only for a genuine Seal over a verified outcome. Run `keys pin` again when OpenAdapt rotates its key; a new key needs your confirmation, and a revoked key stops verifying Seals issued after its revocation.
