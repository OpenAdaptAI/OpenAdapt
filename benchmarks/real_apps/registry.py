"""Central registry for Wave 1 Real-App Verification Matrix specifications and adapters."""

from __future__ import annotations

from collections.abc import Callable

from benchmarks.real_apps.apps.gnucash import GNUCASH_SPEC, GnuCashAdapter
from benchmarks.real_apps.apps.odoo17 import ODOO17_SPEC, Odoo17Adapter
from benchmarks.real_apps.apps.openemr import OPENEMR_SPEC, OpenEMRAdapter
from benchmarks.real_apps.apps.twenty_crm import TWENTY_CRM_SPEC, TwentyCRMAdapter
from benchmarks.real_apps.harness import RealAppAdapter, RealAppSpec, TierKind

_ADAPTER_FACTORIES: dict[str, Callable[[], RealAppAdapter]] = {
    "openemr": OpenEMRAdapter,
    "odoo17": Odoo17Adapter,
    "twenty_crm": TwentyCRMAdapter,
    "gnucash": GnuCashAdapter,
}

_SPECS: dict[str, RealAppSpec] = {
    "openemr": OPENEMR_SPEC,
    "odoo17": ODOO17_SPEC,
    "twenty_crm": TWENTY_CRM_SPEC,
    "gnucash": GNUCASH_SPEC,
}


def list_app_specs(*, tier: TierKind | None = None) -> tuple[RealAppSpec, ...]:
    """Return registered :class:`RealAppSpec` entries, optionally filtered by tier."""
    specs = tuple(_SPECS.values())
    if tier is not None:
        return tuple(s for s in specs if s.tier == tier)
    return specs


def get_app_spec(app_id: str) -> RealAppSpec:
    """Lookup a single :class:`RealAppSpec` by ``app_id``."""
    if app_id not in _SPECS:
        known = ", ".join(sorted(_SPECS))
        raise KeyError(f"Unknown app_id {app_id!r}. Registered apps: {known}")
    return _SPECS[app_id]


def load_adapter(app_id: str) -> RealAppAdapter:
    """Instantiate the :class:`RealAppAdapter` for ``app_id``."""
    if app_id not in _ADAPTER_FACTORIES:
        known = ", ".join(sorted(_ADAPTER_FACTORIES))
        raise KeyError(f"Unknown app_id {app_id!r}. Registered apps: {known}")
    return _ADAPTER_FACTORIES[app_id]()


def load_adapters(app_selector: str = "all") -> list[RealAppAdapter]:
    """Load either all Wave 1 adapters (``'all'``) or one specific ``app_id``."""
    if app_selector == "all":
        return [factory() for factory in _ADAPTER_FACTORIES.values()]
    return [load_adapter(app_selector)]
