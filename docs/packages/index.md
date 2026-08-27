# OpenAdapt package map

The product path uses the launcher, Flow engine, Capture component, Desktop
cockpit, privacy tools, and optional managed control plane.

| Package | Current role | Install route |
| --- | --- | --- |
| `openadapt` | Launcher and unified CLI | `pip install openadapt` |
| `openadapt-flow` | Compiler and governed runtime; installed by the launcher | `pip install openadapt-flow` for engine-only use |
| `openadapt-capture` | Native screen, input, timing, window-scope, and media capture | `pip install 'openadapt[capture]'` |
| `openadapt-privacy` | Local privacy and sanitization support | `pip install 'openadapt[privacy]'` |
| `openadapt-desktop` | Visual authoring and operator application | [Download an installer](https://openadapt.ai/download) |

Package roles do not assign a lifecycle state. Use the
[signed Production record](https://docs.openadapt.ai/production-lifecycle.json)
for the current admission state of each product target.

Use [OpenAdapt Desktop](https://openadapt.ai/download) for the recommended first
workflow. Start with a small read-only task and test data. Review the compiled
steps, run once under supervision, and inspect the saved result.

Install the launcher and run the optional synthetic check:

```bash
python -m pip install --upgrade openadapt
openadapt quickstart
```

The synthetic result checks the installation. It doesn't qualify a workflow.

Install a native or remote capability:

```bash
python -m pip install 'openadapt[capture,windows]'
python -m pip install 'openadapt[capture,macos]'
python -m pip install 'openadapt[capture,linux]'
python -m pip install 'openadapt[capture,rdp]'
```

The `openadapt-ml`, `openadapt-evals`, `openadapt-viewer`,
`openadapt-grounding`, and `openadapt-retrieval` packages are research or
historical surfaces. They are not required to record, compile, replay, or
verify a workflow. The compatibility extras remain available for existing
users, but new onboarding must not lead with model training.

See the [project map](https://docs.openadapt.ai/concepts/ecosystem/) and the
[current capability evidence](https://docs.openadapt.ai/get-started/what-works-today/)
for the maintained status of each surface.
