# Flight scrubber recall on a synthetic PHI corpus

`corpus.py` makes synthetic patient records (seeded; every value is made
up). Each record has five fields: name, date of birth, record number, phone
and street address. Each record is written once as text (one of four
templates: labelled form text, a sentence, JSON, a URL with the record
number and name) and once as a rendered form screenshot.

`measure.py` runs two configurations over the same corpus:

- `presidio`: the openadapt-privacy scrubber alone, which is what the flight
  proxy used before 2026-11-21;
- `flight`: what the proxy sends now (`openadapt_flow.flight.scrub`), with
  each text sent as a tool result. A JSON text there is scrubbed value by
  value; the same JSON sent as plain message text is scrubbed as one string,
  which is weaker and was not measured.

A field leaks when its value, lower-cased with everything but letters and
digits removed, is a substring of the output treated the same way. A name
also leaks when either part appears as a word. For images the output is
tesseract's reading of the scrubbed image; only fields tesseract reads in
the original image count.

```bash
python measure.py --seed 7 --count 50 --out results/2026-11-21-seed7.json
```

## Result (2026-11-21, seed 7, 50 records, 250 fields per medium)

| | Text recall | Image recall |
|---|---|---|
| Presidio alone | 196/250 (78.4%) | 168/249 (67.5%) |
| Flight scrubber | 246/250 (98.4%) | 245/249 (98.4%) |

Every miss of the flight scrubber is a name: 4 of 50 in text, 4 of 50 in
images. Dates, record numbers, phones and addresses: no misses. One phone
in one image was unreadable to tesseract before scrubbing and is not
counted.

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
