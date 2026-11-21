"""``openadapt wrap --example``: the whole wrap loop on a bundled sample task.

One command shows what ``openadapt wrap --task`` does, with nothing to set up
but a model key:

1. A bundled synthetic app starts on this computer (127.0.0.1 only). The
   task, its record and its read-back come from a packaged data file
   (``examples/<name>.json``), not from this module.
2. Run 1: OpenAdapt's built-in agent does the task in a headless browser.
   The save is read back from the app's records, the run is compiled into a
   draft program, a canary runs with another value, and the draft is admitted.
3. Run 2: the same task with a new value runs the compiled program. It makes
   no model calls, and the save is read back again.

Everything the example creates is a throwaway: the approver that authorizes
run 1 and the key that admits the draft are made for this run in its own
folder and are never written to the user's key directory. The checks proposed
from run 1's read-back are confirmed automatically, which is acceptable only
because the app and its data are synthetic. Real tasks use ``openadapt wrap
--task`` with ``--person`` or ``--admit-key``.

The sample app shows only synthetic data, so its screenshots are sent to the
model unscrubbed, under the recorded opt-out (see
:func:`openadapt_flow.privacy.outbound_opt_out`).
"""

from __future__ import annotations

import contextlib
import importlib
import json
import math
import os
import secrets
import sys
import time
import urllib.request
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from importlib.resources import files as resource_files
from pathlib import Path
from typing import IO, Any, Callable, Optional

__all__ = [
    "DEFAULT_EXAMPLE",
    "ExampleError",
    "ExamplePack",
    "load_pack",
    "run_example",
]

DEFAULT_EXAMPLE = "clinic-note"
PACK_SCHEMA = "openadapt.wrap-example/v1"
#: Why the sample's screenshots go to the model unscrubbed. Recorded in the
#: opt-out ledger before anything is sent.
UNSCRUBBED_REASON = (
    "openadapt wrap --example: bundled synthetic sample app, no real data"
)
_PACK_KEYS = {
    "schema",
    "name",
    "summary",
    "server",
    "app_label",
    "path",
    "reset_path",
    "instruction",
    "parameters",
    "expect",
    "on_screen",
    "drop_fields",
    "verifier",
    "runs",
    "canary_params",
}


class ExampleError(RuntimeError):
    """The example could not start. Nothing was sent to a model."""


@dataclass(frozen=True)
class ExamplePack:
    """One bundled sample task, read from packaged data."""

    name: str
    summary: str
    server: str
    app_label: str
    path: str
    reset_path: str
    instruction: str
    parameters: tuple[str, ...]
    expect: dict[str, str]
    on_screen: tuple[str, ...]
    drop_fields: tuple[str, ...]
    verifier: dict[str, Any]
    runs: tuple[dict[str, str], dict[str, str]]
    canary_params: tuple[dict[str, str], ...]


def load_pack(name: str = DEFAULT_EXAMPLE) -> ExamplePack:
    """Read the packaged example ``name``; refuse anything malformed."""

    if not isinstance(name, str) or not name.replace("-", "").isalnum():
        raise ExampleError("unknown example")
    path = (
        resource_files("openadapt_flow.wrap")
        .joinpath("examples")
        .joinpath(name + ".json")
    )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ExampleError("unknown example") from None
    if type(raw) is not dict or set(raw) != _PACK_KEYS or raw["schema"] != PACK_SCHEMA:
        raise ExampleError("the bundled example is malformed")
    runs = raw["runs"]
    parameters = tuple(raw["parameters"])
    if (
        type(runs) is not list
        or len(runs) != 2
        or any(type(r) is not dict or set(r) != set(parameters) for r in runs)
        or runs[0] == runs[1]
    ):
        raise ExampleError("the bundled example needs two different runs")
    return ExamplePack(
        name=str(raw["name"]),
        summary=str(raw["summary"]),
        server=str(raw["server"]),
        app_label=str(raw["app_label"]),
        path=str(raw["path"]),
        reset_path=str(raw["reset_path"]),
        instruction=str(raw["instruction"]),
        parameters=parameters,
        expect={str(k): str(v) for k, v in raw["expect"].items()},
        on_screen=tuple(str(s) for s in raw["on_screen"]),
        drop_fields=tuple(str(s) for s in raw["drop_fields"]),
        verifier=dict(raw["verifier"]),
        runs=(dict(runs[0]), dict(runs[1])),
        canary_params=tuple(dict(c) for c in raw["canary_params"]),
    )


