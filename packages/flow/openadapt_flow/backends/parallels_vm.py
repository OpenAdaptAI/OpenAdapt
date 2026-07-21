"""Programmatic control of a Parallels Desktop VM (Apple-Silicon host).

The Phase-2 desktop benchmark runs against a local Parallels Windows 11 VM
(native Apple-Silicon virtualization — the WAA/QEMU nested-virt stack cannot
run on M-series, so Parallels replaces it). This module is the fully
programmatic control plane the pipeline drives; there are no manual GUI steps.

Everything is built on ``prlctl``:

* lifecycle: ``status`` / ``start`` / ``stop`` / ``suspend`` / ``resume``
* reversibility: ``snapshot`` / ``list_snapshots`` / ``revert``; qualification
  harnesses may delete only the exact ephemeral snapshot id they just created
  after a verified switch back to a preserved base
* in-guest execution: ``exec`` runs as ``NT AUTHORITY\\SYSTEM`` in session 0
* host-side screen capture: ``capture`` (``prlctl capture`` — bypasses the
  session-0 desktop-isolation that blocks an in-guest ``BitBlt``)
* file transfer: ``push_file`` (``prlctl exec`` hangs on long arguments, so
  files move over a short-lived host HTTP server + in-guest ``curl``)
* the WAA HTTP shim: ``launch_shim`` places ``waa_shim.py`` in the interactive
  console session (session 1) via ``session1_launch.py`` +
  ``CreateProcessAsUser`` — session 0 cannot screenshot or drive the desktop.

Importing this module has no side effects and needs no live VM, so the CI
mock tests import it freely; every real operation shells out lazily.
"""

from __future__ import annotations

import functools
import http.server
import os
import re
import shutil
import socketserver
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover - typing only
    from openadapt_flow.backends.windows_backend import WindowsBackend

# The user's existing VM (see docs/desktop/PHASE2.md). Overridable per call.
DEFAULT_VM_UUID = "{d4f9c29a-52e1-4793-9334-7e971c3d0ab3}"
DEFAULT_PRLCTL = "/usr/local/bin/prlctl"
MIN_QUALIFICATION_HOST_FREE_BYTES = 16 * 1024**3
SHIM_PORT = 5000
_SCRIPT_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "desktop")
# In-guest install locations (forward slashes: Python + curl accept them and
# they survive the host shell without backslash mangling).
GUEST_DIR = "C:/oa"
GUEST_PY = r"C:\Program Files\Python312-arm64\python.exe"


@dataclass
class SnapshotInfo:
    """One entry from ``prlctl snapshot-list``."""

    snapshot_id: str
    current: bool
    name: str = ""


@dataclass(frozen=True)
class AgentEndpoint:
    """A launched ``win_agent`` endpoint, wired for an encrypted+pinned client.

    Returned by :meth:`ParallelsVM.launch_agent`. It carries everything the
    client needs to talk to the agent **end to end secure with no manual step**:
    the base ``url`` (``https://`` when TLS was auto-provisioned), the per-run
    bearer ``token`` (independent authorization factor), and the ``pin_fingerprint``
    of the per-run self-signed cert the control plane minted and provisioned into
    the guest. Hand it to :meth:`backend` (or splat the fields into
    :class:`~openadapt_flow.backends.windows_backend.WindowsBackend`) and the
    channel is encrypted + fingerprint-pinned, fail-closed.

    Args:
        url: Agent base URL. ``https://<guest-ip>:<port>`` for the default secure
            launch; ``http://<guest-ip>:<port>`` only for the ``tls=False``
            loopback/dev escape.
        token: Per-run bearer token (None when the launch was tokenless).
        pin_fingerprint: SHA-256 fingerprint of the agent's per-run certificate
            the client pins. None only for the plaintext ``tls=False`` escape.
        require_tls: Value to pass ``WindowsBackend(require_tls=...)`` — True for
            the secure default (fail closed on any plaintext downgrade), False
            for the explicit dev escape.
    """

    url: str
    token: Optional[str] = None
    pin_fingerprint: Optional[str] = None
    require_tls: bool = True

    def backend(self, **kwargs: object) -> "WindowsBackend":
        """Construct a :class:`WindowsBackend` wired to this endpoint.

        The client is encrypted (HTTPS) and **pinned** to the per-run cert, and
        carries the bearer token — the full end-to-end secure channel with no
        manual provisioning. Extra ``kwargs`` (viewport, timeouts, an injected
        ``session``) pass straight through.
        """
        from openadapt_flow.backends.windows_backend import WindowsBackend

        return WindowsBackend(
            server_url=self.url,
            auth_token=self.token,
            pin_fingerprint=self.pin_fingerprint,
            require_tls=self.require_tls,
            **kwargs,  # type: ignore[arg-type]
        )


