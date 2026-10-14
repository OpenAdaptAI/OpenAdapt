"""Weave (W&B) integration for LLM/agent tracing.

Weave auto-patches OpenAI and Anthropic clients after ``weave.init()``,
giving automatic tracing of every VLM call (planner, grounder, evaluator)
with prompts, responses, costs, and latency in hierarchical trace trees.

Usage:
    from openadapt_evals.integrations.weave_integration import weave_init, weave_op

    # Initialize once at startup (alongside wandb.init if used)
    weave_init("openadapt-evals")

    # Decorate functions for explicit trace tree structure
    @weave_op
    def my_agent_step(screenshot, instruction):
        ...

When weave is not installed, ``weave_init`` is a no-op and ``weave_op``
passes through the function unchanged. No runtime cost.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

_weave_initialized = False


def weave_init(project: str = "openadapt-evals") -> bool:
    """Initialize Weave tracing.

    Call once at startup. Auto-patches OpenAI and Anthropic clients
    so all subsequent VLM calls are traced automatically.

    Args:
        project: Weave project name (appears in W&B UI).

    Returns:
        True if Weave initialized successfully, False otherwise.
    """
    global _weave_initialized
    if _weave_initialized:
        return True

    try:
        import weave
        weave.init(project)
        _weave_initialized = True
        logger.info("Weave tracing initialized (project=%s)", project)
        return True
    except ImportError:
        logger.debug("weave not installed — tracing disabled")
        return False
    except Exception as exc:
        logger.warning("Weave init failed: %s", exc)
        return False


def weave_op(fn: F) -> F:
    """Decorator that wraps a function with ``@weave.op`` if available.

    When weave is installed, the decorated function appears in trace
    trees with its arguments, return value, and execution time.

    When weave is not installed, this is a zero-cost passthrough.
    """
    try:
        import weave
        return weave.op(fn)  # type: ignore[return-value]
    except ImportError:
        return fn
    except Exception:
        return fn
