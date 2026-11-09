"""HTTP + MCP surface for the MIT reference Execute server."""

from __future__ import annotations

import hmac
import html
import json
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from openadapt_flow import __version__
from openadapt_flow.execute.dispatch import Runner
from openadapt_flow.execute.models import SelfSignedSealV1
from openadapt_flow.execute.service import ExecuteService, ExecuteServiceError

_JSON = "application/json"
_MCP_PROTOCOL = "2024-11-05"


def create_app(
    data_dir: Path | str,
    *,
    token: str | None = None,
    runner: Runner | None = None,
    process_inline: bool = True,
    seed_mockmed: bool = False,
    service: ExecuteService | None = None,
) -> FastAPI:
    """Build the one-process HTTP+MCP app over a local data directory."""

    store = service or ExecuteService(
        data_dir,
        token=token,
        runner=runner,
        process_inline=process_inline,
        seed_mockmed=seed_mockmed,
    )
    app = FastAPI(
        title="OpenAdapt reference Execute",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.execute = store

    @app.exception_handler(ExecuteServiceError)
    async def _service_error(
        _request: Request, exc: ExecuteServiceError
    ) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.body())

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "openadapt-execute-ref",
            "issuer": "self_signed",
            "issuer_key_fingerprint": store.fingerprint,
            "production_seal": False,
        }

    @app.post("/v1/executions", status_code=202, response_model=None)
    async def create_execution(
        request: Request,
        authorization: Optional[str] = Header(default=None),
    ) -> JSONResponse:
        _require_bearer(store, authorization)
        payload = await _json_object(request)
        accepted = store.create_execution(payload)
        return JSONResponse(
            status_code=202,
            content=accepted.model_dump(mode="json"),
        )

    @app.get("/v1/executions/{execution_id}")
    def get_execution(
        execution_id: str,
        authorization: Optional[str] = Header(default=None),
    ) -> dict[str, Any]:
        _require_bearer(store, authorization)
        return store.get_status(execution_id).model_dump(mode="json")

    @app.get("/v1/executions/{execution_id}/receipt", response_model=None)
    def get_receipt(
        execution_id: str,
        authorization: Optional[str] = Header(default=None),
    ) -> JSONResponse:
        _require_bearer(store, authorization)
        receipt = store.get_receipt(execution_id)
        body = receipt.model_dump(mode="json")
        return JSONResponse(
            content=body,
            headers=_issuer_headers(store),
        )

    @app.get("/seals/{seal_id}", response_model=None)
    def get_seal(
        seal_id: str,
        request: Request,
        authorization: Optional[str] = Header(default=None),
    ) -> Response:
        # Local analog of a public verify page: the seal has no PHI.
        del authorization
        seal = store.get_seal(seal_id)
        accept = request.headers.get("accept", "")
        want_json = request.query_params.get("format") == "json" or (
            _JSON in accept and "text/html" not in accept
        )
        if want_json:
            return JSONResponse(
                content=seal.model_dump(mode="json"),
                headers=_issuer_headers(store),
            )
        return HTMLResponse(_render_seal_html(seal), headers=_issuer_headers(store))

    @app.post("/mcp", response_model=None)
    async def mcp_endpoint(
        request: Request,
        authorization: Optional[str] = Header(default=None),
    ) -> JSONResponse | Response:
        _require_bearer(store, authorization)
        payload = await _json_object(request)
        result = _handle_mcp(store, payload)
        if result is None:
            return Response(status_code=204)
        return JSONResponse(content=result)

    return app


def serve(
    data_dir: Path | str,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    token: str | None = None,
    seed_mockmed: bool = False,
    service: ExecuteService | None = None,
) -> None:
    """Block on uvicorn. Caller prints the banner before this."""

    import uvicorn

    app = create_app(
        data_dir,
        token=token,
        seed_mockmed=seed_mockmed,
        service=service,
    )
    uvicorn.run(app, host=host, port=port, log_level="info")


def _require_bearer(store: ExecuteService, authorization: Optional[str]) -> None:
    scheme, separator, token = (authorization or "").partition(" ")
    if separator != " " or scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="bearer token required")
    if not hmac.compare_digest(token.strip(), store.token):
        raise HTTPException(status_code=401, detail="invalid bearer token")


def _issuer_headers(store: ExecuteService) -> dict[str, str]:
    return {
        "X-OpenAdapt-Issuer": "self_signed",
        "X-OpenAdapt-Issuer-Fingerprint": store.fingerprint,
        "X-OpenAdapt-Production-Seal": "false",
    }


