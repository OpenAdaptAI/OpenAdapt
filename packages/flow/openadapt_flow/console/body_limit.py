"""Bound identity-bound HTTP bodies before Starlette accumulates their chunks."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send


class ConsoleBodyRefused(ValueError):
    def __init__(self, status_code: int) -> None:
        super().__init__("console request body framing refused")
        self.status_code = status_code


class ConsoleIdentityBodyLimit:
    """Lazy receive wrapper; the original console security gates still run first.

    The wrapper never buffers or replaces a body. Starlette's existing request
    cache receives the original bounded chunks and replays them to the handler.
    Missing framing permits an empty body only. Streaming transfer encodings
    are unsupported by this finite action-payload interface.
    """

    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return
        total = 0
        complete = False

        async def bounded_receive() -> Message:
            nonlocal total, complete
            if complete:
                return await receive()
            lengths = [
                value
                for key, value in scope["headers"]
                if key.lower() == b"content-length"
            ]
            if any(key.lower() == b"transfer-encoding" for key, _ in scope["headers"]):
                raise ConsoleBodyRefused(400)
            if len(lengths) > 1 or (lengths and not lengths[0].isdigit()):
                raise ConsoleBodyRefused(400)
            if lengths and len(lengths[0]) > 10:
                raise ConsoleBodyRefused(413)
            declared = int(lengths[0]) if lengths else None
            if declared is not None and declared > self.max_body_bytes:
                raise ConsoleBodyRefused(413)
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_body_bytes:
                    raise ConsoleBodyRefused(413)
                if declared is None and total:
                    raise ConsoleBodyRefused(411)
                if declared is not None and total > declared:
                    raise ConsoleBodyRefused(400)
                if not message.get("more_body", False):
                    if declared is not None and total != declared:
                        raise ConsoleBodyRefused(400)
                    complete = True
            return message

        await self.app(scope, bounded_receive, send)
