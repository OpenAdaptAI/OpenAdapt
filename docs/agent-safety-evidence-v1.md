# Protected-path safety review

The `protected-safety-review` check guards against accidental agent changes to
the paths listed in `.github/agent-safety-manifest.json`. It is an
evidence-presence check for agents that share a GitHub account. It is not a
signature and does not defend against a malicious actor.

## The rule

A pull request that changes a protected path, including the gate's own files,
needs exactly one valid independent review comment for the current head. That
review must say APPROVE, and no current-head review may say BLOCK.

The review is one issue comment with these lines:

```text
## Independent adversarial review: APPROVE
From: <vendor>
Reviewed-Head: <40-character lowercase PR head SHA>
```

Use `## Independent adversarial review: BLOCK` to block. The `From` line starts
with the reviewer's vendor and may add free text, for example
`From: Claude (independent reviewer)`.

## Vendors

The author's vendor comes from the first segment of the pull request branch
name when it is a vendor name, for example `codex/fix-ledger`. This applies
only to branches in this repository. The reviewer's vendor comes from its
`From` line. The two must belong to different vendor families:

| Family | Accepted names |
| --- | --- |
| Anthropic | `claude`, `anthropic` |
| OpenAI | `codex`, `gpt`, `openai` |
| xAI | `grok`, `xai` |
| Google | `gemini`, `google` |
| Human | `human` |

Names are case-insensitive. Both vendor names are self-reported.

A shared or legacy branch, such as `agent/123`, `fix/...` or `flow/...`, or a
branch from a fork, needs exactly one author declaration for the current head.
It is a separate, unedited comment from the pull request author, in the
existing format:

```text
From: <vendor>
Reviewed-Head: <40-character lowercase PR head SHA>
```

A declaration has no heading and no verdict word, and it never counts as a
review. On a vendor-named branch no declaration is needed. An edited
declaration never grants anything, but any current-head declaration from the
pull request author, edited or not, that names a different vendor family than
the branch (or than the valid declaration) fails. An old coordinator note is
ignored only when it has exactly one `From: Coordinator`, one exact
`Reviewed-Head` for the current head, one `Coordinator-Approval: APPROVE` and
no other heading or verdict word; any other shape fails.

## What fails

- No current-head review, or more than one valid APPROVE.
- A current-head BLOCK, or a current-head review that is malformed or edited.
  An edited comment never supplies approval.
- A comment whose `Reviewed-Head` line names the current head, has no review
  heading and is not a valid author declaration, such as a mistyped BLOCK.
- A shared or legacy branch without exactly one valid author declaration.
- A head binding that is not the exact `Reviewed-Head: <full SHA>` line, such
  as a bold, listed, prefixed or Unicode look-alike binding.

Only comments from accounts whose `author_association` is `OWNER`, `MEMBER` or
`COLLABORATOR` count; others neither approve nor block. A review bound to an
earlier head is ignored, unless it also refers to the current head. Text inside
Markdown code fences and `>` quotes is an example and never counts as a review
or a BLOCK. HTML comments (`<!-- -->`) can only remove approval: an APPROVE
inside one never counts, but a BLOCK heading inside one still blocks. A review
whose body has an ambiguous or unclosed HTML comment, including one closed by
`--!>`, never approves. A fence-like line inside an HTML comment does not open
a fence.

## How the check runs

The workflow runs from the trusted base commit under `pull_request_target`. It
reads pull request metadata, changed files and comments through the GitHub
API, and never checks out or runs pull request code. It matches both sides of
file renames against the manifest. It reads the comments and the head SHA,
branch name and head repository again before publishing, and fails if any of
them changed. No timestamp is parsed; comment timestamps are compared as
strings and count only in the exact `YYYY-MM-DDTHH:MM:SSZ` form. Errors never include API response or
exception text.

For merge-group events it checks out the current `main` commit and maps the
group head to queue entries through GitHub's merge-commit chain. Each mapped
pull request needs an earlier passing `protected-safety-review` verdict for its
exact head, and its evidence is evaluated again. Rebase and squash queue
methods are not supported.

Making this check required is a repository ruleset setting, separate from this
file.