async def _json_object(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="body must be JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    return payload


def _handle_mcp(
    store: ExecuteService, payload: dict[str, Any]
) -> dict[str, Any] | None:
    if payload.get("jsonrpc") != "2.0":
        return {
            "jsonrpc": "2.0",
            "id": payload.get("id"),
            "error": {"code": -32600, "message": "jsonrpc must be 2.0"},
        }
    method = payload.get("method")
    rpc_id = payload.get("id")
    params = payload.get("params") or {}
    if method == "notifications/initialized":
        return None
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "protocolVersion": _MCP_PROTOCOL,
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "openadapt-execute-ref",
                    "version": __version__,
                },
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rpc_id, "result": {}}
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {"tools": _mcp_tools()},
        }
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            text = _call_mcp_tool(store, str(name), arguments)
        except ExecuteServiceError as exc:
            return {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(exc.body())}],
                    "isError": True,
                },
            }
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {"content": [{"type": "text", "text": text}]},
        }
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "error": {"code": -32601, "message": f"unknown method {method!r}"},
    }


def _mcp_tools() -> list[dict[str, Any]]:
    from openadapt_types.execute import ExecuteRequestV1

    request_schema = ExecuteRequestV1.model_json_schema()
    return [
        {
            "name": "create_execution",
            "description": (
                "Submit one qualified execution. Same body as POST /v1/executions."
            ),
            "inputSchema": request_schema,
        },
        {
            "name": "get_execution",
            "description": "Read Execute lifecycle state.",
            "inputSchema": {
                "type": "object",
                "properties": {"execution_id": {"type": "string"}},
                "required": ["execution_id"],
            },
        },
        {
            "name": "get_execution_receipt",
            "description": "Read the terminal Execute receipt.",
            "inputSchema": {
                "type": "object",
                "properties": {"execution_id": {"type": "string"}},
                "required": ["execution_id"],
            },
        },
    ]


def _call_mcp_tool(store: ExecuteService, name: str, arguments: dict[str, Any]) -> str:
    if name == "create_execution":
        accepted = store.create_execution(arguments)
        return json.dumps(accepted.model_dump(mode="json"))
    if name == "get_execution":
        status = store.get_status(str(arguments.get("execution_id") or ""))
        return json.dumps(status.model_dump(mode="json"))
    if name == "get_execution_receipt":
        receipt = store.get_receipt(str(arguments.get("execution_id") or ""))
        return json.dumps(receipt.model_dump(mode="json"))
    raise ExecuteServiceError(404, "unknown_tool", f"no MCP tool named {name!r}")


def _render_seal_html(seal: SelfSignedSealV1) -> str:
    receipt = seal.receipt
    outcome = html.escape(str(receipt.get("outcome", "")))
    receipt_id = html.escape(str(receipt.get("receipt_id", "")))
    execution_id = html.escape(str(receipt.get("execution_id", "")))
    digest = html.escape(str(receipt.get("workflow_digest", "")))
    fingerprint = html.escape(seal.issuer_key_fingerprint)
    notice = html.escape(seal.notice)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Self-signed Execute receipt</title>
  <style>
    body {{ font-family: ui-sans-serif, system-ui, sans-serif; margin: 2rem; max-width: 44rem; }}
    .banner {{ background: #111; color: #f5f5f5; padding: 1rem 1.2rem; }}
    dl {{ display: grid; grid-template-columns: 12rem 1fr; gap: 0.4rem 1rem; }}
    dt {{ color: #555; }}
    dd {{ margin: 0; word-break: break-all; }}
  </style>
</head>
<body>
  <div class="banner">
    <strong>Self-signed.</strong> This is not an OpenAdapt production Seal.
  </div>
  <h1>Local Execute receipt</h1>
  <p>{notice}</p>
  <dl>
    <dt>outcome</dt><dd>{outcome}</dd>
    <dt>issuer</dt><dd>self_signed</dd>
    <dt>key fingerprint</dt><dd>{fingerprint}</dd>
    <dt>production seal</dt><dd>false</dd>
    <dt>meter USD</dt><dd>0</dd>
    <dt>verify host</dt><dd>local</dd>
    <dt>receipt id</dt><dd>{receipt_id}</dd>
    <dt>execution id</dt><dd>{execution_id}</dd>
    <dt>workflow digest</dt><dd>{digest}</dd>
  </dl>
</body>
</html>
"""
