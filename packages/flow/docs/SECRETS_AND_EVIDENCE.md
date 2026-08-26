# Secrets and the captured-evidence contract

## Secret input values stay page-local

An `input[type=password]` field (or a field named with `--secret <name>`)
becomes a secret parameter. Flow does not send its literal to Python. It masks
the bound field region in saved frames. For every other piece of page text,
**Flow reports it exactly or withholds it and says why. Flow never rewrites
captured text.** Matching uses only the value a bound element holds at that
moment, read live from the DOM; no value is kept after the field stops holding
it. A shadow field whose identity can change must use a host with the same
declared name or ID; Flow masks the complete host. It refuses an unbound shadow
input before it accepts a value. At replay, Flow injects the secret from the
environment and fails fast when it is absent:

```bash
openadapt-flow record --backend web --url https://your.app --out rec --secret password
export OPENADAPT_FLOW_SECRET_PASSWORD='…'                 # supplied at replay
openadapt-flow replay bundle --backend web --url https://your.app
```

## Identity evidence and reflected evidence

Evidence splits in two. **Identity evidence** is the DOM selector, the control
role, the accessible name, the clicked row's identity characters, and the
receiving field's name. It is exact or withheld with a stated reason, because
replay compares it against the live page and a rewritten copy would compare
against text the page never showed. **Reflected evidence** is the page URL and
the title. It is sampled from Python once the page has settled, never inside the
capture-phase listener, which runs before the page's own handlers and so reads
the previous action's text.

## A URL is reduced by structure, not by rewriting

Within a document, a URL is reduced by **structure**: Flow reports the origin
and the path, keeps every parameter name, and drops the value of any parameter
named after a declared secret field — deterministically, whatever the value is.
A dropped value becomes empty; Flow removes characters from a URL and never adds
characters the page did not show. A single-page application that routes with
`history.pushState` therefore keeps its URL evidence. If the URL Flow is about
to report still holds a value Flow can see, it withholds the whole URL and warns
you that the application put a secret in its own URL — a defect that exposes it
through browser history, logs, proxies and `Referer` headers with or without
Flow.

That reduction does **not** make a later document safe. A path segment has no
parameter name to identify it, so a server that answers a form submit with a
redirect to `/results/<value>` puts the value where structure cannot reach, and
the new document holds nothing to match it against. Flow therefore withholds the
URL and the title of every document after the one that first held a declared
value. A title has no structure to reduce and follows the same rule within a
document. `meta.json` records everything dropped and everything withheld, and
the CLI prints it.

## What this contract does not cover

This source-time contract does not track an application-defined transform of a
secret or an application copy into an unrelated visible element, and it starts
at the moment a bound field holds the value: text and pixels captured before
then are ordinary recording evidence. Keep every raw recording inside its
approved local boundary.

Related: [`PRIVACY.md`](PRIVACY.md) for the PHI scrubbing map,
[`phi_at_rest.md`](phi_at_rest.md) for bundle encryption, and
[`RECEIPTS.md`](RECEIPTS.md) for the shareable-artifact allow-list.
