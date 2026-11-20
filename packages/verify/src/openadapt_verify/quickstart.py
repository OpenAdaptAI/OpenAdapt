"""Five-minute quickstart: one agent write per read path, three outcomes.

``openadapt-verify quickstart`` starts disposable synthetic stores on this
machine, has a scripted "agent" make three writes against each one, and checks
each write through an independent read path with the canonical verifier:

- the agent saves what it said it would: ``CONFIRMED``;
- the agent saves a different value than it said: ``REFUTED``;
- the agent says it saved, but nothing reached the store: ``UNCERTAIN``.

Every result gets a signed portable write receipt (COSE_Sign1 with a local
transparency log), which is then checked against the issuer and log public
keys. The read paths are REST, SQL (SQLite always; PostgreSQL and MySQL when
you pass a disposable database) and FHIR R4.

All state (keys, ledger, receipts) goes to a temporary directory that is
removed afterwards, so the quickstart never touches ``~/.openadapt``. The
quickstart's keys are generated for the run; in a deployment you pin the
issuer and log keys you trust instead of reading them from the signer.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import sqlite3
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence
from urllib.parse import parse_qs, urlparse

#: (scenario, expected outcome). Each scenario writes different values, so
#: one scenario's refusal never holds another's write.
SCENARIOS: tuple[tuple[str, str], ...] = (
    ("correct", "CONFIRMED"),
    ("wrong value", "REFUTED"),
    ("claimed, not saved", "UNCERTAIN"),
)

#: The state directories the verifier, ledger and signers use.
_STATE_ENV = (
    "OPENADAPT_SEAL_HOME",
    "OPENADAPT_RECEIPT_HOME",
    "OPENADAPT_LEDGER_DIR",
    "OPENADAPT_VERIFIER_STATE_DIR",
    "OPENADAPT_FLOW_AT_REST_HOME",
    "OPENADAPT_DURABLE_AUTHORITY_DB",
    "OPENADAPT_FRAME_SECRET",
    "OPENADAPT_FRAME_SECRET_FILE",
    "OPENADAPT_SEAL_KEY",
    "OPENADAPT_SEAL_KEY_FILE",
    "OPENADAPT_SEAL_KEY_PASSPHRASE",
    "OPENADAPT_LOCAL_SEAL",
    "OPENADAPT_FLOW_AT_REST",
    "OPENADAPT_BUNDLE_KEY",
)

#: The environment variable a SQL read path takes its password from.
PASSWORD_ENV = "OPENADAPT_QUICKSTART_DB_PASSWORD"


@dataclass
class Row:
    """One scenario's result."""

    path: str
    scenario: str
    expected: str
    outcome: str
    receipt: str
    receipt_checked: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.outcome == self.expected and self.receipt_checked

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "scenario": self.scenario,
            "expected": self.expected,
            "outcome": self.outcome,
            "receipt": self.receipt,
            "receipt_checked": self.receipt_checked,
            "reasons": self.reasons,
            "ok": self.ok,
        }


@contextlib.contextmanager
def isolated_state(root: Path) -> Iterator[None]:
    """Point every verifier state directory and key at ``root``."""

    saved = {name: os.environ.get(name) for name in _STATE_ENV}
    try:
        for name in _STATE_ENV:
            os.environ.pop(name, None)
        for name in ("SEAL_HOME", "RECEIPT_HOME", "LEDGER_DIR", "VERIFIER_STATE_DIR"):
            os.environ["OPENADAPT_" + name] = str(root / name.lower())
        os.environ["OPENADAPT_FLOW_AT_REST_HOME"] = str(root / "at-rest")
        os.environ["OPENADAPT_DURABLE_AUTHORITY_DB"] = str(root / "authority.sqlite")
        os.environ["OPENADAPT_FRAME_SECRET"] = secrets.token_hex(32)
        os.environ["OPENADAPT_LOCAL_SEAL"] = "on"
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


# -- synthetic stores ----------------------------------------------------------


class _Server:
    """A loopback HTTP server for one synthetic store."""

    def __init__(self, handler: type[BaseHTTPRequestHandler]) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()


