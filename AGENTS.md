# Instructions for coding agents

These rules apply to every automated agent working in this repository
(Codex, Grok, Claude and others). Read [CONTRIBUTING.md](CONTRIBUTING.md) for
layout, install and test commands.

## Taking work

- Work only on open issues labelled `agent-ready` that do not have the
  `claimed` label.
- Claim an issue by pushing a new branch from current `origin/main`:
  `git push origin origin/main:refs/heads/agent/<issue number>`. If the push
  fails because the branch exists, another agent has the issue: pick another.
  Then add the `claimed` label and comment `Claimed by <agent name>`.
- If you stop before opening a pull request, comment why, remove the
  `claimed` label and delete your branch.
- One issue per branch and per pull request. Keep changes small and focused;
  if the work is bigger than the issue describes, comment and stop.

## Delivering work

- Branch from current `origin/main`. Open a **draft** pull request with
  `Closes #<issue number>` and a short plain-English summary: what changed,
  how you tested it, and anything you could not test.
- Never merge, never enable auto-merge, never push to `main`, never
  force-push a branch you did not create, and never close other people's
  issues or pull requests.
- Run the tests and linters for the package you changed before pushing (see
  `.github/workflows/packages.yml` for the exact commands). A pull request
  whose CI fails is not done.
- Do not add attribution lines (`Co-Authored-By`, "Generated with ...") to
  commits or pull requests.

## Safety rules

- Never read, print, copy or commit secrets, `.env` files, tokens or keys.
- Never weaken a check to make a test pass: no skipping, loosening or
  deleting a safety, verification, privacy or rollback test. If a test looks
  wrong, say so in the pull request instead.
- Changes that fail must fail closed: when unsure, stop or refuse, never
  report success.
- Do not copy GPL, AGPL, LGPL or SSPL code into this repository.
- Do not name customers, prospects or real people in code, docs, tests or
  pull requests. Use synthetic data only; never real patient or personal data.
- Do not open browser windows or GUI apps on a shared machine; run browser
  tests headless.
- Engine code stays application-agnostic: no code or tests that only work for
  one specific application inside `packages/flow/openadapt_flow`.

## Writing

Write plainly and briefly, in short sentences, for anything a person will
read.
