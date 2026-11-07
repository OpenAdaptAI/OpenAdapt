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
Evidence comments are accepted only from GitHub accounts whose API
`author_association` is `OWNER`, `MEMBER`, or `COLLABORATOR`. The author
declaration must also be posted by the API-reported pull request author. The
`From` vendor line remains self-reported evidence, not an identity proof.

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
  "completed_utc": "2026-11-05T17:59:00Z"
}
```

`completed_utc` is optional; if present it must include a timezone and be no
later than the commit timestamp from Git metadata. The verifier compares the
commit, ordered parents, and tree with trusted Git plumbing. It requires a
nonempty command list and a zero integer exit code for every entry. Unknown or
missing fields, unsupported versions, mismatched metadata, invalid times,
nonzero exits, or `completed_before_commit` other than `true` fail closed. The
completion statement is self-reported; the parser cannot establish that
commands ran, that they ran before the commit, or whether a hook was bypassed.

The workflow reads PR and comment metadata through the GitHub API. The verifier
and manifest come from a trusted base SHA; it does not check out or execute PR
head code with the token. For merge-group events, it checks out the current
default-branch commit, never the speculative group base or group head, and
publishes status on the separate merge-group head SHA. It maps the group head
to queue entries through GitHub's `baseCommit` and `headCommit` chain for queues
configured to use merge commits, then requires each mapped PR's exact current
head to have an already-passed, provenance-checked `protected-safety-review`
workflow verdict. It also re-evaluates current PR metadata and evidence, and
fails closed if the chain, verdict, or queue response is incomplete or
ambiguous. A speculative group base may be an earlier merge-group commit; it is
used only as a queue-mapping boundary and is never trusted code. Rebase and
squash queue methods are unsupported because they can rewrite commit history.
The stable
`protected-safety-review` check is linked to the running workflow record. This
is an accidental-misuse control; the shared GitHub Actions app and self-reported
comments do not prove command execution or defend against malicious actors.

The verifier compares file and commit API result counts to PR metadata, rejects
duplicate IDs and over-limit commit histories, and includes both sides of file
renames when matching the protected-path manifest. Accepted author, review,
coordinator, and precommit comments must be unedited (`created_at` equals
`updated_at`). It fetches comments again before publishing and fails if their
IDs, bodies, authors, associations, creation times, or update times changed.
Per-PR and per-merge-group workflow concurrency cancels an earlier run when a
newer review event arrives. Rebase onto the current main tip is not required;
the workflow and manifest remain pinned to the trusted event base.

## Enforcement bootstrap

This first change has no running check before it reaches `main`; its own PR's
status is **UNRUN**. After it reaches `main`, adding the workflow still does
not make its status a required branch protection check. The repository owner
must separately configure the exact stable status name in the applicable
ruleset. Until the coordinator confirms the workflow is on `main` and that
ruleset is active, the merge freeze remains in force. This change does not
modify repository settings or claim enforcement before that confirmation.
