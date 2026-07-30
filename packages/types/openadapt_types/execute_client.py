"""Small standard-library client for the public OpenAdapt Execute v1 contract.

The client only submits and reads the versioned public resources. It does not
implement partner enrollment, webhook delivery, permit issuance, runners, or
application connectors.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from openadapt_types.execute import (
    ExecuteAcceptedV1,
    ExecuteEvidenceReceiptV1,
    ExecuteRequestV1,
    ExecuteStatusV1,
)

_JSON_CONTENT_TYPE: Final = "application/json"


class _RejectRedirectHandler(HTTPRedirectHandler):
    """Stop before urllib can forward a bearer token to another origin."""

    def redirect_request(  # type: ignore[override]
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request:
        raise HTTPError(req.full_url, code, "Execute redirects are not allowed", headers, fp)


_NO_REDIRECT_OPENER: Final = build_opener(_RejectRedirectHandler())


class ExecuteApiError(RuntimeError):
    """A transport or HTTP error returned by an Execute endpoint."""

    def __init__(self, status_code: int | None, message: str) -> None:
        self.status_code = status_code
        super().__init__(message)


@dataclass(frozen=True)
class ExecuteClient:
    """Call a partner-provisioned Execute endpoint with its bearer token.

    ``base_url`` is the provider base URL. For OpenAdapt Cloud, use
    ``https://app.openadapt.ai/api``; this client appends the stable ``/v1``
    resource paths from the public contract.
    """

    base_url: str
    bearer_token: str = field(repr=False)
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "base_url must be an HTTPS origin or path prefix without credentials, query, or fragment"
            )
        if not self.bearer_token.strip():
            raise ValueError("bearer_token must not be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

    def create_execution(self, request: ExecuteRequestV1) -> ExecuteAcceptedV1:
        """Submit one qualified execution for durable processing."""

        response = self._json_request(
            method="POST",
            path="/v1/executions",
            payload=request.model_dump(mode="json"),
        )
        return ExecuteAcceptedV1.model_validate(response)

    def get_execution(self, execution_id: str) -> ExecuteStatusV1:
        """Return the current public lifecycle state."""

        response = self._json_request(
            method="GET", path=f"/v1/executions/{quote(execution_id, safe='')}"
        )
        return ExecuteStatusV1.model_validate(response)

    def get_receipt(self, execution_id: str) -> ExecuteEvidenceReceiptV1:
        """Return the terminal receipt after the execution reaches TERMINAL."""

        response = self._json_request(
            method="GET", path=f"/v1/executions/{quote(execution_id, safe='')}/receipt"
        )
        return ExecuteEvidenceReceiptV1.model_validate(response)

    def _json_request(
        self, *, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            self.base_url.rstrip("/") + path,
            data=data,
            method=method,
            headers={
                "Accept": _JSON_CONTENT_TYPE,
                "Authorization": f"Bearer {self.bearer_token}",
                **({"Content-Type": _JSON_CONTENT_TYPE} if data is not None else {}),
            },
        )
        try:
            with _NO_REDIRECT_OPENER.open(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
        except HTTPError as exc:
            detail = "" if exc.fp is None else exc.read().decode("utf-8", errors="replace")
            raise ExecuteApiError(exc.code, detail or exc.reason) from exc
        except URLError as exc:
            raise ExecuteApiError(None, str(exc.reason)) from exc

        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ExecuteApiError(None, "Execute returned a non-JSON response") from exc
        if not isinstance(decoded, dict):
            raise ExecuteApiError(None, "Execute returned a non-object JSON response")
        return decoded
