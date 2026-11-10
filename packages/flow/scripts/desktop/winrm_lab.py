"""Control plane for a Windows lab host reached over SSH or WinRM.

The Parallels harness (``openadapt_flow.backends.parallels_vm``) drives its
guest through ``prlctl exec``. A UTM (QEMU) VM, a lab PC or any Windows host
has no such channel, so this module provides the same few operations over
OpenSSH (key auth) or WinRM (NTLM), for qualification runs only:

* ``run_ps`` / ``run_cmd``: one PowerShell script or command line, in a
  network logon of the lab account (no desktop, no input).
* ``push_file``: copy a small file onto the host.
* ``launch_interactive``: start a command in the signed-in user's desktop
  session through a one-shot scheduled task with an interactive token, the
  same "start it in session 1" step ``session1_launch.py`` does from SYSTEM.
  Registering that task needs no password, so the SSH transport never
  handles one.

Each lab host is a *profile*: a set of environment variables with the prefix
``OAFLOW_<PROFILE>_``, normally loaded from the checkout's gitignored
``.env``:

* ``HOST``, ``USER``: the host address and the lab account.
* ``SSH_KEY``: a private key path. When set, the profile uses SSH.
* ``PASSWORD``: the WinRM password, used only when ``SSH_KEY`` is unset.
* ``PYTHON``: the host's ``pythonw.exe`` (optional; found on the host
  when unset).
* ``APP_PYTHON``: the console ``python.exe`` that test apps and their
  helper scripts run under (optional; the ``python.exe`` beside ``PYTHON``).
* ``GUEST_DIR``: where the agent and its certificate go (default ``C:\\oa``).

``WinRMLab()`` uses the profile named by ``OAFLOW_LAB_PROFILE`` (default
``winlab``), so a harness written for one lab host runs on another by
changing that variable. Nothing here prints a credential. ``pywinrm`` is a lab
dependency only and is imported lazily, so nothing ships with it.

SSH commands default to a finite 600-second budget. The three desktop task
preparation calls use 30 seconds. A local SSH timeout does not confirm guest
exit: ``SSHTimeoutError`` retains ``MAY_STILL_RUN`` and requires reconciliation.
It does not attempt remote cancellation.
"""

from __future__ import annotations

import base64
import math
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# A WinRM command line holds about 8 KB once PowerShell encodes it as UTF-16
# base64, so files move in small chunks.
_CHUNK = 2000

# Python 3.12 install locations the official installers use; ``py -3.12`` is
# asked before these.
_PYTHON_DIRS = (
    r"$env:ProgramFiles\Python312-arm64",
    r"$env:ProgramFiles\Python312",
    r"C:\Python312",
    r"$env:LOCALAPPDATA\Programs\Python\Python312-arm64",
    r"$env:LOCALAPPDATA\Programs\Python\Python312",
)


# Run over SSH: read a length-framed script from stdin as one block. Windows
# OpenSSH does not always deliver EOF, so ReadToEnd can wait indefinitely. Like
# ``powershell -Command``, exit 1 when the script's last statement failed.
_SSH_BOOTSTRAP = base64.b64encode(
    (
        "$ProgressPreference='SilentlyContinue'; "
        "[Console]::InputEncoding=[Text.Encoding]::UTF8; "
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
        "$global:OAFlowLastOk=$true; "
        "$n=0; $header=[Console]::In.ReadLine(); "
        "if (-not [int]::TryParse($header,[ref]$n) -or $n -le 0) { exit 2 }; "
        "$b=New-Object char[] $n; $r=0; "
        "while ($r -lt $n) { "
        "$k=[Console]::In.Read($b,$r,$n-$r); if ($k -le 0) { exit 2 }; $r+=$k }; "
        '$s=(-join $b) + "`n`$global:OAFlowLastOk=`$?"; '
        "& ([scriptblock]::Create($s)); "
        "if (-not $global:OAFlowLastOk) { exit 1 }"
    ).encode("utf-16-le")
).decode("ascii")


class WinRMLabError(RuntimeError):
    """A lab host command failed; carries the exit status and stderr tail."""


