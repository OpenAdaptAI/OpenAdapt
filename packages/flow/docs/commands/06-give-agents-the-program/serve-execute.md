`serve-execute` hosts the local Execute HTTP and MCP interfaces. Supply the
worker's deployment for programs recorded on a native or remote surface:

```sh
openadapt-flow serve-execute --config deploy.yaml --data-dir ./execute-state
```

An admission's own `deployment` takes precedence over the worker's default.
The deployment must name the same backend as the recorded program. Native
and remote dispatch requires the admission's signed
`qualification_authority_file` and runs the program through the standard
`run --config` path, pinned to its bundle digest. A missing authority or
mismatched surface refuses before the child starts. A child that may have
run but leaves no readable report requires reconciliation.

Receipts from this local service are self-signed; they are not OpenAdapt
production Seals.
