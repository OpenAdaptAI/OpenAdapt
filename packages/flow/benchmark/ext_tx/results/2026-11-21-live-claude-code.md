# Live Claude Code session through openadapt proxy (2026-11-21)

Claude Code 2.1.305, `claude -p --model sonnet`, headless on the Mac, with `--allowedTools` limited to the proxy's MCP tools. A synthetic SQLite file of 200 users, 8 of them admins. The command was `python -m openadapt_flow proxy --sqlite app.db --flag users.role=admin -- claude -p ...`, run twice. Both runs used the same default task (one per database and folder), so the second proxy run kept the first run's plan.

1. **Session 1** previewed `UPDATE users SET active = 0 WHERE id <= 60`. The diff showed 60 rows changed, 2 of them admins, which is over the auto-approve limit of 0, so the plan was held. `sql_apply` was refused (`not_approved`), and the database was unchanged.
2. **Approval:** `openadapt-flow plan approve <plan> --automation`. Scripted, so the receipt says agent.
3. **Session 2** applied the plan (`VERIFIED`) and counted 60 inactive users, then undid it (`ROLLED_BACK`, 0 collateral) and counted 0.

Four receipts: plan, the refused apply, the apply and the undo. All four were signed and verified with `verify_run_receipt`.

## Session 1 (agent final message)
```
The update was not applied. `sql_apply` refused the plan because no person has approved it.

**sql_preview** (`UPDATE users SET active = 0 WHERE id <= 60`) returned:
- **Plan id:** `dcb0dd063377d1b1`
- **Diff:** `users` has 0 rows added, 60 changed and 0 removed.
- **Anomalies:**
  - 60 rows is more than the auto-approve limit.
  - Some of the affected rows have `users.role = admin` (`users.role = 2`).
- **Approval:** waiting for a person. They can press `y` in the proxy terminal or run `openadapt plan approve dcb0dd063377d1b1`.

**sql_apply** (`dcb0dd063377d1b1`) returned `{"refused": "not_approved", "detail": "dcb0dd063377"}`.

I didn't try to get around the approval step. The plan includes admin accounts, so you may want to check it before approving. After someone approves it, I can call `sql_apply` again.
```
## Session 2 (agent final message)
```
All four calls are done, so there's nothing left to continue.

1. **sql_apply:** The outcome was VERIFIED. It made 1 write, which was confirmed, with 0 refuted and 0 missed effects.
2. **sql_query:** 60 users had `active = 0` after the apply.
3. **sql_undo:** The outcome was ROLLED_BACK. The undo was verified, with 0 collateral changes.
4. **sql_query:** 0 users had `active = 0` after the undo.
```
