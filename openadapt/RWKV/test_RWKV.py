from openadapt.RWKV.RWKV import run_RWKV
import modal
import os
from openadapt import config

SIGNAL_TEST = False

if __name__ == '__main__':

    for i in range(1):
        MODEL = config.RWKV_MODEL
        
        if SIGNAL_TEST:
            instruction = "You are given a task, and a list of signals in JSON format. Please provide only the id of the signal that is most relevant to the task."

            task_description = ("filling forms and submitting data on web pages")
            
            input = f"""Task: creating a website

    Signals: 
    [{{'id': 0, 'type': 'file', 'descriptor': 'restaurant_menu_data.txt'}}
    ,{{'id': 1, 'type': 'url', 'descriptor': 'https://en.wikipedia.org/wiki/Web_development'}}
    ,{{'id': 2, 'type': 'function', 'descriptor': 'math.sqrt'}}]"""
            
            if config.USE_MODAL:
                Func = modal.Function.lookup("openadapt-rwkv", "run_RWKV")
                parameters = config.RWKV_PARAMETERS
                print("Output:",Func.call(model_number=MODEL,parameters=parameters, instruction=instruction, task_description=task_description, input=input, use_cuda=True))
            else:
                parameters = config.RWKV_PARAMETERS
                print("Output:",run_RWKV(model_number=MODEL,parameters=parameters, instruction=instruction, task_description=task_description, input=input, use_cuda=False))
        else:
            prompt=input("Enter prompt: ")
            if config.USE_MODAL:
                Func = modal.Function.lookup("openadapt-rwkv", "run_RWKV")
                parameters = config.RWKV_PARAMETERS
                print("Output:",Func.call(model_number=MODEL,prompt=prompt, parameters=parameters, use_cuda=True))
            else:
                parameters = config.RWKV_PARAMETERS
                print("Output:",run_RWKV(model_number=MODEL,prompt=prompt, parameters=parameters, use_cuda=False))