import os
import torch
import numpy as np
import modal
import requests
import tempfile

from huggingface_hub import hf_hub_download
from rwkv import rwkv_tokenizer
from rwkv.model import RWKV
from rwkv.utils import PIPELINE, PIPELINE_ARGS
from transformers import PreTrainedTokenizerFast

# use modal to load larger RWKV models
stub = modal.Stub("openadapt-rwkv")

os.environ["RWKV_JIT_ON"] = "1"
os.environ["RWKV_CUDA_ON"] = "0"

torch_image = modal.Image.debian_slim().pip_install(
    "torch", "rwkv", "numpy", "transformers"
)


@stub.function(gpu="a100", timeout=18000, image=torch_image)
def run_RWKV(
    model_number=0,
    prompt=None,
    instruction=None,
    task_description=None,
    input=None,
    parameters=None,
    use_cuda=True,
):
    """Makes a call to the RWKV model and returns the response.

    Args:
        model_number (int, optional): The model to use. Defaults to 0.
        instruction (str, optional): The instruction to use. Defaults to None.
        task_description (str, optional): The task description to use. Defaults to None.
        input (str, optional): The input to use. Defaults to None.
        parameters (dict, optional): The parameters to use. Defaults to None.
        use_cuda (bool, optional): Whether to use cuda. Defaults to True.

    Returns:
        str: The response from the model.
    """
    # use gpu=a100 for Raven-14B and Pile-14B, vs. use gpu=any for other weights
    # switch 'cuda fp16' to 'cpu fp32' if running on cpu is preferred
    if model_number == 0:
        title = "RWKV-4-Raven-14B-v12-Eng98%-Other2%-20230523-ctx8192"
        model_path = hf_hub_download(
            repo_id="BlinkDL/rwkv-4-raven", filename=f"{title}.pth"
        )
    elif model_number == 1:
        title = "RWKV-4-Raven-7B-v12-Eng98%-Other2%-20230521-ctx8192"
        model_path = hf_hub_download(
            repo_id="BlinkDL/rwkv-4-raven", filename=f"{title}.pth"
        )
    elif model_number == 2:
        title = "RWKV-4-Raven-1B5-v12-Eng98%-Other2%-20230520-ctx4096"
        model_path = hf_hub_download(
            repo_id="BlinkDL/rwkv-4-raven", filename=f"{title}.pth"
        )
    elif model_number == 3:
        title = "RWKV-4-Pile-14B-20230313-ctx8192-test1050"
        model_path = hf_hub_download(
            repo_id="BlinkDL/rwkv-4-pile-14b", filename=f"{title}.pth"
        )


    if use_cuda == True:
        if model_number == 4 or model_number == 5:
            model = RWKV(model=model_path, strategy="cuda fp32")
        else:
            model = RWKV(model=model_path, strategy="cuda fp16")
    else:
        model = RWKV(model=model_path, strategy="cpu fp32")

    if model_number == 4:
        tokenizer_url = "https://raw.githubusercontent.com/BlinkDL/ChatRWKV/main/tokenizer/rwkv_vocab_v20230424.txt"
    else:
        tokenizer_url = "https://raw.githubusercontent.com/BlinkDL/RWKV-LM/main/RWKV-v4/20B_tokenizer.json"
    response = requests.get(tokenizer_url)
    if response.status_code == 200:
        # Specify a path to save the tokenizer to if running in a local environment
        tokenizer_path = "/root/rwkv_model/20B_tokenizer.json"
        os.makedirs(os.path.dirname(tokenizer_path), exist_ok=True)
        # with open(tokenizer_path, 'wb') as f:
        #     f.write(response.content)
        tokenizer = tempfile.NamedTemporaryFile(delete=False)
        tokenizer.write(response.content)
        tokenizer.close()
        # tokenizer = PreTrainedTokenizerFast(tokenizer_file=tokenizer_path)
    else:
        print(f"Failed to download tokenizer. Status code: {response.status_code}")
        return

    pipeline = PIPELINE(model, tokenizer.name)

    os.unlink(tokenizer.name)

    if not parameters:
        temperature = 1.0
        top_p = 0.9
        count_penalty = 0.4
        presence_penalty = 0.4
        token_count = 200
        ctx_limit = 1536
    else:
        temperature = parameters["temperature"]
        top_p = parameters["top_p"]
        count_penalty = parameters["count_penalty"]
        presence_penalty = parameters["presence_penalty"]
        token_count = parameters["token_count"]
        ctx_limit = parameters["ctx_limit"]

    args = PIPELINE_ARGS(
        temperature=float(temperature),
        top_p=float(top_p),
        alpha_frequency=count_penalty,
        alpha_presence=presence_penalty,
        token_ban=[],
        token_stop=[0],
    )

    all_tokens = []
    out_last = 0
    out_str = ""
    occurence = {}
    state = None
    if prompt is None:
        prompt = f"""Below is an instruction that describes a task, paired with an input that provides further context. Write a response that appropriately completes the request.

    # Instruction: 
    {instruction}

    # Input: 
    {input}

    # Response:
    """
    else:
        prompt = prompt

    print(prompt) # Visible in Modal's logs
    for i in range(token_count):
        out, state = model.forward(
            pipeline.encode(prompt)[-ctx_limit:] if i == 0 else [token], state
        )

        for n in occurence:
            out[n] -= args.alpha_presence + occurence[n] * args.alpha_frequency

        token = pipeline.sample_logits(
            out, temperature=args.temperature, top_p=args.top_p
        )

        if token in args.token_stop:
            break
        all_tokens += [token]

        if token not in occurence:
            occurence[token] = 1
        else:
            occurence[token] += 1

        tmp = pipeline.decode(all_tokens[out_last:])
        if "\ufffd" not in tmp:
            out_str += tmp
            out_last = i + 1

    # print(out_str.strip())
    return out_str.strip()


@stub.local_entrypoint()
def main():
    run_RWKV.call()
