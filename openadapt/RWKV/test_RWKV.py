from openadapt.RWKV.RWKV import run_RWKV
import modal
import os
from openadapt import config


if __name__ == '__main__':
    if config.USE_MODAL:
        Func = modal.Function.lookup("openadapt-rwkv", "run_RWKV")
        parameters = config.RWKV_PARAMETERS

        instruction = "You will be given a short sentence representing the start of a story."
        task_description = "Continue the story."
        input = "The machine learning engineer was working on a new model, when suddenly the power went out."

        print("Test:",Func.call(model=1,parameters=parameters, instruction=instruction, task_description=task_description, input=input))
    else:
        parameters = config.RWKV_PARAMETERS

        instruction = "You will be given a short sentence representing the start of a story."
        task_description = "Continue the story."
        input = "The machine learning engineer was working on a new model, when suddenly the power went out."

        print("Test:",run_RWKV(model=1,parameters=parameters, instruction=instruction, task_description=task_description, input=input))