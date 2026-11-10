"""Golden runs: prove a refactor leaves what a run does and writes unchanged.

A golden run executes a workflow with the real ``Replayer`` against a real
backend and a local fixture app, then keeps three things:

* the run report, with values that change on every run (times, durations,
  run ids, temporary paths) replaced by placeholders;
* the SHA-256 of every file the run wrote, except files whose bytes embed
  those values (the report itself, which is compared normalized instead);
* the backend action trace: every call that delivers input or changes the
  app's state (click, type, press, scroll, drag, hover, guard arming and
  cancelling, dialogs), in order, with its arguments summarized. Observations
  such as screenshots are left out, because settle loops poll a number of
  times that depends on timing.

The baselines are generated in CI on Linux, where the browser build is
pinned, and committed. A behavior-preserving change must reproduce them
exactly. See docs/design/DECOMPOSITION.md, section 3.1.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

BASELINES = Path(__file__).parent / "baselines"
WRITE_ENV = "OPENADAPT_GOLDEN_WRITE"
OUT_ENV = "OPENADAPT_GOLDEN_OUT"

# Report keys whose values differ between two identical runs: wall-clock
# times, durations, run ids and what is derived from them, and the digest of
# the observed environment (it covers the fixture server's ephemeral port).
# Everything else, digests of the committed bundle included, is compared.
VOLATILE_KEYS = re.compile(
    r"(_at|_ms|^timings|^run_id|_run_id|^run_id_sha256|^idempotency_key|"
    r"^observed_environment_digest|^started|^elapsed)$"
)
# Random per-run tokens that other fields refer to (see Normalizer).
NONCE_KEYS = re.compile(r"(fingerprint|^receipt_id|_token)$")
_FLOAT_DIGITS = 3

# Backend methods that act on the app. Everything else only observes it.
ACTION_METHODS = re.compile(
    r"^(click|right_click|hover|scroll|press|press_guarded|type_text|"
    r"type_text_guarded|drag|drag_guarded|drag_structural_guarded|"
    r"act_guarded_coordinate|act_structural|arm_[a-z_]+|cancel_[a-z_]+|"
    r"take_dialogs|expect_page_close|set_qualification_input_guard|"
    r"select_option[a-z_]*|navigate|goto)$"
)


def tracing_backend(inner: Any, trace: list[dict[str, Any]]) -> Any:
    """The same backend, recording every action call into ``trace``.

    The result is an instance of a subclass of the backend's own class that
    shares the original's instance state, so every capability check gives the
    same answer: the backend protocols are ``runtime_checkable``, and on
    Python 3.12 ``isinstance`` looks their members up statically on the
    class, which a ``__getattr__`` proxy would fail. Calls the backend makes
    on itself are recorded too.
    """
    base = type(inner)
    namespace: dict[str, Any] = {}
    for name in dir(base):
        if not ACTION_METHODS.match(name):
            continue
        static = inspect.getattr_static(base, name)
        if isinstance(static, (staticmethod, classmethod, property)) or not callable(
            static
        ):
            continue
        namespace[name] = _traced_method(name, getattr(base, name), trace)
    cls = type(f"Tracing{base.__name__}", (base,), namespace)
    traced = object.__new__(cls)
    traced.__dict__ = inner.__dict__  # shared state, not a copy
    return traced


def _traced_method(
    name: str, function: Callable[..., Any], trace: list[dict[str, Any]]
) -> Callable[..., Any]:
    def method(self: Any, *args: Any, **kwargs: Any) -> Any:
        entry: dict[str, Any] = {"call": name}
        if args:
            entry["args"] = [summarize(a) for a in args]
        if kwargs:
            entry["kwargs"] = {k: summarize(v) for k, v in sorted(kwargs.items())}
        trace.append(entry)
        try:
            result = function(self, *args, **kwargs)
        except BaseException as exc:
            entry["raised"] = type(exc).__name__
            raise
        if isinstance(result, (bytes, bytearray)):
            entry["returned"] = summarize(result)
        return result

    method.__name__ = name
    return method


def summarize(value: Any) -> Any:
    """A stable, JSON-friendly summary of a call argument."""
    if isinstance(value, (bytes, bytearray)):
        return {"sha256": hashlib.sha256(value).hexdigest()[:16], "len": len(value)}
    if isinstance(value, float):
        return round(value, _FLOAT_DIGITS)
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, Enum):
        return f"{type(value).__name__}.{value.name}"
    if isinstance(value, (list, tuple)):
        return [summarize(v) for v in value]
    if isinstance(value, dict):
        return {str(k): summarize(v) for k, v in sorted(value.items(), key=str)}
    if hasattr(value, "model_dump"):
        return {
            "type": type(value).__name__,
            **summarize(value.model_dump(mode="json")),
        }
    if hasattr(value, "__dataclass_fields__"):
        fields = {n: getattr(value, n) for n in value.__dataclass_fields__}
        return {"type": type(value).__name__, **summarize(fields)}
    return {"type": type(value).__name__}


class Normalizer:
    """Replaces values that change between identical runs with placeholders.

    Times, durations and run ids become ``<key>``. Random guard tokens and
    receipt ids (``NONCE_KEYS``) become ``<key#n>``, numbered by first
    appearance and replaced wherever the same value appears again, so the
    baseline still shows that the handle a step resolved is the handle it
    acted on. Use one instance for everything one run produced.
    """

    def __init__(self, roots: list[str]) -> None:
        self.roots = [r for r in roots if r]
        self.nonces: dict[str, str] = {}

    def collect(self, value: Any, key: str = "") -> None:
        """Number every nonce value before normalizing, in document order."""
        if isinstance(value, dict):
            for k, v in value.items():
                self.collect(v, k)
        elif isinstance(value, list):
            for v in value:
                self.collect(v, key)
        elif isinstance(value, str) and key and NONCE_KEYS.search(key.lower()):
            if value not in self.nonces:
                count = sum(1 for p in self.nonces.values() if p.startswith(f"<{key}#"))
                self.nonces[value] = f"<{key}#{count + 1}>"

    def __call__(self, value: Any, key: str = "") -> Any:
        if key and VOLATILE_KEYS.search(key.lower()) and value is not None:
            return f"<{key}>"
        if isinstance(value, dict):
            return {k: self(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [self(v, key) for v in value]
        if isinstance(value, float):
            return round(value, _FLOAT_DIGITS)
        if isinstance(value, str):
            if value in self.nonces:
                return self.nonces[value]
            for i, root in enumerate(self.roots):
                value = value.replace(root, f"<root{i}>")
            return value
        return value


def normalize(value: Any, *, roots: list[str]) -> Any:
    """Normalize one value on its own (see :class:`Normalizer`)."""
    normalizer = Normalizer(roots)
    normalizer.collect(value)
    return normalizer(value)


def artifact_hashes(run_dir: Path, *, skip: set[str]) -> dict[str, str]:
    """SHA-256 of every file a run wrote, keyed by its path in the run dir."""
    hashes = {}
    for path in sorted(p for p in run_dir.rglob("*") if p.is_file()):
        rel = path.relative_to(run_dir).as_posix()
        if rel in skip:
            continue
        hashes[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


@dataclass
class GoldenResult:
    """What a scenario produced, ready to compare with its baseline."""

    report: dict[str, Any]
    artifacts: dict[str, str]
    trace: list[dict[str, Any]]
    environment: dict[str, str] = field(default_factory=dict)
    # Ground truth read from the live app after the run: tells a safe halt
    # from a wrong action the report did not notice.
    app_state: dict[str, Any] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return {
            "environment": self.environment,
            "app_state": self.app_state,
            "report": self.report,
            "artifacts": self.artifacts,
            "trace": self.trace,
        }


def environment() -> dict[str, str]:
    """What the baseline depends on outside the code under test."""
    import importlib.metadata as metadata

    env = {"platform": sys.platform}
    try:
        env["playwright"] = metadata.version("playwright")
    except metadata.PackageNotFoundError:
        env["playwright"] = "absent"
    return env


def check_or_write(name: str, produce: Callable[[], GoldenResult]) -> None:
    """Compare a scenario with its baseline, or write the baseline.

    In write mode the scenario runs twice and the two results must be equal,
    so a value that changes between identical runs is found before it is
    committed. The actual result is also written to ``$OPENADAPT_GOLDEN_OUT``
    when set, so CI can upload it.
    """
    import pytest

    baseline_path = BASELINES / f"{name}.json"
    writing = os.environ.get(WRITE_ENV) == "1"
    if not writing:
        if not baseline_path.is_file():
            pytest.skip(f"no baseline for {name}; generate it in CI ({WRITE_ENV}=1)")
        expected = json.loads(baseline_path.read_text(encoding="utf-8"))
        if expected["environment"].get("platform") != sys.platform:
            pytest.skip(
                f"baseline recorded on {expected['environment'].get('platform')}; "
                "screens render differently on other platforms"
            )

    first = produce()
    actual = first.as_json()
    _write_out(name, actual)

    if writing:
        second = produce().as_json()
        assert _canonical(second) == _canonical(actual), (
            f"{name}: two identical runs differ; normalize the changing value "
            "before writing a baseline:\n" + _first_difference(actual, second)
        )
        BASELINES.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(_canonical(actual), encoding="utf-8")
        return

    if expected["environment"] != actual["environment"]:
        raise AssertionError(
            f"{name}: baseline environment {expected['environment']} differs from "
            f"{actual['environment']}; regenerate the baselines in CI "
            f"({WRITE_ENV}=1) in a change of its own."
        )
    assert _canonical(actual) == _canonical(expected), (
        f"{name}: the run differs from its golden baseline:\n"
        + _first_difference(expected, actual)
    )


def _canonical(value: Any) -> str:
    return json.dumps(value, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def _write_out(name: str, actual: dict[str, Any]) -> None:
    out = os.environ.get(OUT_ENV)
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        (Path(out) / f"{name}.json").write_text(_canonical(actual), encoding="utf-8")


def _first_difference(expected: Any, actual: Any, path: str = "$") -> str:
    if type(expected) is not type(actual):
        return f"{path}: {expected!r} != {actual!r}"
    if isinstance(expected, dict):
        for key in sorted(set(expected) | set(actual)):
            if key not in expected:
                return f"{path}.{key}: unexpected {actual[key]!r}"[:2000]
            if key not in actual:
                return f"{path}.{key}: missing (expected {expected[key]!r})"[:2000]
            if expected[key] != actual[key]:
                return _first_difference(expected[key], actual[key], f"{path}.{key}")
    if isinstance(expected, list):
        for i, (a, b) in enumerate(zip(expected, actual)):
            if a != b:
                return _first_difference(a, b, f"{path}[{i}]")
        if len(expected) != len(actual):
            return f"{path}: length {len(expected)} != {len(actual)}"
    return f"{path}: {expected!r} != {actual!r}"[:2000]
