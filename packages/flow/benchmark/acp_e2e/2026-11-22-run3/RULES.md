# Rules for every builder (verbatim from the founder's brief, plus workspace rules)

- Work only in your PR's existing worktree under /Users/abrichr/oa/worktrees/. Do not create new worktrees, venvs or `npm ci` installs (disk is ~6 GiB above the 30 GiB floor). Check `df -h /System/Volumes/Data` before long runs; stop if under 30 GiB free.
- Merge, never rebase or force-push. Never merge a PR. No AI attribution: no Co-Authored-By, no "Generated with Claude Code" in commits, PR bodies or comments.
- Never run `git reset --hard`, `git checkout -- .`, `git stash` or `git worktree remove --force` in /Users/abrichr/oa/src (shared tree).
- Raise no budgets (size ratchets, line budgets, import-time budgets, install budgets). Put new code in new modules; keep edits to existing large files (`__main__.py` etc.) minimal.
- Run tests single-process while load (`uptime`) is under 15, or through `~/oa/scratch/cpu-slots/run.sh <cmd>` (3 slots). Never `pytest -n` above 2. Run only the suites you touch plus `tests/test_cli_tasks.py` and `tests/test_capabilities_map.py` when you change verbs or CAPABILITIES.
- Python: reuse an existing venv, e.g. `/Users/abrichr/oa/worktrees/wrap-publish/packages/flow/.venv/bin/python`, with `PYTHONPATH=<worktree>/packages/flow:<worktree>/packages/types:<worktree>/packages/privacy:<worktree>` (spaCy `en_core_web_sm` and `/opt/homebrew/bin/tesseract` are installed). Headless browsers only; never a headed browser or GUI window on this Mac. Never `pkill` Chrome or chrome-headless-shell; close only browsers you started.
- No cloud: AWS is on hold. No model API calls (the coordinator runs the one real Claude run). No GitHub writes except pushing your own branch (no comments, reviews, labels or PR edits; the coordinator posts declarations and reviews).
- Keep scratch files under /private/tmp/claude-501/-Users-abrichr--claude-spares-spare-3ZL3Kk/c1e84ad4-bba0-4240-8351-7df2ad75b5f5/scratchpad/acp/<your-piece>/. Commit any evidence you want kept into the repo.
- Synthetic data only. Never print or log API keys.
- Public copy (website, README marketing) is out of scope. Docs in the PR (CAPABILITIES row, the piece's own docs) must stay true: if a claim is not proven, narrow the claim.
- Writing in commits and docs follows /Users/abrichr/oa/src/WRITING_GUIDE.md (plain, specific, no AI tells). Conventional commit subjects.
- Fail closed: when unsure whether something is safe, refuse and say why.
- Every fix gets a test that fails on the previous code. Every join gets a test in the consumer PR that uses a committed copy of the producer's real artifact (copy it from /Users/abrichr/oa/worktrees/acp-demo-out or from the producer worktree).
- Read the join contracts in /private/tmp/claude-501/-Users-abrichr--claude-spares-spare-3ZL3Kk/c1e84ad4-bba0-4240-8351-7df2ad75b5f5/scratchpad/acp/CONTRACTS.md and follow them exactly. If a contract is wrong or impossible, stop and say so in your report instead of inventing a new shape.
- Skeptic and end-to-end findings: /Users/abrichr/.claude/projects/-Users-abrichr-oa-src-OpenAdapt/c1e84ad4-bba0-4240-8351-7df2ad75b5f5/subagents/workflows/wf_591a3fe1-484/journal.jsonl, the last two lines (`python3 -c "import json;L=open(P).readlines();print(json.loads(L[36])['result']);print(json.loads(L[37])['result'])"`).
- Push your branch when done (`git push origin HEAD:<branch>`). Before pushing, merge fresh `origin/main` if the branch is behind or conflicting (merge, not rebase) and fix conflicts.
- Final report: head SHA, what changed per finding, tests run with counts, what you could not fix and why. Keep it short.
