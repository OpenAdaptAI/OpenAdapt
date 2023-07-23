"""This file adds logging for function entry and exit with 2 utility functions and a decorator.
"""

from functools import wraps
from loguru import logger


def args_to_str(*args):
     """Converts arguments to a string representation."""
     return ", ".join(map(str, args))


def kwargs_to_str(**kwargs):
    """Converts keyword arguments to a string representation."""
    return ",".join([f"{k}={v}" for k, v in kwargs.items()])


def trace(logger):
    """Decorator that adds logging for function entry and exit."""

    def decorator(func):
        @wraps(func)
        def wrapper_logging(*args, **kwargs):
            func_name = func.__qualname__
            func_args = args_to_str(*args)
            func_kwargs = kwargs_to_str(**kwargs)

            if func_kwargs != "":
                logger.info(f" -> Enter: {func_name}({func_args}, {func_kwargs})")
            else:
                logger.info(f" -> Enter: {func_name}({func_args})")

            result = func(*args, **kwargs)

            logger.info(f" <- Leave: {func_name}({result})")
            return result

        return wrapper_logging

    return decorator
