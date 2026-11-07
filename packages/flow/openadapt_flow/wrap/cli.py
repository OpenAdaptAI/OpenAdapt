"""CLI entry point for the agent-to-skill wrapper."""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Sequence
from typing import Any

from openadapt_flow.wrap import TaskArgumentsError, WrappedTask


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openadapt wrap")
    parser.add_argument("--task", required=True, help="Generated task and read-back configuration")
    parser.add_argument("--provider", choices=("claude", "openai"), default="claude")
    parser.add_argument("--model")
    parser.add_argument(
        "--price", action="append", default=[], metavar="KIND=USD_PER_MTOK",
        help="Explicit input, output, cache_write and cache_read model prices",
    )
    parser.add_argument("--max-turns", type=int, default=40)
    parser.add_argument("--max-cost-usd", type=float, help="Explicit model spend cap for a cache miss")
    parser.add_argument("--param", action="append", default=[], metavar="NAME=VALUE")
    parser.add_argument("--request-id", help="Reuse this ID when checking the same request")
    parser.add_argument("--runs-dir", default="~/.openadapt/wrap/runs")
    parser.add_argument("--person", help="Person attending review and canaries at this terminal")
    parser.add_argument("--serve", action="store_true", help="Expose this task as a local stdio MCP tool")
    return parser


def _params(rows: Sequence[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for row in rows:
        name, separator, value = row.partition("=")
        if not separator or not name or name in values:
            raise TaskArgumentsError("--param requires unique NAME=VALUE pairs")
        values[name] = value
    return values


def _prices(rows: Sequence[str]) -> dict[str, float] | None:
    if not rows:
        return None
    raw = _params(rows)
    if set(raw) != {"input", "output", "cache_write", "cache_read"}:
        raise TaskArgumentsError("--price requires input, output, cache_write and cache_read")
    try:
        prices = {name: float(value) for name, value in raw.items()}
    except ValueError:
        raise TaskArgumentsError("model prices must be finite non-negative numbers") from None
    if any(not math.isfinite(value) or value < 0 for value in prices.values()):
        raise TaskArgumentsError("model prices must be finite non-negative numbers")
    return prices


def _task(args: argparse.Namespace, parser: argparse.ArgumentParser) -> WrappedTask:
    from openadapt_flow.integrations.agents.recording import load_task

    options: dict[str, Any] = {}
    if args.person:
        if args.serve or not (sys.stdin.isatty() and sys.stderr.isatty()):
            parser.error("--person requires an attended terminal run")
        from openadapt_flow.compile_agent.cli import (
            _terminal_confirm,
            _terminal_hold,
            _terminal_review,
        )

        options.update(
            person=args.person,
            offer=_terminal_confirm(input),
            review=_terminal_review(input),
            hold=_terminal_hold(input),
        )
    setup = load_task(args.task, **options)
    # The cache key depends on parameter names, not their current values.
    # A healthy hit needs neither a provider SDK nor a model credential.
    cached = setup.cache.server(
        setup.instruction, dict.fromkeys(setup.parameters, "")
    )
    agent = None
    if cached is None:
        cap = args.max_cost_usd
        if cap is None or not math.isfinite(cap) or cap <= 0:
            parser.error("a cache miss requires a positive --max-cost-usd")
        if args.max_turns <= 0:
            parser.error("--max-turns must be positive")
        from openadapt_flow.integrations.agents.providers import build_agent
        from openadapt_flow.jit.keys import load_customer_model_key

        agent = build_agent(
            args.provider,
            model=args.model,
            max_turns=args.max_turns,
            max_cost_usd=cap,
            prices=_prices(args.price),
        )
        if getattr(agent, "requires_model_key", True):
            provider = "anthropic" if args.provider == "claude" else "openai"
            setup.cache.model_key = load_customer_model_key(provider)
    return WrappedTask(
        cache=setup.cache,
        agent=agent,
        instruction=setup.instruction,
        parameters=setup.parameters,
        runs_dir=args.runs_dir,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.serve and (args.param or args.request_id):
        parser.error("--serve takes request IDs and parameters from MCP calls")
    if not args.serve and not args.request_id:
        parser.error("a run requires --request-id")
    try:
        values = _params(args.param)
        task = _task(args, parser)
        if args.serve:
            from openadapt_flow.wrap.mcp import serve

            serve(task)
            return 0
        result = task.call(args.request_id, values)
    except TaskArgumentsError as exc:
        parser.error(str(exc))
    except Exception:  # noqa: BLE001 - do not reflect provider or verifier values
        # Provider, storage and verifier exceptions can contain values or
        # credentials. Do not reflect their messages to a calling agent.
        print("openadapt wrap: local setup or execution failed; check the local run record", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("verified") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
