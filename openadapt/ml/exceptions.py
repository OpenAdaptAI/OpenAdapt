"""Exceptions file for the Completions Provider API."""


class GPUNotAvailableError(BaseException):
    """Raised when no GPU is available on the system."""
