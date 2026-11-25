"""CLI entry point for the agent-to-skill wrapper."""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import signal
import sys
import threading
from collections.abc import Iterator, Sequence
from typing import Any

from openadapt_flow.wrap import TaskArgumentsError, WrappedTask

#: The documented one-liner (docs/WRAP.md): the launcher's ``wrap`` extra
#: brings the built-in agent's SDK, which the base launcher leaves out.
WRAP_EXAMPLE_COMMAND = "uvx --python 3.12 'openadapt[wrap]@latest' wrap --example"


class NoAdmittedProgram(RuntimeError):
    """``wrap run``: the task has no admitted program, so nothing ran.

    ``draft`` is the task folder when run 1 left a draft that was not
    admitted, else None.
    """

    def __init__(self, draft: Any = None) -> None:
        super().__init__("no admitted program")
        self.draft = draft


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openadapt wrap",
        allow_abbrev=False,
        epilog=(
            "openadapt wrap --example runs the whole loop on a bundled synthetic "
            "task (see its --help). "
            "openadapt wrap --task TASK [--param ...] -- COMMAND runs your own "
            "agent command for the first run: it gets OPENADAPT_MCP_URL, "
            "OPENADAPT_MCP_TOKEN and OPENADAPT_TASK, and drives OpenAdapt's "
            "browser through that endpoint (see docs/WRAP.md). "
            "openadapt wrap run --task TASK [--param ...] runs the admitted "
            "program only, with no model calls. "
            "openadapt wrap export --bundle BUNDLE --out NEW_DIR prepares an "
            "untrusted local candidate (see docs/WRAP_CANDIDATE.md). "
            "openadapt wrap publish --task TASK publishes the task's admitted "
            "program as an OpenAdapt Cloud endpoint (see its --help)."
        ),
    )
    parser.add_argument(
        "--task", required=True, help="Generated task and read-back configuration"
    )
    parser.add_argument("--provider", choices=("claude", "openai"), default="claude")
    parser.add_argument("--model")
    parser.add_argument("--key-dir", help="Existing admission key directory")
    parser.add_argument("--teach-authorization", help="Signed teaching authorization")
    parser.add_argument(
        "--approver-root", help="Pinned approver root for that authorization"
    )
    parser.add_argument("--admit-key", help="Encrypted key of a registered admitter")
    parser.add_argument(
        "--admission-policy", help="Signed admission policy for --admit-key"
    )
    parser.add_argument(
        "--price",
        action="append",
        default=[],
        metavar="KIND=USD_PER_MTOK",
        help="Explicit input, output, cache_write and cache_read model prices",
    )
    parser.add_argument("--max-turns", type=int, default=40)
    parser.add_argument("--max-run-seconds", type=float, default=110)
    parser.add_argument("--request-timeout-seconds", type=float, default=25)
    parser.add_argument(
        "--max-input-bytes",
        type=int,
        help="Largest rendered model request, in bytes, on a cache miss "
        "(default: 4 MiB for Claude, 64 KiB for OpenAI; at most 32 MiB). "
        "A larger request is not sent and the run stops",
    )
    parser.add_argument("--price-model", help="Exact model covered by supplied prices")
    parser.add_argument("--pricing-source-url", help="Primary source for model prices")
    parser.add_argument("--pricing-verified-at", help="Date prices were verified")
    parser.add_argument(
        "--max-cost-usd", type=float, help="Explicit model spend cap for a cache miss"
    )
    parser.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="A task input. A sign-in input (a password, or the username "
        "paired with it) may instead come from OPENADAPT_FLOW_SECRET_<NAME>, "
        "so it is not on the command line",
    )
    parser.add_argument(
        "--request-id", help="Reuse this ID when checking the same request"
    )
    parser.add_argument("--runs-dir", default="~/.openadapt/wrap/runs")
    parser.add_argument(
        "--person", help="Person attending review and canaries at this terminal"
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Expose this task as a local stdio MCP tool",
    )
    parser.add_argument(
        "--command-timeout",
        type=float,
        default=600.0,
        help="With -- COMMAND: seconds before the command is stopped (default 600)",
    )
    return parser