def _serve(pack: ExamplePack) -> tuple[str, Callable[[], None]]:
    module, _, attr = pack.server.partition(":")
    if not module.startswith("openadapt_flow.") or not attr:
        # Only an app bundled with this package; never an arbitrary import.
        raise ExampleError("the bundled example names no bundled app")
    serve = getattr(importlib.import_module(module), attr)
    base, _db, stop = serve()
    if not base.startswith("http://127.0.0.1:"):
        stop()
        raise ExampleError("the sample app must listen on 127.0.0.1")
    return base if base.endswith("/") else base + "/", stop


def _http_json(url: str, *, method: str = "GET", body: Any = None) -> Any:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - 127.0.0.1
        return json.loads(response.read().decode("utf-8") or "null")


@contextlib.contextmanager
def _env(values: Mapping[str, str]) -> Iterator[None]:
    """Set process environment values for the example only, then restore."""

    saved = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _admitter(key_dir: Path) -> Any:
    """A throwaway registered admitter key, kept in this example's folder."""

    from openadapt_flow.jit import authority

    pem = key_dir.parent / "keyfiles" / "example-admitter.pem"
    authority.register_principal(
        key_dir,
        role="admitter_key",
        principal_id="example:wrap-admitter",
        public_key_hex=authority.generate_encrypted_key(pem),
    )
    return authority.principal_from_key(authority.load_encrypted_key(pem), key_dir)


def choose_provider(
    requested: Optional[str], env: Optional[Mapping[str, str]] = None
) -> Optional[str]:
    """The provider the example uses: the one asked for, else the key present."""

    from openadapt_flow.jit.keys import PROVIDER_ENV

    if requested is not None:
        return requested
    environ = os.environ if env is None else env
    if (environ.get(PROVIDER_ENV["anthropic"]) or "").strip():
        return "claude"
    if (environ.get(PROVIDER_ENV["openai"]) or "").strip():
        return "openai"
    return None


def _money(value: Any, known: bool) -> str:
    if not known or isinstance(value, bool) or not isinstance(value, (int, float)):
        return "unknown"
    if not math.isfinite(float(value)):
        return "unknown"
    return f"${float(value):.4f}"


def _calls(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, int):
        return "unknown"
    return f"{value} model call" + ("" if value == 1 else "s")


