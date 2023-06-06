from loguru import logger
import modal

from openadapt.recording import Recording
from openadapt.RWKV.RWKV import run_RWKV

use_modal = True

class RWKVReplayStrategyMixin:

    def get_response(self,instruction, task_description, input):
        if use_modal:
            function_call = modal.Function.lookup("openadapt-rwkv", "run_RWKV")
            response = function_call.call(task_description=task_description, input=input)
            logger.debug(f"response=\n{response}")
        else:
            response = run_RWKV(instruction, task_description, input)
        return response