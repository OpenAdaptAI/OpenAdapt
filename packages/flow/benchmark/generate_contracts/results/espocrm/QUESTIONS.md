# Questions for a person

1. Is this the app's own bookkeeping, which a write may change without a task asking? Rows the app adds beside each write, expected exactly that many times (a second save adds a second one, which stops the write; list a table under logs_add to allow any number): call_user (1 per write), note (1 per write). Tables treated as the app's own logs (rows may be added, existing rows must not change): action_history_record, auth_log_record, note, note_user Columns that change on their own and are not compared: account: stream_updated_at; contact: stream_updated_at; lead: modified_at, modified_by_id, stream_updated_at Columns of a new record that differ between demonstrations and are not checked: call: parent_type, uid; opportunity: last_stage, probability Answer `bookkeeping: accept`, or list changes under logs_add, logs_remove, columns_add, columns_remove.
   - Answer key: `bookkeeping`. Proposed: `accept`.
   - Answered: `"accept"`

2. How long after a verified write can the app still write on its own (a background job, a delayed retry)? The database is read again that many seconds later and a late change refutes the write. Proposed: 300. Answer `recheck_after_s: <seconds>`.
   - Answer key: `recheck_after_s`. Proposed: `300`.
   - Answered: `300`

## Kinds

- `create_contact` (create, main table `contact`): 64 contract(s).
- `create_opportunity` (create, main table `opportunity`): 64 contract(s).
- `update_lead` (update, main table `lead`): 43 contract(s).
- `log_call` (create, main table `call`): 64 contract(s).
