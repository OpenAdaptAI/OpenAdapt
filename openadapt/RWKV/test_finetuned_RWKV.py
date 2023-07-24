from transformers import AutoTokenizer, AutoModelForCausalLM

# Replace these two lines with your model and tokenizer names, if different
model_path = "./RWKV-1b5-finetuned-overfit"
tokenizer_name = "RWKV/rwkv-raven-1b5"

# Load the model and tokenizer
tokenizer = AutoTokenizer.from_pretrained(tokenizer_name)
model = AutoModelForCausalLM.from_pretrained(model_path)

# Prepare a prompt for your model
prompt = "Once upon a time"

# Encode the prompt and run it through the model
inputs = tokenizer.encode(prompt, return_tensors="pt")
outputs = model.generate(inputs, max_length=500, do_sample=True, temperature=0.7)

# Decode the output and print it
print(tokenizer.decode(outputs[0]))

model = AutoModelForCausalLM.from_pretrained("RWKV/rwkv-raven-1b5")
tokenizer = AutoTokenizer.from_pretrained("RWKV/rwkv-raven-1b5")

prompt = "Once upon a time"

inputs = tokenizer.encode(prompt, return_tensors="pt")
outputs = model.generate(inputs, max_length=500, do_sample=True, temperature=0.7)

print(tokenizer.decode(outputs[0]))
