"""Package containing different replay strategies.

Module: __init__.py
"""

from pprint import pformat
import pkgutil
import importlib

from loguru import logger


PACKAGE_NAME = "openadapt.strategies"


# Iterate through all modules in the specified package
strategy_by_name = {}
for _, module_name, _ in pkgutil.iter_modules([PACKAGE_NAME.replace('.', '/')]):
    # Import the module
    module = importlib.import_module(f"{PACKAGE_NAME}.{module_name}")
    # Filter and add classes ending with 'ReplayStrategy' to the global namespace
    names = [
        name
        for name in dir(module)
        if name.endswith("ReplayStrategy")
    ]
    existing_names = [name for name in names if name in strategy_by_name]
    if existing_names:
        logger.warning(f"{existing_names=}, {strategy_by_name=}; {module_name=}")
    strategy_by_name = {
        **strategy_by_name,
        **{
            name: getattr(module, name)
            for name in names
        },
    }
logger.info(f"strategy_by_name=\n{pformat(strategy_by_name)}")

globals().update(strategy_by_name)