def _json_handler(
    get: Callable[[str, dict], tuple[int, Any]],
    post: Callable[[str, dict], tuple[int, Any]],
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            pass

        def _reply(self, status: int, payload: Any) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            self._reply(*get(url.path, parse_qs(url.query)))

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            values = json.loads(self.rfile.read(length) or b"{}")
            self._reply(*post(urlparse(self.path).path, values))

    return Handler


class ReadPath:
    """A store, an agent that writes to it, and the operator's contract."""

    name = ""

    def contract(self) -> dict[str, Any]:
        raise NotImplementedError

    def agent_write(self, values: Mapping[str, Any]) -> None:
        """The agent's own write path, separate from the verifier's read."""
        raise NotImplementedError

    def close(self) -> None:
        pass

    @staticmethod
    def effects(match: Mapping[str, str]) -> list[dict[str, Any]]:
        return [
            {
                "kind": "record_written",
                "match": {name: {"param": param} for name, param in match.items()},
                "expected_count": 1,
                "count_new_only": True,
                "timeout_s": 0.5,
            }
        ]


class SqliteReadPath(ReadPath):
    name = "sql/sqlite"

    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "store.sqlite"
        with contextlib.closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "CREATE TABLE item (id INTEGER PRIMARY KEY, code TEXT, quantity TEXT)"
            )

    def contract(self) -> dict[str, Any]:
        return {
            "schema_version": "openadapt.verify/v1",
            "verifier": {
                "kind": "sql",
                "sqlite_database": str(self.path),
                "sql_query": "SELECT id, code, quantity FROM item",
            },
            "effects": self.effects({"code": "code", "quantity": "quantity"}),
            "frame": {"identity_field": "id"},
        }

    def agent_write(self, values: Mapping[str, Any]) -> None:
        with contextlib.closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "INSERT INTO item (code, quantity) VALUES (?, ?)",
                (values["code"], values["quantity"]),
            )

    def records(self) -> list[dict[str, Any]]:
        with contextlib.closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute("SELECT * FROM item ORDER BY id")]


class RestReadPath(ReadPath):
    """A REST app over its own SQLite file: POST /items saves, GET /items reads."""

    name = "rest"

    def __init__(self, root: Path) -> None:
        self.store = SqliteReadPath(root / "rest")

        def get(path: str, _query: dict) -> tuple[int, Any]:
            if path != "/items":
                return 404, {"error": "not found"}
            return 200, {"items": self.store.records()}

        def post(path: str, values: dict) -> tuple[int, Any]:
            if path != "/items":
                return 404, {"error": "not found"}
            self.store.agent_write(values)
            return 201, {"saved": True}

        self.server = _Server(_json_handler(get, post))

    def contract(self) -> dict[str, Any]:
        return {
            "schema_version": "openadapt.verify/v1",
            "verifier": {
                "kind": "rest",
                "base_url": self.server.base,
                "records_path": "/items",
                "records_key": "items",
                # This endpoint returns every record, unpaged.
                "records_complete": True,
            },
            "effects": self.effects({"code": "code", "quantity": "quantity"}),
            "frame": {"identity_field": "id"},
        }

    def agent_write(self, values: Mapping[str, Any]) -> None:
        import httpx

        httpx.post(self.server.base + "/items", json=dict(values)).raise_for_status()

    def close(self) -> None:
        self.server.close()


