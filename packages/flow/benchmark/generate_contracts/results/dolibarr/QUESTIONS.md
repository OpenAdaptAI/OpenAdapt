# Questions for a person

1. Is this the app's own bookkeeping, which a write may change without a task asking? Rows the app adds beside each write, expected exactly that many times (a second save adds a second one, which stops the write; list a table under logs_add to allow any number): llx_actioncomm (1 per write), llx_actioncomm_resources (1 per write). Columns that change on their own and are not compared: llx_societe: fk_user_modif, socialnetworks, tms Columns of a new record that differ between demonstrations and are not checked: llx_facture: date_lim_reglement, multicurrency_total_ht, multicurrency_total_ttc, multicurrency_total_tva, ref, total_ht, total_ttc, total_tva; llx_product: price_ttc Answer `bookkeeping: accept`, or list changes under logs_add, logs_remove, columns_add, columns_remove.
   - Answer key: `bookkeeping`. Proposed: `accept`.
   - Answered: `"accept"`

2. How long after a verified write can the app still write on its own (a background job, a delayed retry)? The database is read again that many seconds later and a late change refutes the write. Proposed: 300. Answer `recheck_after_s: <seconds>`.
   - Answer key: `recheck_after_s`. Proposed: `300`.
   - Answered: `300`

3. Kind `create_draft_invoice`: `invoice_date` is in several places: [0] llx_facture.datef (date); [1] llx_facture.date_lim_reglement (date) Answer `locations.create_draft_invoice: {<parameter>: <number>}` to pick one.
   - Answer key: `locations.create_draft_invoice`. Proposed: `None`.
   - Answered: `{"invoice_date": 0}`

## Kinds

- `create_customer` (create, main table `llx_societe`): 64 contract(s).
- `create_product` (create, main table `llx_product`): 4 contract(s).
- `create_draft_invoice` (create, main table `llx_facture`): 10 contract(s).
- `update_customer` (update, main table `llx_societe`): 15 contract(s).
