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
stub = modal.Stub("test-stub")

os.environ["RWKV_JIT_ON"] = '1'
os.environ["RWKV_CUDA_ON"] = '0'

torch_image = modal.Image.debian_slim().pip_install("torch", "rwkv", "numpy", "transformers")

@stub.function(gpu="a100", timeout=18000, image=torch_image)#, mounts=[modal.Mount.from_local_dir("./openadapt/tokenizer", remote_path="/root/rwkv_model")])
def run_RWKV(task_description=None, input=None):

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

    title = "RWKV-4-Raven-14B-v12-Eng98%-Other2%-20230523-ctx8192"
    model_path = hf_hub_download(repo_id="BlinkDL/rwkv-4-raven", filename=f"{title}.pth")
    model = RWKV(model=model_path, strategy='cuda fp16')  #heavy weight model


    tokenizer_url = "https://raw.githubusercontent.com/BlinkDL/RWKV-LM/main/RWKV-v4/20B_tokenizer.json"
    response = requests.get(tokenizer_url)
    if response.status_code == 200:
        tokenizer_path = "/root/rwkv_model/20B_tokenizer.json"
        os.makedirs(os.path.dirname(tokenizer_path), exist_ok=True)
        with open(tokenizer_path, 'wb') as f:
            f.write(response.content)
        tokenizer = PreTrainedTokenizerFast(tokenizer_file=tokenizer_path)
    else:
        print(f"Failed to download tokenizer. Status code: {response.status_code}")
        return
    #tokenizer = PreTrainedTokenizerFast(tokenizer_file="./openadapt/strategies/20B_tokenizer.json")
    #tokenizer = PreTrainedTokenizerFast(tokenizer_file="/root/rwkv_model/20B_tokenizer.json")
    pipeline = PIPELINE(model,"/root/rwkv_model/20B_tokenizer.json")
    
    temperature = 0.9
    top_p = 0.9
    countPenalty = 0.1
    token_count = 200
    ctx_limit = 1536

    args = PIPELINE_ARGS(temperature= float(temperature), 
                        top_p= float(top_p),
                        alpha_frequency= countPenalty,
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
    task_description = (
        "filling forms and submitting data on web pages"
    )
    prompt = f"""Below is an instruction that describes a task, paired with an input that provides further context. Write a response that appropriately completes the request.

# Instruction: 
you are given a list of signals, each detailed in a JSON format.Please provide only the signal number that would return data that is most beneficial for {task_description}. 

# Input: 
[{{'number': 1, 'title': 'test data file', 'type': 'file', 'address': 'tests/openadapt/test_signal_data.txt', 'description': 'Size: 17, Type: text/plain'}}
,{{'number': 2, 'title': 'wikipedia web development page', 'type': 'web_url', 'address': 'https://en.wikipedia.org/wiki/Web_development', 'description': 'Length: 63230, Type: text/html; charset=UTF-8'}}
,{{'number': 3, 'title': 'test function', 'type': 'function', 'address': 'sample_package.sample_module.sample_function', 'description': 'Function: sample_function, Module: sample_package.sample_module, Description: \n    This function is used to test the openadapt.signals module\n    '}}]

# Signal Number:
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
            #yield out_str.strip()
            #print(out_str.strip())
            out_last = i + 1


    print(out_str.strip())

    # print(prompt)
    # prompt_tokens = tokenizer.encode(prompt, return_tensors="pt")
    # out, state = model.forward(prompt_tokens[0].tolist(), None)
    # predicted_token = np.argmax(out.detach().cpu().numpy())
    # predicted_word = tokenizer.decode(predicted_token)
    # print(predicted_word)

    # for i in range(100):
    #     out, state = model.forward([predicted_token], state)
    #     predicted_token = np.argmax(out.detach().cpu().numpy())
    #     predicted_word = tokenizer.decode(predicted_token)
    #     print(predicted_word)

    #while predicted token is not stop token, 
    # then out,state = model.forward(prompt_tokens  , state)
    # predicted_token = np.argmax(out.detach().cpu().numpy())
    # prompt_tokens.append(predicted_token)

@stub.local_entrypoint()
def main():
    run_RWKV.call()