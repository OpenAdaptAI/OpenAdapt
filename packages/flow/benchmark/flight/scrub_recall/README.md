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
python measure.py --seed 7 --count 50 --out results/2026-11-21-seed7.json
```

## Result (2026-11-21, seed 7, 50 records, 250 fields per path)

| | Tool result | Message text | Long message | Image |
|---|---|---|---|---|
| Presidio alone | 196/250 (78.4%) | 196/250 (78.4%) | 193/250 (77.2%) | 168/249 (67.5%) |
| Flight scrubber | 246/250 (98.4%) | 245/250 (98.0%) | 244/250 (97.6%) | 245/249 (98.4%) |

Every miss of the flight scrubber is a name: 4, 5 and 6 of 50 in the three
text paths, 4 of 50 in images. Dates, record numbers, phones and addresses:
no misses. One phone in one image was unreadable to tesseract before
scrubbing and is not counted. A name in a JSON text sent as message text
can be missed when JSON escapes it (`Gr\u00fcnewald`).

History: the first measurement had only the tool-result and image paths
(98.4% each). A readiness smoke run then showed Presidio missing a name in
a long prompt that it found in the same sentence alone, so the scrubber now
also runs NER on each line, sentence and quoted string. The long-message and
message-text paths were added with that change. The tool-result number fell
to 97.6% with the first version of the change (pieces before the whole
string); running the whole string first brought it back to 98.4%. That
ordering was chosen after seeing seed 7.

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
- It measures what is hidden, not what the agent can still do. The agent
  can't read a patient name it must check; task success with scrubbing on
  has not been measured.
