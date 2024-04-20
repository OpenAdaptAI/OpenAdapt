"""openadapt.cache module.

This module provides a caching decorator for functions.

Example usage:
    from openadapt.cache import cache

    @cache()
    def my_function():
        # Function body
        pass
"""

from functools import wraps
from typing import Any, Callable, Optional, Union
import time

from joblib import Memory
from loguru import logger

from openadapt.config import config


def default(val: Optional[Any], default: Any) -> Any:
    """Set a default value if the given value is None.

    Args:
        val: The value to check.
        default: The default value to set.

    Returns:
        The value or the default value.
    """
    return val if val is not None else default


def cache(
    dir_path: Optional[str] = None,
    enabled: Optional[bool] = None,
    verbosity: Optional[int] = None,
    **cache_kwargs: Union[str, int, bool],
) -> Callable[[Callable], Callable]:
    """Cache decorator for functions.

    Args:
        dir_path (str): The path to the cache directory.
        enabled (bool): Whether caching is enabled.
        verbosity (int): The verbosity level of the cache.
        **cache_kwargs: Additional keyword arguments to pass to the cache.

    Returns:
        The decorator function.
    """
    cache_dir_path = default(dir_path, config.CACHE_DIR_PATH)
    cache_enabled = default(enabled, config.CACHE_ENABLED)
    cache_verbosity = default(verbosity, config.CACHE_VERBOSITY)

    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            logger.debug(f"{cache_enabled=}")
            if cache_enabled:
                memory = Memory(cache_dir_path, verbose=cache_verbosity)
                nonlocal fn
                fn = memory.cache(fn, **cache_kwargs)
                cache_hit = fn.check_call_in_cache(*args, **kwargs)
                logger.debug(f"{fn=} {cache_hit=}")
            start_time = time.time()
            logger.debug(f"{fn=} {start_time=}")
            rval = fn(*args, **kwargs)
            duration = time.time() - start_time
            logger.debug(f"{fn=} {duration=}")

            # workaround for `AttributeError: _min_frame` raised when show()ing an Image
            try:
                from PIL import Image
            except ImportError:
                pass
            else:
                if isinstance(rval, Image.Image):
                    if not hasattr(rval, "_min_frame"):
                        logger.debug(
                            "Image object missing '_min_frame'; refreshing image."
                        )
                        fresh_image = Image.new(rval.mode, rval.size)
                        fresh_image.paste(rval)
                        rval = fresh_image

            return rval

        return wrapper

    return decorator
