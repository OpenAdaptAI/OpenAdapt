import os
import torch
import numpy as np
import modal

from rwkv.model import RWKV
from rwkv.utils import PIPELINE, PIPELINE_ARGS
from transformers import PreTrainedTokenizerFast

#integrate modal to load bigger RWKV model
stub = modal.Stub("RWKV-test")

#check if cuda is available
# print(torch.zeros(1).cuda())
# print(torch.cuda.is_available())


os.environ["RWKV_JIT_ON"] = '1'
os.environ["RWKV_CUDA_ON"] = '0'

torch_image = modal.Image.debian_slim().pip_install("torch", "rwkv", "numpy", "transformers")

#@stub.function(image=torch_image, mounts=[modal.Mount.from_local_file("./openadapt/20B_tokenizer.json", remote_path="/root/rwkv_model")])#,modal.Mount.from_local_file("D:\\models\RWKV-4-Pile-430M-20220808-8066.pth", remote_path="/root/rwkv_model")])
@stub.function(image=torch_image, mounts=[modal.Mount.from_local_dir("D:\models", remote_path="/root/rwkv_model")])#,modal.Mount.from_local_file("D:\\models\RWKV-4-Pile-430M-20220808-8066.pth", remote_path="/root/rwkv_model")])
def run_RWKV():

    #tokenizer = PreTrainedTokenizerFast(tokenizer_file="./openadapt/strategies/20B_tokenizer.json")
    tokenizer = PreTrainedTokenizerFast(tokenizer_file="/root/rwkv_model/20B_tokenizer.json")

    #model = RWKV(model='/root/rwkv_model/RWKV-4-Pile-430M-20220808-8066', strategy='cpu fp32')  #light weight model
    model = RWKV(model='/root/rwkv_model/RWKV-4-Pile-14B-20221020-83', strategy='cpu fp32')  #heavy weight model

    #model = RWKV(model='/root/rwkv_model/RWKV-4-Pile-14B-20221020-83', strategy='cuda fp16')  #heavy weight model
    #switch to 'cuda fp16' for better performance

    out, state = model.forward([187, 510, 1563, 310, 247], None)   # use 20B_tokenizer.json
    print(out.detach().cpu().numpy())                   # get logits
    out, state = model.forward([187, 510], None)
    out, state = model.forward([1563], state)           # RNN has state (use deepcopy if you want to clone it)
    out, state = model.forward([310, 247], state)
    print(out.detach().cpu().numpy())                   # same result as above

    ############################################################
    test_prompt = [187, 510, 1563, 310, 247]
    predicted_token = np.argmax(test_prompt)
    # Convert token back to word
    predicted_word = tokenizer.decode([predicted_token])
    print("input was: "+predicted_word)
    ############################################################







    # Select the token with the highest logit
    predicted_token = np.argmax(out.detach().cpu().numpy())

    # Convert token back to word
    predicted_word = tokenizer.decode([predicted_token])
    print(predicted_word)

    prompt = "Continue the story: The sky is blue and the sun is"
    prompt_tokens = tokenizer.encode(prompt, return_tensors="pt")
    print(prompt_tokens)
    out, state = model.forward(prompt_tokens[0].tolist(), None)
    predicted_token = np.argmax(out.detach().cpu().numpy())
    predicted_word = tokenizer.decode(predicted_token)
    print(predicted_word)

    for i in range(12):
        out, state = model.forward([predicted_token], state)
        predicted_token = np.argmax(out.detach().cpu().numpy())
        predicted_word = tokenizer.decode(predicted_token)
        print(predicted_word)

    #while predicted token is not stop token, 
    # then out,state = model.forward(prompt_tokens  , state)
    # predicted_token = np.argmax(out.detach().cpu().numpy())
    # prompt_tokens.append(predicted_token)

@stub.local_entrypoint()
def main():
    run_RWKV.call()