# Questions for a person

1. These values were stored as other text with the same digits (a phone number written with spaces stored without): create_customer.phone, update_customer.new_phone. Compare them as phone numbers (the same digits, at least 7, and a task value that isn't shaped like a phone number is compared exactly)? Answer `phones: [<kind.parameter>, ...]` with the ones that are phone numbers; any other is compared exactly.
   - Answer key: `phones`. Proposed: `['create_customer.phone', 'update_customer.new_phone']`.
   - Answered: `["create_customer.phone", "update_customer.new_phone"]`

2. Is this the app's own bookkeeping? Proposed as the app's own bookkeeping, excused only on the rows the demonstrations changed (llx_societe: the target row): llx_societe: fk_user_modif, tms. Also changed with no task value, and not excused unless you add them under columns_add (a write that changes them stops): llx_societe: socialnetworks. Rows the app adds beside each write, expected exactly that many times: llx_actioncomm (1 per write), llx_actioncomm_resources (1 per write). Columns of a new record that are not checked (they differ between demonstrations, are free text every demonstration left empty, or are the write's time stamp): llx_facture: close_code, close_note, date_lim_reglement, datec, extraparams, fk_currency, import_key, increment, last_main_doc, location_incoterms, model_pdf, module_source, multicurrency_total_ht, multicurrency_total_ttc, multicurrency_total_tva, note_private, note_public, payment_reference, pos_source, ref, ref_client, ref_ext, tms, total_ht, total_ttc, total_tva; llx_facturedet: batch, description, import_key, label, multicurrency_total_ht, multicurrency_total_ttc, multicurrency_total_tva, ref_ext, total_ht, total_ttc, total_tva, vat_src_code; llx_product: accountancy_code_buy, accountancy_code_buy_export, accountancy_code_buy_intra, accountancy_code_sell, accountancy_code_sell_export, accountancy_code_sell_intra, barcode, batch_mask, canvas, customcode, datec, default_vat_code, description, duration, import_key, last_main_doc, model_pdf, note, note_public, partnumber, price_label, price_ttc, ref_ext, tms, url; llx_product_price: date_price, default_vat_code, import_key, multicurrency_code, price_label, price_ttc, tms; llx_societe: accountancy_code_buy, accountancy_code_customer_general, accountancy_code_sell, accountancy_code_supplier_general, ape, barcode, canvas, code_client, code_compta, code_compta_fournisseur, code_fournisseur, datec, default_lang, deposit_percent, fax, fk_currency, fk_prospectlevel, georesultcode, idprof4, idprof5, idprof6, import_key, ip, last_main_doc, location_incoterms, logo, logo_squarred, model_pdf, multicurrency_code, name_alias, note_private, note_public, phone_mobile, prefix_comm, ref_ext, siren, siret, supplier_account, tms, tp_payment_reference, tva_intra, url, webservices_key, webservices_url. Answer `bookkeeping: accept`, or a mapping with any of logs_add, columns_add, columns_remove. Unanswered, nothing is excused and no table is a log.
   - Answer key: `bookkeeping`. Proposed: `accept`.
   - Answered: `{"columns_add": {"llx_societe": ["socialnetworks"]}}`

3. How long after a verified write can the app still write on its own (a background job, a delayed retry)? The database is read again that many seconds later and a late change refutes the write. Proposed: 300. Answer `recheck_after_s: <seconds>`.
   - Answer key: `recheck_after_s`. Proposed: `300`.
   - Answered: `300`

4. Kind `create_draft_invoice`: `invoice_date` is in several places: [0] llx_facture.datef (date); [1] llx_facture.date_lim_reglement (date) Answer `locations.create_draft_invoice: {<parameter>: <number>}` to pick one.
   - Answer key: `locations.create_draft_invoice`. Proposed: `None`.
   - Answered: `{"invoice_date": 0}`

## Kinds

- `create_customer` (create, main table `llx_societe`): 64 contract(s).
- `create_product` (create, main table `llx_product`): 4 contract(s).
- `create_draft_invoice` (create, main table `llx_facture`): 10 contract(s).
- `update_customer` (update, main table `llx_societe`): 15 contract(s).
