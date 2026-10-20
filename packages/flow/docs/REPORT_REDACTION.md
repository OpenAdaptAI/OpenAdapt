# Session tokens in run reports

A run report (`report.json`) can be signed with a Seal and shared as a
receipt. The URLs and headers a run records can carry the application's
session. OpenEMR, for example, puts its per-session CSRF token in the entry
URL (`main.php?token_main=...`), and an OAuth redirect carries `code` and
`state`. A receipt must not carry those values.

## What is removed

When a run report is written (`RunReport.save`, used by `run`, `replay`,
`resume` and the on-prem service), every string in it is checked and these
values become `<redacted>`:

- The value of a secret query, fragment, matrix or form parameter. This
  includes parameters nested percent-encoded in another URL. The built-in
  list: `token_main`, `csrf_token_form`, `csrf_token`, `csrf`, `_csrf`,
  `csrfmiddlewaretoken`, `xsrf_token`, `_token`, `token`,
  `authenticity_token`, `session`, `session_id`, `sessionid`, `sid`,
  `PHPSESSID`, `JSESSIONID`, `auth`, `auth_token`, `access_token`,
  `id_token`, `refresh_token`, `code`, `state`, `client_secret`,
  `password`, `passwd`, `pwd`, `api_key`, `apikey`, `signature`, `sig`,
  and the S3 `X-Amz-Signature`, `X-Amz-Security-Token` and
  `X-Amz-Credential`.
- The value of a credential header, written as a `Name: value` line, a
  JSON key, or a `{"name": ..., "value": ...}` pair: `Authorization`,
  `Proxy-Authorization`, `Cookie`, `Set-Cookie`, `X-Api-Key`,
  `X-Auth-Token`, `X-CSRF-Token`, `X-XSRF-Token`, `X-Amz-Security-Token`.
- A token anywhere in a string: a bearer token, a JWT, an AWS access key
  ID, a GitHub or Slack token, or a private key block.

Names stay: `token_main=<redacted>`, `Authorization: Bearer <redacted>`,
`Cookie: OpenEMR=<redacted>; theme=<redacted>`. The report still shows where
the run went and which session mechanism the app used.

Each change is listed in the report's `redactions` field. An entry gives the
JSON pointer of the string and the reason, never the value:

```json
"redactions": [
  {"pointer": "/execution_entry_url", "reason": "url_param:token_main"}
]
```

A report with nothing to redact has no `redactions` field and is written
byte for byte as before. The report object in memory keeps the real values,
because the run that is still in progress may need them. Only the file
changes.

## The Seal refuses a report that still holds a token

`openadapt-flow local-seal run RUN_DIR`, and the Seal written at the end of
every run, check the report first. If any value above is still there, the
Seal is not written:

```text
not sealed: unredacted: report.json still holds 1 session token or credential
value(s): /execution_entry_url (url_param:token_main). Run
`openadapt-flow redact-report RUN_DIR` and seal again, or pass
--allow-unredacted to seal the report as it is
```

`--allow-unredacted` seals the report anyway, for a report that will never
leave the site. Without it, a Seal always covers redacted bytes. A broken
redaction policy file also stops the Seal (`redaction_policy_unreadable`):
a policy that was written to redact more must not be quietly replaced by
the built-in list.

## Redact a run that was written before this

```bash
openadapt-flow redact-report RUN_DIR
openadapt-flow local-seal run RUN_DIR
```

`redact-report` applies the same redaction to `report.json`, and to
`report.pre-seal.json` when `attach-seal` left one. Each new entry is marked
`"by": "redact-report"`, so the report shows that it was redacted after the
run. Only the redacted strings and the new `redactions` field change.

The original files move to `RUN_DIR/private/unredacted/<UTC time>/` (folder
mode 0700, files 0600), with a `redaction.json` that records the SHA-256 of
each file before and after. It does not record the values. An existing
`seal.json` moves there too, because it covers the original bytes. Seal the
run again over the redacted report. Keep `private/` on the machine that ran
the work, and never publish it.

A run whose Seal is already anchored in a public transparency log
(`seal.anchor.json`) is refused. The public entry names the original
report. Redacting it now would leave that entry pointing at a report that is
no longer published, so decide that with whoever relies on the anchor. A
run directory that is encrypted at rest is also refused. Redact it where it
is decrypted.

## Per-app policy

The built-in list is the default. To add names, or to keep an ordinary word
for one app, write a JSON policy file. Flow reads the file named by
`OPENADAPT_FLOW_REPORT_REDACTION`, else `$OPENADAPT_HOME/report-redaction.json`
(default `~/.openadapt/report-redaction.json`). `redact-report --policy FILE`
names one for a single command.

```json
{
  "params": ["tenant_key"],
  "headers": ["X-Tenant-Key"],
  "apps": {
    "forms.example.org": {"params": ["portal_ticket"], "keep": ["state"]}
  }
}
```

- `params` and `headers` add to the built-in lists for every app.
- `apps` is keyed by the URL's host (with or without the port). `params`
  adds names for that host. `keep` leaves a default parameter alone there,
  for example `state` in an address form.
- `keep` accepts only `code`, `state`, `session`, `sid` and `auth`, the
  default names that are ordinary words in some apps. A credential name such
  as `token_main` or `access_token` is always redacted, and a policy that
  tries to keep one is refused.

## Limits

- Redaction covers `report.json`. Other files in the run directory
  (`steps/`, `checkpoints/`, `private/`) stay local and can still hold the
  URL. Publish the report and its Seal, not the run directory.
- Reports that hosted validation checks store the entry URL redacted. The
  check accepts the attested URL or its redacted form.
- The match is by name and by token shape. A secret in a parameter with an
  unusual name needs a policy entry, and an ordinary value in a parameter
  called `state` or `code` is redacted unless the app's policy keeps it.
