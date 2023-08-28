"""Exceptions file for the Completions Provider API."""


class ModelNotImplementedError(BaseException):
    """To be raised when a model is not supported by the infrastructure."""

    def __init__(self, model_name: str) -> None:
        """Init method for the ModelNotImplementedError class."""
        super().__init__(f"{model_name} is not currently supported")


class GpuNotAvailableError(BaseException):
    """Raised when no GPU is available on the system."""
