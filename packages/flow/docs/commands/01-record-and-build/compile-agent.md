`compile-agent` runs an agent once, checks the resulting record, and compiles
the demonstration into a program. Use `--url` for a browser app or `--config`
for a deployment that names the target backend:

```sh
openadapt-flow compile-agent "Update the note to {note}" \
  --config deploy.yaml --agent steps.json \
  --verify verify.yaml --expect record_id=record-001 \
  --param note="Follow up" --on-screen "Record 001"
```

The deployment's `backend:` section selects Windows, macOS, Linux, RDP or
Citrix through `build_backend`. `steps.json` can contain scripted control
steps, with no model calls. The record check uses `verify.yaml`; a screen
change alone does not confirm the write.

With `admit TASK_DIR`, a person confirms and admits a waiting draft, such as
one an agent taught over MCP (`openadapt-agent serve --teach`). Resuming a
task reloads its deployment file; credentials are not copied into the saved
task specification.
