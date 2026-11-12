# Lab network chaos

Shared lab infrastructure for real-app runs under bad networks. Generic: an app
gets proxied ports by adding an entry to `profiles.json`; no app code here.

    runner -> shim (0.0.0.0:<port>, HTTP) -> Toxiproxy 2.12.0 (127.0.0.1:1<port>, TCP) -> app

| Profile | What it does |
|---|---|
| `vpn-typical` | 250 +/- 150 ms latency on every request (100 to 400 ms) |
| `vpn-bad` | 1100 +/- 700 ms latency (400 to 1800 ms), and 1 in 100 requests get 502, 1 in 100 get 504 |
| `replica-lag` | for 3 s after a POST, GETs of the app's `list_paths` get the response served before the POST (a stale search index) |
| `vpn-bad+replica-lag` | both |

Toxiproxy (MIT) adds the latency. It works on TCP, so `shim.py` (stdlib) adds
the HTTP status codes and the stale list. The shim rewrites `Location` headers
and absolute links to its own port, so redirects stay behind the proxy. Each
shim logs what it injected to `~/lab_chaos/<app>-<profile>.jsonl` in WSL.

## Configured ports

| App | upstream | vpn-typical | vpn-bad | replica-lag | vpn-bad+replica-lag |
|---|---|---|---|---|---|
| Dolibarr | 9635 | 9650 | 9651 | 9652 | 9653 |
| OpenEMR | 9640 | 9660 | 9661 | 9662 | 9663 |

## Use it from a test

    export OPENADAPT_LAB_HOST=fixture.example
    OPENADAPT_LAB_CHAOS=vpn-bad python benchmark/<suite>/live.py ...
    pytest -p benchmark.lab_chaos.chaos --chaos vpn-bad tests/<real-app test>

The committed hostname is a reserved placeholder. Requested chaos refuses an
unset, empty or placeholder deployment hostname, including when selected through
the environment. Set the host to the actual fixture host before either command,
and pass app URLs through `benchmark.lab_chaos.chaos.chaos_url(url)`.

## Start or restart (idempotent)

Set `OPENADAPT_LAB_HOST` to the fixture's hostname on the deployment host.
Copy this directory to the operator-selected `LAB_CHAOS_DIR` there, then run
`zen_up.sh` inside WSL. The script validates the hostname before it downloads
or starts any process. `OPENADAPT_LAB_CHAOS_STATE` selects the process/log
state directory (default: `~/lab_chaos`). Restart after a WSL reboot.

Choose ports in `profiles.json` that do not overlap other deployments.
