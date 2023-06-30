from openadapt.RWKV.RWKV import run_RWKV
import modal
import os
from openadapt import config


if __name__ == '__main__':

    for i in range(10):
        MODEL = config.RWKV_MODEL
        USE_MODAL = True
        MODEL = 4

        instruction = "You are given a list of signals, each detailed in a JSON format. Please provide only the signal number that would return data that is most beneficial to the task of "

        task_description = ("filling forms and submitting data on web pages")
        
        input = f"""[{{'number': 1, 'title': 'restaurant menu', 'type': 'file', 'address': 'tests/openadapt/restaurant_menu_data.txt', 'description': 'Size: 17, Type: text/plain'}}
,{{'number': 2, 'title': 'wikipedia web development page', 'type': 'web_url', 'address': 'https://en.wikipedia.org/wiki/Web_development', 'description': 'Length: 63230, Type: text/html; charset=UTF-8'}}
,{{'number': 3, 'title': 'square root function', 'type': 'function', 'address': 'math.sqrt', 'description': 'Function: sqrt, Module: math, Summary: function is used to compute the square root of a given number.'}}]"""
        
        if config.USE_MODAL:
            Func = modal.Function.lookup("openadapt-rwkv", "run_RWKV")
            parameters = config.RWKV_PARAMETERS
            print("Output:",Func.call(model_number=MODEL,parameters=parameters, instruction=instruction, task_description=task_description, input=input, use_cuda=True))
        else:
            parameters = config.RWKV_PARAMETERS
            print("Output:",run_RWKV(model_number=MODEL,parameters=parameters, instruction=instruction, task_description=task_description, input=input, use_cuda=False))