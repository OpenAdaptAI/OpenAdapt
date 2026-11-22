"""CLI: ``openadapt-flow exceptions serve|watch|list|answer``. See
``docs/EXCEPTION_QUEUE.md``.

    openadapt-flow exceptions serve --run-ref intake-0412
    openadapt-flow exceptions watch --cdp http://127.0.0.1:9222 --config guard.yaml
    openadapt-flow exceptions list
    openadapt-flow exceptions answer sha256:… done_by_person

``answer`` signs with an approver key on the pinned roster (by default the
key file Desktop keeps, ``~/.openadapt/approver/desktop-approver.pem``, opened
with ``OPENADAPT_JIT_KEY_PASSPHRASE``; read only). It refuses to write an
answer the waiting run wouldn't accept. Exit 0 on success, 2 when refused.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Optional

KEY_DEFAULT = Path.home() / ".openadapt" / "approver" / "desktop-approver.pem"


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--registry",
        help="the exceptions folder (default: $OPENADAPT_HOME/exceptions)",
    )


def _roster(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--key-dir", help="the approver roster's folder (default: ~/.openadapt/jit)"
    )
    p.add_argument(
        "--approver-root",
        help="the roster's root public key (default: OPENADAPT_APPROVER_ROOT)",
    )


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "exceptions",
        allow_abbrev=False,
        help="Let any agent stop and ask a person (2FA, a dialog, a sign-in, a "
        "stall, a possible duplicate), and answer from here, Desktop or a phone",
    )
    ops = p.add_subparsers(dest="exceptions_command", required=True)

    s = ops.add_parser(
        "serve",
        allow_abbrev=False,
        help="Serve the raise_exception MCP tool over stdio",
    )
    s.add_argument("--run-ref", help="this run's folder name (default: a new one)")
    s.add_argument("--task-id", help="the task the cards belong to")
    s.add_argument(
        "--deadline-s",
        type=float,
        default=None,
        help="how long a card waits for a person (default 900)",
    )
    _roster(s)
    _common(s)
    s.set_defaults(func=cmd_serve)

    w = ops.add_parser(
        "watch",
        allow_abbrev=False,
        help="Watch the agent's Chromium, raise cards without the agent, and "
        "block its saves while a card is open",
    )
    w.add_argument(
        "--cdp",
        required=True,
        help="the agent's DevTools endpoint (start the watch after the guard "
        "has attached to it)",
    )
    w.add_argument(
        "--config",
        required=True,
        help="the guard's guard.yaml: the app's origin and its not_saves",
    )
    w.add_argument("--page-url", help="watch the tab whose URL starts with this")
    w.add_argument(
        "--run-dir",
        help="write exceptions_watch.jsonl here (the guard's --out)",
    )
    w.add_argument(
        "--run-ref",
        help="this run's folder name in the registry (default: --run-dir's "
        "name, else a new one)",
    )
    w.add_argument("--task-id", help="the task the cards belong to")
    w.add_argument("--deadline-s", type=float, default=None, help="default 900")
    w.add_argument(
        "--stall-s",
        type=float,
        default=None,
        help="raise timeout_or_stall after this long with no request and no "
        "screen change (default: off)",
    )
    w.add_argument("--poll-s", type=float, default=0.5, help="look every N seconds")
    w.add_argument("--max-s", type=float, default=None, help="stop after N seconds")
    _roster(w)
    _common(w)
    w.set_defaults(func=cmd_watch)

    ls = ops.add_parser(
        "list", allow_abbrev=False, help="Show cards waiting for a person"
    )
    ls.add_argument("--run-ref", help="only this run's cards")
    ls.add_argument("--json", action="store_true", help="print JSON")
    _common(ls)
    ls.set_defaults(func=cmd_list)

    a = ops.add_parser(
        "answer",
        allow_abbrev=False,
        help="Answer a card, signed with your approver key",
    )
    a.add_argument("card_id", metavar="CARD_ID")
    a.add_argument(
        "choice",
        metavar="CHOICE",
        choices=["continue", "skip", "decline", "done_by_person", "abort"],
    )
    a.add_argument("--key", help=f"the encrypted approver key (default: {KEY_DEFAULT})")
    a.add_argument("--json", action="store_true", help="print JSON")
    _roster(a)
    _common(a)
    a.set_defaults(func=cmd_answer)


def _registry(args: argparse.Namespace):
    from openadapt_flow.exceptions.registry import ExceptionRegistry

    return ExceptionRegistry(args.registry)


def _pin(args: argparse.Namespace) -> tuple[Path, Optional[str]]:
    from openadapt_flow.approval_mode.approver_roster import RosterError, pinned_root
    from openadapt_flow.jit.lifecycle import default_key_dir

    try:
        root = pinned_root(args.approver_root)
    except RosterError as exc:
        raise SystemExit(f"REFUSED ({exc.code}): {exc}. Nothing changed.") from None
    return (Path(args.key_dir) if args.key_dir else default_key_dir()), root


def cmd_serve(args: argparse.Namespace) -> int:
    from openadapt_flow.exceptions.mcp_server import RaiseException, serve
    from openadapt_flow.exceptions.registry import DEFAULT_DEADLINE_S, roster_verifier

    key_dir, root = _pin(args)
    run_ref = args.run_ref or f"mcp-{uuid.uuid4().hex[:16]}"
    registry = _registry(args)
    registry.run_dir(run_ref)  # refuse a bad name before serving
    print(
        f"exceptions: serving raise_exception for run {run_ref}; "
        f"cards in {registry.root / run_ref}",
        file=sys.stderr,
    )
    serve(
        RaiseException(
            registry,
            run_ref=run_ref,
            verify=roster_verifier(key_dir, root),
            task_id=args.task_id,
            deadline_s=args.deadline_s or DEFAULT_DEADLINE_S,
        )
    )
    return 0


def _watch_run_ref(args: argparse.Namespace) -> str:
    import re

    from openadapt_flow.exceptions.registry import RUN_REF_PATTERN

    if args.run_ref:
        return str(args.run_ref)
    name = Path(args.run_dir).name if args.run_dir else ""
    if name and re.fullmatch(RUN_REF_PATTERN, name):
        return name
    return f"watch-{uuid.uuid4().hex[:16]}"


def cmd_watch(args: argparse.Namespace) -> int:
    from openadapt_flow.agent_guard.config import ConfigError, load_config
    from openadapt_flow.exceptions.registry import (
        DEFAULT_DEADLINE_S,
        ExceptionRegistryError,
        roster_verifier,
    )
    from openadapt_flow.exceptions.watch import (
        WATCH_EVENTS,
        ExceptionWatch,
        WatchAttachError,
        watch_browser,
    )

    key_dir, root = _pin(args)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"exceptions: {getattr(exc, 'message', exc)}", file=sys.stderr)
        return 2
    run_ref = _watch_run_ref(args)
    try:
        watch = ExceptionWatch(
            _registry(args),
            run_ref=run_ref,
            verify=roster_verifier(key_dir, root),
            app_origin=cfg.app_origin,
            not_saves=cfg.not_saves,
            task_id=args.task_id,
            deadline_s=args.deadline_s or DEFAULT_DEADLINE_S,
            stall_s=args.stall_s,
            events_path=Path(args.run_dir) / WATCH_EVENTS if args.run_dir else None,
        )
    except ExceptionRegistryError as exc:
        print(f"exceptions: REFUSED ({exc.code}): {exc}", file=sys.stderr)
        return 2
    import signal

    stopping: list[int] = []

    def _stop(signum: int, _frame: object) -> None:
        stopping.append(signum)  # the loop ends; paused saves are decided first

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, _stop)
    try:
        code = watch_browser(
            watch,
            cdp_endpoint=args.cdp,
            page_url=args.page_url,
            poll_s=args.poll_s,
            max_s=args.max_s,
            stop=lambda: bool(stopping),
            on_attached=lambda: print(
                f"exceptions: watching for run {run_ref}; saves are blocked "
                "while a card is open",
                file=sys.stderr,
                flush=True,
            ),
        )
    except WatchAttachError as exc:
        print(f"exceptions: {exc}", file=sys.stderr)
        return 2
    except BaseException as exc:  # noqa: BLE001 - any failure is loud, never 1
        if isinstance(exc, (SystemExit, KeyboardInterrupt)) and not watch.holding:
            raise
        watch.fail(exc if isinstance(exc, Exception) else RuntimeError(str(exc)))
        code = watch.stop_code()
    print(
        json.dumps(
            {
                "run_ref": run_ref,
                "stopped": watch.stopped,
                "lost": watch.lost,
                "failed": watch.failed,
                "outcomes": [o.wire() for o in watch.outcomes],
            }
        )
    )
    return code


def cmd_list(args: argparse.Namespace) -> int:
    from openadapt_flow.exceptions.registry import ExceptionRegistryError

    try:
        cards = _registry(args).open_cards(args.run_ref)
    except ExceptionRegistryError as exc:
        print(f"REFUSED ({exc.code}): {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"cards": [c.wire() for c in cards]}, indent=2))
        return 0
    if not cards:
        print("Nothing is waiting for a person.")
        return 0
    for card in cards:
        print(
            f"{card.card_id}  {card.kind}  run {card.run_ref}  until {card.deadline_at}"
        )
        print(f"  {card.summary}")
        for choice in card.choices:
            print(f"  - {choice.id}: {choice.label}. {choice.consequence}")
    return 0


def cmd_answer(args: argparse.Namespace) -> int:
    from openadapt_types.exception_card import ExceptionAnswerV1, answer_message

    from openadapt_flow.exceptions.registry import (
        ExceptionRegistryError,
        parse_time,
        roster_verifier,
    )
    from openadapt_flow.execute.keys import sign_bytes
    from openadapt_flow.jit.authority import (
        AuthorityError,
        load_encrypted_key,
        public_hex,
    )

    def refuse(code: str, message: str) -> int:
        if args.json:
            print(json.dumps({"ok": False, "code": code, "error": message}))
        else:
            print(f"REFUSED ({code}): {message} Nothing changed.", file=sys.stderr)
        return 2

    registry = _registry(args)
    card_id = (
        args.card_id if args.card_id.startswith("sha256:") else f"sha256:{args.card_id}"
    )
    try:
        found = registry.card(card_id)
    except ExceptionRegistryError as exc:
        return refuse(exc.code, str(exc))
    if found is None:
        return refuse("card_not_found", "No card has that id.")
    _run_ref, card = found
    try:
        unsigned = registry.unsigned_answer(card, args.choice, route="cli")
    except ExceptionRegistryError as exc:
        return refuse(exc.code, str(exc))
    if parse_time(unsigned["decided_at"]) > parse_time(card.deadline_at):
        return refuse("card_expired", "The card expired.")
    key_dir, root = _pin(args)
    try:
        key = load_encrypted_key(args.key or KEY_DEFAULT)
    except (AuthorityError, OSError, ValueError) as exc:
        return refuse("approver_key_unusable", str(exc))
    signed = {
        **unsigned,
        "public_key": public_hex(key),
        "signature": sign_bytes(key, answer_message(unsigned)),
    }
    del key
    answer = ExceptionAnswerV1.model_validate(signed)
    refusal = roster_verifier(key_dir, root)(answer)
    if refusal is not None:
        return refuse(refusal, "The waiting run wouldn't accept this key's answer.")
    try:
        registry.write_answer(card, answer.wire())
    except ExceptionRegistryError as exc:
        return refuse(exc.code, str(exc))
    if args.json:
        print(json.dumps({"ok": True, "answer": answer.wire()}))
    else:
        print(f"Answered {card.card_id}: {args.choice}.")
    return 0


__all__ = ["register"]
