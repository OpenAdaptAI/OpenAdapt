"""Silent-failure fault injectors for OpenEMR."""

from __future__ import annotations

from benchmarks.real_apps.fault_proxy import FaultRule


def silent_referral_drop_rule() -> FaultRule:
    """Drop OpenEMR referral write POST and return a deceptive HTTP 200 OK."""
    return FaultRule(
        mode="drop_write_return_200",
        path_contains="/ServiceRequest",
        methods=("POST", "PUT"),
        synthetic_body={"status": "saved", "message": "Referral saved"},
    )


def session_expiry_redirect_rule() -> FaultRule:
    """Simulate OpenEMR PHP session expiry redirecting write POSTs to login."""
    return FaultRule(
        mode="session_expiry_redirect",
        path_contains="/ServiceRequest",
        methods=("POST", "PUT"),
        redirect_location="/interface/login/login.php?site=default",
    )
