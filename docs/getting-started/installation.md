# Install OpenAdapt

> **Current product path.** The canonical documentation is at
> [docs.openadapt.ai](https://docs.openadapt.ai). This repository page keeps the
> launcher install contract close to the package that implements it.

OpenAdapt supports Python 3.10, 3.11, and 3.12. Use a virtual environment for
an isolated install.

## Install and run the browser tutorial

Install the launcher:

```bash
python -m pip install --upgrade openadapt
openadapt quickstart
```

The launcher installs the compatible `openadapt-flow` engine. Do not install
the launcher and engine separately. The first browser action downloads the
matching Chromium build once.

The tutorial uses the bundled synthetic MockMed application. It records,
compiles, certifies, and replays one workflow. A separate read-only interface
verifies the saved record. The healthy result is `VERIFIED` under the Standard
profile with no model or Cloud call.

## Capability-specific installs

The base install includes the launcher, the Flow engine, and the Playwright
driver for the browser tutorial. Chromium downloads only on the first browser
action. Add the applicable capability for a native or remote workflow:

```bash
python -m pip install 'openadapt[capture]'          # local human demonstration
python -m pip install 'openadapt[capture,windows]'  # Windows UI Automation
python -m pip install 'openadapt[capture,macos]'    # macOS Accessibility
python -m pip install 'openadapt[capture,linux]'    # Linux AT-SPI
python -m pip install 'openadapt[capture,rdp]'      # RDP transport
python -m pip install 'openadapt[privacy]'          # PII/PHI scrubbing
```

These extras install platform bindings. They do not certify an arbitrary
application. Qualify each workflow against its exact application, environment,
identity rules, and independent effect verifier.

## Verify the install

```bash
openadapt version
openadapt doctor --backend web
openadapt flow --help
```

For the visual authoring and review interface, install
[OpenAdapt Desktop](https://openadapt.ai/download).

## Development install

```bash
git clone https://github.com/OpenAdaptAI/OpenAdapt
cd OpenAdapt
python -m pip install -e '.[dev]'
```

The engine source is in
[`OpenAdaptAI/openadapt-flow`](https://github.com/OpenAdaptAI/openadapt-flow).
Read its `CONTRIBUTING.md` before a package or release change.

## Next steps

- [Run the local tutorial](quickstart.md)
- [Read the canonical first-workflow guide](https://docs.openadapt.ai/get-started/)
- [Review current substrate evidence](https://docs.openadapt.ai/get-started/what-works-today/)
- [Read the CLI reference](https://docs.openadapt.ai/reference/cli/)
