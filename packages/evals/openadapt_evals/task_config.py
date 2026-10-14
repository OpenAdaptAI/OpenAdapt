"""Custom task configuration supporting YAML and WAA JSON formats.

Lets users define tasks with setup commands and evaluation checks in
simple YAML files or native WAA JSON format, without forking WAA or
modifying the Docker image.  The WAA server already accepts evaluator
configs in POST /evaluate -- this module translates both formats into
that structure.

Usage:
    from openadapt_evals.task_config import TaskConfig

    # Load a single task from YAML
    task = TaskConfig.from_yaml("tasks/change-font.yaml")
    benchmark_task = task.to_benchmark_task()

    # Load a single task from WAA JSON
    task = TaskConfig.from_waa_json("examples/writer/abc123.json")

    # Load all tasks from a directory (YAML + JSON auto-detected)
    tasks = TaskConfig.from_dir("tasks/")

    # Load from a WAA examples directory tree (examples/{domain}/{id}.json)
    tasks = TaskConfig.from_waa_dir("evaluation_examples_windows/examples/")
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from openadapt_evals.adapters.base import BenchmarkTask, EvaluationUnavailableError
from openadapt_evals.evaluation.receipts import (
    ReceiptValidationError,
    parse_strict_json_response,
)

logger = logging.getLogger(__name__)


@dataclass
class TaskCheck:
    """A single evaluation check."""

    check: str  # "command", "file", "screenshot", "python"
    # command check
    run: str | None = None
    expect: str | None = None
    match: str = "exact"  # "exact", "contains", "regex", "fuzzy"
    # file check
    path: str | None = None
    exists: bool = True
    contains: str | None = None
    # screenshot check
    description: str | None = None
    # python check
    code: str | None = None


@dataclass
class Milestone:
    """An intermediate checkpoint for dense rewards."""

    name: str
    check: TaskCheck


@dataclass
class TaskConfig:
    """A custom task definition loaded from YAML or WAA JSON."""

    name: str
    id: str
    domain: str
    setup: list[dict[str, Any]]
    checks: list[TaskCheck]
    combine: str  # "and" | "or"
    max_steps: int
    milestones: list[Milestone]

    # Raw WAA evaluator preserved for lossless round-trip.  When present,
    # to_waa_config() emits this directly instead of re-translating checks.
    _raw_evaluator: dict[str, Any] | None = field(
        default=None, repr=False, compare=False
    )

    @classmethod
    def from_yaml(cls, path: str) -> TaskConfig:
        """Load a task config from a YAML file."""
        with open(path) as f:
            data = yaml.safe_load(f)

        task_id = data.get("id", f"custom-{uuid.uuid4().hex[:8]}")
        name = data.get("name", Path(path).stem)
        domain = data.get("domain", "desktop")
        max_steps = data.get("max_steps", 15)
        combine = data.get("combine", "and")

        # Parse setup commands
        setup = []
        for item in data.get("setup", []):
            if isinstance(item, dict):
                setup.append(item)
            else:
                setup.append({"execute": str(item)})

        # Parse evaluation checks
        checks = []
        for item in data.get("evaluate", []):
            checks.append(TaskCheck(**{k: v for k, v in item.items()}))

        # Parse milestones
        milestones = []
        for item in data.get("milestones", []):
            ms_name = item.get("name", "milestone")
            check_fields = {k: v for k, v in item.items() if k != "name"}
            milestones.append(Milestone(name=ms_name, check=TaskCheck(**check_fields)))

        return cls(
            name=name,
            id=task_id,
            domain=domain,
            setup=setup,
            checks=checks,
            combine=combine,
            max_steps=max_steps,
            milestones=milestones,
        )

    @classmethod
    def from_waa_json(cls, path: str) -> TaskConfig:
        """Load a task config from a WAA native JSON file.

        WAA JSON files live in ``evaluation_examples_windows/examples/
        {domain}/{task_id}.json`` and contain fields like ``id``,
        ``instruction``, ``config`` (setup array), and ``evaluator``.

        Common evaluator patterns (exact_match, contains, fuzzy_match with
        vm_command_line / vm_file / literal) are reverse-translated into
        :class:`TaskCheck` objects.  Evaluators that use specialised WAA
        metric functions (compare_table, compare_font_names, etc.) are
        preserved as-is for lossless round-trip via :meth:`to_waa_config`.

        Args:
            path: Path to a ``.json`` file in WAA native format.

        Returns:
            A :class:`TaskConfig` instance.
        """
        with open(path, encoding="utf-8") as f:
            data = json.load(f)

        task_id = data.get("id", Path(path).stem)
        name = data.get("instruction", data.get("task", Path(path).stem))
        domain = _infer_domain(data, path)
        max_steps = data.get("max_steps", 15)

        # --- setup -------------------------------------------------------
        setup = _parse_waa_setup(data.get("config", []))

        # --- evaluator ---------------------------------------------------
        evaluator = data.get("evaluator", {})
        checks, combine, raw_evaluator = _parse_waa_evaluator(evaluator)

        return cls(
            name=name,
            id=task_id,
            domain=domain,
            setup=setup,
            checks=checks,
            combine=combine,
            max_steps=max_steps,
            milestones=[],
            _raw_evaluator=raw_evaluator,
        )

    @classmethod
    def from_waa_dir(cls, dir_path: str) -> list[TaskConfig]:
        """Load all WAA JSON task configs from a directory tree.

        Expects the WAA layout ``{dir_path}/{domain}/{task_id}.json`` (i.e.
        the ``examples/`` directory inside ``evaluation_examples_windows``).

        Args:
            dir_path: Root of the WAA examples tree.

        Returns:
            List of :class:`TaskConfig` instances, sorted by file path.
        """
        tasks: list[TaskConfig] = []
        base = Path(dir_path)
        if not base.is_dir():
            logger.warning("WAA examples dir not found: %s", dir_path)
            return tasks

        for json_file in sorted(base.rglob("*.json")):
            try:
                tasks.append(cls.from_waa_json(str(json_file)))
            except Exception as exc:
                logger.warning("Skipping %s: %s", json_file, exc)
        return tasks

    @classmethod
    def from_dir(cls, dir_path: str) -> list[TaskConfig]:
        """Load all task configs from a directory (YAML and JSON).

        Files are auto-detected by extension:
        - ``.yaml`` / ``.yml`` -- loaded as YAML task configs
        - ``.json`` -- loaded as WAA native JSON task configs
        """
        tasks: list[TaskConfig] = []
        for fname in sorted(os.listdir(dir_path)):
            full = os.path.join(dir_path, fname)
            try:
                if fname.endswith((".yaml", ".yml")):
                    tasks.append(cls.from_yaml(full))
                elif fname.endswith(".json"):
                    tasks.append(cls.from_waa_json(full))
            except Exception as exc:
                logger.warning("Skipping %s: %s", fname, exc)
        return tasks

    def to_waa_config(self) -> dict[str, Any]:
        """Translate to WAA's native JSON format for /evaluate.

        If this TaskConfig was loaded from WAA JSON and carries a raw
        evaluator (one that uses specialised metric functions), it is
        emitted as-is for lossless round-trip.
        """
        config: dict[str, Any] = {
            "task_id": self.id,
            "instruction": self.name,
            "config": self._translate_setup(),
        }

        if self._raw_evaluator is not None:
            config["evaluator"] = self._raw_evaluator
        else:
            evaluator = self._translate_evaluator()
            if evaluator:
                config["evaluator"] = evaluator

        return config

    def to_benchmark_task(self) -> BenchmarkTask:
        """Create a BenchmarkTask for use with adapters."""
        waa_config = self.to_waa_config()
        return BenchmarkTask(
            task_id=self.id,
            instruction=self.name,
            domain=self.domain,
            time_limit_steps=self.max_steps,
            raw_config=waa_config,
            evaluation_spec=waa_config.get("evaluator"),
        )

    def _translate_setup(self) -> list[dict[str, Any]]:
        """Translate setup items to WAA config format."""
        result = []
        for item in self.setup:
            if "launch" in item:
                result.append({
                    "type": "launch",
                    "parameters": {"command": item["launch"]},
                })
            elif "open" in item:
                result.append({
                    "type": "open",
                    "parameters": {"path": item["open"]},
                })
            elif "execute" in item:
                cmd = item["execute"].strip()
                # WAA execute handler runs the command via subprocess.
                # Pass as a single string for shell execution.
                result.append({
                    "type": "execute",
                    "parameters": {"command": cmd},
                })
            elif "sleep" in item:
                result.append({
                    "type": "sleep",
                    "parameters": {"seconds": float(item["sleep"])},
                })
            elif "download" in item:
                dl = item["download"]
                result.append({
                    "type": "download",
                    "parameters": {"url": dl["url"], "path": dl["dest"]},
                })
            else:
                # Pass through raw WAA setup items
                result.append(item)
        return result

    def _translate_evaluator(self) -> dict[str, Any] | None:
        """Translate checks to WAA evaluator format."""
        # Separate server-side checks from client-side (VLM) checks
        server_checks = [c for c in self.checks if c.check != "screenshot"]
        if not server_checks:
            return None

        if len(server_checks) == 1:
            return self._translate_check(server_checks[0])

        # Multiple checks — use conjunction
        metrics = [self._translate_check(c) for c in server_checks]
        return {
            "func": [m["func"] for m in metrics],
            "result": [m["result"] for m in metrics],
            "expected": [m["expected"] for m in metrics],
            "conj": self.combine,
        }

    def _translate_check(self, check: TaskCheck) -> dict[str, Any]:
        """Translate a single check to WAA evaluator format."""
        if check.check == "command":
            match_func = {
                "exact": "exact_match",
                "contains": "contains",
                "regex": "regex_match",
                "fuzzy": "fuzzy_match",
            }.get(check.match, "exact_match")
            return {
                "func": match_func,
                "result": {
                    "type": "vm_command_line",
                    "command": check.run,
                },
                "expected": {
                    "type": "literal",
                    "value": check.expect or "",
                },
            }
        elif check.check == "file":
            if check.contains:
                return {
                    "func": "contains",
                    "result": {
                        "type": "vm_file",
                        "path": check.path,
                    },
                    "expected": {
                        "type": "literal",
                        "value": check.contains,
                    },
                }
            return {
                "func": "exact_match",
                "result": {
                    "type": "vm_command_line",
                    "command": f'python -c "import os; print(os.path.exists(r\'{check.path}\'))"',
                },
                "expected": {
                    "type": "literal",
                    "value": "True",
                },
            }
        elif check.check == "python":
            # Wrap code so it prints True/False
            escaped = check.code.replace('"', '\\"').replace("\n", "\\n")
            return {
                "func": "exact_match",
                "result": {
                    "type": "vm_command_line",
                    "command": f'python -c "{escaped}"',
                },
                "expected": {
                    "type": "literal",
                    "value": "True",
                },
            }
        else:
            raise ValueError(f"Cannot translate check type '{check.check}' to WAA format")

    def get_vlm_checks(self) -> list[TaskCheck]:
        """Return checks that need client-side VLM evaluation."""
        return [c for c in self.checks if c.check == "screenshot"]

    def evaluate_milestones(
        self,
        screenshot: bytes,
        server_url: str,
    ) -> tuple[int, int]:
        """Evaluate milestones and return (passed, total).

        Server-side milestones are evaluated via /execute_windows.
        Screenshot milestones are evaluated via VLM.
        """
        passed = 0
        total = len(self.milestones)
        if total == 0:
            raise EvaluationUnavailableError(
                "No milestone evaluation contract is configured",
                error_type="evaluation",
            )

        for ms in self.milestones:
            try:
                if ms.check.check == "screenshot":
                    if not ms.check.description:
                        raise ValueError("Screenshot milestone has no description")
                    from openadapt_evals.vlm_evaluator import vlm_judge

                    success, _ = vlm_judge(screenshot, ms.check.description)
                    if success:
                        passed += 1
                elif ms.check.check == "command":
                    if not isinstance(ms.check.run, str) or not ms.check.run.strip():
                        raise ValueError("Command milestone has no command")
                    if ms.check.expect is None:
                        raise ValueError("Command milestone has no expected result")
                    result = self._run_vm_command(ms.check.run, server_url)
                    if self._check_match(result, ms.check.expect, ms.check.match):
                        passed += 1
                else:
                    raise ValueError(
                        f"Unsupported milestone check type: {ms.check.check!r}"
                    )
            except Exception as exc:
                if isinstance(exc, EvaluationUnavailableError):
                    raise
                raise EvaluationUnavailableError(
                    f"Milestone {ms.name!r} could not be evaluated: {exc}",
                    error_type="evaluation",
                ) from exc

        logger.info("Milestones: %d/%d passed", passed, total)
        return passed, total

    @staticmethod
    def _run_vm_command(command: str, server_url: str) -> str:
        """Execute a shell command and return stdout from a proved receipt.

        WAA's /execute_windows endpoint runs Python code via exec().
        Shell commands (PowerShell, cmd) must be wrapped in subprocess.
        """
        import json

        import requests

        if not isinstance(command, str) or not command.strip():
            raise EvaluationUnavailableError(
                "VM command must be a non-empty string",
                error_type="evaluation",
            )

        # Print one structured receipt. Plain stdout cannot distinguish an
        # empty successful command from a failed command that produced no text.
        python_code = (
            "import json, subprocess; "
            f"r = subprocess.run({json.dumps(command)}, shell=True, "
            "capture_output=True, text=True); "
            "print(json.dumps({'returncode': r.returncode, "
            "'stdout': r.stdout, 'stderr': r.stderr}))"
        )

        try:
            resp = requests.post(
                f"{server_url}/execute_windows",
                json={"command": python_code},
                timeout=30,
            )
        except requests.RequestException as exc:
            raise EvaluationUnavailableError(
                f"VM command request failed: {exc}",
                error_type="infrastructure",
            ) from exc
        if resp.status_code != 200:
            raise EvaluationUnavailableError(
                f"VM command returned HTTP {resp.status_code}",
                error_type="infrastructure",
            )
        try:
            payload = parse_strict_json_response(resp, context="VM command")
        except ReceiptValidationError as exc:
            raise EvaluationUnavailableError(
                "VM command response was not valid JSON",
                error_type="infrastructure",
            ) from exc
        if not isinstance(payload, dict):
            raise EvaluationUnavailableError(
                "VM command response was not an object",
                error_type="infrastructure",
            )
        for success_field in ("success", "ok"):
            if success_field in payload and payload[success_field] is not True:
                raise EvaluationUnavailableError(
                    f"VM command endpoint returned {success_field}="
                    f"{payload[success_field]!r}",
                    error_type="infrastructure",
                )
        if "failed" in payload and payload["failed"] is not False:
            raise EvaluationUnavailableError(
                f"VM command endpoint returned failed={payload['failed']!r}",
                error_type="infrastructure",
            )
        if (
            "delivery_state" in payload
            and payload["delivery_state"] != "delivered"
        ):
            raise EvaluationUnavailableError(
                "VM command endpoint did not confirm delivery: "
                f"{payload['delivery_state']!r}",
                error_type="infrastructure",
            )
        for response_field in ("error", "stderr"):
            value = payload.get(response_field)
            if value not in (None, ""):
                raise EvaluationUnavailableError(
                    f"VM command endpoint returned {response_field}: {value}",
                    error_type="infrastructure",
                )
        outer_returncode = payload.get("returncode")
        if outer_returncode is not None and (
            isinstance(outer_returncode, bool)
            or not isinstance(outer_returncode, int)
            or outer_returncode != 0
        ):
            raise EvaluationUnavailableError(
                f"VM command endpoint returned status {outer_returncode!r}",
                error_type="infrastructure",
            )

        output = payload.get("output")
        if not isinstance(output, str):
            raise EvaluationUnavailableError(
                "VM command response did not contain a string receipt",
                error_type="infrastructure",
            )
        def reject_duplicate_fields(
            pairs: list[tuple[str, Any]],
        ) -> dict[str, Any]:
            parsed: dict[str, Any] = {}
            for key, value in pairs:
                if key in parsed:
                    raise ValueError(f"duplicate field {key!r}")
                parsed[key] = value
            return parsed

        try:
            receipt = json.loads(output, object_pairs_hook=reject_duplicate_fields)
        except (json.JSONDecodeError, ValueError) as exc:
            raise EvaluationUnavailableError(
                "VM command response did not contain a command receipt",
                error_type="evaluation",
            ) from exc
        if not isinstance(receipt, dict) or set(receipt) != {
            "returncode",
            "stdout",
            "stderr",
        }:
            raise EvaluationUnavailableError(
                "VM command receipt was malformed",
                error_type="evaluation",
            )
        returncode = receipt["returncode"]
        stdout = receipt["stdout"]
        stderr = receipt["stderr"]
        if (
            isinstance(returncode, bool)
            or not isinstance(returncode, int)
            or returncode != 0
        ):
            raise EvaluationUnavailableError(
                f"VM command failed with return code {returncode!r}",
                error_type="evaluation",
            )
        if not isinstance(stdout, str) or not isinstance(stderr, str):
            raise EvaluationUnavailableError(
                "VM command receipt output fields were malformed",
                error_type="evaluation",
            )
        if stderr.strip():
            raise EvaluationUnavailableError(
                f"VM command wrote to stderr: {stderr.strip()}",
                error_type="evaluation",
            )
        return stdout.strip()

    def evaluate_checks_local(
        self, screenshot: bytes, server_url: str,
    ) -> float:
        """Evaluate the task's own ``checks`` without the /evaluate endpoint.

        Uses the same logic as ``evaluate_milestones`` but on the top-level
        ``evaluate:`` entries.  This is a fallback for when the WAA
        ``/evaluate`` endpoint is unavailable.

        Returns:
            1.0 if checks pass (respecting ``combine`` mode), else 0.0.
        """
        if not self.checks:
            raise EvaluationUnavailableError(
                "No local evaluation checks are configured",
                error_type="evaluation",
            )

        results: list[bool] = []
        for check in self.checks:
            try:
                if check.check == "screenshot":
                    if not check.description:
                        raise ValueError("Screenshot check has no description")
                    from openadapt_evals.vlm_evaluator import vlm_judge
                    success, _ = vlm_judge(screenshot, check.description)
                    results.append(success)
                elif check.check == "command":
                    if not isinstance(check.run, str) or not check.run.strip():
                        raise ValueError("Command check has no command")
                    if check.expect is None:
                        raise ValueError("Command check has no expected result")
                    result = self._run_vm_command(check.run, server_url)
                    results.append(
                        self._check_match(result, check.expect, check.match)
                    )
                else:
                    raise ValueError(
                        f"Unsupported local check type: {check.check!r}"
                    )
            except Exception as exc:
                if isinstance(exc, EvaluationUnavailableError):
                    raise
                raise EvaluationUnavailableError(
                    f"Local {check.check!r} check could not be evaluated: {exc}",
                    error_type="evaluation",
                ) from exc

        if not results:
            raise EvaluationUnavailableError(
                "Local evaluation produced no check results",
                error_type="evaluation",
            )

        if self.combine == "or":
            return 1.0 if any(results) else 0.0
        if self.combine == "and":
            return 1.0 if all(results) else 0.0
        raise EvaluationUnavailableError(
            f"Unsupported local check combination: {self.combine!r}",
            error_type="evaluation",
        )

    @staticmethod
    def _check_match(actual: str, expected: str, match_type: str) -> bool:
        """Check if actual matches expected using the specified method."""
        if not isinstance(actual, str) or not isinstance(expected, str):
            raise ValueError("Check values must be strings")
        if match_type == "exact":
            return actual.strip() == expected.strip()
        elif match_type == "contains":
            if not expected.strip():
                raise ValueError("Contains checks require a non-empty expected value")
            return expected.strip() in actual
        elif match_type == "regex":
            import re
            if not expected.strip():
                raise ValueError("Regex checks require a non-empty expected pattern")
            return bool(re.search(expected, actual))
        elif match_type == "fuzzy":
            import difflib
            if not expected.strip():
                raise ValueError("Fuzzy checks require a non-empty expected value")
            return difflib.SequenceMatcher(None, actual, expected).ratio() >= 0.8
        raise ValueError(f"Unsupported match type: {match_type!r}")


def evaluate_milestones_screenshot(
    task_config: TaskConfig,
    screenshot: bytes,
    *,
    model: str = "gpt-4.1-mini",
) -> float:
    """Evaluate a screenshot-only milestone contract using a VLM.

    Every required milestone must be measurable from the screenshot. A caller
    must use :meth:`TaskConfig.evaluate_milestones` for mixed screenshot and
    command contracts.

    Args:
        task_config: A TaskConfig with milestones defined.
        screenshot: PNG screenshot bytes.
        model: VLM model name for screenshot evaluation.

    Returns:
        Fraction of milestones passed (0.0 to 1.0).

    Raises:
        EvaluationUnavailableError: If the contract is empty or any required
            milestone cannot be measured from the screenshot.
    """
    if not task_config.milestones:
        raise EvaluationUnavailableError(
            "No milestone evaluation contract is configured",
            error_type="evaluation",
        )
    unsupported = [
        milestone.name
        for milestone in task_config.milestones
        if milestone.check.check != "screenshot"
    ]
    if unsupported:
        raise EvaluationUnavailableError(
            "Screenshot-only evaluation cannot measure required milestones: "
            + ", ".join(repr(name) for name in unsupported),
            error_type="evaluation",
        )

    from openadapt_evals.vlm_evaluator import vlm_judge

    passed = 0
    for milestone in task_config.milestones:
        if not milestone.check.description:
            raise EvaluationUnavailableError(
                f"Screenshot milestone {milestone.name!r} has no description",
                error_type="evaluation",
            )
        try:
            success, _confidence = vlm_judge(
                screenshot, milestone.check.description, model=model
            )
        except Exception as exc:
            if isinstance(exc, EvaluationUnavailableError):
                raise
            raise EvaluationUnavailableError(
                f"Milestone {milestone.name!r} could not be evaluated: {exc}",
                error_type="evaluation",
            ) from exc
        if success:
            passed += 1

    return passed / len(task_config.milestones)


# ---------------------------------------------------------------------------
# WAA JSON parsing helpers (module-level to keep TaskConfig class concise)
# ---------------------------------------------------------------------------

# Metric functions that we can reverse-translate to TaskCheck.  Everything
# else is treated as a specialised WAA evaluator and preserved verbatim.
_KNOWN_SIMPLE_FUNCS = frozenset({
    "exact_match",
    "contains",
    "fuzzy_match",
    "regex_match",
})


def _infer_domain(data: dict[str, Any], path: str) -> str:
    """Best-effort domain inference from WAA JSON data or file path."""
    # Explicit field
    if data.get("domain"):
        return data["domain"]
    # related_apps hint
    apps = data.get("related_apps", [])
    if apps:
        app = apps[0].lower().replace("-", "_")
        if "calc" in app:
            return "libreoffice_calc"
        if "writer" in app:
            return "libreoffice_writer"
        return app
    # Fall back to parent directory name
    parent = Path(path).parent.name
    if parent and parent != ".":
        return parent
    return "desktop"


def _parse_waa_setup(config_array: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reverse-translate WAA ``config`` array to YAML-style setup dicts.

    WAA format: ``[{"type": "launch", "parameters": {"command": "..."}}]``
    YAML format: ``[{"launch": "..."}]``
    """
    setup: list[dict[str, Any]] = []
    for item in config_array:
        cfg_type = item.get("type", "")
        params = item.get("parameters", {})

        if cfg_type == "launch":
            cmd = params.get("command", "")
            # command can be a list (["code"]) or a string
            if isinstance(cmd, list):
                cmd = " ".join(cmd)
            setup.append({"launch": cmd})
        elif cfg_type == "open":
            setup.append({"open": params.get("path", "")})
        elif cfg_type in ("execute", "command"):
            cmd = params.get("command", "")
            if isinstance(cmd, list):
                cmd = " ".join(cmd)
            setup.append({"execute": cmd})
        elif cfg_type == "sleep":
            setup.append({"sleep": params.get("seconds", 1)})
        elif cfg_type == "download":
            files = params.get("files", [])
            if files:
                for f in files:
                    setup.append({
                        "download": {"url": f["url"], "dest": f["path"]},
                    })
            else:
                # Single-file shorthand
                setup.append({
                    "download": {
                        "url": params.get("url", ""),
                        "dest": params.get("path", ""),
                    },
                })
        else:
            # Pass through verbatim (activate_window, verify_apps, etc.)
            setup.append(item)

    return setup


