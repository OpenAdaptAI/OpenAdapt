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
import subprocess
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
_SAFE = re.compile(r"[a-z][a-z0-9_]{0,31}")
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
# Processes of the desktop account that stay across a reset: its shell. The
# in-guest agent stays too (matched by its command line). Everything else the
# account runs in its desktop session (the app, a save still writing, any
# window the last episode opened) is stopped. System processes in that
# session (winlogon, csrss, dwm, fontdrvhost) are owned by other accounts and
# never touched.
_KEEP = (
    "explorer",
    "sihost",
    "ctfmon",
    "svchost",
    "taskhostw",
    "runtimebroker",
    "searchhost",
    "startmenuexperiencehost",
    "shellexperiencehost",
    "textinputhost",
    "rdpclip",
    "securityhealthsystray",
    "widgets",
    "widgetservice",
    "lockapp",
    "applicationframehost",
    "dllhost",
    "backgroundtaskhost",
    "crossdeviceresume",
    "phoneexperiencehost",
    "msedgewebview2",
    "onedrive",
    "useroobebroker",
    "smartscreen",
)


def _stop_desktop_processes(user: str) -> str:
    """PowerShell that stops the desktop account's own processes but its shell."""
    short = _ps_quote(user.split("\\")[-1])
    return (
        "$keep = @(" + ",".join(f"'{n}'" for n in _KEEP) + ")\n"
        f"$line = (query session '{short}' 2>$null | Select-Object -Skip 1 | Select-Object -First 1)\n"
        "if ($line -and $line -match '\\s(\\d+)\\s+(Active|Disc)') {\n"
        "  $sid = [int]$Matches[1]\n"
        "  $agent = @(Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "
        "'*win_agent_server.py*' } | ForEach-Object { $_.ProcessId })\n"
        "  Get-Process -IncludeUserName | Where-Object { $_.SessionId -eq $sid -and $_.UserName "
        f"-and $_.UserName.Split('\\')[-1] -eq '{short}' -and $keep -notcontains "
        "$_.ProcessName.ToLower() -and $agent -notcontains $_.Id } | "
        "ForEach-Object { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }\n"
        "}\n"
        # A save launched from another session, or still exiting.
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -like '*pack_db.py*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }\n"
        "Start-Sleep -Milliseconds 300\n"
    )


# The pack's own processes only: its apps, and any save still writing.
_STOP_PACK_PROCESSES = (
    "Get-CimInstance Win32_Process | Where-Object { ($_.Name -eq 'powershell.exe' -and "
    "$_.CommandLine -like '*\\pack\\*.ps1*') -or ($_.Name -eq 'python.exe' -and "
    "$_.CommandLine -like '*pack_db.py*') } | "
    "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }\n"
    "Start-Sleep -Milliseconds 300\n"
)
# Over SSH: UTF-8 output, no progress records, and exit 1 when the script's
# last statement failed (as ``powershell -Command`` does).
_SSH_PRELUDE = (
    "$ProgressPreference='SilentlyContinue'\n"
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8\n"
)
_SSH_EPILOGUE = "\nif (-not $?) { exit 1 }\n"
# Room left on a cmd.exe command line for an encoded script.
_CMD_LINE_ROOM = 7800
# Reads "<length>\n<script>" from stdin, exactly <length> UTF-16 units, and
# runs the script.
_SSH_READER = base64.b64encode(
    (
        "[Console]::InputEncoding=[Text.Encoding]::UTF8; "
        "$n=[int][Console]::In.ReadLine(); $b=New-Object char[] $n; $r=0; "
        "while ($r -lt $n) { $k=[Console]::In.Read($b,$r,$n-$r); if ($k -le 0) { break }; $r+=$k }; "
        "& ([scriptblock]::Create(-join $b))"
    ).encode("utf-16-le")
).decode("ascii")

