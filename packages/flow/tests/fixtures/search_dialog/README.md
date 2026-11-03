Recordings of a small search-before-save page (`app.html`), made with the real
recorder in headless Chromium by `record.py` (2026-11-03). Each one types a
name, clicks "Create record", which opens a dialog named "Dublettensuche", and
clicks the dialog's "Confirm". What the dialog says is set by the `s` query
parameter:

- `de_marsh`, `de_weber`, `de_marsh_again`: "Keine Treffer gefunden." (the
  last one repeats the first one's name);
- `archive_marsh`, `archive_weber`: "Keine Treffer im Archiv Nord" or "West",
  chosen by the typed name (a line that changes with the data);
- `names_marsh`: "Keine Treffer für Elena Marsh" (the typed value);
- `en_weber`: "No results found" (the English vocabulary);
- `question_marsh`: "Wirklich speichern?" (a question, shown whatever the
  search found).

The buttons are in English because the confirmation pair is found from the
Confirm label. The page URL is replaced with `http://records.test/new` in the
events. The names are made up. Used by
`tests/test_empty_result_guard_evidence.py`.