class SSHTimeoutError(WinRMLabError):
    """The local SSH deadline expired; the guest outcome remains unknown."""

    def __init__(self, host: str, operation: str, timeout_s: float) -> None:
        self.host = host
        self.operation = operation
        self.timeout_s = timeout_s
        super().__init__(
            f"{operation} over SSH to {host} timed out after {timeout_s:g}s; "
            "guest state unknown (MAY_STILL_RUN); ownership unverified; "
            "no cancellation attempted; reconciliation required"
        )

    def as_dict(self) -> dict[str, str | float | bool]:
        """Retain the unknown guest outcome without payload or partial output."""
        return {
            "host": self.host,
            "operation": self.operation,
            "timeout_s": self.timeout_s,
            "guest_state": "MAY_STILL_RUN",
            "identity_verified": False,
            "cancellation_attempted": False,
            "reconciliation_required": True,
        }


@dataclass
class Result:
    status: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class HostProfile:
    """One lab host's connection facts. Holds no password."""

    name: str
    host: str
    user: str
    ssh_key: Optional[str] = None
    python: Optional[str] = None
    app_python: Optional[str] = None
    guest_dir: str = r"C:\oa"

    @property
    def transport(self) -> str:
        return "ssh" if self.ssh_key else "winrm"

    @property
    def prefix(self) -> str:
        return f"OAFLOW_{self.name.upper()}_"

    @classmethod
    def from_env(cls, name: Optional[str] = None) -> "HostProfile":
        name = name or os.environ.get("OAFLOW_LAB_PROFILE") or "winlab"
        prefix = f"OAFLOW_{name.upper()}_"
        missing = [k for k in ("HOST", "USER") if not os.environ.get(prefix + k)]
        if missing:
            raise WinRMLabError(
                f"lab profile {name!r} needs {', '.join(prefix + k for k in missing)}"
            )
        key = os.environ.get(prefix + "SSH_KEY")
        return cls(
            name=name,
            host=os.environ[prefix + "HOST"],
            user=os.environ[prefix + "USER"],
            ssh_key=os.path.expanduser(key) if key else None,
            python=os.environ.get(prefix + "PYTHON") or None,
            app_python=os.environ.get(prefix + "APP_PYTHON") or None,
            guest_dir=os.environ.get(prefix + "GUEST_DIR") or r"C:\oa",
        )


def load_env_file(path: Path) -> None:
    """Load ``KEY=value`` lines into ``os.environ`` without overriding."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _clixml_text(stderr: str) -> str:
    """Error text from PowerShell's CLIXML stderr, or ``stderr`` unchanged."""
    if not stderr.lstrip().startswith("#< CLIXML"):
        return stderr
    parts = re.findall(r'<S S="Error">(.*?)</S>', stderr, flags=re.S)
    text = "".join(parts).replace("_x000D_", "").replace("_x000A_", "\n")
    for entity, char in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&amp;", "&")):
        text = text.replace(entity, char)
    return text


