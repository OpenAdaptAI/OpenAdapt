"""Explicit single-act guard for a caller-owned Stagehand Python session."""

from __future__ import annotations

from typing import Any, Mapping

from openadapt_flow.integrations.agents import WriteGuard


class VerifiedSession:
    """Only this object's act call is checked; the original session is unchanged.

    ``values`` bind the operator's existing verification contract. Stagehand's
    response is tool output, never evidence that the record was persisted.
    """

    def __init__(self, session: Any, *, contract: Any, **options: Any) -> None:
        if not callable(getattr(session, "act", None)):
            raise TypeError("a Stagehand session with an async act method is required")
        self._session = session
        self._guard = WriteGuard(contract, **options)

    async def act(
        self, *, values: Mapping[str, Any], input: Any, **options: Any
    ) -> dict:
        return await self._guard.arun(
            values, lambda: self._session.act(input=input, **options)
        )


def verified(session: Any, *, contract: Any, **options: Any) -> VerifiedSession:
    return VerifiedSession(session, contract=contract, **options)
