# Questions for a person

1. Is this the app's own bookkeeping? Proposed as the app's own bookkeeping, excused only on the rows the demonstrations changed (issues: the target row): issues: lock_version, updated_on. Also changed with no task value, and not excused unless you add them under columns_add (a write that changes them stops): user_preferences: others. Rows the app adds beside each write, expected exactly that many times: custom_values (2 per write). Tables that gained a different number of rows in different demonstrations; they are not logs unless you list them under logs_add (rows may then be added, existing rows must not change): journal_details. Columns of a new record that are not checked (they differ between demonstrations, are free text every demonstration left empty, or are the write's time stamp): issues: created_on, description, updated_on; time_entries: created_on, tmonth, tweek, tyear, updated_on. Answer `bookkeeping: accept`, or a mapping with any of logs_add, columns_add, columns_remove. Unanswered, nothing is excused and no table is a log.
   - Answer key: `bookkeeping`. Proposed: `accept`.
   - Answered: `{"logs_add": ["journal_details"], "columns_add": {"user_preferences": ["others"]}}`

2. How long after a verified write can the app still write on its own (a background job, a delayed retry)? The database is read again that many seconds later and a late change refutes the write. Proposed: 300. Answer `recheck_after_s: <seconds>`.
   - Answer key: `recheck_after_s`. Proposed: `300`.
   - Answered: `300`

## Kinds

- `create_issue` (create, main table `issues`): 26 contract(s).
- `update_issue` (update, main table `issues`): 63 contract(s).
- `log_time` (create, main table `time_entries`): 4 contract(s).
- `ensure_issue` (create, main table `issues`): 2 contract(s).
