"""``openadapt proxy``: OpenAdapt holds the database credentials; the agent gets
scoped MCP tools.

    openadapt proxy --postgres "$DATABASE_URL" -- claude
    openadapt proxy --sqlite app.db -- claude
    openadapt proxy --sqlite app.db                 # serve here; approve with y / u

The proxy serves a Model Context Protocol endpoint (streamable HTTP, JSON
responses) on ``127.0.0.1`` with a random per-run bearer token. It starts the
command with ``OPENADAPT_MCP_URL`` and ``OPENADAPT_MCP_TOKEN`` and with the
database URL and the ``PG*`` variables removed from its environment. For
``claude`` it also passes ``--mcp-config``. Each write goes through five
stages:

1. **Pre-read**: open a transaction and read every table.
2. **Dry-run diff**: run the statement inside it, read every table again,
   compute the row-level diff ("412 rows changed in users; 3 rows where
   users.role = admin"), and roll back.
3. **Gate**: a plan within the blast-radius limit and with no anomaly is
   approved by policy. Anything else waits for a person: ``y`` in the proxy's
   terminal, or ``openadapt plan approve`` in another. Approval binds to the plan
   digest and is used once.
4. **Execute once**: under the idempotency ledger. The statement commits only
   when its diff, read inside the same transaction, is exactly the planned
   diff. Otherwise it is rolled back.
5. **Read back and undo**: the frame is read again and compared. The undo
   images (before rows, added keys) give a generated compensating undo that is
   verified by read-back. Each stage writes a signed receipt.

The agent never receives write credentials in its environment. An agent that
runs as the same OS user with a shell can still find credentials the user
keeps elsewhere (a ``.env`` file, ``~/.pgpass``). For a hard boundary, give
the proxy credentials the agent's user can't read.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from openadapt_flow.txplan import engine
from openadapt_flow.txplan.adapters import AdapterError, SqlAdapter, tag
from openadapt_flow.txplan.engine import Ctx, TxRefused
from openadapt_flow.txplan.spec import Anomaly, SqlSystem, TxSpec
from openadapt_flow.txplan.state import State, StateError, sha256

PROTOCOL = "2025-06-18"
PATH = "/mcp"
MAX_QUERY_ROWS = 200
#: Environment variables that carry database credentials, removed from the child.
SCRUB = ("DATABASE_URL", "PGPASSWORD", "PGPASSFILE", "PGSERVICE", "PGSERVICEFILE", "PGHOST",
         "PGUSER", "PGDATABASE", "PGPORT", "MYSQL_PWD")


def child_environment(environ: Mapping[str, str], secrets_: Sequence[str]) -> dict[str, str]:
    """The command's environment: credentials and their copies removed."""
    out = {}
    for k, v in environ.items():
        if k in SCRUB or any(s and s in v for s in secrets_):
            continue
        out[k] = v
    return out


def sql_spec(*, postgres: Optional[str] = None, sqlite: Optional[str] = None,
             max_rows: int = 0, flags: Sequence[str] = ()) -> TxSpec:
    """A one-system tx spec for a database URL or a SQLite file."""
    if bool(postgres) == bool(sqlite):
        raise ValueError("give exactly one of --postgres or --sqlite")
    if postgres:
        system = SqlSystem(name="db", kind="sql", dialect="postgresql", driver="psycopg",
                           connect_args={"conninfo": postgres})
    else:
        system = SqlSystem(name="db", kind="sql", dialect="sqlite", path=str(Path(str(sqlite)).resolve()))
    anomalies = [Anomaly(label="more rows than the auto-approve limit", max_rows=max_rows)]
    for flag in flags:
        target, _, value = flag.partition("=")
        table, _, column = target.partition(".")
        if not (table and column and value):
            raise ValueError(f"--flag {flag!r}: use table.column=value")
        anomalies.append(Anomaly(label=f"rows where {table}.{column} = {value}", table=table,
                                 column=column, equals=value))
    return TxSpec(schema_version="openadapt.tx/v1", holdback_s=0, anomalies=anomalies, systems=[system])


