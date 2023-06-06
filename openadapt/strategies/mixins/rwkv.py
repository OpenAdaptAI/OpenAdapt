from loguru import logger
import modal

from openadapt.recording import Recording
from openadapt.RWKV.RWKV import run_RWKV
from openadapt import config

USE_MODAL = config.USE_MODAL
parameters = config.RWKV_PARAMETERS

class RWKVReplayStrategyMixin:

    def get_response(self,instruction=None, task_description=None, input=None, parameters=None):
        if USE_MODAL:
            function_call = modal.Function.lookup("openadapt-rwkv", "run_RWKV")
            response = function_call.call(task_description=task_description, input=input)
            logger.debug(f"response=\n{response}")
        else:
            response = run_RWKV(instruction, task_description, input)
        return response