class FhirReadPath(ReadPath):
    """A FHIR R4 server keeping notes as Observations for one patient."""

    name = "fhir"
    patient = "Patient/example-1"

    def __init__(self, root: Path) -> None:
        del root
        self.resources: list[dict[str, Any]] = []
        self.lock = threading.Lock()

        def get(path: str, query: dict) -> tuple[int, Any]:
            if path != "/fhir/Observation":
                return 404, {"resourceType": "OperationOutcome"}
            subject = (query.get("subject") or [None])[0]
            with self.lock:
                found = [
                    r
                    for r in self.resources
                    if subject is None or r["subject"]["reference"] == subject
                ]
            return 200, {
                "resourceType": "Bundle",
                "type": "searchset",
                "total": len(found),
                "link": [{"relation": "self", "url": self.server.base + path}],
                "entry": [
                    {
                        "fullUrl": f"{self.server.base}/fhir/Observation/{r['id']}",
                        "resource": r,
                    }
                    for r in found
                ],
            }

        def post(path: str, resource: dict) -> tuple[int, Any]:
            if path != "/fhir/Observation":
                return 404, {"resourceType": "OperationOutcome"}
            resource = {**resource, "id": uuid.uuid4().hex, "meta": {"versionId": "1"}}
            with self.lock:
                self.resources.append(resource)
            return 201, resource

        self.server = _Server(_json_handler(get, post))

    def contract(self) -> dict[str, Any]:
        return {
            "schema_version": "openadapt.verify/v1",
            "verifier": {
                "kind": "fhir",
                "base_url": self.server.base + "/fhir",
                "resource_type": "Observation",
                "search_params": {"subject": self.patient},
                "field_paths": {
                    "id": "id",
                    "patient": "subject.reference",
                    "note": "valueString",
                },
            },
            "effects": self.effects({"note": "note"}),
            "frame": {"identity_field": "id"},
        }

    def agent_write(self, values: Mapping[str, Any]) -> None:
        import httpx

        httpx.post(
            self.server.base + "/fhir/Observation",
            json={
                "resourceType": "Observation",
                "status": "final",
                "code": {"text": "Clinical note"},
                "subject": {"reference": self.patient},
                "valueString": values["note"],
            },
        ).raise_for_status()

    def close(self) -> None:
        self.server.close()


class ServerSqlReadPath(ReadPath):
    """A disposable PostgreSQL or MySQL database, read through its DB-API driver.

    ``connect_args`` are the driver's non-secret ``connect(**kwargs)``
    arguments; the password comes from :data:`PASSWORD_ENV` when it is set.
    The quickstart creates (and drops) its own table, so point it only at a
    disposable database.
    """

    def __init__(self, dialect: str, connect_args: Mapping[str, Any]) -> None:
        import importlib

        self.dialect = dialect
        self.driver = "psycopg" if dialect == "postgresql" else "pymysql"
        self.name = "sql/" + dialect
        self.connect_args = dict(connect_args)
        if dialect == "mysql":
            # The verifier refuses a MySQL read whose result charset isn't
            # utf8mb4, so the read path declares it.
            self.connect_args.setdefault("charset", "utf8mb4")
        self.module = importlib.import_module(self.driver)
        self.table = "openadapt_quickstart_item"
        key = "SERIAL" if dialect == "postgresql" else "INTEGER AUTO_INCREMENT"
        self._execute(f"DROP TABLE IF EXISTS {self.table}")
        self._execute(
            f"CREATE TABLE {self.table} (id {key} PRIMARY KEY, "
            "code VARCHAR(64), quantity VARCHAR(64))"
        )

    def _connect(self) -> Any:
        args = dict(self.connect_args)
        if os.environ.get(PASSWORD_ENV):
            args["password"] = os.environ[PASSWORD_ENV]
        return self.module.connect(**args)

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute(sql, tuple(params))
            conn.commit()
        finally:
            conn.close()

    def contract(self) -> dict[str, Any]:
        verifier: dict[str, Any] = {
            "kind": "sql",
            "sql_driver": self.driver,
            "sql_dialect": self.dialect,
            "sql_connect_args": self.connect_args,
            "sql_query": f"SELECT id, code, quantity FROM {self.table}",
        }
        if os.environ.get(PASSWORD_ENV):
            verifier["sql_password_env"] = PASSWORD_ENV
        return {
            "schema_version": "openadapt.verify/v1",
            "verifier": verifier,
            "effects": self.effects({"code": "code", "quantity": "quantity"}),
            "frame": {"identity_field": "id"},
        }

    def agent_write(self, values: Mapping[str, Any]) -> None:
        self._execute(
            f"INSERT INTO {self.table} (code, quantity) VALUES (%s, %s)",
            (values["code"], values["quantity"]),
        )

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._execute(f"DROP TABLE IF EXISTS {self.table}")


# -- the run -------------------------------------------------------------------


def _values(path: ReadPath, index: int) -> tuple[dict[str, str], dict[str, str]]:
    """(what the agent declares, what it actually saves) for one scenario."""

    if isinstance(path, FhirReadPath):
        declared = {"note": f"Follow up in {index + 2} weeks"}
        saved = {"note": f"Follow up in {index + 12} weeks"}
    else:
        declared = {"code": f"item-{index + 1}", "quantity": str(index + 3)}
        saved = {**declared, "quantity": str(index + 30)}
    return declared, saved