class WinRMLab:
    """One Windows lab host, reached over SSH or WinRM per its profile."""

    def __init__(
        self,
        host: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        *,
        port: int = 5985,
        profile: Optional[str] = None,
    ) -> None:
        if (host or user) and not profile:
            # Explicit connection facts: the original WinRM-only call, which
            # fills gaps (and the password) from the ``winlab`` variables.
            host = host or os.environ.get("OAFLOW_WINLAB_HOST")
            user = user or os.environ.get("OAFLOW_WINLAB_USER")
            if not (host and user):
                raise WinRMLabError("WinRMLab needs a host and a user")
            self.profile = HostProfile(name="winlab", host=host, user=user)
        else:
            base = HostProfile.from_env(profile)
            self.profile = HostProfile(
                name=base.name,
                host=host or base.host,
                user=user or base.user,
                ssh_key=base.ssh_key,
                python=base.python,
                app_python=base.app_python,
                guest_dir=base.guest_dir,
            )
        self.host = self.profile.host
        self.user = self.profile.user
        self._session = None
        if self.profile.transport == "winrm":
            import winrm  # noqa: PLC0415 - lab-only dependency

            password = password or os.environ[self.profile.prefix + "PASSWORD"]
            # NTLM with message encryption: the payload is encrypted even over
            # HTTP 5985 on a private lab network.
            self._session = winrm.Session(
                f"http://{self.host}:{port}/wsman",
                auth=(self.user, password),
                transport="ntlm",
                message_encryption="always",
            )

    # -- transport -----------------------------------------------------------

    def _ssh_base(self) -> list[str]:
        return [
            "ssh",
            "-i",
            str(self.profile.ssh_key),
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=15",
            f"{self.user}@{self.host}",
        ]

    def _ssh(
        self,
        remote_command: str,
        stdin: bytes = b"",
        *,
        timeout_s: float = 600.0,
        operation: str = "PowerShell command",
    ) -> Result:
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("SSH timeout must be finite and positive")
        try:
            proc = subprocess.run(
                self._ssh_base() + [remote_command],
                input=stdin,
                capture_output=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            # The exception's own text repeats the command line.
            raise SSHTimeoutError(self.host, operation, timeout_s) from None
        return Result(
            proc.returncode,
            proc.stdout.decode("utf-8", "replace"),
            _clixml_text(proc.stderr.decode("utf-8", "replace")),
        )

    def run_ps(
        self,
        script: str,
        *,
        check: bool = True,
        timeout_s: float = 600.0,
        operation: str = "PowerShell command",
    ) -> Result:
        if self._session is not None:
            raw = self._session.run_ps(script)
            result = Result(
                raw.status_code,
                raw.std_out.decode("utf-8", "replace"),
                raw.std_err.decode("utf-8", "replace"),
            )
        else:
            # The script goes on stdin, so neither its length nor what it
            # holds (an agent token) is on a command line. Progress records
            # would arrive as CLIXML on stderr.
            units = len(script.encode("utf-16-le")) // 2
            result = self._ssh(
                f"powershell -NoProfile -NonInteractive -EncodedCommand {_SSH_BOOTSTRAP}",
                stdin=f"{units}\n{script}".encode("utf-8"),
                timeout_s=timeout_s,
                operation=operation,
            )
        if check and result.status != 0:
            raise WinRMLabError(
                f"guest PowerShell exited {result.status}: {result.stderr[-600:]}"
            )
        return result

    def run_cmd(
        self,
        command: str,
        *args: str,
        check: bool = True,
        timeout_s: float = 600.0,
        operation: str = "guest command",
    ) -> Result:
        """Run a command, forwarding its finite budget and phase on SSH."""
        if self._session is not None:
            raw = self._session.run_cmd(command, list(args))
            result = Result(
                raw.status_code,
                raw.std_out.decode("utf-8", "replace"),
                raw.std_err.decode("utf-8", "replace"),
            )
        else:
            # PowerShell quoting, not cmd.exe's: no metacharacter reaches cmd.
            quoted = " ".join(
                "'" + a.replace("'", "''") + "'" for a in (command, *args)
            )
            result = self.run_ps(
                f"& {quoted}; exit $LASTEXITCODE",
                check=False,
                timeout_s=timeout_s,
                operation=operation,
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
        if self._session is None:
            dest = "/" + remote.replace("\\", "/")
            proc = subprocess.run(
                [
                    "scp",
                    "-q",
                    "-i",
                    str(self.profile.ssh_key),
                    "-o",
                    "BatchMode=yes",
                    str(local),
                    f"{self.user}@{self.host}:{dest}",
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=600,
            )
            if proc.returncode != 0:
                raise WinRMLabError(
                    f"scp to {remote} failed: "
                    f"{proc.stderr.decode('utf-8', 'replace')[-300:]}"
                )
        else:
            for start in range(0, len(data), _CHUNK):
                chunk = base64.b64encode(data[start : start + _CHUNK]).decode("ascii")
                self.run_ps(
                    f"$p='{target}'; $b=[Convert]::FromBase64String('{chunk}'); "
                    "$f=[IO.File]::Open($p,'Append'); $f.Write($b,0,$b.Length); $f.Close()"
                )
        size = self.run_ps(f"(Get-Item '{target}').Length").stdout.strip()
        if size != str(len(data)):
            raise WinRMLabError(f"push_file size mismatch for {remote}: {size}")

    # -- desktop session -----------------------------------------------------

    def resolve_python(self) -> str:
        """The host's windowless ``pythonw.exe`` for Python 3.12."""
        if self.profile.python:
            return self.profile.python
        dirs = ", ".join(f'"{d}"' for d in _PYTHON_DIRS)
        script = f"""
$found = $null
if (Get-Command py -ErrorAction SilentlyContinue) {{
  $prefix = (& py -3.12 -c "import sys; print(sys.exec_prefix)" 2>$null)
  if ($LASTEXITCODE -eq 0 -and $prefix) {{ $found = Join-Path $prefix.Trim() 'pythonw.exe' }}
}}
if (-not $found -or -not (Test-Path $found)) {{
  $found = $null
  foreach ($d in @({dirs})) {{
    $p = Join-Path $d 'pythonw.exe'
    if (Test-Path $p) {{ $found = $p; break }}
  }}
}}
$found
"""
        path = self.run_ps(script).stdout.strip()
        if not path:
            raise WinRMLabError(
                f"no Python 3.12 on {self.host}; set {self.profile.prefix}PYTHON"
            )
        return path

    def resolve_app_python(self) -> str:
        """The host's console ``python.exe`` for test apps and helper scripts.

        Harnesses call this instead of naming an interpreter path, which
        differs between hosts (an ARM VM's ``Python312-arm64``, an x64 PC's
        ``C:\\Python312``).
        """
        if self.profile.app_python:
            return self.profile.app_python
        pythonw = self.resolve_python()
        folder, _, name = pythonw.rpartition("\\")
        return f"{folder}\\python.exe" if name.lower() == "pythonw.exe" else pythonw

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
        user = self.user.replace("'", "''")
        script = f"""
$action = New-ScheduledTaskAction -Execute '{exe_quoted}'{argument}
$principal = New-ScheduledTaskPrincipal -UserId '{user}' -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 12)
Register-ScheduledTask -TaskName '{task}' -Action $action -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName '{task}'
"""
        self.run_ps(script, timeout_s=30.0, operation="launch interactive task")

    def stop_interactive(self, name: str = "OAFlowLab") -> None:
        task = name.replace("'", "")
        self.run_ps(
            f"Stop-ScheduledTask -TaskName '{task}' -ErrorAction SilentlyContinue; "
            f"Unregister-ScheduledTask -TaskName '{task}' -Confirm:$false "
            "-ErrorAction SilentlyContinue",
            check=False,
            timeout_s=30.0,
            operation="stop interactive task",
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
        user = self.user.replace("'", "''")
        script = f"""
$line = (query session '{user}' 2>$null | Select-Object -Skip 1 | Select-Object -First 1)
if ($line -and $line -match '\\s(\\d+)\\s+Disc') {{
  $id = $Matches[1]
  $action = New-ScheduledTaskAction -Execute 'tscon.exe' -Argument "$id /dest:console"
  Register-ScheduledTask -TaskName OAFlowConsole -Action $action -User SYSTEM -RunLevel Highest -Force | Out-Null
  Start-ScheduledTask -TaskName OAFlowConsole
  Start-Sleep -Seconds 4
  Unregister-ScheduledTask -TaskName OAFlowConsole -Confirm:$false
}}
"""
        self.run_ps(
            script, check=False, timeout_s=30.0, operation="attach desktop console"
        )

    def launch_agent(
        self,
        *,
        token: str,
        python: Optional[str] = None,
        guest_dir: Optional[str] = None,
        port: int = 5000,
        wait_s: float = 40.0,
        server_path: Optional[Path] = None,
    ):
        """Deploy and start ``win_agent`` on the desktop with a pinned TLS cert.

        The interpreter is ``pythonw.exe``: a console window would open over
        the app under test, take the recorded clicks, and its Quick Edit
        selection would stall the agent's writes.

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

        python = python or self.resolve_python()
        guest_dir = guest_dir or self.profile.guest_dir
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
