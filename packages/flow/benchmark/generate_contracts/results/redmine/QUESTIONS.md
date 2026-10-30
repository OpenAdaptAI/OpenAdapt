# Questions for a person

1. Is this the app's own bookkeeping, which a write may change without a task asking? Rows the app adds beside each write, expected exactly that many times (a second save adds a second one, which stops the write; list a table under logs_add to allow any number): custom_values (2 per write). Tables treated as the app's own logs (rows may be added, existing rows must not change; on PostgreSQL they are not read at all): journal_details Columns that change on their own and are not compared: issues: lock_version, updated_on; user_preferences: others Columns of a new record that differ between demonstrations and are not checked: time_entries: tmonth, tweek, tyear Answer `bookkeeping: accept`, or list changes under logs_add, logs_remove, columns_add, columns_remove.
   - Answer key: `bookkeeping`. Proposed: `accept`.
   - Answered: `"accept"`

## Kinds

- `create_issue` (create, main table `issues`): 26 contract(s).
- `update_issue` (update, main table `issues`): 63 contract(s).
- `log_time` (create, main table `time_entries`): 4 contract(s).
- `ensure_issue` (create, main table `issues`): 2 contract(s).
