from openadapt.RWKV.RWKV import run_RWKV
import modal
import os
from openadapt import config


if __name__ == '__main__':

    MODEL = config.RWKV_MODEL

    instruction = "You will be given a short sentence representing the start of a story."
    task_description = "Continue the story."
    input = "The machine learning engineer was working on a new model, when suddenly the power went out. He decided to name the model: "

    if config.USE_MODAL:
        Func = modal.Function.lookup("openadapt-rwkv", "run_RWKV")
        parameters = config.RWKV_PARAMETERS
        print("Output:",Func.call(model=MODEL,parameters=parameters, instruction=instruction, task_description=task_description, input=input, use_cuda=True))
    else:
        parameters = config.RWKV_PARAMETERS
        print("Output:",run_RWKV(model=MODEL,parameters=parameters, instruction=instruction, task_description=task_description, input=input, use_cuda=False))