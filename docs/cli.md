# OpenAdapt CLI reference

> **Canonical reference.** The complete current option list is at
> [docs.openadapt.ai/reference/cli](https://docs.openadapt.ai/reference/cli/).
> Run `openadapt flow <command> --help` for the exact installed engine version.

The launcher command is `openadapt`. It delegates the product lifecycle to the
installed `openadapt-flow` engine.

## First run

```bash
python -m pip install --upgrade openadapt
openadapt flow tutorial [--headed] [--break-it] [--out NEW_DIRECTORY]
openadapt-agent serve --allow-run
```

`openadapt flow tutorial` is the command that writes a `VERIFIED` receipt. It
runs the bundled synthetic MockMed workflow from recording through an
independently verified Standard-profile result. `openadapt quickstart --break-it`
is the halt demo: it reruns the same certified bundle against a lying backend,
and the independent oracle must HALT and leave the store unchanged.
`openadapt-agent serve --allow-run` then generates the public MockMed bundle at
serve time and hosts it over MCP. A client POSTs authorized work and gets a
receipt. Frames stay on this machine.

## Flow lifecycle

The launcher and engine forms are equivalent:

```bash
openadapt flow <command> [options]
openadapt-flow <command> [options]
```

Use one form for the complete command. `demo-record` is a Flow subcommand. It
is not a standalone executable.

| Command | Purpose |
| --- | --- |
| `record` | Record a human demonstration on a declared surface. |
| `demo-record` | Create the bundled synthetic demonstration. |
| `compile` | Compile a recording into a deterministic bundle. |
| `lint` | Report identity, action-risk, effect, and policy gaps. |
| `certify` | Evaluate a bundle against a named policy. |
| `replay` | Run a bundle in the explicit Demo posture. |
| `run` | Admit and run a bundle with a deployment configuration. |
| `resume` | Resume a verified durable pause. |
| `visualize` | Render the compiled program for inspection. |
| `report-run` | Create a closed-schema receipt from a verified run. |
| `sanitize` | Create a sanitized derivative without changing the source. |
| `review-sanitized` | Compare a derivative with its local source. |
| `approve-sanitized` | Bind approval to the exact derivative bytes. |
| `repair` | Review, test, promote, or roll back a repair candidate. |

The manual synthetic lifecycle is:

```bash
openadapt flow demo-record --out rec
openadapt flow compile rec --out bundle --name my-task
openadapt flow lint bundle --strict
openadapt flow certify bundle --policy permissive
openadapt flow replay bundle --run-dir run
```

The strict lint step refuses the unarmed demo write. The permissive policy is a
smoke gate. The replay uses the Demo profile and returns
`COMPLETED_UNVERIFIED`.

## Record and replay one surface

```bash
openadapt flow record --out rec
openadapt flow record --url https://your-app.example --out rec
openadapt flow replay bundle --url https://your-app.example --run-dir run
```

Omit `--backend`. No `--url` records this OS. `--url` records the browser.

Supported selectors are `web`, `windows`, `macos`, `linux`, `rdp`, and
`citrix`. The required target flags differ by surface. Run these commands for
the installed option contract:

```bash
openadapt flow record --help
openadapt flow replay --help
```

## Launcher diagnostics

```bash
openadapt version
openadapt doctor --backend web
openadapt deploy --backend web
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
openadapt flow connect
openadapt flow login --token oai_ingest_...
openadapt flow push APPROVED_DERIVATIVE --kind recording
```

Do not upload a raw recording. The push path accepts an approved sanitized
derivative and validates its exact bytes.

## Capture and research commands

The optional `capture` group exposes the supported low-level Capture component
for raw native sessions. Use `openadapt flow record` when the output must enter
the compiler directly. The `train` and `eval` groups are research surfaces and
are not required for the record-compile-replay product path.

## Exit status

An exit status of zero means that the requested command completed. It does not
by itself prove a business effect. Read `report.json` and the transaction
outcome. A production success requires a `VERIFIED` outcome from the complete
configured contract.
