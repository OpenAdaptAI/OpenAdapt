"""Handoff tools for ``openadapt agent serve --handoffs`` (MCP and A2A alike).

* ``get_handoff``: a finished run's public result with its receipt attached
  (``openadapt.handoff-document/v1``), ready to pass to another agent.
* ``verify_handoff``: check a handoff another agent sent, against the keys
  this server pins, and return the verdict (:mod:`openadapt_flow.handoff`).

Both are read-only. ``get_handoff`` reads only this server's own runs folder
and never sends ``receipt.private.json``. ``verify_handoff`` trusts only the
operator's pinned registry, never a key inside the handoff.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional, Sequence

_READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
_RUN_ID_PATTERN = r"^run-[A-Za-z0-9._-]{1,124}$"

HANDOFF_TOOLS = ("get_handoff", "verify_handoff")


class HandoffTools:
    """The handoff tool surface over one bridge's runs folder."""

    def __init__(
        self,
        *,
        registry_path: Optional[Path] = None,
        registry: Optional[Sequence[Any]] = None,
        signer: Optional[Any] = None,
    ) -> None:
        self.registry_path = Path(registry_path) if registry_path else None
        self._registry = list(registry) if registry is not None else None
        #: The key that signs this computer's run receipts; it binds each
        #: handed-off result as the receipt's payload subject.
        self._signer = signer

    def signer(self) -> Any:
        if self._signer is None:
            from openadapt_flow.offline_seal import seal_home, signer_for

            self._signer = signer_for(seal_home())
        return self._signer

    def registry(self) -> list[Any]:
        if self._registry is None:
            from openadapt_flow.handoff import load_registry

            self._registry = load_registry(self.registry_path)
        return self._registry

    def list_tool_specs(self, *, runs_enabled: bool) -> list[Any]:
        from openadapt_flow.agent.bridge import ToolSpec

        specs = []
        if runs_enabled:
            specs.append(
                ToolSpec(
                    name="get_handoff",
                    description=(
                        "Get a run's result with its signed receipt attached, "
                        "to hand to another agent. The receiving agent checks "
                        "it with verify_handoff or `openadapt handoff verify`. "
                        "Only finished runs have a receipt."
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "run_id": {"type": "string", "pattern": _RUN_ID_PATTERN},
                            "public": {
                                "type": "boolean",
                                "default": False,
                                "description": (
                                    "Attach the public card instead of the full "
                                    "receipt, if the run has one."
                                ),
                            },
                        },
                        "required": ["run_id"],
                        "additionalProperties": False,
                    },
                    annotations=_READ_ONLY,
                )
            )
        specs.append(
            ToolSpec(
                name="verify_handoff",
                description=(
                    "Check a handoff another agent sent (an openadapt.handoff-document/v1 "
                    "document) against the keys this computer pins. Returns who "
                    "signed it, whether it is intact, which checks passed and "
                    "which were not checked, and any confirmation read from the "
                    "other party's system. A valid signature proves who signed, "
                    "not that the claim is true."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "handoff": {
                            "type": "object",
                            "description": "The openadapt.handoff-document/v1 document.",
                        }
                    },
                    "required": ["handoff"],
                    "additionalProperties": False,
                },
                annotations=_READ_ONLY,
            )
        )
        return specs

    def handles(self, name: str) -> bool:
        return name in HANDOFF_TOOLS

    def verify(self, document: Any) -> dict[str, Any]:
        from openadapt_flow.handoff import verify_handoff

        return verify_handoff(document, self.registry()).to_dict()

    def get(
        self,
        run_id: str,
        *,
        public: bool,
        result_for: Callable[[str], dict[str, Any]],
        run_dir_for: Callable[[str], Path],
    ) -> dict[str, Any]:
        from openadapt_flow.agent.bridge import BridgeError
        from openadapt_flow.handoff import HandoffError, make_handoff, receipt_for_run

        result = result_for(run_id)
        if result.get("outcome") == "running":
            raise BridgeError("the run is still running; it has no receipt yet")
        try:
            receipt = receipt_for_run(run_dir_for(run_id), public=public)
            return make_handoff(
                result, receipt, name=f"{run_id}.result.json", signer=self.signer()
            )
        except HandoffError as exc:
            raise BridgeError(f"no handoff for this run: {exc}") from None
        except RuntimeError:
            raise BridgeError(
                "no handoff for this run: this computer's receipt key is unavailable"
            ) from None
