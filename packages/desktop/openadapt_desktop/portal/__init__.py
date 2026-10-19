"""The runner-local mobile decision portal Desktop owns.

Desktop owns portal lifecycle, customer ingress configuration, one-use QR
device pairing, generic operating-system notifications, and the local task
shell.  It deliberately owns **no decision semantics**: the question, the
allowed actions, the evidence, and the resume/verification contract all come
from ``openadapt-flow``'s attended console over
:mod:`openadapt_desktop.portal.flow_client`.  Nothing in this package may interpret,
re-derive, or override an allowed action.

The portal binds loopback by default.  Reaching it from a phone requires the
customer to configure their own HTTPS/VPN ingress explicitly
(:mod:`openadapt_desktop.portal.ingress`); an unconfigured portal fails closed rather than
widening its bind address for convenience.
"""

from openadapt_desktop.portal.ingress import (
    IngressError,
    PortalIngress,
    resolve_ingress,
)
from openadapt_desktop.portal.notifications import (
    NOTIFICATION_FIELDS,
    NotificationLeak,
    assert_generic_notification,
    build_notification,
)
from openadapt_desktop.portal.pairing import (
    PAIRING_TTL_S,
    PORTAL_PAIRING_SECRET_RE,
    DevicePairingStore,
    PairingRefused,
)

__all__ = [
    "DevicePairingStore",
    "IngressError",
    "NOTIFICATION_FIELDS",
    "NotificationLeak",
    "PAIRING_TTL_S",
    "PORTAL_PAIRING_SECRET_RE",
    "PairingRefused",
    "PortalIngress",
    "assert_generic_notification",
    "build_notification",
    "resolve_ingress",
]
