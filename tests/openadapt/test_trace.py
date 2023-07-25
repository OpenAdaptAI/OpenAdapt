from loguru import logger

from openadapt.trace import trace

def test_trace():
    """Simple smoke test for trace."""
    @trace(logger)
    def foo():
        pass