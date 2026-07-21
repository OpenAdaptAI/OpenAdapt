## What & why

<!-- What does this change and why? Link any issue: Closes #123 -->

## Type of change

<!-- Conventional Commit type; the PR title MUST match (feat:/fix:/docs:/…). -->

- [ ] `fix:` bug fix
- [ ] `feat:` new feature
- [ ] `docs:` documentation only
- [ ] `ci:` / `chore:` / `refactor:` / `test:` (no user-facing behavior change)

## Checklist

- [ ] PR title uses Conventional Commit format
- [ ] `ruff check openadapt_flow` and `ruff format --check openadapt_flow` pass
- [ ] `mypy` passes
- [ ] `pytest -q` passes locally
- [ ] Tests added/updated for behavior changes
- [ ] Docs updated (README/DESIGN/docs) if behavior or contracts changed
- [ ] If this touches the identity gate / resolution ladder / halt logic, I
      explained why the never-false-accept invariant still holds

## Notes for reviewers

<!-- Anything that helps review: tradeoffs, follow-ups, out-of-scope items. -->
