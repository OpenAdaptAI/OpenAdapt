from loguru import logger
from functools import wraps


def args_to_str(*args):
    return ", ".join(map(str, args))


def kwargs_to_str(**kwargs):
    return ",".join([f"{k}={v}" for k, v in kwargs.items()])

def trace(logger):
    def decorator(func):
        @wraps(func)
        def wrapper_logging(*args, **kwargs):
            func_name = func.__qualname__
            func_args = args_to_str(*args)
            func_kwargs = kwargs_to_str(**kwargs)

            if func_kwargs != "":
                logger.info(
                    f" -> Enter: {func_name}({func_args}, {func_kwargs})"
                )
            else:
                logger.info(f" -> Enter: {func_name}({func_args})")

            result = func(*args, **kwargs)

            logger.info(f" <- Leave: {func_name}({result})")
            return result

        return wrapper_logging

    return decorator

