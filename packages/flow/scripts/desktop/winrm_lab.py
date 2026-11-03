"""Control plane for a Windows lab VM reached over WinRM (no Parallels needed).

The Parallels harness (``openadapt_flow.backends.parallels_vm``) drives its
guest through ``prlctl exec``. A UTM (QEMU) VM, a lab PC or any Windows host
with WinRM has no such channel, so this module provides the same few
operations over WinRM with NTLM, for qualification runs only:

* ``run_ps`` / ``run_cmd``: one PowerShell script or command line, in a
  network logon of the lab account (session 0: no desktop, no input).
* ``push_file``: copy a small file into the guest (base64 over WinRM).
* ``launch_interactive``: start a command in the signed-in user's desktop
  session through a one-shot scheduled task with an interactive token, the
  same "start it in session 1" step ``session1_launch.py`` does from SYSTEM.

Credentials come from the environment (``OAFLOW_WINLAB_HOST``,
``OAFLOW_WINLAB_USER``, ``OAFLOW_WINLAB_PASSWORD``), normally loaded from the
checkout's gitignored ``.env``; nothing here prints them. ``pywinrm`` is a lab
dependency only and is imported lazily, so nothing ships with it.
"""

from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# A WinRM command line holds about 8 KB once PowerShell encodes it as UTF-16
# base64, so files move in small chunks.
_CHUNK = 2000


class WinRMLabError(RuntimeError):
    """A lab guest command failed; carries the exit status and stderr tail."""


@dataclass
class Result:
    status: int
    stdout: str
    stderr: str


def load_env_file(path: Path) -> None:
    """Load ``KEY=value`` lines into ``os.environ`` without overriding."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


class WinRMLab:
    """One Windows lab guest reached over WinRM (HTTP 5985, NTLM)."""

    def __init__(
        self,
        host: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        *,
        port: int = 5985,
    ) -> None:
        import winrm  # noqa: PLC0415 - lab-only dependency

        self.host = host or os.environ["OAFLOW_WINLAB_HOST"]
        self.user = user or os.environ["OAFLOW_WINLAB_USER"]
        password = password or os.environ["OAFLOW_WINLAB_PASSWORD"]
        # NTLM with message encryption: the payload is encrypted even over
        # HTTP 5985 on the host-only VM network.
        self._session = winrm.Session(
            f"http://{self.host}:{port}/wsman",
            auth=(self.user, password),
            transport="ntlm",
            message_encryption="always",
        )

    def run_ps(self, script: str, *, check: bool = True) -> Result:
        raw = self._session.run_ps(script)
        result = Result(
            raw.status_code,
            raw.std_out.decode("utf-8", "replace"),
            raw.std_err.decode("utf-8", "replace"),
        )
        if check and result.status != 0:
            raise WinRMLabError(
                f"guest PowerShell exited {result.status}: {result.stderr[-600:]}"
            )
        return result

    def run_cmd(self, command: str, *args: str, check: bool = True) -> Result:
        raw = self._session.run_cmd(command, list(args))
        result = Result(
            raw.status_code,
            raw.std_out.decode("utf-8", "replace"),
            raw.std_err.decode("utf-8", "replace"),
        )
        if check and result.status != 0:
            raise WinRMLabError(
                f"guest command exited {result.status}: {result.stderr[-600:]}"
            )
        return result

    def push_file(self, local: Path, remote: str) -> None:
        """Copy ``local`` to the guest path ``remote`` (overwrites)."""
        import hashlib  # noqa: PLC0415

        data = Path(local).read_bytes()
        target = remote.replace("'", "''")
        digest = hashlib.sha256(data).hexdigest()
        existing = self.run_ps(
            f"if (Test-Path '{target}') {{ (Get-FileHash '{target}' -Algorithm SHA256).Hash }}",
            check=False,
        ).stdout.strip()
        if existing.lower() == digest:
            return
        self.run_ps(
            f"$p='{target}'; New-Item -ItemType Directory -Force "
            "(Split-Path -Parent $p) | Out-Null; "
            "[IO.File]::WriteAllBytes($p, [byte[]]@())"
        )
        for start in range(0, len(data), _CHUNK):
            chunk = base64.b64encode(data[start : start + _CHUNK]).decode("ascii")
            self.run_ps(
                f"$p='{target}'; $b=[Convert]::FromBase64String('{chunk}'); "
                "$f=[IO.File]::Open($p,'Append'); $f.Write($b,0,$b.Length); $f.Close()"
            )
        size = self.run_ps(f"(Get-Item '{target}').Length").stdout.strip()
        if size != str(len(data)):
            raise WinRMLabError(f"push_file size mismatch for {remote}: {size}")

    def launch_interactive(self, command_line: str, *, name: str = "OAFlowLab") -> None:
        """Start ``command_line`` on the signed-in user's desktop.

        A scheduled task registered with the interactive logon type runs in
        the session where that user is signed in, which is the only place a
        window can be shown, captured or driven.
        """
        task = name.replace("'", "")
        if command_line.startswith('"'):
            exe, _, arguments = command_line[1:].partition('" ')
        else:
            exe, _, arguments = command_line.partition(" ")
        quoted = arguments.replace("'", "''")
        exe_quoted = exe.replace("'", "''")
        argument = f" -Argument '{quoted}'" if arguments.strip() else ""
        script = f"""
