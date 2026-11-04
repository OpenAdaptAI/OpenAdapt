# Lab network chaos (zen lane)

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

## Ports on 100.111.126.101

| App | upstream | vpn-typical | vpn-bad | replica-lag | vpn-bad+replica-lag |
|---|---|---|---|---|---|
| Dolibarr | 9635 | 9650 | 9651 | 9652 | 9653 |
| OpenEMR | 9640 | 9660 | 9661 | 9662 | 9663 |

## Use it from a test

    OPENADAPT_LAB_CHAOS=vpn-bad python benchmark/<suite>/live.py ...
    pytest -p benchmark.lab_chaos.chaos --chaos vpn-bad tests/<real-app test>

and pass app URLs through `benchmark.lab_chaos.chaos.chaos_url(url)`.

## Start or restart (idempotent)

    cd packages/flow/benchmark/lab_chaos
    tar czf - shim.py profiles.json zen_up.sh | ssh -i ~/.ssh/oalab_ed25519 user@100.111.126.101 \
      "wsl -d Ubuntu-24.04 -- tar xzf - -C /home/oa/lab_chaos_src"
    ssh -i ~/.ssh/oalab_ed25519 user@100.111.126.101 "wsl -d Ubuntu-24.04 -- bash /home/oa/lab_chaos_src/zen_up.sh"

The processes don't restart after a WSL reboot; run `zen_up.sh` again. Ports
9654 to 9656 are taken by another stack on zen; don't use them.