class Proxy:
    """The tools, over one database, for one task (this proxy session)."""

    def __init__(self, ctx: Ctx, *, task: str, system: str = "db", interactive: bool = False,
                 extra: Any = None) -> None:
        self.ctx = ctx
        self.task = task
        self.system = system
        self.interactive = interactive
        #: More tools on the same endpoint, with ``tools()`` and ``call()``:
        #: the API tools (:class:`openadapt_flow.txplan.scoped.ApiTools`).
        self.extra = extra
        self.lock = threading.Lock()
        self.pending: list[str] = []
        self.applied: list[str] = []

    @property
    def adapter(self) -> SqlAdapter:
        return self.ctx.adapter(self.system)

    # tools -------------------------------------------------------------------

    def query(self, sql: str) -> dict[str, Any]:
        """A read, in a transaction that is always rolled back."""
        con = self.adapter.connect()
        try:
            cur = con.cursor()
            self.adapter._begin(cur)
            if self.adapter.system.dialect == "postgresql":
                cur.execute("SET TRANSACTION READ ONLY")
            try:
                cur.execute(sql)
                cols = [str(d[0]) for d in (cur.description or [])]
                rows = cur.fetchmany(MAX_QUERY_ROWS + 1) if cols else []
            finally:
                con.rollback()
        finally:
            con.close()
        return {
            "columns": cols,
            "rows": [[tag(v) for v in r] for r in rows[:MAX_QUERY_ROWS]],
            "truncated": len(rows) > MAX_QUERY_ROWS,
        }

    def preview(self, sql: str) -> dict[str, Any]:
        with self.lock:
            plan = engine.make_plan(self.ctx, self.task, [{"system": self.system, "op": "sql", "fields": {"sql": sql}}])
            digest = plan["digest"]
            w = plan["writes"][0]
            counts = w.get("staged_diff") or {}
            auto = not plan["anomalies"] and w["ledger"] == "free" and not w.get("ddl")
            if auto:
                engine.approve_plan(self.ctx.state, digest, approver="policy", decided_by="automation")
            else:
                self.pending.append(digest)
            text = "\n".join(engine.summary_lines(plan))
            if self.interactive and not auto:
                print("\n" + text + "\n  press y to approve, n to decline", file=sys.stderr, flush=True)
            return {
                "plan": digest[:16],
                "tables": counts,
                "ledger": w["ledger"],
                "anomalies": [a["label"] for a in plan["anomaly_details"]],
                "approval": "approved by policy" if auto else (
                    "waiting for a person (y in the proxy terminal, or `openadapt plan approve "
                    f"{digest[:16]}`)"
                ),
                "summary": text,
            }

    def apply(self, plan: str) -> dict[str, Any]:
        with self.lock:
            digest = self.ctx.state.find_plan(plan)
            out = engine.apply_plan(self.ctx, digest)
            if out["outcome"] in ("VERIFIED", "COMPLETED_UNVERIFIED"):
                self.applied.append(digest)
            return {"outcome": out["outcome"], "tx": out["tx"], "receipt": out["run_dir"]}

    def undo(self, plan: str) -> dict[str, Any]:
        with self.lock:
            digest = self.ctx.state.find_plan(plan)
            out = engine.undo_plan(self.ctx, digest)
            return {"outcome": out["outcome"], "tx": out["tx"], "receipt": out["run_dir"]}

    def tools(self) -> list[dict[str, Any]]:
        """The SQL tools when this proxy holds a database, then the extra tools."""
        out = list(TOOLS) if any(s.name == self.system for s in self.ctx.spec.systems) else []
        return out + (list(self.extra.tools()) if self.extra is not None else [])

    def call(self, name: str, args: Mapping[str, Any]) -> tuple[str, bool]:
        if self.extra is not None and name in {t["name"] for t in self.extra.tools()}:
            result: tuple[str, bool] = self.extra.call(name, args)
            return result
        try:
            if name == "sql_query":
                got: Any = self.query(str(args["sql"]))
            elif name == "sql_preview":
                got = self.preview(str(args["sql"]))
            elif name == "sql_apply":
                got = self.apply(str(args["plan"]))
            elif name == "sql_undo":
                got = self.undo(str(args["plan"]))
            else:
                return f"unknown tool {name}", True
        except TxRefused as exc:
            return json.dumps({"refused": exc.code, "detail": exc.detail}), True
        except (AdapterError, StateError, KeyError, ValueError) as exc:
            return json.dumps({"error": type(exc).__name__, "detail": str(exc)[:300]}), True
        except Exception as exc:  # noqa: BLE001 - a database error is the tool's answer
            return json.dumps({"error": type(exc).__name__, "detail": str(exc)[:300]}), True
        return json.dumps(got, default=str), False

    # the person's keys ---------------------------------------------------------

    def key(self, ch: str) -> str:
        """``y`` approves the oldest waiting plan, ``n`` declines it, ``u`` undoes
        the last applied one."""
        with self.lock:
            if ch == "y" and self.pending:
                digest = self.pending.pop(0)
                engine.approve_plan(self.ctx.state, digest, approver=os.environ.get("USER", "person"),
                                    decided_by="human")
                return f"approved {digest[:16]}"
            if ch == "n" and self.pending:
                return f"declined {self.pending.pop(0)[:16]}"
        if ch == "u" and self.applied:
            digest = self.applied.pop()
            out = engine.undo_plan(self.ctx, digest)
            return f"undo {digest[:16]}: {out['outcome']}"
        return "nothing to do"