# Every oracle-side script starts here: no profile scripts (run_ps passes
# -NoProfile) and only the system's own modules, so nothing the desktop
# account can write is loaded into the elevated WinRM shell.
_PREAMBLE = (
    '$env:PSModulePath = "$env:windir\\system32\\WindowsPowerShell\\v1.0\\Modules"\n'
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


def _setting(key: str, *, required: bool = False) -> Optional[str]:
    """``OPENADAPT_WINENV_<KEY>``, else the lab profile's ``OAFLOW_<PROFILE>_<KEY>``.

    The lab profile is the one ``OAFLOW_LAB_PROFILE`` names (default
    ``winlab``), the same variables the lab harnesses read.
    """
    profile = os.environ.get("OAFLOW_LAB_PROFILE", "winlab").upper()
    for name in (f"OPENADAPT_WINENV_{key}", f"OAFLOW_{profile}_{key}"):
        value = os.environ.get(name)
        if value:
            return value
    if required:
        raise GuestError(
            f"set OPENADAPT_WINENV_{key} (the Windows guest is not configured)"
        )
    return None


# PowerShell also ends a single-quoted string at these.
_QUOTES = ("'", "\u2018", "\u2019", "\u201a", "\u201b")


def _ps_quote(value: str) -> str:
    for quote in _QUOTES:
        value = value.replace(quote, quote * 2)
    return value


def _safe(name: str, value: str) -> str:
    if not isinstance(value, str) or not _SAFE.fullmatch(value):
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
        ssh_key: Optional[str] = None,
    ) -> None:
        self.host = host or _setting("HOST", required=True)
        self.user = user or _setting("USER", required=True)
        key = ssh_key or _setting("SSH_KEY")
        # SSH with a key when one is configured (no password handled), else
        # WinRM over HTTP with NTLM message encryption.
        self._ssh_key: Optional[str] = os.path.expanduser(key) if key else None
        secret = password or _setting("PASSWORD", required=self._ssh_key is None)
        # The account signed in to the desktop (apps, agent, RDP). A separate
        # standard account is safest; by default it is the control account.
        self.desktop_user = os.environ.get("OPENADAPT_WINENV_DESKTOP_USER") or self.user
        self._desktop_secret = (
            os.environ.get("OPENADAPT_WINENV_DESKTOP_PASSWORD")
            if self.desktop_user != self.user
            else secret
        )
        self._session: Any = None
        if self._ssh_key is None:
            import winrm  # noqa: PLC0415 - optional dependency

            self._session = winrm.Session(
                f"http://{self.host}:{port}/wsman",
                auth=(self.user, secret),
                transport="ntlm",
                message_encryption="always",
            )
        configured = (
            python
            or _setting("PYTHON")
            or r"C:\Program Files\Python312-arm64\python.exe"
        )
        # A lab profile may name pythonw.exe; the oracle wants the console one.
        self.python = re.sub(r"pythonw\.exe$", "python.exe", configured, flags=re.I)
        #: ``account``: a reset stops every process of the desktop account
        #: in its session but its shell and the agent (a dedicated machine).
        #: ``pack``: only the pack's own processes (a desktop shared with
        #: other work, which then must not cover the app).
        self.reset_scope = os.environ.get("OPENADAPT_WINENV_RESET_SCOPE", "account")
        if self.reset_scope not in ("account", "pack"):
            raise GuestError("OPENADAPT_WINENV_RESET_SCOPE is account or pack")

    def rdp_password(self) -> str:
        """The desktop account's password, for the RDP surface's sign-in only."""
        if not self._desktop_secret:
            raise GuestError(
                "set OPENADAPT_WINENV_DESKTOP_PASSWORD for the RDP surface"
            )
        return self._desktop_secret

    # -- primitives ---------------------------------------------------------
    def run_ps(self, script: str, *, check: bool = True) -> GuestResult:
        """Run ``script`` in PowerShell without profile scripts or user modules."""
        if self._session is None:
            body = _SSH_PRELUDE + _PREAMBLE + script + _SSH_EPILOGUE
            encoded = base64.b64encode(body.encode("utf-16-le")).decode("ascii")
            if len(encoded) < _CMD_LINE_ROOM:
                # Short: one command line, no stdin at all.
                remote, stdin = (
                    f"powershell -NoProfile -NonInteractive -EncodedCommand {encoded}",
                    b"",
                )
            else:
                # Long (cmd.exe caps a command line at 8191 characters): the
                # script goes on stdin after its length, so the reader stops
                # at the last character and never waits for end-of-input,
                # which Windows OpenSSH does not always deliver.
                units = len(body.encode("utf-16-le")) // 2
                remote = f"powershell -NoProfile -NonInteractive -EncodedCommand {_SSH_READER}"
                stdin = f"{units}\n{body}".encode("utf-8")
            try:
                proc = subprocess.run(
                    [*self._ssh_base(), remote],
                    input=stdin,
                    capture_output=True,
                    timeout=180,
                )
            except subprocess.TimeoutExpired:
                # The exception's text would repeat the command line.
                raise GuestError(f"ssh to {self.host} timed out") from None
            status, stdout, stderr = (
                proc.returncode,
                proc.stdout.decode("utf-8", "replace"),
                proc.stderr.decode("utf-8", "replace"),
            )
        else:
            encoded = base64.b64encode((_PREAMBLE + script).encode("utf-16-le")).decode(
                "ascii"
            )
            raw = self._session.run_cmd(
                "powershell.exe",
                [
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-EncodedCommand",
                    encoded,
                ],
            )
            status = raw.status_code
            stdout = raw.std_out.decode("utf-8", "replace")
            stderr = raw.std_err.decode("utf-8", "replace")
        if stderr.startswith("#< CLIXML"):
            stderr = re.sub(r"<[^>]+>", " ", stderr)
        result = GuestResult(status, stdout, stderr)
        if check and result.status != 0:
            raise GuestError(
                f"guest PowerShell exited {result.status}: {result.stderr[-600:]}"
            )
        return result

    def _ssh_base(self) -> list[str]:
        assert self._ssh_key is not None
        return [
            "ssh",
            "-i",
            self._ssh_key,
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=15",
            "-p",
            str(self._via("SSH", 22)[1]),
            f"{self.user}@{self._via('SSH', 22)[0]}",
        ]

    def _via(self, channel: str, port: int) -> tuple[str, int]:
        """Where to connect for ``channel`` (``SSH``, ``AGENT``, ``RDP``).

        ``OPENADAPT_WINENV_<CHANNEL>_VIA=host:port`` sends it through a relay
        (a container that cannot reach the guest's network directly); the
        guest's own address and the default port otherwise.
        """
        via = _setting(f"{channel}_VIA")
        if not via:
            return self.host, port
        host, _, number = via.rpartition(":")
        if not host or not number.isdigit():
            raise GuestError(f"OPENADAPT_WINENV_{channel}_VIA must be host:port")
        return host, int(number)

    def rdp_address(self) -> tuple[str, int]:
        return self._via("RDP", 3389)

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
        if self._session is None:
            proc = subprocess.run(
                [
                    "scp",
                    "-q",
                    "-i",
                    str(self._ssh_key),
                    "-o",
                    "BatchMode=yes",
                    "-P",
                    str(self._via("SSH", 22)[1]),
                    str(local),
                    f"{self.user}@{self._via('SSH', 22)[0]}:/"
                    + remote.replace("\\", "/"),
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=600,
            )
            if proc.returncode != 0:
                raise GuestError(f"copy of {Path(local).name} failed")
        else:
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
$principal = New-ScheduledTaskPrincipal -UserId '{_ps_quote(self.desktop_user)}' -LogonType Interactive
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
$line = (query session '{_ps_quote(self.desktop_user.split(chr(92))[-1])}' 2>$null | Select-Object -Skip 1 | Select-Object -First 1)
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
        user = _ps_quote(self.desktop_user)
        self.run_ps(
            f"New-Item -ItemType Directory -Force '{DATA_DIR}' | Out-Null\n"
            # /remove:g drops an explicit grant an earlier setup left behind.
            f"icacls '{GUEST_DIR}' /remove:g '{user}' | Out-Null\n"
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
        # The probe writes into data (the positive control: it ran, as the
        # desktop account) and tries the code directory, which must refuse.
        probe = rf"{GUEST_DIR}\write-probe.txt"
        control = rf"{DATA_DIR}\write-probe.txt"
        # Over SSH a missing file would make this read as a failed script.
        self.run_ps(
            f"Remove-Item -Force -ErrorAction SilentlyContinue '{probe}','{control}'",
            check=False,
        )
        self.run_ps(
            self._interactive_script(
                "powershell.exe",
                f"-NoProfile -WindowStyle Hidden -Command Set-Content -Path '{control}' "
                f"-Value probe; Set-Content -Path '{probe}' -Value probe",
                "OAEnvProbe",
            )
            + f"$deadline = (Get-Date).AddSeconds(30)\n"
            f"while (-not (Test-Path '{control}') -and (Get-Date) -lt $deadline) "
            "{ Start-Sleep -Milliseconds 500 }\n"
            "Start-Sleep -Seconds 1\n"
            "Unregister-ScheduledTask -TaskName OAEnvProbe -Confirm:$false\n"
        )
        ran = self.run_ps(f"Test-Path '{control}'").stdout.strip() == "True"
        wrote = self.run_ps(f"Test-Path '{probe}'").stdout.strip() != "False"
        # Over SSH a missing file would make this read as a failed script.
        self.run_ps(
            f"Remove-Item -Force -ErrorAction SilentlyContinue '{probe}','{control}'",
            check=False,
        )
        if not ran:
            raise GuestError(
                "the desktop write probe did not run (is the desktop account signed in?)"
            )
        if wrote:
            raise GuestError(
                "the desktop session can write the pack's code directory (is UAC "
                "off, or the desktop session elevated?); refusing a guest where an "
                "agent could rewrite the oracle"
            )

    def _db(self, *args: str) -> Any:
        quoted = " ".join(f"'{_ps_quote(a)}'" for a in args)
        # -I: no user site-packages or .pth files, nothing from the
        # environment the desktop account controls.
        out = self.run_ps(
            f"& '{_ps_quote(self.python)}' -I '{GUEST_DIR}\\pack_db.py' {quoted}"
        )
        return last_json(out.stdout)

    def dump(self, app: str) -> dict[str, Any]:
        return self._db("dump", app)

    def attempts(self, app: str) -> list[dict[str, Any]]:
        """The saves the app asked for since the last reset (before any fault)."""
        return list(self._db("attempts", app)["attempts"])

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
            + (
                _stop_desktop_processes(self.desktop_user)
                if self.reset_scope == "account"
                else _STOP_PACK_PROCESSES
            )
            + f"$seed = & '{_ps_quote(self.python)}' -I '{GUEST_DIR}\\pack_db.py' seed '{app}' "
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
        if not isinstance(result, dict) or not result.get("digest"):
            raise GuestError("the guest did not restore the database")
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
            url="https://{}:{}".format(*self._via("AGENT", port)),
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
