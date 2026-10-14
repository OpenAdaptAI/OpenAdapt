"""Errors that must remain distinct from valid benchmark outcomes."""

from enum import Enum


class ActionDeliveryState(str, Enum):
    """What the runtime knows about action delivery after an exception."""

    NOT_DELIVERED = "not_delivered"
    UNCERTAIN = "uncertain"
    DELIVERED = "delivered"


class ActionExecutionError(RuntimeError):
    """An action failed with a machine-readable delivery state."""

    error_type = "infrastructure"

    def __init__(
        self,
        message: str,
        *,
        delivery_state: ActionDeliveryState = ActionDeliveryState.NOT_DELIVERED,
    ) -> None:
        super().__init__(message)
        self.delivery_state = delivery_state

    @property
    def retry_safe(self) -> bool:
        """Return True only when the runtime proves no action was delivered."""
        return self.delivery_state is ActionDeliveryState.NOT_DELIVERED


class ActionDeliveryUncertainError(ActionExecutionError):
    """An action may have reached the target, so blind retry is unsafe."""

    def __init__(self, message: str) -> None:
        super().__init__(message, delivery_state=ActionDeliveryState.UNCERTAIN)


class ActionDeliveredObservationError(ActionExecutionError):
    """The action was delivered, but the required next observation failed."""

    def __init__(self, message: str) -> None:
        super().__init__(message, delivery_state=ActionDeliveryState.DELIVERED)


class ActionParseError(ValueError):
    """Model output did not contain one valid explicit action."""

    error_type = "agent"


class RolloutInfrastructureError(RuntimeError):
    """A rollout could not run because its observation infrastructure failed."""

    error_type = "infrastructure"


class RolloutEvaluationError(RuntimeError):
    """A rollout outcome could not be measured."""

    error_type = "evaluation"


class RolloutLossError(RuntimeError):
    """A rollout did not contain the evidence required to compute loss."""

    error_type = "evaluation"
