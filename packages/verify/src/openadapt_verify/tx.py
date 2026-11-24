"""Slim facade and decorator for O(1) Wire-Scoped Transactional Agent Writes."""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, TypeVar, Union

from openadapt_flow.tx import (
    CertifiedLensPack,
    EntityLensClient,
    EntityLensSpec,
    FrameAndIntentJudge,
    LensProbeReport,
    SafeNormalizedEntailmentOracle,
    SessionWireTap,
    SQLiteAtomicRowAuthority,
    TransactionalCommitEngine,
    TxCommitResult,
    TxTask,
    WirePolicy,
    audit_legacy_tx_receipt,
    certify_lens_on_sandbox,
    induce_lens_from_har,
    load_har,
    load_network_capture_as_har,
    verify_tx_receipt,
)

__all__ = [
    "CertifiedLensPack",
    "EntityLensClient",
    "EntityLensSpec",
    "FrameAndIntentJudge",
    "LensProbeReport",
    "SQLiteAtomicRowAuthority",
    "SafeNormalizedEntailmentOracle",
    "SessionWireTap",
    "TransactionalCommitEngine",
    "TxCommitResult",
    "TxTask",
    "WirePolicy",
    "certify_lens_on_sandbox",
    "induce_lens_from_har",
    "load_lens_or_pack",
    "load_network_capture_as_har",
    "tx_verified",
    "audit_legacy_tx_receipt",
    "verify_tx_receipt",
]

F = TypeVar("F", bound=Callable[..., Any])


def load_lens_or_pack(
    source: Union[str, Path, Mapping[str, Any], EntityLensSpec, CertifiedLensPack],
) -> Union[EntityLensSpec, CertifiedLensPack]:
    """Load an ``EntityLensSpec`` or ``CertifiedLensPack`` from JSON file, dict, or object."""
    if isinstance(source, (EntityLensSpec, CertifiedLensPack)):
        return source
    if isinstance(source, (str, Path)):
        data = json.loads(Path(source).read_text(encoding="utf-8"))
    elif isinstance(source, Mapping):
        data = dict(source)
    else:
        raise TypeError(f"Unsupported lens source: {type(source)!r}")

    if "lens" in data:
        return CertifiedLensPack.model_validate(data)
    return EntityLensSpec.model_validate(data)


def tx_verified(
    lens: Union[str, Path, Mapping[str, Any], EntityLensSpec, CertifiedLensPack],
    *,
    operation: str = "update",
    id_arg: str = "entity_id",
    task_id_arg: str = "task_id",
    actor_id: str = "agent",
    workdir: Optional[Union[str, Path]] = None,
    allow_unqualified_prototype: bool = False,
) -> Callable[[F], F]:
    """Wrap a Python agent/tool write function in an O(1) TransactionalCommitEngine."""
    loaded = load_lens_or_pack(lens)
    judge = FrameAndIntentJudge()
    engine = TransactionalCommitEngine(
        loaded,
        workdir=workdir,
        judge=judge,
        allow_unqualified_prototype=allow_unqualified_prototype,
    )

    def decorate(fn: F) -> F:
        sig = inspect.signature(fn)

        @wraps(fn)
        def wrapped(*args: Any, **kwargs: Any) -> TxCommitResult:
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            call_map = dict(bound.arguments)
            entity_id = call_map.pop(id_arg, None)
            task_id = str(
                call_map.pop(task_id_arg, f"{fn.__name__}-{entity_id or 'create'}")
            )
            expected_identity = dict(call_map.pop("expected_identity", {}) or {})
            delta = dict(call_map.pop("intended_delta", call_map))
            op = "create" if operation == "create" else "update"
            task = TxTask(
                task_id=task_id,
                operation=op,  # type: ignore[arg-type]
                entity_id=str(entity_id) if entity_id is not None else None,
                expected_identity=expected_identity,
                intended_delta=delta,
                actor_id=actor_id,
            )
            return engine.wrap_action(task, lambda: fn(*args, **kwargs))

        wrapped.engine = engine  # type: ignore[attr-defined]
        return wrapped  # type: ignore[return-value]

    return decorate