def _check_receipt(result: Any) -> tuple[str, bool]:
    from openadapt_flow.receipt_log import LocalLog
    from openadapt_flow.write_receipt import default_signer

    from openadapt_verify import ReceiptError, verify_receipt

    if result.write_receipt != "issued" or not result.write_receipt_path:
        return str(result.write_receipt), False
    receipt = Path(result.write_receipt_path)
    evidence = Path(str(receipt) + ".evidence.json")
    try:
        checked = verify_receipt(
            receipt.read_bytes(),
            issuer_public=default_signer().public_raw(),
            log_public=LocalLog().public_raw(),
            evidence=evidence.read_bytes() if evidence.exists() else None,
        )
    except ReceiptError as exc:
        return f"signature check failed: {exc}", False
    verdict = checked.payload.verdict
    return str(receipt), verdict == result.outcome.value


def run_path(path: ReadPath, workdir: Path) -> list[Row]:
    """Run the three scenarios against one read path."""

    from openadapt_verify import (
        Actor,
        Contract,
        WriteBlocked,
        begin_write,
        verify_write,
    )

    contract = Contract.from_dict(path.contract())
    rows = []
    for index, (scenario, expected) in enumerate(SCENARIOS):
        declared, saved = _values(path, index)
        try:
            pending = begin_write(
                contract,
                declared,
                actor=Actor("agent", "quickstart-agent"),
                workdir=workdir / path.name.replace("/", "-"),
            )
        except WriteBlocked as exc:
            # Fail closed: the store couldn't be read first, so nothing is sent.
            rows.append(
                Row(
                    path.name, scenario, expected, "NOT SENT", "none", False, [str(exc)]
                )
            )
            continue
        try:
            if scenario == "correct":
                path.agent_write(declared)
            elif scenario == "wrong value":
                path.agent_write(saved)
            # "claimed, not saved": the agent reports success, writes nothing.
        finally:
            result = verify_write(pending, write_receipt=True)
        receipt, checked = _check_receipt(result)
        rows.append(
            Row(
                path=path.name,
                scenario=scenario,
                expected=expected,
                outcome=result.outcome.value,
                receipt=receipt,
                receipt_checked=checked,
                reasons=list(result.reasons),
            )
        )
    return rows


def run(
    paths: Sequence[str] = ("rest", "sqlite", "fhir"),
    *,
    postgres: Optional[Mapping[str, Any]] = None,
    mysql: Optional[Mapping[str, Any]] = None,
) -> list[Row]:
    """Run the quickstart and return one row per (read path, scenario)."""

    with tempfile.TemporaryDirectory(prefix="openadapt-verify-quickstart-") as tmp:
        root = Path(tmp)
        with isolated_state(root / "state"):
            factories: dict[str, Callable[[], ReadPath]] = {
                "rest": lambda: RestReadPath(root),
                "sqlite": lambda: SqliteReadPath(root),
                "fhir": lambda: FhirReadPath(root),
            }
            if postgres is not None:
                factories["postgres"] = lambda: ServerSqlReadPath(
                    "postgresql", postgres
                )
            if mysql is not None:
                factories["mysql"] = lambda: ServerSqlReadPath("mysql", mysql)
            selected = list(paths)
            selected += [name for name in ("postgres", "mysql") if name in factories]
            rows: list[Row] = []
            for name in dict.fromkeys(selected):
                if name not in factories:
                    raise ValueError(f"unknown read path {name!r}")
                path = factories[name]()
                try:
                    rows += run_path(path, root / "writes")
                finally:
                    path.close()
            return rows


def render(rows: Sequence[Row]) -> str:
    lines = [f"{'read path':<16}{'agent write':<22}{'outcome':<12}signed receipt"]
    for row in rows:
        mark = "checked" if row.receipt_checked else "NOT CHECKED: " + row.receipt
        if row.outcome != row.expected:
            mark += f"  (expected {row.expected})"
        lines.append(f"{row.path:<16}{row.scenario:<22}{row.outcome:<12}{mark}")
    passed = sum(row.ok for row in rows)
    lines.append(
        f"\n{passed}/{len(rows)} writes got the expected outcome and a "
        "checked signed receipt."
    )
    return "\n".join(lines)
