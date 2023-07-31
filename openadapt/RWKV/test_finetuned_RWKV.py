from transformers import AutoTokenizer, AutoModelForCausalLM
from peft import PeftModel, PeftConfig
import modal


stub = modal.Stub("finetune-RWKV")

torch_image = modal.Image.debian_slim().pip_install("transformers", "peft", "trl", "torch", "datasets")
torch_image = torch_image.apt_install("git")
torch_image = torch_image.apt_install("git-lfs")

@stub.function(timeout=18000, image = torch_image, mounts=[modal.Mount.from_local_dir("./openadapt/RWKV", remote_path="/root/data")])
def test():
    # Replace these two lines with your model and tokenizer names, if different
    model_path = "./RWKV-1b5-finetuned-overfit"
    repo_path = "avidoavid/RWKV-14b-finetuned"
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
    
    #prompt = "Once upon a time"
    prompt = "Below is an instruction that describes a task, paired with an input that provides further context. Write a response that appropriately completes the request.\n# Instruction:\nYou are booking a flight. A list of information signals is provided in JSON format. Please respond with only the id of the signal that is most relevant to the task formatted as a list.\n# Input:\n[{'id': 0, 'type': 'database', 'descriptor': 'social_media_accounts.db'}, {'id': 1, 'type': 'url', 'descriptor': 'https://www.acuweather.com'}, {'id': 2, 'type': 'function', 'descriptor': 'pandas.DataFrame'}, {'id': 3, 'type': 'url', 'descriptor': 'https://www.chess.com'}, {'id': 4, 'type': 'function', 'descriptor': 'sklearn.tree.DecisionTreeClassifier'}, {'id': 5, 'type': 'file', 'descriptor': 'electronic_medical_record_template.xls'}, {'id': 6, 'type': 'database', 'descriptor': 'footwear.db'}, {'id': 7, 'type': 'file', 'descriptor': 'File_Sorting_Script.py'}, {'id': 8, 'type': 'file', 'descriptor': 'restaurant_menu_data.txt'}, {'id': 9, 'type': 'database', 'descriptor': 'user_info.db'}, {'id': 10, 'type': 'function', 'descriptor': 'openai.Completion.create'}, {'id': 11, 'type': 'url', 'descriptor': 'https://en.wikipedia.org/wiki/Web_development'}, {'id': 12, 'type': 'url', 'descriptor': 'https://www.skyscanner.com'}, {'id': 13, 'type': 'url', 'descriptor': 'https://www.linkedin.com'}, {'id': 14, 'type': 'function', 'descriptor': 'math.sqrt'}]\n# Response:"

    # Encode the prompt and run it through the model
    inputs = tokenizer.encode(prompt, return_tensors="pt")
    batch = tokenizer(prompt, return_tensors="pt")
    output_tokens = model.generate(**batch, max_length=30)
    #outputs = model.generate(inputs, max_length=500, do_sample=True, temperature=0.7)

    # Decode the output and print it
    print(tokenizer.decode(output_tokens[0], skip_special_tokens=True))

    # model = AutoModelForCausalLM.from_pretrained("RWKV/rwkv-raven-1b5")
    # tokenizer = AutoTokenizer.from_pretrained("RWKV/rwkv-raven-1b5")

    # prompt = "Once upon a time"

    # inputs = tokenizer.encode(prompt, return_tensors="pt")
    # outputs = model.generate(inputs, max_length=500, do_sample=True, temperature=0.7)

    # print(tokenizer.decode(outputs[0]))

@stub.local_entrypoint()
def main():
    test.call()