class ParallelsError(RuntimeError):
    """A ``prlctl`` invocation failed."""


class ParallelsVM:
    """Thin, fully-programmatic wrapper over ``prlctl`` for one VM.

    Args:
        uuid: VM UUID (braces included) or name.
        prlctl: Path to the ``prlctl`` binary.
        python_guest: In-guest Python interpreter path.
    """

    def __init__(
        self,
        uuid: str = DEFAULT_VM_UUID,
        *,
        prlctl: str = DEFAULT_PRLCTL,
        python_guest: str = GUEST_PY,
    ) -> None:
        self.uuid = uuid
        self.prlctl = prlctl
        self.python_guest = python_guest

    # -- low-level -----------------------------------------------------------

    def _run(
        self, args: list[str], *, timeout: float = 120.0, check: bool = True
    ) -> subprocess.CompletedProcess:
        """Invoke ``prlctl <args>`` and return the completed process.

        Raises:
            ParallelsError: If ``check`` and the return code is nonzero.
        """
        proc = subprocess.run(
            [self.prlctl, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if check and proc.returncode != 0:
            raise ParallelsError(
                f"prlctl {' '.join(args)} -> rc={proc.returncode}: "
                f"{proc.stderr.strip() or proc.stdout.strip()}"
            )
        return proc

    # -- lifecycle -----------------------------------------------------------

    def status(self) -> str:
        """Return the VM power state (running/paused/suspended/stopped)."""
        proc = self._run(["list", "--all", "-o", "status,uuid"], check=False)
        for line in proc.stdout.splitlines():
            if self.uuid in line:
                return line.split()[0].strip()
        # Fall back to name match.
        proc = self._run(["list", "--all"], check=False)
        for line in proc.stdout.splitlines():
            if self.uuid in line:
                return line.split()[1].strip()
        return "unknown"

    def start(self) -> None:
        self._run(["start", self.uuid])

    def stop(self, *, force: bool = False) -> None:
        args = ["stop", self.uuid]
        if force:
            args.append("--kill")
        self._run(args)

    def suspend(self) -> None:
        """Suspend the VM (saves state; leaves nothing running)."""
        self._run(["suspend", self.uuid])

    def resume(self) -> None:
        self._run(["resume", self.uuid], check=False)

    def set_pause_idle(self, on: bool) -> None:
        """Toggle Parallels' pause-when-idle (must be OFF for headless runs)."""
        self._run(
            ["set", self.uuid, "--pause-idle", "on" if on else "off"], check=False
        )

    def ensure_running(self, *, settle_s: float = 6.0) -> None:
        """Bring the VM to a running state from any state, idempotently.

        Handles Parallels' auto-pause (``paused`` -> ``resume``) and disables
        pause-idle so a headless benchmark run is not silently frozen.
        """
        state = self.status()
        if state in ("suspended", "paused"):
            self.resume()
        elif state in ("stopped", "unknown"):
            self.start()
        self.set_pause_idle(False)
        # A resume can be immediately re-paused; force it running.
        for _ in range(3):
            if self.status() == "running":
                break
            self.resume()
            time.sleep(1)
        time.sleep(settle_s)

    def require_host_free_space(
        self,
        *,
        minimum_bytes: int = MIN_QUALIFICATION_HOST_FREE_BYTES,
        storage_path: str | os.PathLike[str] = ".",
    ) -> int:
        """Fail before VM mutation when its host volume lacks safe headroom.

        Snapshot creation can consume many GiB and Parallels may fail a later
        switch or suspend when the host volume fills. Qualification callers
        pass a path on the VM bundle's volume (the workspace and VM share one
        volume in the supported local harness). The free-byte count is returned
        for evidence; no VM command runs on refusal.
        """
        if minimum_bytes <= 0:
            raise ValueError("minimum_bytes must be positive")
        try:
            free_bytes = shutil.disk_usage(storage_path).free
        except OSError as exc:
            raise ParallelsError(
                f"cannot inspect host free space at {os.fspath(storage_path)!r}"
            ) from exc
        if free_bytes < minimum_bytes:
            gib = 1024**3
            raise ParallelsError(
                "insufficient host free space for a snapshot-safe qualification: "
                f"{free_bytes / gib:.1f} GiB available, "
                f"{minimum_bytes / gib:.1f} GiB required"
            )
        return free_bytes

    # -- snapshots (reversibility) ------------------------------------------

    def snapshot(self, name: str, description: str = "") -> str:
        """Create a snapshot and return its id.

        Snapshots are the drift-reset and warm-boot mechanism: every run
        reverts to a clean ``opendental-ready`` state in seconds.
        """
        args = ["snapshot", self.uuid, "-n", name]
        if description:
            args += ["-d", description]
        proc = self._run(args)
        m = re.search(r"\{[0-9a-fA-F-]+\}", proc.stdout)
        if not m:
            raise ParallelsError(f"could not parse snapshot id: {proc.stdout}")
        return m.group(0)

    def list_snapshots(self) -> list[SnapshotInfo]:
        """Return all snapshots (``*`` marks the current one)."""
        proc = self._run(["snapshot-list", self.uuid], check=False)
        out: list[SnapshotInfo] = []
        for line in proc.stdout.splitlines():
            # Columns are PARENT_SNAPSHOT_ID then SNAPSHOT_ID; the current
            # snapshot is marked ``*`` before its SNAPSHOT_ID (the LAST id on
            # the line). Take the last match so the parent id is not mistaken
            # for the snapshot id.
            matches = re.findall(r"(\*?)(\{[0-9a-fA-F-]+\})", line)
            if matches:
                star, sid = matches[-1]
                out.append(SnapshotInfo(snapshot_id=sid, current=bool(star)))
        return out

    def revert(self, snapshot_id: str) -> None:
        """Revert to a snapshot (the per-run clean-state reset)."""
        self._run(["snapshot-switch", self.uuid, "-i", snapshot_id])

    def delete_owned_snapshot(self, snapshot_id: str) -> None:
        """Delete one exact harness-owned snapshot, never its children.

        This primitive is intentionally singular and has no ``children`` or
        name-based mode. Callers must retain the id returned by :meth:`snapshot`
        and must first switch to their preserved base with
        :meth:`restore_base_and_delete_owned_snapshot`.
        """
        snapshot_pattern = (
            r"\{[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\}"
        )
        if re.fullmatch(snapshot_pattern, snapshot_id) is None:
            raise ValueError("snapshot_id must be one exact braced UUID")
        self._run(["snapshot-delete", self.uuid, "-i", snapshot_id])

    def restore_base_and_delete_owned_snapshot(
        self,
        *,
        base_snapshot_id: str,
        owned_snapshot_id: str,
    ) -> None:
        """Switch to a preserved base, then delete one trial snapshot safely.

        The deletion is attempted only after ``snapshot-list`` proves the base
        is current. A post-check proves the owned id is gone and the base is
        still current. Any failure stops immediately and leaves the evidence
        trial rejected.
        """
        if base_snapshot_id == owned_snapshot_id:
            raise ValueError("refusing to delete the preserved base snapshot")
        before = self.list_snapshots()
        before_ids = {item.snapshot_id for item in before}
        if base_snapshot_id not in before_ids:
            raise ParallelsError("preserved base snapshot is missing")
        if owned_snapshot_id not in before_ids:
            raise ParallelsError("owned trial snapshot is missing")
        self.revert(base_snapshot_id)
        switched = self.list_snapshots()
        if not any(
            item.snapshot_id == base_snapshot_id and item.current for item in switched
        ):
            raise ParallelsError("preserved base did not become current")
        self.delete_owned_snapshot(owned_snapshot_id)
        after = self.list_snapshots()
        if any(item.snapshot_id == owned_snapshot_id for item in after):
            raise ParallelsError("owned trial snapshot still exists after deletion")
        if not any(
            item.snapshot_id == base_snapshot_id and item.current for item in after
        ):
            raise ParallelsError("preserved base stopped being current after deletion")

    # -- in-guest execution --------------------------------------------------

    def exec(
        self, args: list[str], *, timeout: float = 120.0, check: bool = False
    ) -> subprocess.CompletedProcess:
        """Run a program in-guest as SYSTEM (``prlctl exec``).

        NOTE: ``prlctl exec`` hangs on very long single arguments — keep
        commands short and move file payloads with :meth:`push_file`.
        """
        return self._run(["exec", self.uuid, *args], timeout=timeout, check=check)

    def exec_cmd(
        self, cmdline: str, *, timeout: float = 120.0
    ) -> subprocess.CompletedProcess:
        """Run ``cmd /c <cmdline>`` in-guest (quoting preserved by cmd)."""
        return self.exec(["cmd", "/c", cmdline], timeout=timeout)

    def exec_ps(
        self, script: str, *, timeout: float = 120.0
    ) -> subprocess.CompletedProcess:
        """Run a short PowerShell command in-guest."""
        return self.exec(
            ["powershell", "-NoProfile", "-Command", script], timeout=timeout
        )

    # -- host-side capture ---------------------------------------------------

    def capture(self, local_path: str) -> str:
        """Capture the VM screen host-side to ``local_path`` (PNG).

        Independent of in-guest state — the ground-truth screenshot even when
        the shim is down.
        """
        self._run(["capture", self.uuid, "--file", local_path])
        return local_path

    # -- networking ----------------------------------------------------------

    def guest_ip(self) -> str:
        """Return the guest's shared-network IPv4 (skips APIPA 169.254)."""
        proc = self.exec_cmd("ipconfig")
        addrs = re.findall(r"IPv4[^\n:]*:\s*([0-9.]+)", proc.stdout)
        for a in addrs:
            if not a.startswith("169.254"):
                return a
        if addrs:
            return addrs[0]
        raise ParallelsError("no guest IPv4 found")

    def host_ip(self, guest_ip: Optional[str] = None) -> str:
        """Return the Mac's IP on the guest's subnet (for host->guest HTTP)."""
        guest_ip = guest_ip or self.guest_ip()
        prefix = guest_ip.rsplit(".", 1)[0] + "."
        out = subprocess.run(["ifconfig"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("inet "):
                addr = line.split()[1]
                if addr.startswith(prefix):
                    return addr
        return prefix + "2"  # Parallels host is conventionally .2

    # -- file transfer -------------------------------------------------------

    def push_file(
        self,
        local_path: str,
        guest_path: str,
        *,
        host_ip: Optional[str] = None,
        port: int = 0,
    ) -> None:
        """Copy a host file into the guest over a short-lived HTTP server.

        ``prlctl exec`` hangs on long arguments, ruling out base64-in-argv;
        an in-guest ``curl`` from a throwaway host server is robust and fast.
        Port 0 (default) binds an ephemeral port so concurrent pushes never
        collide.
        """
        host_ip = host_ip or self.host_ip()
        directory = os.path.dirname(os.path.abspath(local_path))
        name = os.path.basename(local_path)
        handler = functools.partial(
            http.server.SimpleHTTPRequestHandler, directory=directory
        )
        httpd = socketserver.TCPServer((host_ip, port), handler)
        bound_port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://{host_ip}:{bound_port}/{name}"
            proc = self.exec_cmd(f"curl -s -o {guest_path} {url}")
            if proc.returncode != 0:
                raise ParallelsError(f"push_file curl failed: {proc.stderr}")
        finally:
            httpd.shutdown()
            httpd.server_close()

    # -- the WAA shim (session 1) -------------------------------------------

    def _shim_paths(self) -> tuple[str, str]:
        shim = os.path.abspath(os.path.join(_SCRIPT_DIR, "waa_shim.py"))
        launcher = os.path.abspath(os.path.join(_SCRIPT_DIR, "session1_launch.py"))
        return shim, launcher

    def kill_shim(self) -> None:
        """Kill any in-guest Python (frees the shim port)."""
        self.exec_cmd("taskkill /F /IM python.exe /IM pythonw.exe 2>nul & echo done")

    def launch_shim(
        self,
        *,
        port: int = SHIM_PORT,
        host_ip: Optional[str] = None,
        wait_s: float = 20.0,
    ) -> str:
        """Deploy + start the WAA shim in session 1; return its URL.

        Steps (all programmatic): ensure guest dir, push shim + launcher,
        open the firewall, kill stale Python, run the session-1 launcher as
        SYSTEM, then poll until the shim answers a real PNG.
        """
        host_ip = host_ip or self.host_ip()
        shim, launcher = self._shim_paths()
        self.exec_cmd(f"if not exist {GUEST_DIR} mkdir {GUEST_DIR}")
        self.push_file(shim, f"{GUEST_DIR}/waa_shim.py", host_ip=host_ip)
        self.push_file(launcher, f"{GUEST_DIR}/session1_launch.py", host_ip=host_ip)
        self.exec(
            [
                "netsh",
                "advfirewall",
                "firewall",
                "add",
                "rule",
                "name=OAShim",
                "dir=in",
                "action=allow",
                "protocol=TCP",
                f"localport={port}",
            ]
        )
        self.kill_shim()
        time.sleep(2)
        # Run the launcher as SYSTEM; it CreateProcessAsUser's the shim into
        # the interactive console session so mss/pyautogui address the real
        # desktop. Forward-slash script paths dodge host-shell mangling.
        self.exec(
            [
                self.python_guest,
                f"{GUEST_DIR}/session1_launch.py",
                f"{GUEST_DIR}/waa_shim.py",
                "--port",
                str(port),
            ]
        )
        url = f"http://{self.guest_ip()}:{port}"
        deadline = time.time() + wait_s
        while time.time() < deadline:
            if self._shim_alive(url):
                return url
            time.sleep(1.5)
        raise ParallelsError(f"shim did not come up at {url}")

    def _shim_alive(self, url: str) -> bool:
        try:
            import requests

            r = requests.get(f"{url}/screenshot", timeout=8)
            return r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n"
        except Exception:  # noqa: BLE001
            return False

    def shim_url(self, *, port: int = SHIM_PORT) -> str:
        """URL WindowsBackend should target (``http://<guest-ip>:<port>``)."""
        return f"http://{self.guest_ip()}:{port}"

    # -- the hardened win_agent (session 1, optional bearer token) -----------

    def _agent_server_path(self) -> str:
        """Host path to the packaged win_agent server (self-contained script)."""
        return os.path.abspath(
            os.path.join(os.path.dirname(__file__), "win_agent", "server.py")
        )

    def launch_agent(
        self,
        *,
        port: int = SHIM_PORT,
        host: str = "0.0.0.0",
        token: Optional[str] = None,
        host_ip: Optional[str] = None,
        wait_s: float = 25.0,
        tls: bool = True,
        tls_hostnames: Optional[list[str]] = None,
    ) -> AgentEndpoint:
        """Deploy + start the hardened ``win_agent`` server in session 1.

        The successor to :meth:`launch_shim`: it ships the dependency-free,
        auth-capable ``openadapt_flow.backends.win_agent.server`` into the guest
        and runs it in the interactive console session (session 1) via
        ``session1_launch.py``, so ``mss``/``pyautogui`` address the real
        desktop. ``host`` defaults to ``0.0.0.0`` because a host->guest
        ``WindowsBackend`` must reach it over the shared network; pass a
        ``token`` (strongly recommended when exposed like this).

        **TLS is auto-provisioned by default** (``tls=True``): this control
        plane mints a fresh per-run self-signed cert for the guest IP
        (``win_agent.tls.generate_self_signed_cert``), provisions the cert + key
        into the guest, starts the agent serving **HTTPS** with them, and returns
        the cert **fingerprint** on the :class:`AgentEndpoint` so the client pins
        it. A launched desktop session is therefore encrypted **and** pinned end
        to end with **no manual step** — the PHI-in-transit control (see
        ``docs/phi_in_transit.md``). The minted host-side key/cert are deleted
        once provisioned into the guest.

        Set ``tls=False`` for the documented **loopback/dev escape**: the agent
        serves plaintext HTTP and the returned endpoint carries
        ``require_tls=False`` so the client does not fail closed. Never carry
        real PHI over that path.

        Args:
            tls: Auto-provision TLS (default True, secure). False = plaintext
                dev escape.
            tls_hostnames: Extra SANs the per-run cert must be valid for, on top
                of the guest IP and loopback (e.g. a DNS name the client uses).

        Returns:
            An :class:`AgentEndpoint` (url + token + pin fingerprint) once
            ``/health`` reports ok. Raises :class:`ParallelsError` on timeout.
        """
        host_ip = host_ip or self.host_ip()
        guest_ip = self.guest_ip()
        server = self._agent_server_path()
        launcher = os.path.abspath(os.path.join(_SCRIPT_DIR, "session1_launch.py"))
        self.exec_cmd(f"if not exist {GUEST_DIR} mkdir {GUEST_DIR}")
        self.push_file(server, f"{GUEST_DIR}/win_agent_server.py", host_ip=host_ip)
        self.push_file(launcher, f"{GUEST_DIR}/session1_launch.py", host_ip=host_ip)
        self.exec(
            [
                "netsh",
                "advfirewall",
                "firewall",
                "add",
                "rule",
                "name=OAAgent",
                "dir=in",
                "action=allow",
                "protocol=TCP",
                f"localport={port}",
            ]
        )

        # Auto-provision the per-run cert BEFORE launching so the agent can serve
        # HTTPS from the first request (no plaintext window). Minting happens on
        # this control plane (cryptography); the guest only needs stdlib ssl.
        fingerprint: Optional[str] = None
        if tls:
            fingerprint = self._provision_agent_cert(
                guest_ip, host_ip=host_ip, extra_hostnames=tls_hostnames
            )

        self.kill_shim()
        time.sleep(2)
        launch_args = [
            self.python_guest,
            f"{GUEST_DIR}/session1_launch.py",
            f"{GUEST_DIR}/win_agent_server.py",
            "--host",
            host,
            "--port",
            str(port),
        ]
        if token:
            launch_args += ["--token", token]
        if tls:
            launch_args += [
                "--certfile",
                f"{GUEST_DIR}/agent-cert.pem",
                "--keyfile",
                f"{GUEST_DIR}/agent-key.pem",
            ]
        self.exec(launch_args)

        scheme = "https" if tls else "http"
        url = f"{scheme}://{guest_ip}:{port}"
        deadline = time.time() + wait_s
        while time.time() < deadline:
            if self._agent_alive(url, fingerprint=fingerprint):
                return AgentEndpoint(
                    url=url,
                    token=token,
                    pin_fingerprint=fingerprint,
                    require_tls=tls,
                )
            time.sleep(1.5)
        raise ParallelsError(f"win_agent did not come up at {url}")

    def _provision_agent_cert(
        self,
        guest_ip: str,
        *,
        host_ip: Optional[str] = None,
        extra_hostnames: Optional[list[str]] = None,
    ) -> str:
        """Mint a per-run self-signed cert and push it into the guest.

        Runs on the control plane (uses ``cryptography``). The cert's SAN covers
        the guest IP the client reaches (plus loopback and any
        ``extra_hostnames``). The cert + key are copied into ``GUEST_DIR`` so the
        agent can serve HTTPS with them, then the host-side copies (the private
        key is a secret) are deleted. Returns the cert's SHA-256 fingerprint for
        the client to pin.
        """
        from openadapt_flow.backends.win_agent.tls import generate_self_signed_cert

        hostnames = [guest_ip, *(extra_hostnames or [])]
        bundle = generate_self_signed_cert(hostnames)
        try:
            self.push_file(
                bundle.certfile, f"{GUEST_DIR}/agent-cert.pem", host_ip=host_ip
            )
            self.push_file(
                bundle.keyfile, f"{GUEST_DIR}/agent-key.pem", host_ip=host_ip
            )
        finally:
            # The host copies (esp. the private key) are secrets and no longer
            # needed once in the guest -- never leave them on the control plane.
            shutil.rmtree(os.path.dirname(bundle.certfile), ignore_errors=True)
        return bundle.fingerprint

    def _agent_alive(self, url: str, *, fingerprint: Optional[str] = None) -> bool:
        """True when the agent's unauthenticated ``/health`` reports ok.

        Over HTTPS the per-run cert is self-signed, so the liveness probe must
        pin ``fingerprint`` too (system-CA validation would reject it) -- the
        same pin the client will use, verified here before we hand it back.
        """
        try:
            import requests

            if fingerprint:
                from openadapt_flow.backends.win_agent.tls import pinned_session

                r = pinned_session(fingerprint).get(f"{url}/health", timeout=8)
            else:
                r = requests.get(f"{url}/health", timeout=8)
            return r.status_code == 200 and r.json().get("status") == "ok"
        except Exception:  # noqa: BLE001
            return False
