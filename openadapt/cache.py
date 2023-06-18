from functools import wraps
import time

from joblib import Memory
from loguru import logger


from openadapt import config


def default(val, default):
    return val if val is not None else default


def cache(dir_path=None, enabled=None, verbosity=None, **cache_kwargs):
    """TODO"""

    cache_dir_path = default(dir_path, config.CACHE_DIR_PATH)
    cache_enabled = default(enabled, config.CACHE_ENABLED)
    cache_verbosity = default(verbosity, config.CACHE_VERBOSITY)

    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
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
            return rval

        return wrapper

    return decorator
