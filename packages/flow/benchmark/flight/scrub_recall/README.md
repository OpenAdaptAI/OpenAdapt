# Flight scrubber recall on a synthetic PHI corpus

`corpus.py` makes synthetic patient records (seeded; every value is made
up). Each record has five fields: name, date of birth, record number, phone
and street address. Each record is written once as text (one of four
templates: labelled form text, a sentence, JSON, a URL with the record
number and name) and once as a rendered form screenshot.

`measure.py` runs two configurations over the same corpus:

- `presidio`: the openadapt-privacy scrubber alone, which is what the flight
  proxy used before 2026-11-21;
- `flight`: what the proxy sends now (`openadapt_flow.flight.scrub`).

Each text is measured three ways: sent as a tool result (`text`; a JSON text
there is scrubbed value by value), as plain message text (`text_message`; a
JSON text is then one string), and as plain message text after a 600-character
agent preamble (`text_long`, `LONG_PREAMBLE` in `corpus.py`), the way a
computer-use loop sends its task.

A field leaks when its value, lower-cased with everything but letters and
digits removed, is a substring of the output treated the same way. A name
also leaks when either part appears as a word. For images the output is
tesseract's reading of the scrubbed image; only fields tesseract reads in
the original image count.

```bash
python measure.py --seed 7 --count 50 --out results/2026-11-22-seed7.json
```

## Result (2026-11-22, seed 7, 50 records, 250 fields per path)

`results/2026-11-22-seed7.json`. The flight scrubber here replaces values
with run tokens (`[PERSON_1]`), and each measurement uses a fresh token map,
so a name found in one text doesn't help find it in another.

| | Tool result | Message text | Long message | Image |
|---|---|---|---|---|
| Presidio alone | 196/250 (78.4%) | 196/250 (78.4%) | 193/250 (77.2%) | 168/249 (67.5%) |
| Flight scrubber | 245/250 (98.0%) | 244/250 (97.6%) | 244/250 (97.6%) | 242/249 (97.2%) |

Every miss of the flight scrubber is a name: 5, 6 and 6 of 50 in the three
text paths, 7 of 50 in images. Dates, record numbers, phones and addresses:
no misses. One phone in one image was unreadable to tesseract before
scrubbing and is not counted.

## Over-redaction (precision)

The first 20 records are also drawn on a work-list screenshot (app name,
four navigation items, a "Referral Tasks" heading, column headers, the
patient's row with a referral reason, a priority and an Open button, and
the record number), and written into a task instruction ("Open the referral
for <name> (MRN <number>) and add a triage note: <reason>. Then go back to
Referral Tasks."). Counted: the non-personal words tesseract reads on the
original screen, and how many it still reads after scrubbing; the
instruction's non-personal words, and how many are kept.

| | Screen words kept | Instruction words kept | Screen PHI hidden | Instruction PHI hidden |
|---|---|---|---|---|
| Presidio alone | 103/333 (30.9%) | 334/375 (89.1%) | 21/40 | 32/40 |
| Flight scrubber | 310/333 (93.1%) | 354/375 (94.4%) | 38/40 | 37/40 |

The flight scrubber loses the app name "MockMed" on all 20 screens
(Presidio reads it as a person) and "Knee" on 3. Presidio's image
redaction, as the proxy used it first, blacks out the navigation, the
heading and every column header.

## History

- 2026-11-21, first run: tool result and image only, with bare
  placeholders: 98.4% and 98.4%.
- 2026-11-21, after a readiness smoke run showed Presidio missing a name in
  a long prompt: NER also on each line, sentence and quoted string; message
  and long-message paths added (98.4 / 98.0 / 97.6%; image 98.4%). The
  order (whole string first) was chosen after seeing seed 7.
- 2026-11-22, run tokens and less over-redaction: ORGANIZATION and NRP hits
  are no longer redacted unless NER asked about the words alone names an
  identifier, and screenshots are no longer passed through Presidio's
  image redaction (each OCR cell goes through the text detection instead).
  Recall fell by 1 to 3 fields per path; words kept on screen rose from
  30.9% to 93.1%. The re-check of organisation hits was added after seeing
  seed 7's instruction misses (it hid 2 more names there and changed no
  corpus number).

What this does and doesn't show:

- The corpus and the patterns were written by the same author, so this is
  not an independent test. The patterns were written before seed 7 was run;
  seed 1 (10 records) was used while building them. Nothing was changed
  after the seed 7 run.
- The screenshots are clean black-on-white forms. Real screens (small
  fonts, colour, anti-aliasing, dense tables) will read worse, both for the
  OCR pass and for tesseract in the leak check.
- Names are the weak point: an unusual name the spaCy model doesn't tag,
  with no label next to it, gets through.
- Faces, handwriting and text tesseract can't read are not covered.
- Task success with scrubbing on has not been measured. With run tokens the
  agent can match a name between its task and the screen and type it, but
  it can't read the name itself.
