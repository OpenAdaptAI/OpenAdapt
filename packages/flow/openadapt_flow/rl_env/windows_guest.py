"""The Windows guest behind a desktop environment, reached over WinRM.

The environment server drives one Windows machine (a buyer's VM built from
their own Windows image, an Azure or AWS Windows template, or a lab VM). This
module is its control plane, separate from the agent's surface:

* deploy the pack's apps and its database CLI into ``C:\\oa\\pack``, which
  the desktop session can read but not change (its data, in ``data``, it
  can); deploy checks that with a write probe from the desktop and refuses
  a guest where the desktop session could rewrite the oracle's code;
* reset an episode: stop the app, restore the pristine database, set the
  planted fault and the UI knobs, start the app on the signed-in desktop, all
  in one WinRM round trip;
* read the system of record (``pack_db.py dump``) for the oracle, and its
  digest for the state-leak check;
* start the in-guest UIA agent (``win_agent``) with a pinned TLS certificate.

WinRM runs in a network logon (session 0): it can see files and processes
but not the desktop, so it is never part of what the agent observes or acts
through. Credentials come from ``OPENADAPT_WINENV_HOST``, ``..._USER`` and
``..._PASSWORD`` (or the lab's ``OAFLOW_WINLAB_*``) and are never printed.
``pywinrm`` is imported lazily: ``pip install pywinrm``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

#: Where the pack lives in the guest. Fixed so the apps can name it. The
#: code is read-only to the desktop session; ``DATA_DIR`` holds what the
#: apps write (the databases, the fault and the UI knobs).
GUEST_DIR = r"C:\oa\pack"
DATA_DIR = r"C:\oa\pack\data"
#: Values that reach a PowerShell script from a task file.
_SAFE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
APPS_DIR = (
    Path(__file__).resolve().parents[2]
    / "environments"
    / "openenv"
    / "_windows"
    / "apps"
)
APP_FILES = ("pack_db.py", "patient_chart.ps1", "claims_desk.ps1")
PACK_DB_SHA256 = hashlib.sha256((APPS_DIR / "pack_db.py").read_bytes()).hexdigest()
APP_TASK = "OAEnvApp"
# Every process in the desktop session except the session's own shell and
# the in-guest agent. Explorer stays (it is the desktop); the rest is
# whatever an app or an agent started.
_KEEP = (
    "explorer.exe",
    "sihost.exe",
    "ctfmon.exe",
    "dwm.exe",
    "svchost.exe",
    "taskhostw.exe",
    "runtimebroker.exe",
    "searchhost.exe",
    "startmenuexperiencehost.exe",
    "shellexperiencehost.exe",
    "textinputhost.exe",
    "conhost.exe",
    "rdpclip.exe",
    "smartscreen.exe",
    "securityhealthsystray.exe",
    "widgets.exe",
    "widgetservice.exe",
    "lockapp.exe",
    "useroobebroker.exe",
    "applicationframehost.exe",
    "systemsettings.exe",
    "dllhost.exe",
    "backgroundtaskhost.exe",
    "crossdeviceresume.exe",
    "phoneexperiencehost.exe",
    "msedgewebview2.exe",
    "onedrive.exe",
    "wsmprovhost.exe",
)
_STOP_DESKTOP_PROCESSES = (
    "$keep = @(" + ",".join(f"'{n}'" for n in _KEEP) + ")\n"
    "$session = (Get-Process -Name explorer -ErrorAction SilentlyContinue | "
    "Select-Object -First 1).SessionId\n"
    "if ($null -ne $session) {\n"
    "  Get-CimInstance Win32_Process | Where-Object { $_.SessionId -eq $session -and "
    "$keep -notcontains $_.Name.ToLower() -and $_.CommandLine -notlike '*win_agent_server.py*' } | "
    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }\n"
    "}\n"
    # A save launched from session 0 or still exiting.
    "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
    "Where-Object { $_.CommandLine -like '*pack_db.py*' } | "
    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }\n"
    "Start-Sleep -Milliseconds 300\n"
)
AGENT_TASK = "OAEnvAgent"
# One WinRM command line holds about 8 KB once PowerShell encodes it.
_CHUNK = 2000


class GuestError(RuntimeError):
    """A guest command failed; carries the exit status and stderr tail."""


@dataclass
class GuestResult:
    status: int
    stdout: str
    stderr: str


def _env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    raise GuestError(f"set {names[0]} (the Windows guest is not configured)")


# PowerShell also ends a single-quoted string at these.
_QUOTES = ("'", "\u2018", "\u2019", "\u201a", "\u201b")


def _ps_quote(value: str) -> str:
    for quote in _QUOTES:
        value = value.replace(quote, quote * 2)
    return value


def _safe(name: str, value: str) -> str:
    if not _SAFE.match(value):
        raise GuestError(f"{name} {value!r} is not a plain identifier")
    return value


def last_json(text: str) -> Any:
    """The last line of ``text`` that parses as JSON (PowerShell adds noise)."""
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    raise GuestError("guest returned no JSON")


class WindowsGuest:
    """One Windows guest reached over WinRM (HTTP 5985, NTLM, encrypted)."""

    def __init__(
        self,
        host: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        *,
        port: int = 5985,
        python: Optional[str] = None,
    ) -> None:
        import winrm  # noqa: PLC0415 - optional dependency

        self.host = host or _env("OPENADAPT_WINENV_HOST", "OAFLOW_WINLAB_HOST")
        self.user = user or _env("OPENADAPT_WINENV_USER", "OAFLOW_WINLAB_USER")
        secret = password or _env("OPENADAPT_WINENV_PASSWORD", "OAFLOW_WINLAB_PASSWORD")
        self._secret = secret
        self._session = winrm.Session(
            f"http://{self.host}:{port}/wsman",
            auth=(self.user, secret),
            transport="ntlm",
            message_encryption="always",
        )
        self.python = python or os.environ.get(
            "OPENADAPT_WINENV_PYTHON", r"C:\Program Files\Python312-arm64\python.exe"
        )

    def rdp_password(self) -> str:
        """The account's password, for the RDP surface's sign-in only."""
        return self._secret

    # -- primitives ---------------------------------------------------------
    def run_ps(self, script: str, *, check: bool = True) -> GuestResult:
        raw = self._session.run_ps(script)
        result = GuestResult(
            raw.status_code,
            raw.std_out.decode("utf-8", "replace"),
            raw.std_err.decode("utf-8", "replace"),
        )
        if check and result.status != 0:
            raise GuestError(
                f"guest PowerShell exited {result.status}: {result.stderr[-600:]}"
            )
        return result

    def push_file(self, local: Path, remote: str) -> None:
        """Copy ``local`` to ``remote`` in the guest unless it is already there."""
        data = Path(local).read_bytes()
        target = _ps_quote(remote)
        digest = hashlib.sha256(data).hexdigest()
        existing = self.run_ps(
            f"if (Test-Path '{target}') {{ (Get-FileHash '{target}' -Algorithm SHA256).Hash }}",
            check=False,
        ).stdout.strip()
        if existing.lower() == digest:
            return
        self.run_ps(
            f"$p='{target}'; New-Item -ItemType Directory -Force (Split-Path -Parent $p) "
            "| Out-Null; [IO.File]::WriteAllBytes($p, [byte[]]@())"
        )
        for start in range(0, len(data), _CHUNK):
            chunk = base64.b64encode(data[start : start + _CHUNK]).decode("ascii")
            self.run_ps(
                f"$p='{target}'; $b=[Convert]::FromBase64String('{chunk}'); "
                "$f=[IO.File]::Open($p,'Append'); $f.Write($b,0,$b.Length); $f.Close()"
            )
        got = self.run_ps(
            f"(Get-FileHash '{target}' -Algorithm SHA256).Hash"
        ).stdout.strip()
        if got.lower() != digest:
            raise GuestError(f"copy of {Path(local).name} did not arrive intact")

    def _interactive_script(self, exe: str, arguments: str, name: str) -> str:
        """PowerShell that starts ``exe arguments`` on the signed-in desktop.

        A scheduled task with the interactive logon type runs in the session
        where the user is signed in, the only place a window can be shown.
        """
        argument = f" -Argument '{_ps_quote(arguments)}'" if arguments.strip() else ""
        task = name.replace("'", "")
        return f"""
