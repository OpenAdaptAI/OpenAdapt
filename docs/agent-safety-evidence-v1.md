# Agent safety evidence v1

This format supports a small accidental-misuse guard for agents sharing a
GitHub account. It checks that declarations are present, current, and
consistent with Git metadata read by the trusted verifier. Reports are
self-reported evidence. They are not signatures, and do not prove that a
command ran or that a Git hook could not be bypassed. This mechanism does not
defend against a malicious actor.

## Exact-head declarations

Each author and independent reviewer posts a separate issue comment containing
one `From: <vendor>` line and one `Reviewed-Head: <40-character lowercase SHA>`
line. The author declaration identifies the vendor that authored the change.
The independent reviewer comment must use the existing heading
`## Independent adversarial review: APPROVE` or
`## Independent adversarial review: BLOCK`. The verifier accepts exactly one
heading and requires the reviewed SHA to match the current pull request head.
The author and reviewer declarations must name different vendors. A stale,
missing, duplicate, malformed, or BLOCK declaration fails closed.

For a change to the manifest or gate implementation itself, add one separate
current-head coordinator comment with exactly these headers:

```text
From: Coordinator
Reviewed-Head: <40-character lowercase PR head SHA>
Coordinator-Approval: APPROVE
```

The comment is a coordinator declaration, not a cryptographic signature.

## Per-commit precommit report

For every commit in the reviewed change range, post one separate comment with
exactly one fenced JSON object in this shape:

```json
{
  "schema_version": 1,
  "commit": "0123456789abcdef0123456789abcdef01234567",
  "parents": ["89abcdef0123456789abcdef0123456789abcdef"],
  "tree": "abcdef0123456789abcdef0123456789abcdef01",
  "commands": [
    {"argv": ["uv", "run", "pytest", "-q", "tests/test_example.py"], "exit_code": 0}
  ],
  "completed_before_commit": true,
  "hook_bypass": false,
  "completed_utc": "2026-11-05T17:59:00Z"
}
```

`completed_utc` is optional; if present it must include a timezone and be no
later than the commit timestamp from Git metadata. The verifier compares the
commit, ordered parents, and tree with trusted Git plumbing. It requires a
nonempty command list and a zero integer exit code for every entry. Unknown or
missing fields, unsupported versions, mismatched metadata, invalid times,
nonzero exits, `completed_before_commit` other than `true`, or
`hook_bypass` other than `false` fail closed. The statements about completion
and hook use remain declarations; the parser cannot establish that they are
true.

The workflow must obtain PR and comment metadata through read-only GitHub API
permissions, and must not import, run, or otherwise execute pull-request-head
code with a token. Its source, manifest, and parser come from the trusted base
commit. The workflow reads PR metadata, files, commits, and issue comments via
the GitHub API, then publishes the stable `protected-safety-review` check for
the exact PR head with `checks: write`. The report parser is pure and does not
make network requests. This reduces accidental misuse under a shared account;
the self-reported declarations are not proof against a malicious actor.

## Enforcement bootstrap

GitHub only starts `pull_request_target` and `issue_comment` workflows when
their workflow file exists on the default branch. This first change therefore
has no running check before it reaches `main`; its own PR's status is **UNRUN**.
After it reaches `main`, adding the workflow still does not make its status a
required branch protection check. The repository owner must separately
configure the exact stable status name in the applicable ruleset. Until the
coordinator confirms the workflow is on `main` and that ruleset is active, the
merge freeze remains in force. This change does not modify repository settings
or claim enforcement before that confirmation.