TOOLS = [
    {"name": "sql_query", "description": "Run a read-only SQL query (rolled back; at most 200 rows).",
     "inputSchema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}},
    {"name": "sql_preview", "description": (
        "Dry-run one SQL write statement: OpenAdapt runs it in a transaction, returns the exact "
        "row-level diff and a plan id, and rolls back. Small plans are approved by policy; others "
        "wait for a person."),
     "inputSchema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}},
    {"name": "sql_apply", "description": "Apply an approved plan once; it commits only if the diff is the planned diff.",
     "inputSchema": {"type": "object", "properties": {"plan": {"type": "string"}}, "required": ["plan"]}},
    {"name": "sql_undo", "description": "Undo an applied plan and verify it by reading every table back.",
     "inputSchema": {"type": "object", "properties": {"plan": {"type": "string"}}, "required": ["plan"]}},
]


class McpServer:
    """A minimal streamable-HTTP MCP server: JSON responses, one bearer token."""

    def __init__(self, proxy: Proxy, *, port: int = 0) -> None:
        self.proxy = proxy
        self.token = secrets.token_urlsafe(32)
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:  # no access log: never log the token
                pass

            def _send(self, code: int, body: Any = None) -> None:
                data = b"" if body is None else json.dumps(body).encode()
                self.send_response(code)
                if body is not None:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _authorized(self) -> bool:
                given = self.headers.get("Authorization", "")
                return self.path.split("?")[0] == PATH and hmac.compare_digest(
                    given.encode(), f"Bearer {outer.token}".encode()
                )

            def do_GET(self) -> None:  # noqa: N802
                self._send(405 if self._authorized() else 401)

            def do_DELETE(self) -> None:  # noqa: N802
                self._send(405 if self._authorized() else 401)

            def do_POST(self) -> None:  # noqa: N802
                if self.path.split("?")[0] != PATH:
                    return self._send(404)
                if not self._authorized():
                    return self._send(401)
                try:
                    msg = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                except ValueError:
                    return self._send(400, {"jsonrpc": "2.0", "id": None,
                                            "error": {"code": -32700, "message": "parse error"}})
                if isinstance(msg, list):
                    replies = [r for r in (outer.handle(m) for m in msg) if r is not None]
                    return self._send(200, replies) if replies else self._send(202)
                reply = outer.handle(msg)
                return self._send(202) if reply is None else self._send(200, reply)

        self.http = ThreadingHTTPServer(("127.0.0.1", port), H)
        self.url = f"http://127.0.0.1:{self.http.server_address[1]}{PATH}"

    def handle(self, msg: Mapping[str, Any]) -> Optional[dict[str, Any]]:
        mid = msg.get("id")
        method = msg.get("method")
        if mid is None:
            return None  # a notification
        if method == "initialize":
            asked = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL
            result: Any = {"protocolVersion": asked, "capabilities": {"tools": {}},
                           "serverInfo": {"name": "openadapt-proxy", "version": "1"}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": self.proxy.tools()}
        elif method == "tools/call":
            params = msg.get("params") or {}
            text, is_error = self.proxy.call(str(params.get("name")), params.get("arguments") or {})
            result = {"content": [{"type": "text", "text": text}], "isError": is_error}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def start(self) -> None:
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.http.shutdown()
        self.http.server_close()


def mcp_config(server: McpServer, folder: Path) -> Path:
    path = folder / "openadapt-mcp.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"mcpServers": {"openadapt": {
            "type": "http", "url": server.url,
            "headers": {"Authorization": f"Bearer {server.token}"},
        }}}, f)
    return path


def run(argv_command: Sequence[str], *, spec: TxSpec, state: State, task: str,
        secrets_: Sequence[str], port: int = 0, api_spec: Optional[TxSpec] = None) -> int:
    ctx = Ctx(spec=spec, state=state)
    extra = None
    if api_spec is not None:
        from openadapt_flow.txplan.scoped import ApiTools

        extra = ApiTools(Ctx(spec=api_spec, state=state), task=task)
    proxy = Proxy(ctx, task=task, interactive=not argv_command, extra=extra)
    server = McpServer(proxy, port=port)
    server.start()
    tmp = Path(tempfile.mkdtemp(prefix="openadapt-proxy-"))
    try:
        config = mcp_config(server, tmp)
        print(f"openadapt proxy: MCP endpoint {server.url} (task {task})", file=sys.stderr, flush=True)
        if not argv_command:
            print(f"MCP config for your agent: {config}\n"
                  "keys: y approve the waiting plan, n decline it, u undo the last applied plan, q quit",
                  file=sys.stderr, flush=True)
            for line in sys.stdin:
                ch = line.strip()[:1].lower()
                if ch == "q":
                    return 0
                print(proxy.key(ch), file=sys.stderr, flush=True)
            return 0
        env = child_environment(os.environ, secrets_)
        env.update({"OPENADAPT_MCP_URL": server.url, "OPENADAPT_MCP_TOKEN": server.token,
                    "OPENADAPT_MCP_CONFIG": str(config), "OPENADAPT_TASK": task})
        cmd = list(argv_command)
        if Path(cmd[0]).name == "claude" and "--mcp-config" not in cmd:
            cmd[1:1] = ["--mcp-config", str(config)]
        exe = shutil.which(cmd[0], path=env.get("PATH"))
        if exe is None:
            print(f"openadapt proxy: {cmd[0]} is not on PATH", file=sys.stderr)
            return 127
        child = subprocess.Popen([exe, *cmd[1:]], env=env)
        try:
            return child.wait()
        except KeyboardInterrupt:
            child.send_signal(signal.SIGINT)
            return child.wait()
    finally:
        server.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def default_task() -> str:
    return "proxy-" + sha256([os.getpid(), secrets.token_hex(8)])[:16]
