# Governed emitted MCP tools

Regenerate an emitted server with `openadapt-flow emit-mcp BUNDLE --out server.py`.
All surfaces use the same startup contract:

```bash
python server.py --config deploy.yaml --profile standard \
  --qualification-authority-file /path/to/existing-authority.json
```

`regulated` is the other supported profile. The deployment owns the application
target and independent readback configuration. Its backend kind must match the
bundle's surface. A conflicting configured profile refuses. The server requires
an existing authority file supplied through the current qualification path;
it never creates or admits one. A certificate or a bundle's own status does not
supply that authority. Qualification and authority checks remain in the shared
`openadapt-flow run` path for both ordinary local and pulled bundles.

Tool arguments supply the workflow's ordinary data parameters. They cannot select
or override the deployment, profile, target, readback or authority. The optional
`url` field is retained only to reject legacy target overrides: any non-null
value, including an empty string or the current target, refuses before launching
a child. A workflow data parameter named `url` receives a separate generated
alias and remains ordinary workflow data.

Every invocation uses a new private run directory and a private parameters file.
Parameter values stay off the command line. Windows parameter delivery uses the
existing private-file ACL check and refuses when privacy cannot be established. Existing `--pin-digest` and
`--config-sha256` guards bind the governed child to the captured bundle and
deployment. The shared CLI checks qualification, intended instance, authority
and runtime inputs before delivery, including changes to the deployment target.
The server does not fall back to a browser or direct replay.

## Interpret the result

`REFUSED` from the adapter means it launched no child. Correct the startup
configuration or complete the original bundle's existing qualification path;
changing a per-call URL cannot fix it.

After child launch, missing, malformed, unreadable or inconsistent reports and
abnormal child exits return `HALTED` with `RECONCILIATION_REQUIRED`. A write may
have landed. Inspect the retained run and reconcile the record before another
attempt. Missing output does not establish that nothing ran. The adapter never
retries automatically and does not echo child output or report validation errors.

Success requires a complete typed outcome envelope for the requested production
profile, a verified transaction, production eligibility, and the captured bundle,
semantic, runtime-input and target bindings. Qualification-only evidence cannot
report production success. Sealed reports use the existing artifact reader;
there is no plaintext fallback when that reader fails. The output directory is
owned before launch; links, replacement, unknown directory identity and observed
file drift cannot supply a new completion. The response is a
projection of a governed run report, not a new signed receipt or qualification.

Existing generated servers retain their old code until regenerated. This source
change does not qualify a bundle, execute an app, publish a registry release or
activate an MCP deployment. Synthetic transport fixtures validate the interface;
a genuine positive run still requires independently supplied qualification and
authority for the intended instance and inputs.