$action = New-ScheduledTaskAction -Execute '{exe_quoted}'{argument}
$principal = New-ScheduledTaskPrincipal -UserId '{self.user}' -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 12)
Register-ScheduledTask -TaskName '{task}' -Action $action -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName '{task}'
"""
        self.run_ps(script)

    def stop_interactive(self, name: str = "OAFlowLab") -> None:
        task = name.replace("'", "")
        self.run_ps(
            f"Stop-ScheduledTask -TaskName '{task}' -ErrorAction SilentlyContinue; "
            f"Unregister-ScheduledTask -TaskName '{task}' -Confirm:$false "
            "-ErrorAction SilentlyContinue",
            check=False,
        )

    def wait_port(self, port: int, *, timeout_s: float = 60.0) -> bool:
        import socket  # noqa: PLC0415

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((self.host, port), timeout=2):
                    return True
            except OSError:
                time.sleep(1)
        return False

    def attach_console(self) -> None:
        """Put the lab account's desktop back on the console.

        Closing an RDP connection leaves that account's session disconnected:
        it is still signed in, but has no display, so screenshots and input
        fail. ``tscon <id> /dest:console`` reattaches it; it needs SYSTEM, so
        a one-shot scheduled task runs it.
        """
        script = f"""
$line = (query session {self.user} 2>$null | Select-Object -Skip 1 | Select-Object -First 1)
if ($line -and $line -match '\\s(\\d+)\\s+Disc') {{
  $id = $Matches[1]
  $action = New-ScheduledTaskAction -Execute 'tscon.exe' -Argument "$id /dest:console"
  Register-ScheduledTask -TaskName OAFlowConsole -Action $action -User SYSTEM -RunLevel Highest -Force | Out-Null
  Start-ScheduledTask -TaskName OAFlowConsole
  Start-Sleep -Seconds 4
  Unregister-ScheduledTask -TaskName OAFlowConsole -Confirm:$false
}}
"""
        self.run_ps(script, check=False)

    def launch_agent(
        self,
        *,
        token: str,
        python: str = r"C:\Program Files\Python312-arm64\pythonw.exe",
        guest_dir: str = r"C:\oa",
        port: int = 5000,
        wait_s: float = 40.0,
        server_path: Optional[Path] = None,
    ):
        """Deploy and start ``win_agent`` on the desktop with a pinned TLS cert.

        The default interpreter is ``pythonw.exe``: a console window would
        open over the app under test, take the recorded clicks, and its Quick
        Edit selection would stall the agent's writes.

        Mirrors :meth:`ParallelsVM.launch_agent`: a fresh self-signed cert for
        the guest address is minted here, copied in, and the host copies are
        deleted; the returned endpoint pins its fingerprint and carries the
        bearer token, so the channel is encrypted and authenticated.
        """
        import shutil  # noqa: PLC0415

        from openadapt_flow.backends.parallels_vm import (  # noqa: PLC0415
            AgentEndpoint,
        )
        from openadapt_flow.backends.win_agent import server as agent_server  # noqa: PLC0415
        from openadapt_flow.backends.win_agent.tls import (  # noqa: PLC0415
            generate_self_signed_cert,
        )

        self.attach_console()
        self.stop_interactive("OAFlowAgent")
        # A previous agent stuck inside a UI Automation call keeps its port.
        self.run_ps(
            "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "
            "'*win_agent_server.py*' } | ForEach-Object { Stop-Process -Id "
            "$_.ProcessId -Force }",
            check=False,
        )
        self.push_file(
            Path(server_path or agent_server.__file__),
            rf"{guest_dir}\win_agent_server.py",
        )
        bundle = generate_self_signed_cert([self.host])
        try:
            self.push_file(Path(bundle.certfile), rf"{guest_dir}\agent-cert.pem")
            self.push_file(Path(bundle.keyfile), rf"{guest_dir}\agent-key.pem")
        finally:
            shutil.rmtree(os.path.dirname(bundle.certfile), ignore_errors=True)
        self.launch_interactive(
            f'"{python}" "{guest_dir}\\win_agent_server.py" --host 0.0.0.0 '
            f"--port {port} --token {token} "
            f'--certfile "{guest_dir}\\agent-cert.pem" --keyfile "{guest_dir}\\agent-key.pem"',
            name="OAFlowAgent",
        )
        endpoint = AgentEndpoint(
            url=f"https://{self.host}:{port}",
            token=token,
            pin_fingerprint=bundle.fingerprint,
            require_tls=True,
        )
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            try:
                if endpoint.backend().probe():
                    return endpoint
            except Exception:  # noqa: BLE001 - not up yet
                pass
            time.sleep(1.5)
        raise WinRMLabError(f"win_agent did not come up at {endpoint.url}")
