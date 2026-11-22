# Flight recorder evidence

`real_model_smoke.json` is written by
`tests/e2e/test_flight_real_model.py` when it runs with `ANTHROPIC_API_KEY`
in the environment and `OPENADAPT_FLIGHT_EVIDENCE=1`:

```bash
OPENADAPT_FLIGHT_EVIDENCE=1 pytest packages/flow/tests/e2e/test_flight_real_model.py
```

The test sends one synthetic screen (a rendered intake form with a planted
synthetic name and a Save button) to the real Claude API through the flight
proxy, with the real openadapt-privacy scrubber. It records:

- the run's `flight.json` fields (calls, tokens, priced cost, gate events);
- how many computer actions Claude asked for, and how many were held;
- that the executor performed none of them.

No real patient data is used. The evidence holds no screenshot and no key.

## Recorded run (2026-11-22)

`real_model_smoke.json`: two calls to `claude-opus-5-5` through the proxy,
11,025 input and 217 output tokens, $0.04844 from the price table. Claude
asked for a screenshot, then a `left_click` on Save; the click was held
(`button Save`) and never reached the executor. The one executor call that
ran was a `wait`.

An earlier attempt with a vaguer instruction showed the scrubbing from the
model's side: Claude replied that the field contents were "hidden behind red
boxes" and that part of the message had become a `<PERSON>` placeholder, and
asked what to do instead of clicking.

This run predates the 2026-11-21 changes (every string scrubbed, the
fail-closed gate, the egress block). It has not been repeated since.

## Scrub recall

See [scrub_recall/README.md](scrub_recall/README.md): 97.6 to 98.4% of
fields in text and 98.4% in rendered screenshots on 50 synthetic records,
every miss a name.
