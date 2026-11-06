"""Test-only FreeRDP settings probe, isolated from the campaign process.

The child loads only the installed parser libraries. It creates no client,
connection, display or input context. Library ABI failures stay in the child.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

SCHEMA = "openadapt.freerdp-codec-parser/v1"
CODECS = ("SupportGraphicsPipeline", "RemoteFxCodec", "NSCodec")
OPTION = re.compile(
    r"(?:/size:[0-9]{3,5}x[0-9]{3,5}|/bpp:(?:16|24|32)"
    r"|/scale:(?:100|140|180)|/scale-desktop:[0-9]{3}"
    r"|/kbd:layout:0x[0-9a-fA-F]{1,8}|/gfx:AVC444:on"
    r"|[+-](?:gfx|rfx|nsc|fonts|wallpaper|themes|clipboard))\Z"
)


class ProfilePreflightError(RuntimeError):
    def __init__(self, reason: str, returncode: int | None = None):
        self.reason = reason
        self.returncode = returncode
        super().__init__("freerdp_codec_preflight_" + reason)


def _profile_json(args: list[str]) -> str:
    if (
        not isinstance(args, list)
        or not 1 <= len(args) <= 24
        or any(not isinstance(arg, str) or not OPTION.fullmatch(arg) for arg in args)
        or sum(arg.startswith("/bpp:") for arg in args) != 1
    ):
        raise ValueError("invalid fixed profile arguments")
    return json.dumps(args, separators=(",", ":"), ensure_ascii=True)


def assert_lossless_profile(args: list[str], *, cwd: Path) -> dict[str, Any]:
    payload = _profile_json(args)
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-S", str(Path(__file__).resolve()), payload],
            cwd=cwd,
            env={"PATH": os.defpath, "HOME": str(cwd), "LC_ALL": "C"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ProfilePreflightError("timeout") from None
    except OSError:
        raise ProfilePreflightError("unavailable") from None
    if result.returncode != 0:
        raise ProfilePreflightError("child_exit", result.returncode)
    try:
        if not isinstance(result.stdout, bytes) or not 0 < len(result.stdout) <= 4096:
            raise ValueError("invalid output bound")
        record = json.loads(result.stdout)
        if (
            not isinstance(record, dict)
            or set(record)
            != {"schema", "profile_sha256", "version", "parse_status", "codecs"}
            or record["schema"] != SCHEMA
            or record["profile_sha256"] != hashlib.sha256(payload.encode()).hexdigest()
            or not isinstance(record["version"], str)
            or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", record["version"]) is None
            or type(record["parse_status"]) is not int
            or not isinstance(record["codecs"], dict)
            or set(record["codecs"]) != set(CODECS)
            or any(type(value) is not bool for value in record["codecs"].values())
        ):
            raise ValueError("invalid fixed parser proof")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ProfilePreflightError("invalid_proof") from None
    if record["parse_status"] != 0:
        raise ProfilePreflightError("parse_failed")
    if any(record["codecs"].values()):
        raise ProfilePreflightError("codec_enabled")
    return record


def _parse_in_child(args: list[str]) -> dict[str, Any]:
    import ctypes as C
    import resource

    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if sys.platform != "linux" or C.sizeof(C.c_void_p) != 8:
        raise ValueError("unsupported parser ABI")
    payload = _profile_json(args)
    core = C.CDLL("libfreerdp3.so.3")
    client = C.CDLL("libfreerdp-client3.so.3")
    core.freerdp_get_version_string.argtypes = []
    core.freerdp_get_version_string.restype = C.c_char_p
    core.freerdp_settings_new.argtypes = [C.c_uint32]
    core.freerdp_settings_new.restype = C.c_void_p
    core.freerdp_settings_free.argtypes = [C.c_void_p]
    core.freerdp_settings_free.restype = None
    core.freerdp_settings_get_key_for_name.argtypes = [C.c_char_p]
    core.freerdp_settings_get_key_for_name.restype = C.c_ssize_t
    core.freerdp_settings_get_bool.argtypes = [C.c_void_p, C.c_int]
    core.freerdp_settings_get_bool.restype = C.c_int
    parse = client.freerdp_client_settings_parse_command_line_arguments
    parse.argtypes = [C.c_void_p, C.c_int, C.POINTER(C.c_char_p), C.c_int]
    parse.restype = C.c_int
    keys = {
        name: core.freerdp_settings_get_key_for_name(("FreeRDP_" + name).encode())
        for name in CODECS
    }
    if any(not 0 <= key <= 2**31 - 1 for key in keys.values()):
        raise ValueError("required parser key unavailable")
    version = core.freerdp_get_version_string()
    if version is None:
        raise ValueError("parser version unavailable")
    version_text = version.decode("ascii")
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version_text) is None:
        raise ValueError("unsupported parser version")
    argv = ["xfreerdp3", "/v:127.0.0.1", *args]
    buffers = [C.create_string_buffer(value.encode()) for value in argv]
    array = (C.c_char_p * (len(argv) + 1))(
        *[C.cast(value, C.c_char_p) for value in buffers], None
    )
    settings = core.freerdp_settings_new(0)
    if not settings:
        raise ValueError("settings allocation unavailable")
    try:
        status = parse(settings, len(argv), array, 0)
        codecs = {
            name: core.freerdp_settings_get_bool(settings, key)
            for name, key in keys.items()
        }
        if any(value not in (0, 1) for value in codecs.values()):
            raise ValueError("invalid native boolean")
        return {
            "schema": SCHEMA,
            "profile_sha256": hashlib.sha256(payload.encode()).hexdigest(),
            "version": version_text,
            "parse_status": status,
            "codecs": {name: bool(value) for name, value in codecs.items()},
        }
    finally:
        core.freerdp_settings_free(settings)


def main() -> int:
    try:
        if len(sys.argv) != 2:
            return 3
        result = _parse_in_child(json.loads(sys.argv[1]))
        print(json.dumps(result, separators=(",", ":")), flush=True)
        return 0
    except Exception:
        # No raw native errors, argv, environment or library paths in evidence.
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
