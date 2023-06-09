import os
import torch
import numpy as np
import modal
import requests
from huggingface_hub import hf_hub_download

from rwkv.model import RWKV
from rwkv.utils import PIPELINE, PIPELINE_ARGS
from transformers import PreTrainedTokenizerFast

#integrate modal to load bigger RWKV model
stub = modal.Stub("openadapt-rwkv")

os.environ["RWKV_JIT_ON"] = '1'
os.environ["RWKV_CUDA_ON"] = '0'

torch_image = modal.Image.debian_slim().pip_install("torch", "rwkv", "numpy", "transformers")

@stub.function(gpu="a100", timeout=18000, image=torch_image)
def run_RWKV(instruction=None, task_description=None, input=None, parameters=None):

    #use gpu=a100 for Raven-14B, vs. use gpu=any for other weights
    """
    title = "RWKV-4-Raven-7B-v12-Eng98%-Other2%-20230521-ctx8192"
    model_path = hf_hub_download(repo_id="BlinkDL/rwkv-4-raven", filename=f"{title}.pth")
    model = RWKV(model=model_path, strategy='cuda fp16')  #light weight model
    """
    """
    title = "RWKV-4-Pile-14B-20221020-83"
    model_path = hf_hub_download(repo_id="BlinkDL/rwkv-4-pile-14b", filename=f"{title}.pth")
    model = RWKV(model=model_path, strategy='cuda fp16')  #heavy weight model
    """
    """
    title = "RWKV-4-Raven-14B-v12-Eng98%-Other2%-20230523-ctx8192"
    model_path = hf_hub_download(repo_id="BlinkDL/rwkv-4-raven", filename=f"{title}.pth")
    model = RWKV(model=model_path, strategy='cuda fp16')  #heavy weight model
    """
    """
    title = "RWKV-4-Pile-14B-20230313-ctx8192-test1050"
    model_path = hf_hub_download(repo_id="BlinkDL/rwkv-4-pile-14b", filename=f"{title}.pth")
    model = RWKV(model=model_path, strategy='cuda fp16')  #heavy weight model
    """
    title = "RWKV-4-World-1.5B-v1-20230607-ctx4096"
    model_path = hf_hub_download(repo_id="BlinkDL/rwkv-4-world", filename=f"{title}.pth")
    model = RWKV(model=model_path, strategy='cuda fp32')  #heavy weight model
    
    tokenizer_url = "https://raw.githubusercontent.com/BlinkDL/RWKV-LM/main/RWKV-v4/20B_tokenizer.json"
    response = requests.get(tokenizer_url)
    if response.status_code == 200:
        tokenizer_path = "/root/rwkv_model/20B_tokenizer.json" #Switch to desired path if running locally
        os.makedirs(os.path.dirname(tokenizer_path), exist_ok=True)
        with open(tokenizer_path, 'wb') as f:
            f.write(response.content)
        tokenizer = PreTrainedTokenizerFast(tokenizer_file=tokenizer_path)
    else:
        print(f"Failed to download tokenizer. Status code: {response.status_code}")
        return
    #tokenizer = PreTrainedTokenizerFast(tokenizer_file="./openadapt/strategies/20B_tokenizer.json")
    #tokenizer = PreTrainedTokenizerFast(tokenizer_file="/root/rwkv_model/20B_tokenizer.json")
    #pipeline = PIPELINE(model,"/root/rwkv_model/20B_tokenizer.json")
    pipeline = PIPELINE(model,"rwkv_vocab_v20230424")

    if not parameters:
        temperature = 0.9
        top_p = 0.9
        count_penalty = 0.1
        token_count = 200
        ctx_limit = 1536
    else:
        temperature = parameters["temperature"]
        top_p = parameters["top_p"]
        count_penalty = parameters["count_penalty"]
        token_count = parameters["token_count"]
        ctx_limit = parameters["ctx_limit"]

    args = PIPELINE_ARGS(temperature= float(temperature), 
                        top_p= float(top_p),
                        alpha_frequency= count_penalty,
                        token_ban= [],
                        token_stop= [0])

    all_tokens = []
    out_last = 0
    out_str = ""
    occurence = {}
    state = None

    
    #model = RWKV(model='/root/rwkv_model/RWKV-4-Pile-14B-20221020-83', strategy='cpu fp32')  #heavy weight model

    #model = RWKV(model='/root/rwkv_model/RWKV-4-Pile-14B-20221020-83', strategy='cuda fp16')  #heavy weight model
    #switch to 'cuda fp16' for better performance

    print()
    if not task_description:
        task_description = (
            "filling forms and submitting data on web pages"
        )
        
    if not input:
        input = f"""[{{'number': 1, 'title': 'restaurant menu', 'type': 'file', 'address': 'tests/openadapt/restaurant_menu_data.txt', 'description': 'Size: 17, Type: text/plain'}}
,{{'number': 2, 'title': 'wikipedia web development page', 'type': 'web_url', 'address': 'https://en.wikipedia.org/wiki/Web_development', 'description': 'Length: 63230, Type: text/html; charset=UTF-8'}}
,{{'number': 3, 'title': 'square root function', 'type': 'function', 'address': 'math.sqrt', 'description': 'Function: sqrt, Module: math, Summary: function is used to compute the square root of a given number.'}}]"""

    if not instruction:  
        instruction = "You are given a list of signals, each detailed in a JSON format. Please provide only the signal number that would return data that is most beneficial to the task of "

    prompt = f"""Below is an instruction that describes a task, paired with an input that provides further context. Write a response that appropriately completes the request.

# Instruction: 
{instruction} {task_description}. 

# Input: 
{input}

# Response:
"""
    
    print(prompt)
    for i in range(token_count):
        out, state = model.forward(pipeline.encode(prompt)[-ctx_limit:] if i == 0 else [token], state)

        for n in occurence:
            out[n] -= (args.alpha_presence + occurence[n] * args.alpha_frequency)

        token = pipeline.sample_logits(out, temperature=args.temperature, top_p=args.top_p)

        if token in args.token_stop:
            break
        all_tokens += [token]
        
        if token not in occurence:
            occurence[token] = 1
        else:
            occurence[token] += 1

        tmp = pipeline.decode(all_tokens[out_last:])
        if '\ufffd' not in tmp:
            out_str += tmp
            out_last = i + 1


    return(out_str.strip())

@stub.local_entrypoint()
def main():
    run_RWKV.call()