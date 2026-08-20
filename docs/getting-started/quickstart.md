# Run the OpenAdapt quickstart

> **Current product path.** The canonical walkthrough is at
> [docs.openadapt.ai/get-started](https://docs.openadapt.ai/get-started/).

## Run the verified tutorial

Use Python 3.10, 3.11, or 3.12:

```bash
python -m pip install --upgrade 'openadapt[browser]'
openadapt quickstart
```

On Windows `cmd.exe`, use double quotes around the install target.

The command runs one complete local lifecycle against synthetic MockMed data:

1. It records a demonstrated browser workflow.
2. It compiles the recording into an inspectable bundle.
3. It certifies the bundle with the shipped tutorial policy.
4. It runs the bundle under the Standard profile.
5. It verifies the saved record through a separate read-only interface.
6. It writes a report and a privacy-safe synthetic receipt.

The healthy run returns `VERIFIED`. It makes no model or Cloud call.

Inspect the artifacts:

```bash
openadapt flow visualize openadapt-quickstart/bundle --out graph.html
openadapt flow lint openadapt-quickstart/bundle
```

Run the same certified bundle against a fault-injecting backend:

```bash
openadapt quickstart --break-it --out openadapt-quickstart-broken
```

The application displays success, but the backend does not save the record.
The independent effect verifier detects the mismatch and returns `HALTED`.

## Run the manual demo lifecycle

The engine command is `openadapt-flow`. The launcher provides the equivalent
two-word form `openadapt flow`. There is no standalone `demo-record` command.

```bash
openadapt flow demo-record --out rec
openadapt flow compile rec --out bundle --name my-task
openadapt flow lint bundle --strict
openadapt flow certify bundle --policy permissive
openadapt flow replay bundle --run-dir run
```

The strict lint step returns a nonzero exit code. The bundled manual demo has
an unarmed irreversible click. The permissive certification is only a smoke
gate. The Demo replay returns `COMPLETED_UNVERIFIED`, not `VERIFIED`.

Use `openadapt quickstart` for the effect-verified first run. For a real
workflow, add the application boundary, action risks, identity requirements,
effect verifier, fault cases, and deployment policy before production use.

## Record a browser workflow

```bash
openadapt flow record --backend web --url https://your-app.example --out rec
openadapt flow compile rec --out bundle --name my-workflow
openadapt flow replay bundle --backend web \
  --url https://your-app.example --run-dir run
```

Password fields and fields declared with `--secret` exclude their values at
record time. Read the
[canonical recording guide](https://docs.openadapt.ai/guides/record-your-app/)
before a real-data demonstration.

## Production qualification

A runnable workflow is not automatically a qualified production workflow. A
qualification binds the exact application, version, environment, input schema,
identity checks, effect checks, policy, and verification rules. A Production
run gate must reject an absent, expired, revoked, or mismatched qualification.
Check the
[live signed Production record](https://docs.openadapt.ai/production-lifecycle.json)
for the current admitted releases.
