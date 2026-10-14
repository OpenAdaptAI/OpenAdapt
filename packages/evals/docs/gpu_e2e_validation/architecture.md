# GPU E2E Architecture: Proxy Chain and Two-Port Setup

This document details the networking architecture connecting the GPU training
VM (AWS) to the WAA evaluation VM (Azure), including the UNIX socket bridge
workaround for Docker port forwarding issues.

## Two-Port Architecture

The WAA VM exposes two independent services. Both must be reachable from the
GPU VM for the RL training loop to function.

```
Port 5000 (host): WAA Flask API (WAALiveAdapter)
  Endpoints:
    POST /screenshot      -> returns current Windows screen (PNG)
    POST /execute_windows -> executes pyautogui action on Windows VM
  Note: Standard Docker port forwarding (-p 5000:5000). Works normally.

Port 5051 (host) -> 5050 (container): evaluate_server.py (WAADesktopEnv)
  Endpoints:
    POST /setup           -> initialize task environment
    POST /evaluate        -> run evaluation after agent actions
  Note: Docker port forwarding for 5050 is broken by QEMU NET_ADMIN.
        Exposed via socat/nsenter UNIX socket bridge as 5051 on the host.
        See "UNIX Socket Bridge" section below.
```

The GPU VM's `WAALiveConfig` holds both URLs:

```python
WAALiveConfig(
    server_url="http://172.173.66.131:5000",       # Flask API (direct Docker port forward)
    evaluate_url="http://172.173.66.131:5051",     # evaluate_server.py (via socat bridge)
)
```

## UNIX Socket Bridge (Docker Port 5050 Workaround)

### Problem

The WAA Docker container runs QEMU with `--cap-add NET_ADMIN` for TAP
networking (bridging the Windows VM network). This breaks Docker's standard
port forwarding for port 5050 (evaluate_server.py inside the container).
Connecting to `localhost:5050` on the VM host returns "Empty reply from
server".

The `docker exec -i ... socat STDIO TCP:localhost:5050` pipe approach also
fails — it produces "Empty reply from server" because the stdio-based pipe
doesn't handle HTTP framing correctly.

### Solution: nsenter + socat

A two-stage socat proxy using a UNIX socket as the bridge:

```
GPU VM                   WAA VM Host              Docker Container
+--------+              +---------------+         +------------------+
| client | --TCP:5051-> | socat (host)  |         | evaluate_server  |
|        |              | UNIX socket   | -sock-> | :5050            |
+--------+              | nsenter+socat |         +------------------+
                        +---------------+
```

#### Stage 1: Inside Docker's network namespace (on WAA VM host)

```bash
# Find the PID of any process in the container's network namespace
CONTAINER_PID=$(docker inspect --format '{{.State.Pid}}' <container_name>)

# Bridge UNIX socket to container port 5050
nsenter -t $CONTAINER_PID -n socat \
    UNIX-LISTEN:/tmp/waa-bridge.sock,fork \
    TCP:localhost:5050
```

`nsenter -t <PID> -n` enters the container's network namespace, so
`TCP:localhost:5050` resolves to the container's loopback — directly reaching
evaluate_server.py without Docker's broken port mapping.

#### Stage 2: On WAA VM host (host network namespace)

```bash
socat TCP-LISTEN:5051,fork,reuseaddr \
    UNIX-CONNECT:/tmp/waa-bridge.sock
```

This makes port 5051 on the VM host forward through the UNIX socket to the
container's port 5050.

#### Client connection (from GPU VM)

```bash
curl http://172.173.66.131:5051/setup
```

Or in Python:

```python
config = WAALiveConfig(
    server_url="http://172.173.66.131:5000",
    evaluate_url="http://172.173.66.131:5051",
)
```

> **Note**: During the initial validation the evaluate port was configured as
> 5001 in some test scripts. The canonical mapping is **5051** (host) ->
> **5050** (container) via the socat bridge described above.

### Recovery After Container Restart

The socat processes and UNIX socket do not survive a container restart. After
restarting the WAA Docker container:

1. Remove stale socket: `rm -f /tmp/waa-bridge.sock`
2. Re-run Stage 1 (nsenter + socat) with the new container PID
3. Re-run Stage 2 (host socat listener)

## SSH Tunnel Setup (Optional)

If the WAA VM is behind a firewall/NSG (typical for Azure), SSH tunnels can
forward ports to localhost on the GPU VM:

```bash
# From GPU VM (or local machine)
ssh -N -L 5000:localhost:5000 -L 5051:localhost:5051 azureuser@172.173.66.131
```

Then use `localhost` URLs in the config:

```python
config = WAALiveConfig(
    server_url="http://localhost:5000",
    evaluate_url="http://localhost:5051",
)
```

> **Tip**: When using SSH tunnels, the socat bridge is still required on the
> WAA VM host — SSH tunnels forward traffic to the host ports, not directly
> into the Docker container.

## Full Data Flow

```
1. RLEnvironment.reset()
   -> WAADesktopEnv.reset()
   -> POST evaluate_url/setup  (host :5051 -> container :5050)
   -> evaluate_server.py initializes task

2. RLEnvironment.step(action)
   -> WAALiveAdapter.execute(action)
   -> POST server_url/execute_windows  (host :5000 -> container :5000)
   -> pyautogui executes action in Windows VM

3. RLEnvironment.observe()
   -> WAALiveAdapter.screenshot()
   -> POST server_url/screenshot  (host :5000 -> container :5000)
   -> returns PNG of current Windows screen

4. RLEnvironment.evaluate()
   -> WAADesktopEnv.evaluate()
   -> POST evaluate_url/evaluate  (host :5051 -> container :5050)
   -> evaluate_server.py checks task completion
   -> returns reward signal
```

## Key Operational Notes

- **Never `az vm restart` for SSH issues** — it kills the QEMU Windows
  session, forcing a 35+ minute cold boot. Retry SSH instead.
- **Port 5050 vs 5051**: evaluate_server.py listens on 5050 _inside_ the
  container. The socat bridge exposes it as 5051 on the host. The canonical
  `evaluate_url` port is **5051**.
- **First WAA boot takes 35+ min** (fresh Windows install). Subsequent
  resumes from deallocated state take ~1 min.