def run_cli_induce(
    *,
    har_path: Optional[str],
    recording_dir: Optional[str],
    out_path: str,
    sandbox_url: Optional[str] = None,
    sample_record_path: Optional[str] = None,
    sample_update_path: Optional[str] = None,
    allow_remote_sandbox: bool = False,
) -> int:
    """CLI handler for ``openadapt-verify tx-induce``."""
    if bool(har_path) == bool(recording_dir):
        raise ValueError("pass exactly one of --har or --recording")
    if recording_dir:
        har_doc = load_network_capture_as_har(recording_dir)
    else:
        assert har_path is not None
        har_doc = load_har(Path(har_path))

    # A guessed item route is allowed only when the sandbox certifies it.
    lens = induce_lens_from_har(har_doc, allow_unobserved_item_route=bool(sandbox_url))
    if sandbox_url:
        if not sample_record_path or not sample_update_path:
            raise ValueError(
                "--sandbox-url requires --sample-record and --sample-update JSON files"
            )
        lens = lens.model_copy(update={"base_url": sandbox_url})
        sample_rec = json.loads(Path(sample_record_path).read_text(encoding="utf-8"))
        sample_upd = json.loads(Path(sample_update_path).read_text(encoding="utf-8"))
        pack = certify_lens_on_sandbox(
            lens,
            sample_record=sample_rec,
            sample_update=sample_upd,
            allow_remote_sandbox=allow_remote_sandbox,
        )
        Path(out_path).write_text(
            json.dumps(pack.model_dump(), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "certified": False,
                    "probes_passed": pack.probes_passed,
                    "sensitivity": pack.sensitivity,
                    "specificity": pack.specificity,
                    "write_mode": pack.lens.write_mode,
                    "pack_sha256": pack.pack_sha256,
                    "out": out_path,
                },
                sort_keys=True,
            )
        )
        return 0 if pack.probes_passed and not pack.probe_integrity_errors() else 4

    Path(out_path).write_text(
        json.dumps(lens.model_dump(), indent=2, sort_keys=True),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "certified": False,
                "entity_name": lens.entity_name,
                "write_mode": lens.write_mode,
                "out": out_path,
            },
            sort_keys=True,
        )
    )
    return 0


def run_cli_tx(
    *,
    lens_path: str,
    task_path: str,
    state_dir: Optional[str],
    command: list[str],
    allow_unqualified_prototype: bool = False,
) -> int:
    """CLI handler for ``openadapt-verify tx-run``."""
    loaded = load_lens_or_pack(lens_path)
    spec = loaded.lens if isinstance(loaded, CertifiedLensPack) else loaded
    task = TxTask.model_validate(
        json.loads(Path(task_path).read_text(encoding="utf-8"))
    )
    cmd = list(command)
    if cmd[:1] == ["--"]:
        cmd.pop(0)

    judge = FrameAndIntentJudge()
    if not cmd:
        engine = TransactionalCommitEngine(
            loaded,
            workdir=state_dir,
            judge=judge,
            allow_unqualified_prototype=allow_unqualified_prototype,
        )
        res = engine.execute(task)
    else:
        with SessionWireTap(
            spec.base_url,
            WirePolicy(max_mutations=1, id_fields=(spec.id_field, "id", "uuid")),
        ) as tap:
            engine = TransactionalCommitEngine(
                loaded,
                workdir=state_dir,
                judge=judge,
                wire_tap=tap,
                allow_unqualified_prototype=allow_unqualified_prototype,
            )
            env = {
                **os.environ,
                "OPENADAPT_TX_PROXY_URL": tap.base_url,
                "OPENADAPT_TX_TASK_ID": task.task_id,
            }
            res = engine.wrap_action(
                task,
                lambda: subprocess.run(
                    cmd,
                    check=False,
                    env=env,
                    stdout=sys.stderr,
                    stderr=sys.stderr,
                ),
            )

    print(json.dumps(res.model_dump(), sort_keys=True))
    if res.outcome == "CONFIRMED":
        return 0
    return {"UNCERTAIN": 3, "REFUTED": 4, "BLOCKED_PRECOMMIT": 1}.get(res.outcome, 2)
