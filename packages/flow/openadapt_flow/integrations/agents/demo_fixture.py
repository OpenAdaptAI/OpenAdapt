"""Data-driven disposable demo setup; canonical wrap owns agent execution."""

from __future__ import annotations

import base64
import json
from importlib.resources import files as resource_files
import re
import sqlite3
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PROJECT = re.compile(r"oawrapdemo-[a-z0-9][a-z0-9-]{0,35}")
_PACK_KEYS = {
    "format",
    "name",
    "vendor",
    "image",
    "container_port",
    "data_mount",
    "database_file",
    "read_table",
    "parameter_columns",
    "generated_fields",
    "canary_parameters",
    "record_scope_column",
    "scope_collection",
    "scope_key_column",
    "scope_name_column",
    "bootstrap",
    "instruction",
    "project_name",
    "environment",
    "volumes",
    "case_file",
    "source_refs",
    "status",
    "task_id",
}


@dataclass(frozen=True)
class FixtureFiles:
    root: Path
    project: str
    compose: Path
    database: Path
    url: str


def packaged_pack(name: str) -> Path:
    """Resolve one installed demo DATA pack without a source checkout."""
    if not isinstance(name, str) or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", name) is None:
        raise ValueError("INVALID_DEMO_PACK_NAME")
    path = Path(str(resource_files("openadapt_flow.integrations.agents").joinpath("demo_packs", name + ".json")))
    if not path.is_file() or path.is_symlink():
        raise ValueError("DEMO_PACK_UNAVAILABLE")
    return path


def load_pack(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    spec = json.loads(path.read_bytes())
    if (
        not isinstance(spec, dict)
        or set(spec) != _PACK_KEYS
        or spec["format"] != "openadapt.demo-fixture/v1"
    ):
        raise ValueError("INVALID_DEMO_PACK")
    if not re.fullmatch(
        r"[a-z0-9./_-]+:[A-Za-z0-9._-]+@sha256:[0-9a-f]{64}", spec["image"]
    ):
        raise ValueError("UNPINNED_DEMO_IMAGE")
    if (
        not isinstance(spec["container_port"], int)
        or isinstance(spec["container_port"], bool)
        or not 1 <= spec["container_port"] <= 65535
    ):
        raise ValueError("INVALID_DEMO_PORT")
    if not all(
        isinstance(spec[k], str) and _IDENTIFIER.fullmatch(spec[k])
        for k in (
            "read_table",
            "record_scope_column",
            "scope_collection",
            "scope_key_column",
            "scope_name_column",
        )
    ):
        raise ValueError("INVALID_DEMO_COLLECTION")
    if not isinstance(spec["project_name"], str) or not spec["project_name"].strip():
        raise ValueError("INVALID_DEMO_SCOPE_NAME")
    columns = spec["parameter_columns"]
    if (
        not isinstance(columns, list)
        or not columns
        or len(columns) != len(set(columns))
        or not all(isinstance(x, str) and _IDENTIFIER.fullmatch(x) for x in columns)
    ):
        raise ValueError("INVALID_DEMO_PARAMETERS")
    generated = spec["generated_fields"]
    if (
        not isinstance(generated, list)
        or len(generated) != len(set(generated))
        or not all(isinstance(n, str) and _IDENTIFIER.fullmatch(n) for n in generated)
        or set(generated).intersection([*columns, spec["record_scope_column"]])
    ):
        raise ValueError("INVALID_DEMO_GENERATED_FIELDS")
    filename = spec["database_file"]
    case_file = spec["case_file"]
    if not all(
        isinstance(n, str)
        and Path(n).name == n
        and n not in (".", "..")
        and "/" not in n
        and "\\" not in n
        for n in (filename, case_file)
    ):
        raise ValueError("INVALID_DEMO_FILENAME")
    document = json.loads((path.parent / case_file).read_bytes())
    if (
        not isinstance(document, dict)
        or set(document) != {"format", "family", "cases"}
        or document["format"] != "openadapt.demo-cases/v1"
    ):
        raise ValueError("INVALID_DEMO_CASES")
    rows = document["cases"]
    if not isinstance(rows, list) or len(rows) != 20:
        raise ValueError("INVALID_DEMO_DENOMINATOR")
    seen = set()
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != {"case_id", "parameters"}
            or not isinstance(row["case_id"], str)
            or not row["case_id"]
            or row["case_id"] in seen
        ):
            raise ValueError("INVALID_DEMO_CASE_ID")
        seen.add(row["case_id"])
        values = row["parameters"]
        if (
            not isinstance(values, dict)
            or set(values) != set(columns)
            or not all(isinstance(v, str) and v for v in values.values())
        ):
            raise ValueError("INVALID_DEMO_CASE_PARAMETERS")
    canaries = spec["canary_parameters"]
    if (
        not isinstance(canaries, list)
        or len(canaries) < 2
        or not all(
            isinstance(row, dict)
            and set(row) == set(columns)
            and all(isinstance(v, str) and v for v in row.values())
            for row in canaries
        )
    ):
        raise ValueError("INVALID_DEMO_CANARIES")
    measured = {tuple(row["parameters"][column] for column in columns) for row in rows}
    checks = [tuple(row[column] for column in columns) for row in canaries]
    if len(checks) != len(set(checks)) or measured.intersection(checks):
        raise ValueError("DEMO_CANARY_WORKLOAD_COLLISION")
    return spec, rows


