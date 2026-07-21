"""Remote on-prem VLM inference service.

One open VLM on a GPU box serves a fleet of GPU-less automation runners over the
LAN: identity same/different veto, GUI grounding, and semantic state
verification. See :mod:`openadapt_flow.services.vlm_service.app` for the
FastAPI app and :mod:`openadapt_flow.runtime.remote_vlm` for the fail-safe
clients.
"""

from openadapt_flow.services.vlm_service.app import create_app
from openadapt_flow.services.vlm_service.config import ServiceConfig

__all__ = ["create_app", "ServiceConfig"]