$action = New-ScheduledTaskAction -Execute '{_ps_quote(exe)}'{argument}
$principal = New-ScheduledTaskPrincipal -UserId '{_ps_quote(self.user)}' -LogonType Interactive
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 12)
Register-ScheduledTask -TaskName '{task}' -Action $action -Principal $principal -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName '{task}'
"""

    def attach_console(self) -> None:
        """Put the account's disconnected desktop session back on the console.

        Closing an RDP connection leaves the session signed in but without a
        display; ``tscon`` (as SYSTEM, through a one-shot task) reattaches it.
        """
        self.run_ps(
            f"""
$line = (query session '{_ps_quote(self.user)}' 2>$null | Select-Object -Skip 1 | Select-Object -First 1)
if ($line -and $line -match '\\s(\\d+)\\s+Disc') {{
  $action = New-ScheduledTaskAction -Execute 'tscon.exe' -Argument "$($Matches[1]) /dest:console"
  Register-ScheduledTask -TaskName OAEnvConsole -Action $action -User SYSTEM -RunLevel Highest -Force | Out-Null
  Start-ScheduledTask -TaskName OAEnvConsole
  Start-Sleep -Seconds 4
  Unregister-ScheduledTask -TaskName OAEnvConsole -Confirm:$false
}}
""",
            check=False,
        )

    # -- the pack -----------------------------------------------------------
    def deploy(self) -> None:
        """Copy the apps and the database CLI in, read-only to the desktop.

        The WinRM account must be an administrator. The code directory grants
        Administrators and SYSTEM full control and everyone else read and
        execute; ``data`` adds modify for the desktop account. A desktop
        process (the apps, or anything an agent opened) then cannot rewrite
        ``pack_db.py``, which the oracle runs. A write probe started on the
        desktop proves it: if the probe's file appears, deploy refuses.
        """
        user = _ps_quote(self.user)
        self.run_ps(
            f"New-Item -ItemType Directory -Force '{DATA_DIR}' | Out-Null\n"
            f"icacls '{GUEST_DIR}' /inheritance:r /grant:r '*S-1-5-32-544:(OI)(CI)F' "
            "'*S-1-5-18:(OI)(CI)F' '*S-1-5-32-545:(OI)(CI)RX' | Out-Null\n"
            "if ($LASTEXITCODE) { throw 'icacls on the pack directory failed' }\n"
            f"icacls '{DATA_DIR}' /grant:r '{user}:(OI)(CI)M' | Out-Null\n"
            "if ($LASTEXITCODE) { throw 'icacls on the data directory failed' }\n"
        )
        for name in APP_FILES:
            self.push_file(APPS_DIR / name, rf"{GUEST_DIR}\{name}")
        self.run_ps(
            f"Set-Content -Encoding ascii -Path '{GUEST_DIR}\\python.txt' "
            f"-Value '{_ps_quote(self.python)}'"
        )
        probe = rf"{GUEST_DIR}\write-probe.txt"
        self.run_ps(f"Remove-Item -Force -ErrorAction SilentlyContinue '{probe}'")
        self.run_ps(
            self._interactive_script(
                "powershell.exe",
                f"-NoProfile -WindowStyle Hidden -Command Set-Content -Path '{probe}' -Value probe",
                "OAEnvProbe",
            )
            + "Start-Sleep -Seconds 4\n"
            "Unregister-ScheduledTask -TaskName OAEnvProbe -Confirm:$false\n"
        )
        wrote = self.run_ps(f"Test-Path '{probe}'").stdout.strip()
        if wrote != "False":
            self.run_ps(f"Remove-Item -Force -ErrorAction SilentlyContinue '{probe}'")
            raise GuestError(
                "the desktop session can write the pack's code directory (is UAC "
                "off, or the desktop session elevated?); refusing a guest where an "
                "agent could rewrite the oracle"
            )

    def _db(self, *args: str) -> Any:
        quoted = " ".join(f"'{_ps_quote(a)}'" for a in args)
        out = self.run_ps(
            f"& '{_ps_quote(self.python)}' '{GUEST_DIR}\\pack_db.py' {quoted}"
        )
        return last_json(out.stdout)

    def dump(self, app: str) -> dict[str, Any]:
        return self._db("dump", app)

    def digest(self, app: str) -> str:
        return str(self._db("digest", app)["digest"])

    def reset(
        self,
        app: str,
        *,
        fault: str = "ok",
        variant: str = "none",
        ui: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Stop the app, restore its database, set the fault and UI, start it.

        One WinRM round trip. Returns the restored database's digest, which
        the caller checks against the pristine one: an episode that starts
        from anything else is refused.
        """
        app, fault, variant = (
            _safe("app", app),
            _safe("fault", fault),
            _safe("variant", variant),
        )
        ui_json = json.dumps({"window": "maximized", **(ui or {})})
        script = (
            f"Stop-ScheduledTask -TaskName '{APP_TASK}' -ErrorAction SilentlyContinue\n"
            # The desktop account's processes other than the agent and the
            # shell: the app, a save still writing, and anything the last
            # episode opened.
            + _STOP_DESKTOP_PROCESSES
            + f"$seed = & '{_ps_quote(self.python)}' '{GUEST_DIR}\\pack_db.py' seed '{app}' "
            f"--fault '{fault}' --variant '{variant}'\n"
            f"Set-Content -Encoding utf8 -Path '{DATA_DIR}\\{app}_ui.json' "
            f"-Value '{_ps_quote(ui_json)}'\n"
            f"$code = (Get-FileHash '{GUEST_DIR}\\pack_db.py' -Algorithm SHA256).Hash\n"
            + self._interactive_script(
                "powershell.exe",
                f"-STA -ExecutionPolicy Bypass -WindowStyle Hidden -File {GUEST_DIR}\\{app}.ps1",
                APP_TASK,
            )
            + "\n$r = $seed | ConvertFrom-Json; $r | Add-Member pack_db_sha256 $code.ToLower()\n"
            "Write-Output ($r | ConvertTo-Json -Compress)\n"
        )
        result = last_json(self.run_ps(script).stdout)
        if result.get("pack_db_sha256") != PACK_DB_SHA256:
            raise GuestError("the guest's pack_db.py differs from this server's copy")
        return result

    def stop_app(self) -> None:
        self.run_ps(
            f"Stop-ScheduledTask -TaskName '{APP_TASK}' -ErrorAction SilentlyContinue; "
            "Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | "
            "Where-Object { $_.CommandLine -like '*\\pack\\*.ps1*' } | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }",
            check=False,
        )

    def app_processes(self) -> int:
        out = self.run_ps(
            "@(Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | "
            "Where-Object { $_.CommandLine -like '*\\pack\\*.ps1*' }).Count"
        )
        return int(out.stdout.strip() or 0)

    # -- the in-guest UIA agent ---------------------------------------------
    def launch_agent(
        self, *, token: str, port: int = 5000, wait_s: float = 45.0
    ) -> Any:
        """Start ``win_agent`` on the desktop with a fresh pinned certificate.

        ``pythonw.exe`` so no console window opens over the app. Returns an
        :class:`~openadapt_flow.backends.parallels_vm.AgentEndpoint`.
        """
        from openadapt_flow.backends.parallels_vm import AgentEndpoint  # noqa: PLC0415
        from openadapt_flow.backends.win_agent import server as agent_server  # noqa: PLC0415
        from openadapt_flow.backends.win_agent.tls import (  # noqa: PLC0415
            generate_self_signed_cert,
        )

        self.attach_console()
        self.run_ps(
            f"Stop-ScheduledTask -TaskName '{AGENT_TASK}' -ErrorAction SilentlyContinue; "
            "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "
            "'*win_agent_server.py*' } | ForEach-Object { Stop-Process -Id "
            "$_.ProcessId -Force }",
            check=False,
        )
        self.push_file(Path(agent_server.__file__), rf"{GUEST_DIR}\win_agent_server.py")
        bundle = generate_self_signed_cert([self.host])
        try:
            self.push_file(Path(bundle.certfile), rf"{GUEST_DIR}\agent-cert.pem")
            self.push_file(Path(bundle.keyfile), rf"{GUEST_DIR}\agent-key.pem")
        finally:
            shutil.rmtree(os.path.dirname(bundle.certfile), ignore_errors=True)
        pythonw = self.python[: -len("python.exe")] + "pythonw.exe"
        self.run_ps(
            self._interactive_script(
                pythonw,
                f'"{GUEST_DIR}\\win_agent_server.py" --host 0.0.0.0 '
                f"--port {port} --token {token} "
                f'--certfile "{GUEST_DIR}\\agent-cert.pem" --keyfile "{GUEST_DIR}\\agent-key.pem"',
                AGENT_TASK,
            )
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
            time.sleep(1.0)
        raise GuestError(f"win_agent did not come up at {endpoint.url}")
