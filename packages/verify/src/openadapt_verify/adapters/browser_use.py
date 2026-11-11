"""Wrap a typed async action before registering it with browser-use Tools."""

from __future__ import annotations

import inspect
from functools import wraps
from typing import Any, Callable

from openadapt_flow.integrations.agents import WriteGuard, reply_text


def verified(
    action: Callable[..., Any], *, contract: Any, **options: Any
) -> Callable[..., Any]:
    """Return tool text from a guarded action; other browser actions stay outside.

    Register the returned function with ``tools.action(...)``. Its parameter
    names and evaluated annotations are preserved, including browser_session.
    """
    if not inspect.iscoroutinefunction(action):
        raise TypeError("browser-use verified actions must be async functions")
    signature = inspect.signature(action, eval_str=True)
    guard = WriteGuard(contract, **options)

    @wraps(action)
    async def checked(*args: Any, **kwargs: Any) -> str:
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        reply = await guard.arun(bound.arguments, lambda: action(*args, **kwargs))
        return reply_text(reply)

    checked.__signature__ = signature.replace(return_annotation=str)  # type: ignore[attr-defined]
    checked.__annotations__ = {
        name: parameter.annotation
        for name, parameter in signature.parameters.items()
        if parameter.annotation is not inspect.Parameter.empty
    }
    checked.__annotations__["return"] = str
    return checked
