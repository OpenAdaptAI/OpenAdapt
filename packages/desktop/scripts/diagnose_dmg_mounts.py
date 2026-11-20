"""Read-only, failure-time observations of this build's scoped DMG resources.

Path matching is not exclusive ownership or cleanup authority. No file contents,
process arguments, environment, detach, kill or retry are collected/performed.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import selectors
import subprocess
import time
from pathlib import Path

INFO = ["/usr/bin/hdiutil", "info", "-plist"]
LSOF = ["/usr/sbin/lsof", "-nP", "-Fpcu"]
BUDGET_SECONDS = 8.0
# The parser checks this defensively; probe also enforces it while reading.
MAX_BYTES = 1024 * 1024


class Incomplete(ValueError):
    pass


def snapshot(raw: bytes, root: Path) -> list[dict]:
    if len(raw) > MAX_BYTES:
        raise Incomplete("oversized_metadata")
    try:
        document = plistlib.loads(raw)
    except Exception as exc:
        raise Incomplete("invalid_metadata") from exc
    if not isinstance(document, dict) or not isinstance(document.get("images"), list):
        raise Incomplete("invalid_metadata")
    found = []
    for image in document["images"]:
        if not isinstance(image, dict):
            raise Incomplete("invalid_metadata")
        path = image.get("image-path")
        if not isinstance(path, str) or not Path(path).is_absolute():
            continue
        path = Path(path)
        match = re.fullmatch(r"rw\.([0-9]+)\..+\.dmg", path.name)
        # Reject unrelated paths before any filesystem/symlink lookup.
        if path.parent != root or not match or path.is_symlink():
            continue
        path = path.resolve()
        if path.parent != root:
            continue
        entities = image.get("system-entities")
        if not isinstance(entities, list) or len(entities) > 8:
            raise Incomplete("invalid_scoped_entities")
        mounts = []
        for entity in entities:
            if not isinstance(entity, dict):
                raise Incomplete("invalid_scoped_entities")
            device = entity.get("dev-entry")
            mount = entity.get("mount-point")
            if not isinstance(device, str) or not re.fullmatch(
                r"/dev/disk[0-9]+(?:s[0-9]+)*", device
            ):
                raise Incomplete("invalid_scoped_device")
            if mount is not None:
                if not isinstance(mount, str) or not re.fullmatch(
                    r"/Volumes/dmg\.[A-Za-z0-9_-]+", mount
                ):
                    raise Incomplete("invalid_scoped_mount")
                mounts.append((device, mount))
        found.append({"path": str(path), "pid_hint": int(match[1]), "mounts": sorted(mounts)})
    found.sort(key=lambda item: item["path"])
    if len(found) > 4 or len({item["path"] for item in found}) != len(found):
        raise Incomplete("ambiguous_or_excessive_images")
    return found


def processes(raw: bytes) -> list[dict]:
    if len(raw) > MAX_BYTES:
        raise Incomplete("oversized_handles")
    result = []
    current = {}
    for line in raw.decode("utf-8", errors="strict").splitlines():
        field, value = line[:1], line[1:]
        if field == "p":
            if current:
                result.append(current)
            if not value.isascii() or not value.isdecimal():
                raise Incomplete("invalid_handle_pid")
            current = {"pid": int(value)}
        elif field == "c" and current:
            if not re.fullmatch(r"[A-Za-z0-9_.+ -]{1,64}", value):
                raise Incomplete("invalid_handle_command")
            if "command" in current:
                raise Incomplete("duplicate_handle_field")
            current["command"] = value
        elif field == "u" and current:
            if not value.isascii() or not value.isdecimal():
                raise Incomplete("invalid_handle_uid")
            if "uid" in current:
                raise Incomplete("duplicate_handle_field")
            current["uid"] = int(value)
        # File names, arguments and all other fields are deliberately discarded.
    if current:
        result.append(current)
    if (
        not result
        or len(result) > 256
        or len({item["pid"] for item in result}) != len(result)
        or any(set(item) != {"pid", "command", "uid"} for item in result)
    ):
        raise Incomplete("incomplete_handle_metadata")
    return result


def collect(root: Path, *, run, clock=time.monotonic) -> dict:
    root = root.resolve()
    report = {
        "schema_version": 1,
        "outcome": "incomplete",
        "cleanup_authorized": False,
        "attribution": "path_scoped_observation_not_exclusive_ownership",
        "images": [],
    }
    deadline = clock() + BUDGET_SECONDS

    def query(argv):
        remaining = deadline - clock()
        if remaining <= 0:
            raise Incomplete("diagnostic_budget_exhausted")
        done = run(argv, timeout=min(3.0, remaining))
        if done.returncode != 0:
            # lsof1 can mean no matches OR unavailable observations. Never assert no holder.
            raise Incomplete("probe_unavailable_or_no_result")
        return done.stdout

    try:
        before = snapshot(query(INFO), root)
        observations = []
        for image in before:
            handles = [
                {
                    "scope": "backing_image",
                    "processes": processes(query([*LSOF, "--", image["path"]])),
                }
            ]
            for device, mount in image["mounts"]:
                handles.append(
                    {
                        "scope": "mounted_filesystem",
                        "device": device,
                        "processes": processes(query([*LSOF, "+f", "--", mount])),
                    }
                )
            observations.append(
                {
                    "image": Path(image["path"]).name,
                    "filename_pid_hint": image["pid_hint"],
                    "handles": handles,
                }
            )
        after = snapshot(query(INFO), root)
        if after != before:
            raise Incomplete("resource_identity_changed")
        if clock() > deadline:
            raise Incomplete("diagnostic_budget_exhausted")
        report.update(outcome="observed", images=observations)
    except (Incomplete, OSError, TypeError, ValueError, subprocess.SubprocessError) as exc:
        report["reason"] = (
            str(exc) if isinstance(exc, Incomplete) else "probe_or_metadata_unavailable"
        )
        # Drop all tentative process attribution on an unstable/incomplete read.
    return report


def probe(argv, *, timeout):
    """Bound bytes while reading; terminate/reap only the query child we start.

    This POSIX reader is for the macOS-only diagnostic step. No observed holder
    PID, process group or unrelated process is ever used for cleanup. Reserve
    part of the supplied query budget for owned-child termination/reaping.
    """
    if os.name != "posix" or timeout < 0.25 or not Path(argv[0]).is_absolute():
        raise Incomplete("query_budget_or_platform_unavailable")
    deadline = time.monotonic() + timeout
    capture_deadline = deadline - min(0.2, timeout / 4)
    child = None
    try:
        # Absolute program, explicit streams and no cwd/session/preexec options:
        # permit posix_spawn rather than forking a potentially threaded Python.
        child = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=False,
        )
        os.set_blocking(child.stdout.fileno(), False)
        captured = bytearray()
        with selectors.DefaultSelector() as ready:
            ready.register(child.stdout, selectors.EVENT_READ)
            while True:
                remaining = capture_deadline - time.monotonic()
                if remaining <= 0 or not ready.select(remaining):
                    raise Incomplete("query_deadline_exceeded")
                chunk = os.read(child.stdout.fileno(), min(65536, MAX_BYTES + 1 - len(captured)))
                if not chunk:
                    break
                if len(captured) + len(chunk) > MAX_BYTES:
                    raise Incomplete("query_output_limit")
                captured.extend(chunk)
        remaining = capture_deadline - time.monotonic()
        if remaining <= 0:
            raise Incomplete("query_deadline_exceeded")
        return subprocess.CompletedProcess(argv, child.wait(timeout=remaining), bytes(captured))
    except subprocess.TimeoutExpired:
        raise Incomplete("query_deadline_exceeded") from None
    finally:
        if child is not None:
            try:
                if child.poll() is None:
                    try:
                        child.kill()
                    except ProcessLookupError:
                        pass
                try:
                    child.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    raise Incomplete("owned_query_child_cleanup_incomplete") from None
            finally:
                child.stdout.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--bundle-root", type=Path, required=True)
    args = parser.parse_args(argv)
    report = collect(args.bundle_root, run=probe)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["outcome"] == "observed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