def _parse_waa_evaluator(
    evaluator: dict[str, Any],
) -> tuple[list[TaskCheck], str, dict[str, Any] | None]:
    """Reverse-translate a WAA evaluator dict to TaskCheck list.

    Returns:
        (checks, combine, raw_evaluator)
        ``raw_evaluator`` is non-None when the evaluator uses specialised
        WAA metric functions that cannot be represented as TaskCheck objects.
    """
    if not evaluator:
        return [], "and", None

    func_spec = evaluator.get("func", "exact_match")
    result_spec = evaluator.get("result", {})
    expected_spec = evaluator.get("expected", {})
    conj = evaluator.get("conj", "and")

    # Multi-metric evaluator (list of funcs)
    if isinstance(func_spec, list):
        results = result_spec if isinstance(result_spec, list) else [result_spec]
        expecteds = expected_spec if isinstance(expected_spec, list) else [expected_spec]

        # Check if ALL funcs are simple/known
        all_simple = all(fn in _KNOWN_SIMPLE_FUNCS for fn in func_spec)
        if not all_simple:
            return [], conj, evaluator

        checks = []
        for i, fn in enumerate(func_spec):
            r = results[i] if i < len(results) else {}
            e = expecteds[i] if i < len(expecteds) else {}
            check = _reverse_translate_single(fn, r, e)
            if check is None:
                # Fall back to raw evaluator
                return [], conj, evaluator
            checks.append(check)
        return checks, conj, None

    # Single-metric evaluator
    if func_spec not in _KNOWN_SIMPLE_FUNCS:
        return [], conj, evaluator

    check = _reverse_translate_single(func_spec, result_spec, expected_spec)
    if check is None:
        return [], conj, evaluator
    return [check], conj, None


