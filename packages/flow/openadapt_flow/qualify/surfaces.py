"""Backend-specific campaign plumbing; application knowledge stays in data."""

from __future__ import annotations

from typing import Any

from openadapt_flow.qualify.environment import (
    WebEnvironmentObserver,
    web_environment_digest,
    web_environment_payload,
)
from openadapt_flow.qualify.faults import WebFaultDriver
from openadapt_flow.qualify.windows import (
    WindowsEnvironmentObserver,
    WindowsFaultDriver,
)


def observer(config: Any) -> Any:
    if config.app.target == "windows":
        return WindowsEnvironmentObserver(application_version=config.app.version)
    return WebEnvironmentObserver(
        application_version=config.app.version,
        headless=config.app.headless,
    )


def fault_driver(workflow: Any, config: Any, *, key_id: str) -> Any:
    kind = WindowsFaultDriver if config.app.target == "windows" else WebFaultDriver
    return kind(workflow, key_id=key_id)


def environment_payload(
    backend: Any, config: Any, *, web_payload_fn: Any = None
) -> dict[str, Any]:
    if config.app.target == "windows":
        if not callable(getattr(backend, "stage_qualification_fault", None)):
            raise ValueError("Windows qualification needs an opt-in native fault agent")
        capabilities: set[str] = set(
            getattr(backend, "agent_capabilities", lambda: ())()
        )
        if not {
            "qualification_faults_v1",
            "uia_v1",
            "context_environment_v1",
            "native_keyboard_lease_v1",
        } <= set(capabilities):
            raise ValueError(
                "Windows qualification needs enabled fault, UIA, environment and native keyboard lease capabilities"
            )
        return observer(config).observe(backend, "windows").model_dump(mode="json")
    payload_fn = web_payload_fn or web_environment_payload
    return payload_fn(
        backend,
        application_version=config.app.version,
        headless=config.app.headless,
    )


def environment_digest(
    environment: dict[str, Any], config: Any, *, web_digest_fn: Any = None
) -> str:
    digest_fn = web_digest_fn or web_environment_digest
    return (
        environment["environment_digest"]
        if config.app.target == "windows"
        else digest_fn(environment)
    )


def boundary_fields(config: Any, environment: dict[str, Any]) -> dict[str, Any]:
    observed = observer(config)
    return {
        "target_kind": config.app.target,
        "application_identity": (
            environment["application_identity"]
            if config.app.target == "windows"
            else config.app.origin
        ),
        "environment_observer_id": observed.observer_id,
        "environment_observer_contract_sha256": observed.contract_sha256,
        "environment_digest": environment_digest(environment, config),
        "required_capabilities": (
            [
                "native_windows",
                "uia_v1",
                "native_keyboard_lease_v1",
                "independent_system_of_record",
            ]
            if config.app.target == "windows"
            else sorted(
                {
                    "headless_chromium" if config.app.headless else "headed_chromium",
                    "independent_system_of_record",
                    "playwright_dom",
                }
            )
        ),
    }


def execution_fields(backend: Any, config: Any) -> dict[str, Any]:
    if config.app.target == "windows":
        return {"execution_target_kind": "windows"}
    return {
        "execution_target_kind": "web",
        "execution_origin": config.app.origin,
        "execution_entry_url": backend.page.url,
    }


def cleanup_faults(driver: Any, backend: Any) -> None:
    cleanup = getattr(driver, "cleanup", None)
    if callable(cleanup):
        cleanup(backend)