def _params(rows: Sequence[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for row in rows:
        name, separator, value = row.partition("=")
        if not separator or not name or name in values:
            raise TaskArgumentsError("--param requires unique NAME=VALUE pairs")
        values[name] = value
    return values


def _with_secret_env(
    values: dict[str, str], parameters: Sequence[str]
) -> dict[str, str]:
    """Fill each missing input from ``OPENADAPT_FLOW_SECRET_<NAME>`` when set.

    A sign-in value then never appears in argv or shell history. It is held
    in memory for this run only, like a ``--param`` value.
    """
    import os

    from openadapt_flow.runtime.replayer import secret_env_var, secret_env_vars

    out = dict(values)
    # Before compilation these can still be ordinary public inputs. Only the
    # names actually sourced from the secret environment need a slot here;
    # explicit public values must not acquire a new case-folding restriction.
    sourced = [
        name
        for name in parameters
        if name not in out and os.environ.get(secret_env_var(name))
    ]
    for name, key in secret_env_vars(sourced).items():
        out[name] = os.environ[key]
    return out


def _prices(rows: Sequence[str]) -> dict[str, float] | None:
    if not rows:
        return None
    raw = _params(rows)
    if set(raw) != {"input", "output", "cache_write", "cache_read"}:
        raise TaskArgumentsError(
            "--price requires input, output, cache_write and cache_read"
        )
    try:
        prices = {name: float(value) for name, value in raw.items()}
    except ValueError:
        raise TaskArgumentsError(
            "model prices must be finite non-negative numbers"
        ) from None
    if any(not math.isfinite(value) or value < 0 for value in prices.values()):
        raise TaskArgumentsError("model prices must be finite non-negative numbers")
    return prices


def _task(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    *,
    command_agent: Any = None,
    replay_only: bool = False,
) -> WrappedTask:
    from openadapt_flow.integrations.agents.recording import load_task

    options: dict[str, Any] = {}
    if args.admission_policy and not args.admit_key:
        parser.error("--admission-policy requires --admit-key")
    if args.key_dir:
        options["key_dir"] = args.key_dir
    if args.teach_authorization:
        from openadapt_flow.compile_agent.cli import _teach_authority

        options["teach_authorization"] = _teach_authority(args)
    if args.admit_key:
        from openadapt_flow.compile_agent.cli import _admitter

        options["admitter"] = _admitter(args)
    if args.person:
        if args.serve or not (sys.stdin.isatty() and sys.stderr.isatty()):
            # A person's typed confirmation is the admission evidence, so it
            # must come from a terminal. Unattended admission uses --admit-key.
            parser.error(
                "--person requires an interactive terminal (a person types the "
                "confirmations); for an unattended run use --admit-key"
            )
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
    names = dict.fromkeys(setup.parameters, "")
    held = getattr(setup.cache, "held", None)
    if callable(held) and held(setup.instruction, names):
        # A held task returns CACHE_HALTED with its reason and never reruns
        # the agent, so it needs no model, key or budget.
        cached = None
        halted = True
    else:
        cached = setup.cache.server(setup.instruction, names)
        halted = False
    agent = None
    if cached is None and not halted and replay_only:
        compiler, _key = setup.cache._compiler(setup.instruction, names)
        draft = compiler._jit().candidate() is not None
        raise NoAdmittedProgram(compiler.dir if draft else None)
    if command_agent is not None:
        from openadapt_flow.wrap.process import verify_env_names

        # The system of record's credentials never reach the wrapped command.
        command_agent.withheld_env = verify_env_names(setup.cache.common["verify"])
    if cached is None and not halted and command_agent is not None:
        # The wrapped command brings its own model; OpenAdapt sends none.
        agent = command_agent
    elif cached is None and not halted:
        cap = args.max_cost_usd
        if cap is None or not math.isfinite(cap) or cap <= 0:
            parser.error("a cache miss requires a positive --max-cost-usd")
        if args.max_turns <= 0:
            parser.error("--max-turns must be positive")
        from openadapt_flow.integrations.agents.providers import (
            MAX_INPUT_BYTES_CEILING,
        )

        if args.max_input_bytes is not None and not (
            0 < args.max_input_bytes <= MAX_INPUT_BYTES_CEILING
        ):
            parser.error(
                f"--max-input-bytes must be between 1 and {MAX_INPUT_BYTES_CEILING}"
            )
        from openadapt_flow.integrations.agents.providers import build_agent
        from openadapt_flow.jit.keys import load_customer_model_key

        agent = build_agent(
            args.provider,
            model=args.model,
            max_turns=args.max_turns,
            max_cost_usd=cap,
            prices=_prices(args.price),
            max_run_seconds=args.max_run_seconds,
            request_timeout_seconds=args.request_timeout_seconds,
            max_input_bytes=args.max_input_bytes,
            price_model=args.price_model,
            pricing_source_url=args.pricing_source_url,
            pricing_verified_at=args.pricing_verified_at,
        )
        if getattr(agent, "requires_model_key", True):
            provider = getattr(agent, "model_key_provider", None)
            expected = "anthropic" if args.provider == "claude" else "openai"
            if provider != expected:
                parser.error("provider adapter has no matching model-key provenance")
            setup.cache.model_key = load_customer_model_key(provider)
    return WrappedTask(
        cache=setup.cache,
        agent=agent,
        instruction=setup.instruction,
        parameters=setup.parameters,
        runs_dir=args.runs_dir,
    )


def build_example_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openadapt wrap --example",
        allow_abbrev=False,
        description=(
            "Run the whole wrap loop on a bundled synthetic task: the built-in "
            "agent does it once, the run is compiled and admitted, then a new "
            "input runs the program with no model calls. Headless; needs "
            "ANTHROPIC_API_KEY (or OPENAI_API_KEY with --provider openai, "
            "--model and its --price values)."
        ),
    )
    parser.add_argument("--example", action="store_true", required=True)
    parser.add_argument("--provider", choices=("claude", "openai"))
    parser.add_argument("--model")
    parser.add_argument(
        "--max-cost-usd",
        type=float,
        default=2.0,
        help="Model spend cap for run 1 (default 2.00)",
    )
    parser.add_argument("--max-turns", type=int, default=30)
    parser.add_argument("--max-run-seconds", type=float, default=120)
    parser.add_argument("--request-timeout-seconds", type=float, default=60)
    parser.add_argument(
        "--price",
        action="append",
        default=[],
        metavar="KIND=USD_PER_MTOK",
        help="Explicit input, output, cache_write and cache_read model prices",
    )
    parser.add_argument("--price-model", help="Exact model covered by supplied prices")
    parser.add_argument("--pricing-source-url", help="Primary source for model prices")
    parser.add_argument("--pricing-verified-at", help="Date prices were verified")
    parser.add_argument(
        "--out",
        help="New folder for the example's files "
        "(default ~/.openadapt/wrap/example/<time>)",
    )
    parser.add_argument("--json", action="store_true", help="Print one JSON result")
    return parser


def example_main(rows: Sequence[str], *, client: Any = None) -> int:
    """``openadapt wrap --example``. ``client`` is a test-only model client."""

    import importlib.util
    import time
    from pathlib import Path

    from openadapt_flow.jit.keys import PROVIDER_ENV, MissingModelKey
    from openadapt_flow.wrap.example import ExampleError, choose_provider, run_example

    parser = build_example_parser()
    args = parser.parse_args(list(rows))
    provider = choose_provider(args.provider)
    if provider is None:
        print(
            "openadapt wrap --example: no model key. Set ANTHROPIC_API_KEY, or "
            "OPENAI_API_KEY with --provider openai, --model and its --price "
            "values, then run it again [MISSING_MODEL_KEY]",
            file=sys.stderr,
        )
        return 2
    cap = args.max_cost_usd
    if not math.isfinite(cap) or cap <= 0:
        parser.error("--max-cost-usd must be positive")
    sdk = "anthropic" if provider == "claude" else "agents"
    if client is None and importlib.util.find_spec(sdk) is None:
        if provider == "claude":
            print(
                "openadapt wrap --example: the built-in agent needs the wrap "
                f"extra; run: {WRAP_EXAMPLE_COMMAND}",
                file=sys.stderr,
            )
            return 2
        print(
            "openadapt wrap --example: the openai-agents package is not "
            "installed; install it next to OpenAdapt (pip install openai-agents)",
            file=sys.stderr,
        )
        return 2
    key_provider = "anthropic" if provider == "claude" else "openai"
    try:
        from openadapt_flow.integrations.agents.providers import build_agent
        from openadapt_flow.jit.keys import load_customer_model_key

        model_key = load_customer_model_key(key_provider)
        agent = build_agent(
            "claude" if provider == "claude" else "openai",
            model=args.model,
            max_turns=args.max_turns,
            max_cost_usd=cap,
            prices=_prices(args.price),
            max_run_seconds=args.max_run_seconds,
            request_timeout_seconds=args.request_timeout_seconds,
            price_model=args.price_model,
            pricing_source_url=args.pricing_source_url,
            pricing_verified_at=args.pricing_verified_at,
            client=client,
        )
    except MissingModelKey:
        print(
            f"openadapt wrap --example: no model key; set {PROVIDER_ENV[key_provider]} "
            f"(--provider {provider}) [MISSING_MODEL_KEY]",
            file=sys.stderr,
        )
        return 2
    except (TaskArgumentsError, ValueError) as exc:
        # Pricing and limits are local configuration, never a provider reply.
        parser.error(str(exc))
    import os

    home = Path(os.environ.get("OPENADAPT_HOME") or "~/.openadapt").expanduser()
    out = (
        Path(args.out)
        if args.out
        else home / "wrap" / "example" / time.strftime("%Y%m%dT%H%M%S")
    )
    try:
        return run_example(
            agent=agent, model_key=model_key, out_dir=out, as_json=args.json
        )
    except ExampleError as exc:
        print(f"openadapt wrap --example: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - do not reflect provider values
        # Name the failure's type only; its message can carry provider text.
        print(
            f"openadapt wrap --example: the example failed ({type(exc).__name__}); "
            f"its files are in {out}",
            file=sys.stderr,
        )
        return 2


@contextlib.contextmanager
def _commands_stopped_on_signal(active: bool) -> Iterator[None]:
    """End the wrapped command's process group on Ctrl-C, SIGTERM or any exit.

    The command runs in its own session, so a terminal's Ctrl-C and a SIGTERM
    sent to OpenAdapt never reach it. The handlers stop the group themselves
    before raising: the main thread may be inside a browser tool call whose
    error handling keeps the interrupt from reaching this block, and the
    command must still end. Every exit from this block stops what is left.
    """

    if not active:
        yield
        return
    from openadapt_flow.wrap.process import stop_running_commands

    main_thread = threading.current_thread() is threading.main_thread()
    previous: dict[signal.Signals, Any] = {}

    def on_signal(signum: int, frame: Any) -> None:
        stop_running_commands()
        raise KeyboardInterrupt

    if main_thread:
        for caught in (signal.SIGINT, signal.SIGTERM):
            previous[caught] = signal.signal(caught, on_signal)
    try:
        yield
    finally:
        stop_running_commands()
        for caught, handler in previous.items():
            signal.signal(caught, handler)


def main(argv: Sequence[str] | None = None) -> int:
    rows = list(sys.argv[1:] if argv is None else argv)
    head = rows[: rows.index("--")] if "--" in rows else rows
    if "--example" in head:
        return example_main(head)
    if rows[:1] == ["export"]:
        from openadapt_flow.wrap.candidate import main as candidate_main

        return candidate_main(rows[1:])
    if rows[:1] == ["publish"]:
        from openadapt_flow.wrap.publish import main as publish_main

        return publish_main(rows[1:])
    command = rows[rows.index("--") + 1 :] if "--" in rows else None
    replay_only = head[:1] == ["run"]
    if replay_only:
        head = head[1:]
    parser = build_parser()
    if replay_only:
        parser.prog = "openadapt wrap run"
    args = parser.parse_args(head)
    if command is not None and (replay_only or args.serve):
        parser.error(
            "-- COMMAND runs the first run; it can't be used with run or --serve"
        )
    if command == []:
        parser.error("-- needs a command to run")
    if args.serve and (args.param or args.request_id):
        parser.error("--serve takes request IDs and parameters from MCP calls")
    if not args.serve and not args.request_id:
        if command is None and not replay_only:
            parser.error("a run requires --request-id")
    from openadapt_flow.jit.keys import PROVIDER_ENV, MissingModelKey

    agent = None
    derived_id = False
    try:
        values = _params(args.param)
        derived_id = not args.serve and not args.request_id
        if derived_id:
            from openadapt_flow.wrap.process import request_id_for

            # Never random: asking for the same run again must dedupe.
            args.request_id = request_id_for(args.task, command or [], values)
        if command is not None:
            from openadapt_flow.wrap.process import ProcessAgent

            agent = ProcessAgent(command, timeout_s=args.command_timeout)
        if agent is None and not replay_only:
            task = _task(args, parser)
        else:
            task = _task(args, parser, command_agent=agent, replay_only=replay_only)
        if agent is not None and task.agent is not agent:
            print(
                "openadapt wrap: this task has an admitted program; it runs "
                "instead of the command, with no model calls",
                file=sys.stderr,
            )
        if args.serve:
            from openadapt_flow.wrap.mcp import serve

            serve(task)
            return 0
        with _commands_stopped_on_signal(agent is not None):
            result = task.call(
                args.request_id, _with_secret_env(values, task.parameters)
            )
    except KeyboardInterrupt:
        print(
            "openadapt wrap: interrupted; the wrapped command was stopped",
            file=sys.stderr,
        )
        return 130
    except TaskArgumentsError as exc:
        parser.error(str(exc))
    except NoAdmittedProgram as exc:
        if exc.draft is not None:
            print(
                "openadapt wrap run: run 1 left a draft that was not admitted, "
                f"in {exc.draft}; nothing ran. A draft is admitted when a person "
                "confirms it during run 1 (--person) [DRAFT_NOT_ADMITTED]",
                file=sys.stderr,
            )
        else:
            print(
                "openadapt wrap run: this task has no admitted program; nothing "
                "ran. Do the first run with openadapt wrap --task TASK -- COMMAND "
                "[NO_ADMITTED_PROGRAM]",
                file=sys.stderr,
            )
        return 2
    except MissingModelKey:
        # Raised before the agent runs, so no run record exists yet. Name the
        # variable only; the message never carries a key value.
        provider = "anthropic" if args.provider == "claude" else "openai"
        print(
            f"openadapt wrap: no model key; set {PROVIDER_ENV[provider]} "
            f"(--provider {args.provider}) for the first run [MISSING_MODEL_KEY]",
            file=sys.stderr,
        )
        return 2
    except Exception:  # noqa: BLE001 - do not reflect provider or verifier values
        # Provider, storage and verifier exceptions can contain values or
        # credentials. Do not reflect their messages to a calling agent.
        print(
            "openadapt wrap: local setup or execution failed; check the local run record",
            file=sys.stderr,
        )
        return 2
    if derived_id and result.get("status") == "RECONCILIATION_REQUIRED":
        # An earlier attempt was interrupted after it may have saved: a new
        # request ID now could save twice, so the record comes first.
        print(
            f"openadapt wrap: request {args.request_id} has an unfinished "
            "earlier attempt; the earlier attempt may have saved; check the "
            "record in the app before rerunning with a new --request-id",
            file=sys.stderr,
        )
    elif derived_id and result.get("replayed") is True:
        # The default dedupes; say so instead of replaying silently.
        print(
            f"openadapt wrap: request {args.request_id} already ran with this "
            "task, command and inputs; this is its earlier result and nothing "
            "ran again. To run it again, pass a new --request-id",
            file=sys.stderr,
        )
    if isinstance(result, dict):
        if "agent_used" not in result:
            from pathlib import Path

            if (
                replay_only
                or result.get("source") == "compiled"
                or (agent is not None and task.agent is not agent)
            ):
                sha = result.get("program_sha256")
                result["agent_used"] = (
                    f"compiled-plan@{str(sha)[:12]}" if sha else "compiled-plan"
                )
            elif command:
                result["agent_used"] = Path(command[0]).name
            else:
                result["agent_used"] = args.model or args.provider
        result.setdefault("verifier_adapter", "deterministic")
    print(json.dumps(result, sort_keys=True))
    exit_code = getattr(agent, "exit_code", None)
    if isinstance(exit_code, int) and exit_code != 0:
        # The wrapped command's own failure is its exit status.
        return exit_code if 0 < exit_code < 256 else 1
    return 0 if result.get("verified") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
