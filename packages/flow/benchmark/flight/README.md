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
