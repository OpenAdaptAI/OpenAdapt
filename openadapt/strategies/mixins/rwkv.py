from loguru import logger
import modal

from openadapt.RWKV.RWKV import run_RWKV
from openadapt import config

USE_MODAL = config.USE_MODAL
PARAMETERS = config.RWKV_PARAMETERS

# Choose a model:
MODEL = config.RWKV_MODEL
# model 0: RWKV-4-Raven-14B
# model 1: RWKV-4-Raven-7B
# model 2: RWKV-4-Raven-1B5
# model 3: RWKV-4-Pile-14B
# model 4: RWKV-4-World-1.5B

class RWKVReplayStrategyMixin:

    def get_response(self,model=MODEL, instruction=None, task_description=None, input=None, parameters=None):
        if parameters is None:
            parameters = PARAMETERS
        if USE_MODAL:
            function_call = modal.Function.lookup("openadapt-rwkv", "run_RWKV")
            response = function_call.call(model=model, task_description=task_description, input=input, parameters=parameters, use_cuda=True)
            logger.debug(f"response=\n{response}")
        else:
            response = run_RWKV(model=MODEL, task_description=task_description, input=input, parameters=parameters, use_cuda=False)
        return response