def run_example(
    *,
    agent: Any,
    model_key: Any,
    out_dir: Path,
    stream: IO[str] = sys.stdout,
    pack: Optional[ExamplePack] = None,
    as_json: bool = False,
) -> int:
    """Run the bundled example into ``out_dir`` and print what happened.

    ``agent`` is a provider agent from
    :func:`openadapt_flow.integrations.agents.providers.build_agent`, with its
    spend cap set. Returns 0 when run 1 was admitted and run 2 made no model
    calls and was read back VERIFIED; 1 otherwise.
    """

    from openadapt_flow.integrations.agents.skills import SkillCache
    from openadapt_flow.jit import authority
    from openadapt_flow.privacy import OUTBOUND_OPT_OUT_ENV
    from openadapt_flow.teach_authorization_fixture import demo_authority
    from openadapt_flow.wrap import WrappedTask

    pack = pack or load_pack()
    out_dir = Path(out_dir).expanduser().resolve()
    if out_dir.exists() and any(out_dir.iterdir()):
        raise ExampleError(f"{out_dir} is not empty; give the example a new folder")
    out_dir.mkdir(parents=True, exist_ok=True)

    def say(text: str = "") -> None:
        if not as_json:
            print(text, file=stream, flush=True)

    base, stop = _serve(pack)
    try:
        url = base + pack.path

        def reset() -> None:
            _http_json(base + pack.reset_path, method="POST", body={})

        verify = out_dir / "verify.yaml"
        import yaml

        verify.write_text(
            yaml.safe_dump(
                {"verifier": {**pack.verifier, "base_url": base.rstrip("/")}},
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        key_dir = out_dir / "keys"
        env = {
            # Throwaway keys for this folder only; never the user's key dir.
            authority.KEY_PASSPHRASE_ENV: secrets.token_hex(16),
            OUTBOUND_OPT_OUT_ENV: UNSCRUBBED_REASON,
        }
        confirmed: list[dict[str, Any]] = []

        def offer(proposal: dict[str, Any]) -> str:
            # Synthetic app only: the checks proposed from run 1's read-back
            # are confirmed without a person. Real tasks name --person.
            confirmed.append(proposal)
            return "example:auto-confirmed"

        with _env(env):
            cache = SkillCache(
                url=url,
                verify=verify,
                expect=pack.expect,
                on_screen=list(pack.on_screen),
                offer=offer,
                admitter=_admitter(key_dir),
                canary_params=[dict(c) for c in pack.canary_params],
                drop_fields=list(pack.drop_fields),
                task_id=f"example.{pack.name}",
                cache_dir=out_dir / "cache",
                key_dir=key_dir,
                model_key=model_key,
                reset=reset,
                headless=True,
                on_warning=lambda _text: None,
            )
            task = WrappedTask(
                cache=cache,
                agent=agent,
                instruction=pack.instruction,
                parameters=pack.parameters,
                runs_dir=out_dir / "runs",
            )
            first_values, second_values = pack.runs
            model = getattr(agent, "model", None) or "the agent"
            say(f"OpenAdapt wrap example: {pack.app_label} on this computer, headless.")
            say(f"Task: {pack.instruction}")
            say(
                "The app's data is synthetic, so its screenshots go to the model "
                "unscrubbed; each send is logged in the opt-out ledger."
            )
            say()
            say(f"Run 1: {model} does the task ({_values(first_values)}).")
            reset()
            # A throwaway approver authorizes exactly this run's values.
            cache.common["teach_authorization"] = demo_authority(
                out_dir,
                task_id=cache.task_id(pack.instruction, first_values),
                url=url,
                record=pack.expect,
                inputs=first_values,
                constants=pack.on_screen,
            )
            first = task.call("example-run-1", first_values)
            say(
                f"  {_calls(first.get('model_calls'))}, "
                f"{_money(first.get('cost_usd'), bool(first.get('usage_known')))}, "
                f"{first.get('seconds')} s; status {first.get('status')}"
            )
            second: dict[str, Any] = {}
            readback: list[dict[str, Any]] = []
            admitted = (
                first.get("path") == "agent" and first.get("status") == "admitted"
            )
            if admitted:
                say(
                    "  The save was read back, the run was compiled, a canary ran, "
                    "and a throwaway key made for this example admitted it."
                )
                say()
                say(
                    f"Run 2: the compiled program, new input ({_values(second_values)})."
                )
                reset()
                second = task.call("example-run-2", second_values)
                say(
                    f"  {_calls(second.get('model_calls'))}, "
                    f"{_money(second.get('cost_usd'), bool(second.get('usage_known')))}, "
                    f"{second.get('seconds')} s; status {second.get('status')}"
                )
                readback = _readback(base, pack)
            else:
                say("  Run 1 was not admitted, so there is no program to run.")
            ok = (
                admitted
                and second.get("path") == "skill"
                and second.get("verified") is True
                and second.get("model_calls") == 0
            )
            if ok:
                say()
                say(
                    f"Read back from the app's records ({pack.verifier['records_path']}):"
                )
                for row in readback:
                    say("  " + json.dumps(row, sort_keys=True))
                say()
                say(
                    "Cost: run 1 "
                    f"{_money(first.get('cost_usd'), bool(first.get('usage_known')))}"
                    f" ({_calls(first.get('model_calls'))}); run 2 "
                    f"{_money(second.get('cost_usd'), bool(second.get('usage_known')))}"
                    f" ({_calls(second.get('model_calls'))}). Later runs of this "
                    "task make no model calls."
                )
            say(f"Files: {out_dir}")
            summary = {
                "schema": "openadapt.wrap-example-result/v1",
                "example": pack.name,
                "model": model,
                "ok": ok,
                "run1": _public(first),
                "run2": _public(second),
                "readback": readback,
                "checks_confirmed_automatically": len(confirmed),
                "out_dir": str(out_dir),
                "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            (out_dir / "example-result.json").write_text(
                json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            if as_json:
                print(json.dumps(summary, sort_keys=True), file=stream)
            return 0 if ok else 1
    finally:
        stop()


def _values(values: Mapping[str, str]) -> str:
    return ", ".join(f'{k} "{v}"' for k, v in values.items())


_PUBLIC_RESULT_KEYS = (
    "path",
    "status",
    "verified",
    "model_calls",
    "cost_usd",
    "usage_known",
    "seconds",
    "receipt_outcome",
)


def _public(result: Mapping[str, Any]) -> dict[str, Any]:
    return {k: result.get(k) for k in _PUBLIC_RESULT_KEYS if k in result}


def _readback(base: str, pack: ExamplePack) -> list[dict[str, Any]]:
    """The task's record as the app's own records endpoint reports it."""

    rows = _http_json(base + pack.verifier["records_path"].lstrip("/"))
    records = (
        rows.get(pack.verifier["records_key"], []) if isinstance(rows, dict) else []
    )
    mine = [
        r
        for r in records
        if isinstance(r, dict)
        and all(str(r.get(k)) == v for k, v in pack.expect.items())
    ]
    return [{k: v for k, v in r.items() if k not in pack.drop_fields} for r in mine]
