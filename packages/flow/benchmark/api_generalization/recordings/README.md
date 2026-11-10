# Recorded data

The demonstrations the report depends on, copied from the session scratch
directory so a reboot can't lose them. Synthetic values only; the capture
withholds every header, cookie and session token (`<session:...>`).

- `<workflow>/rec1/`: the recording (`events.jsonl`, `network.jsonl`,
  `meta.json`, `frames/`). The counterfactual copies (`rec1-cf`) are rebuilt
  from these by `counterfactual.py`.
- `<workflow>/mode.json`: the fixture proxy's fault mode when the data was taken.
- `<workflow>/requests.jsonl`: the fixture proxy's request log (method, path,
  status, timing; no bodies or headers).
- `explore-*/requests.jsonl`: the proxy logs from finding each app's save request.
