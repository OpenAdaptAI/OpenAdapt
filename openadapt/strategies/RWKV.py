import os
import torch
import numpy as np


from rwkv.model import RWKV
from transformers import PreTrainedTokenizerFast

tokenizer = PreTrainedTokenizerFast(tokenizer_file="./openadapt/strategies/20B_tokenizer.json")

os.environ["RWKV_JIT_ON"] = '1'
os.environ["RWKV_CUDA_ON"] = '0'

model = RWKV(model='D:\models\RWKV-4-Pile-430M-20220808-8066', strategy='cpu fp32') 
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

prompt = "The sky is blue and the grass is"
prompt_tokens = tokenizer.encode(prompt, return_tensors="pt")
print(prompt_tokens)
out, state = model.forward(prompt_tokens[0].tolist(), None)
predicted_token = np.argmax(out.detach().cpu().numpy())
predicted_word = tokenizer.decode(predicted_token)
print(predicted_word)

#while predicted token is not stop token, 
# then out,state = model.forward(prompt_tokens  , state)
# predicted_token = np.argmax(out.detach().cpu().numpy())
# prompt_tokens.append(predicted_token)