def prepare(
    spec: dict[str, Any], root: Path, *, project: str, port: int
) -> FixtureFiles:
    if (
        not _PROJECT.fullmatch(project)
        or isinstance(port, bool)
        or not 1024 <= port <= 65535
    ):
        raise ValueError("INVALID_DEMO_ENDPOINT")
    if (
        not root.is_absolute()
        or root.exists()
        or root.is_symlink()
        or any(p.is_symlink() for p in root.parents)
    ):
        raise ValueError("DEMO_STATE_MUST_BE_FRESH")
    root.mkdir(mode=0o700)
    data = root / "data"
    data.mkdir(mode=0o755)
    service: dict[str, Any] = {
        "image": spec["image"],
        "restart": "no",
        "ports": [f"127.0.0.1:{port}:{spec['container_port']}"],
        "environment": spec["environment"],
        "volumes": [
            {"type": "bind", "source": str(data), "target": spec["data_mount"]}
        ],
        "cpus": 1,
        "mem_limit": "512m",
    }
    for name, target in spec["volumes"].items():
        service["volumes"].append({"type": "volume", "source": name, "target": target})
    compose = root / "compose.json"
    compose.write_text(
        json.dumps(
            {
                "services": {"app": service},
                "volumes": {name: {} for name in spec["volumes"]},
            },
            indent=2,
        )
        + "\n"
    )
    return FixtureFiles(
        root, project, compose, data / spec["database_file"], f"http://127.0.0.1:{port}"
    )


def compose_argv(files: FixtureFiles, operation: str) -> list[str]:
    operations = {
        "up": ["up", "-d", "--wait", "--wait-timeout", "55"],
        "stop": ["stop", "--timeout", "10"],
        "down": ["down", "--timeout", "10"],
    }
    if operation not in operations:
        raise ValueError("UNSUPPORTED_DEMO_OPERATION")
    return [
        "docker",
        "compose",
        "--project-name",
        files.project,
        "--file",
        str(files.compose),
        *operations[operation],
    ]