def _reverse_translate_single(
    func: str,
    result_spec: dict[str, Any],
    expected_spec: dict[str, Any],
) -> TaskCheck | None:
    """Reverse-translate a single WAA metric to a TaskCheck.

    Returns None if the pattern is not recognised.
    """
    result_type = result_spec.get("type", "")
    expected_type = expected_spec.get("type", "")

    # WAA func name -> our match name
    match_name = {
        "exact_match": "exact",
        "contains": "contains",
        "fuzzy_match": "fuzzy",
        "regex_match": "regex",
    }.get(func, "exact")

    # Pattern: vm_command_line + literal -> "command" check
    if result_type == "vm_command_line" and expected_type == "literal":
        return TaskCheck(
            check="command",
            run=result_spec.get("command", ""),
            expect=expected_spec.get("value", ""),
            match=match_name,
        )

    # Pattern: vm_file + literal -> "file" check (contains or content match)
    if result_type == "vm_file" and expected_type == "literal":
        file_path = result_spec.get("path", "")
        expected_value = expected_spec.get("value", "")
        if func == "contains":
            return TaskCheck(
                check="file",
                path=file_path,
                contains=expected_value,
            )
        return TaskCheck(
            check="command",
            run=f'type "{file_path}"',
            expect=expected_value,
            match=match_name,
        )

    # Pattern: vm_command_line + no expected type -> command check
    if result_type == "vm_command_line" and not expected_type:
        return TaskCheck(
            check="command",
            run=result_spec.get("command", ""),
            expect=expected_spec.get("value", expected_spec.get("expected", "")),
            match=match_name,
        )

    return None
