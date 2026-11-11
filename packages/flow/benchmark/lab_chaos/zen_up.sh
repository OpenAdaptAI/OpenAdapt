#!/usr/bin/env bash
# Start (or restart) the chaos chain on the fixture host, inside WSL. Idempotent.
#   runner -> shim (0.0.0.0:<profile port>) -> Toxiproxy (127.0.0.1:1<port>) -> app (127.0.0.1:<upstream>)
# Usage: copy this directory to an operator-selected path in WSL, then run
#   OPENADAPT_LAB_HOST=fixture.example bash /path/to/lab_chaos/zen_up.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# Validate before downloads, process changes or network requests.
export OPENADAPT_LAB_HOST
OPENADAPT_LAB_HOST=$(PYTHONPATH="$HERE" python3 -c 'from chaos import deployment_host; print(deployment_host())')
STATE="${OPENADAPT_LAB_CHAOS_STATE:-$HOME/lab_chaos}"; mkdir -p "$STATE"
VER=$(python3 -c "import json;print(json.load(open('$HERE/profiles.json'))['toxiproxy_version'])")
BIN="$STATE/toxiproxy-server-$VER"
if [ ! -x "$BIN" ]; then
  curl -fsSL -o "$BIN" "https://github.com/Shopify/toxiproxy/releases/download/v$VER/toxiproxy-server-linux-amd64"
  chmod +x "$BIN"
fi
pkill -f "toxiproxy-server" || true
pkill -f "lab_chaos/shim.py" || true
cp "$HERE/shim.py" "$STATE/shim.py"
nohup "$BIN" -host 127.0.0.1 -port 8474 >"$STATE/toxiproxy.log" 2>&1 &
sleep 1
python3 - "$HERE/profiles.json" "$STATE" <<'PY'
import json, os, subprocess, sys, urllib.request
cfg = json.load(open(sys.argv[1])); state = sys.argv[2]
api = "http://127.0.0.1:8474"
def call(method, path, body=None):
    req = urllib.request.Request(api + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req).read()
host = os.environ["OPENADAPT_LAB_HOST"]
for app_name, app in cfg["apps"].items():
    for prof, port in app["ports"].items():
        p = cfg["profiles"][prof]
        name = f"{app_name}-{prof}"
        call("POST", "/proxies", {"name": name, "listen": f"127.0.0.1:1{port}",
             "upstream": f"127.0.0.1:{app['upstream_port']}", "enabled": True})
        if p["latency_ms"]:
            call("POST", f"/proxies/{name}/toxics", {"name": "latency", "type": "latency",
                 "stream": "upstream", "toxicity": 1.0,
                 "attributes": {"latency": p["latency_ms"], "jitter": p["jitter_ms"]}})
        args = ["python3", f"{state}/shim.py", "--listen", str(port),
                "--upstream", f"127.0.0.1:1{port}", "--public", f"{host}:{port}",
                "--app-origin", f"{host}:{app['upstream_port']}",
                "--p502", str(p["p502"]), "--p504", str(p["p504"]),
                "--stale-s", str(p["stale_s"]), "--seed", str(port),
                "--log", f"{state}/{name}.jsonl"]
        for path in app["list_paths"]:
            args += ["--stale-path", path]
        subprocess.Popen(args, stdout=open(f"{state}/{name}.out", "a"),
                         stderr=subprocess.STDOUT, start_new_session=True)
        print("up", name, port)
PY
