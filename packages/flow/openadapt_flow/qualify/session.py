"""Turn ``verify.yaml`` into the callables a campaign needs.

- a session opener: a browser at the trial's start screen;
- a reset: the test system back at its baseline;
- a verifier factory: the independent read-back for one run's inputs.

Each has a declarative default and a ``module:function`` escape hatch for
applications whose sign-in or reset cannot be written as data.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from typing import Any, Callable, Mapping, Optional

from openadapt_flow.qualify.config import (
    AppConfig,
    QualifyConfigError,
    ResetConfig,
    VerifyConfig,
)

SessionOpener = Callable[[], tuple[Any, Callable[[], None]]]


def load_hook(reference: str) -> Callable[..., Any]:
    """Import ``package.module:function``."""

    module_name, _, attr = reference.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise QualifyConfigError(
            f"cannot import {module_name!r} for {reference!r}: {exc}"
        ) from exc
    hook = getattr(module, attr, None)
    if not callable(hook):
        raise QualifyConfigError(f"{reference!r} is not a callable")
    return hook


def playwright_session(config: VerifyConfig) -> SessionOpener:
    """Open a fresh browser context at ``entry_url`` for each trial."""

    app: AppConfig = config.app
    storage_state = (
        str(config.resolve_path(app.storage_state)) if app.storage_state else None
    )

    def open_session() -> tuple[Any, Callable[[], None]]:
        from openadapt_flow._browser_setup import ensure_chromium_installed
        from openadapt_flow.backends.playwright_backend import PlaywrightBackend

        ensure_chromium_installed()
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        try:
            browser = pw.chromium.launch(headless=app.headless)
            context = browser.new_context(
                viewport={"width": app.viewport[0], "height": app.viewport[1]},
                device_scale_factor=1,
                storage_state=storage_state,
            )
            page = context.new_page()
            page.goto(app.entry_url or app.url)
        except Exception:
            pw.stop()
            raise

        def close() -> None:
            try:
                browser.close()
            finally:
                pw.stop()

        return PlaywrightBackend(page), close

    return open_session


def session_opener(config: VerifyConfig) -> SessionOpener:
    if config.app.session:
        hook = load_hook(config.app.session)
        return lambda: hook(config)
    if config.app.target != "web":
        raise QualifyConfigError(
            f"app.target {config.app.target!r} needs app.session (module:function "
            "returning (backend, close)); only web has a built-in session"
        )
    return playwright_session(config)


def reset_runner(config: VerifyConfig) -> Optional[Callable[[], None]]:
    reset = config.app.reset
    if reset is None:
        return None
    return step_runner(reset, "app.reset")


def step_runner(step: ResetConfig, where: str) -> Callable[[], None]:
    """A callable for one setup step: a command, one HTTP request, or a hook.

    ``where`` names the step in errors (``app.reset``, a business rule's
    ``seed``).
    """

    if step.python:
        return load_hook(step.python)
    if step.command:
        command = step.command

        def run_command() -> None:
            done = subprocess.run(
                command, shell=True, timeout=step.timeout_s, capture_output=True
            )
            if done.returncode != 0:
                raise RuntimeError(
                    f"{where} command exited {done.returncode}: "
                    f"{done.stderr.decode(errors='replace')[-400:]}"
                )

        return run_command
    http = step.http
    assert http is not None

    def run_http() -> None:
        import httpx

        body = None if http.body is None else json.dumps(http.body)
        response = httpx.request(
            http.method,
            http.url,
            content=body,
            headers={"Content-Type": "application/json"} if body is not None else None,
            timeout=step.timeout_s,
        )
        if response.status_code >= 300:
            raise RuntimeError(f"{where} HTTP {response.status_code}")

    return run_http


def verifier_factory(config: VerifyConfig) -> Callable[[Mapping[str, object]], Any]:
    """Build the read-back verifier for one run's inputs."""

    sor = config.system_of_record
    cfg = sor.verifier
    if sor.session:
        hook = load_hook(sor.session)
        from openadapt_flow.runtime.effects import RestRecordVerifier

        if not cfg.base_url:
            raise QualifyConfigError("verifier.base_url is required for kind: rest")

        def build_rest(params: Mapping[str, object]) -> Any:
            return RestRecordVerifier(
                cfg.base_url.rstrip("/"),  # type: ignore[union-attr]
                records_path=cfg.records_path,
                records_key=cfg.records_key,
                session=hook(config),
                timeout_s=10.0,
                poll_interval_s=0.2,
            )

        return build_rest
    from openadapt_flow.deployment import build_effect_verifier

    def build(params: Mapping[str, object]) -> Any:
        verifier = build_effect_verifier(cfg, params)
        if verifier is None:
            raise QualifyConfigError("verifier built no verifier")
        return verifier

    return build


__all__ = [
    "load_hook",
    "playwright_session",
    "reset_runner",
    "session_opener",
    "step_runner",
    "verifier_factory",
]
