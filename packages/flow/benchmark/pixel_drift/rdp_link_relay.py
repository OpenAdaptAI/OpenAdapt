"""TCP relay for an RDP session, clean or shaped like a bad VPN link.

The RDP client container cannot reach the lab laptop directly; this relay
listens on the Mac's loopback and forwards to it. With ``--shape`` the
server-to-client direction is limited to ``--kbps``, each chunk is delayed by
a random 0 to ``--jitter-ms`` (order kept), and with probability
``--stall-p`` a chunk is held ``--stall-ms`` longer. TCP retransmits lost
packets, so a lossy link reaches the client as these stalls and as bursts of
late data; this models that at the TCP level, not packet loss on the wire.
The random generator is seeded, so a run's shaping is repeatable::

    python rdp_link_relay.py 13390 HOST 3389 --shape --kbps 1500 --jitter-ms 80
"""

from __future__ import annotations

import argparse
import queue
import random
import socket
import threading
import time


def plain(a: socket.socket, b: socket.socket) -> None:
    try:
        while data := a.recv(65536):
            b.sendall(data)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def shaped(a: socket.socket, b: socket.socket, args: argparse.Namespace) -> None:
    rng = random.Random(args.seed)
    out: queue.Queue = queue.Queue()
    bytes_per_s = args.kbps * 1000 / 8

    def sender() -> None:
        while (item := out.get()) is not None:
            due, data = item
            wait = due - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            try:
                b.sendall(data)
            except OSError:
                return

    threading.Thread(target=sender, daemon=True).start()
    line_free = time.monotonic()
    last_due = 0.0
    try:
        while data := a.recv(4096):
            now = time.monotonic()
            line_free = max(line_free, now) + len(data) / bytes_per_s
            due = line_free + rng.uniform(0, args.jitter_ms) / 1000
            if rng.random() < args.stall_p:
                due += args.stall_ms / 1000
            last_due = max(last_due, due)  # keep order
            out.put((last_due, data))
    except OSError:
        pass
    finally:
        out.put(None)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lport", type=int)
    parser.add_argument("host")
    parser.add_argument("port", type=int)
    parser.add_argument("--shape", action="store_true")
    parser.add_argument("--kbps", type=float, default=1500)
    parser.add_argument("--jitter-ms", type=float, default=80)
    parser.add_argument("--stall-p", type=float, default=0.02)
    parser.add_argument("--stall-ms", type=float, default=400)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", args.lport))
    srv.listen(8)
    while True:
        client, _ = srv.accept()
        upstream = socket.create_connection((args.host, args.port), timeout=10)
        upstream.settimeout(None)
        threading.Thread(target=plain, args=(client, upstream), daemon=True).start()
        if args.shape:
            threading.Thread(
                target=shaped, args=(upstream, client, args), daemon=True
            ).start()
        else:
            threading.Thread(target=plain, args=(upstream, client), daemon=True).start()


if __name__ == "__main__":
    main()