def up(files: FixtureFiles) -> dict[str, Any]:
    started = time.monotonic()
    result: dict[str, Any] = {
        "status": "UNAVAILABLE",
        "exit": None,
        "timed_out": False,
        "seconds": None,
    }
    try:
        child = subprocess.run(
            compose_argv(files, "up"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
            check=False,
        )
        result["exit"] = child.returncode
        result["status"] = "STARTED" if child.returncode == 0 else "UNAVAILABLE"
    except subprocess.TimeoutExpired:
        result["timed_out"] = True
    finally:
        result["seconds"] = time.monotonic() - started
        result["descendant_drain"] = "UNKNOWN"
        (files.root / "startup.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def generate_task(
    spec: dict[str, Any], rows: list[dict[str, Any]], files: FixtureFiles
) -> dict[str, Any]:
    """Generate the READ projection from a real local SQLite schema observation."""
    if set(spec["generated_fields"]).intersection(
        [*spec["parameter_columns"], spec["record_scope_column"]]
    ):
        raise ValueError("INVALID_DEMO_GENERATED_FIELDS")
    database = files.database
    if not database.is_file() or database.is_symlink():
        raise ValueError("DEMO_DATABASE_UNAVAILABLE")
    name = spec["read_table"]
    quoted = '"' + name.replace('"', '""') + '"'
    with sqlite3.connect(
        database.as_uri() + "?mode=ro", uri=True, timeout=2
    ) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        info = connection.execute(f"PRAGMA table_info({quoted})").fetchall()
        names = [str(row[1]) for row in info]
        if (
            not names
            or len(names) != len(set(names))
            or not set(
                spec["parameter_columns"]
                + spec["generated_fields"]
                + [spec["record_scope_column"]]
            ).issubset(names)
        ):
            raise ValueError("DEMO_SCHEMA_UNAVAILABLE")
        query = (
            "SELECT "
            + ", ".join('"' + column.replace('"', '""') + '"' for column in names)
            + " FROM "
            + quoted
        )
        connection.execute(query + " LIMIT 0")
        scope = '"' + spec["scope_collection"] + '"'
        key = '"' + spec["scope_key_column"] + '"'
        field = '"' + spec["scope_name_column"] + '"'
        records = connection.execute(
            f"SELECT {key} FROM {scope} WHERE {field} = ?", (spec["project_name"],)
        ).fetchall()
        if (
            len(records) != 1
            or isinstance(records[0][0], bool)
            or not isinstance(records[0][0], int)
            or records[0][0] < 1
        ):
            raise ValueError("DEMO_RECORD_SCOPE_UNAVAILABLE")
        project_id = records[0][0]
    task = {
        "instruction": spec["instruction"],
        "parameters": spec["parameter_columns"],
        "url": files.url,
        "verifier": {
            "kind": "sql",
            "sqlite_database": str(database),
            "sql_query": query,
            "records_drop_fields": spec["generated_fields"],
        },
        "expect": {spec["record_scope_column"]: str(project_id)},
        "on_screen": [spec["project_name"]],
        "canary_params": spec["canary_parameters"],
        "environment": spec["vendor"]["release"] + ":" + spec["vendor"]["revision"],
        "task_id": spec["task_id"],
        "cache_dir": str(files.root / "cache"),
        "headless": True,
        "allow_origins": [files.url],
    }
    (files.root / "TASK.json").write_text(json.dumps(task, indent=2) + "\n")
    (files.root / "schema-observation.json").write_text(
        json.dumps(
            {
                "status": "RUNTIME_SCHEMA_OBSERVED",
                "table": name,
                "columns": names,
                "task_read_generated": True,
                "privacy_or_admission_proof": False,
            },
            indent=2,
        )
        + "\n"
    )
    return task


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def bootstrap(
    files: FixtureFiles, spec: dict[str, Any], *, auth: tuple[str, str]
) -> None:
    """Invoke the declared vendor setup API on the owned local instance only."""
    action = spec["bootstrap"]
    if (
        not isinstance(action, dict)
        or set(action) != {"path", "method", "parameters"}
        or not isinstance(action["path"], str)
        or not action["path"].startswith("/")
        or any(c in action["path"] for c in ("?", "#", "\\"))
        or ".." in action["path"].split("/")
    ):
        raise ValueError("INVALID_DEMO_BOOTSTRAP")
    if re.fullmatch(r"http://127\.0\.0\.1:[1-9][0-9]{3,4}", files.url) is None:
        raise ValueError("DEMO_BOOTSTRAP_REQUIRES_OWN_LOOPBACK")
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "method": action["method"],
            "params": action["parameters"],
            "id": 1,
        }
    ).encode()
    token = base64.b64encode((auth[0] + ":" + auth[1]).encode()).decode()
    request = urllib.request.Request(
        files.url + action["path"],
        data=body,
        headers={"Content-Type": "application/json", "Authorization": "Basic " + token},
        method="POST",
    )
    try:
        with urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect()
        ).open(request, timeout=5) as response:
            raw = response.read(65537)
        if len(raw) > 65536:
            raise ValueError("DEMO_BOOTSTRAP_UNAVAILABLE")
        result = json.loads(raw)
        if (
            not isinstance(result, dict)
            or result.get("id") != 1
            or result.get("error") is not None
            or isinstance(result.get("result"), bool)
            or not isinstance(result.get("result"), int)
            or result["result"] < 1
        ):
            raise ValueError("DEMO_BOOTSTRAP_UNAVAILABLE")
    except Exception:  # noqa: BLE001 - fixed boundary withholds URL/auth/response context
        raise ValueError("DEMO_BOOTSTRAP_UNAVAILABLE") from None
