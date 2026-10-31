# Questions for a person

1. These values were stored as other text with the same digits (a phone number written with spaces stored without): create_contact.phone. Compare them as phone numbers (the same digits, at least 7, and a task value that isn't shaped like a phone number is compared exactly)? Answer `phones: [<kind.parameter>, ...]` with the ones that are phone numbers; any other is compared exactly.
   - Answer key: `phones`. Proposed: `['create_contact.phone']`.
   - Answered: `["create_contact.phone"]`

2. Is this the app's own bookkeeping? Proposed as the app's own bookkeeping, excused only on the rows the demonstrations changed (account: the account row the task's account, account_city name; lead: the lead row the task's lead_company name, the target row): account: stream_updated_at; lead: modified_at, modified_by_id, stream_updated_at. Changed on rows the generator can't identify, so a write that changes them stops: contact: stream_updated_at. Rows the app adds beside each write, expected exactly that many times: action_history_record (1 per write), auth_log_record (1 per write), call_user (1 per write), note (1 per write). Tables that gained a different number of rows in different demonstrations; they are not logs unless you list them under logs_add (rows may then be added, existing rows must not change): note, note_user. Columns of a new record that are not checked (they differ between demonstrations, are free text every demonstration left empty, or are the write's time stamp): account_contact: role; call: created_at, modified_at, modified_by_id, parent_type, uid; contact: address_city, address_country, address_postal_code, address_state, address_street, assigned_user_id, campaign_id, created_at, description, middle_name, modified_at, modified_by_id, salutation_name, stream_updated_at; opportunity: assigned_user_id, campaign_id, contact_id, created_at, description, last_stage, lead_source, modified_at, modified_by_id, probability, stream_updated_at. Answer `bookkeeping: accept`, or a mapping with any of logs_add, columns_add, columns_remove. Unanswered, nothing is excused and no table is a log.
   - Answer key: `bookkeeping`. Proposed: `accept`.
   - Answered: `{"logs_add": ["action_history_record", "auth_log_record"]}`

3. How long after a verified write can the app still write on its own (a background job, a delayed retry)? The database is read again that many seconds later and a late change refutes the write. Proposed: 300. Answer `recheck_after_s: <seconds>`.
   - Answer key: `recheck_after_s`. Proposed: `300`.
   - Answered: `300`

## Kinds

- `create_contact` (create, main table `contact`): 64 contract(s).
- `create_opportunity` (create, main table `opportunity`): 64 contract(s).
- `update_lead` (update, main table `lead`): 43 contract(s).
- `log_call` (create, main table `call`): 64 contract(s).
