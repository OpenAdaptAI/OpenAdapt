from transformers import AutoTokenizer, AutoModelForCausalLM
from peft import PeftModel, PeftConfig

# Replace these two lines with your model and tokenizer names, if different
model_path = "./RWKV-1b5-finetuned-overfit"
repo_path = "avidoavid/RWKV-1b5-finetuned-overfit"
tokenizer_name = "RWKV/rwkv-raven-1b5"

# Load the model and tokenizer
tokenizer = AutoTokenizer.from_pretrained(repo_path)
tokenizer.pad_token = tokenizer.eos_token

#model = AutoModelForCausalLM.from_pretrained(repo_path, return_dict=True)

config = PeftConfig.from_pretrained(repo_path)
model = AutoModelForCausalLM.from_pretrained(config.base_model_name_or_path, return_dict=True)
model = PeftModel.from_pretrained(model, repo_path)
#model = model.merge_and_unload()
# Prepare a prompt for your model
prompt = "Once upon a time"

# Encode the prompt and run it through the model
inputs = tokenizer.encode(prompt, return_tensors="pt")
batch = tokenizer(prompt, return_tensors="pt")
output_tokens = model.generate(**batch, max_length=500)
#outputs = model.generate(inputs, max_length=500, do_sample=True, temperature=0.7)

# Decode the output and print it
print(tokenizer.decode(output_tokens[0], skip_special_tokens=True))

# model = AutoModelForCausalLM.from_pretrained("RWKV/rwkv-raven-1b5")
# tokenizer = AutoTokenizer.from_pretrained("RWKV/rwkv-raven-1b5")

# prompt = "Once upon a time"

# inputs = tokenizer.encode(prompt, return_tensors="pt")
# outputs = model.generate(inputs, max_length=500, do_sample=True, temperature=0.7)

# print(tokenizer.decode(outputs[0]))
