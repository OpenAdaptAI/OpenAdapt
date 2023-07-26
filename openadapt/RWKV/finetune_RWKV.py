from transformers import AutoTokenizer, RwkvForCausalLM, AutoModelForCausalLM, Trainer, TrainingArguments, DataCollatorWithPadding
from peft import LoraConfig, PeftModel, get_peft_model, prepare_model_for_int8_training
from trl import SFTTrainer
#from huggingface_hub import notebook_login
import torch
from datasets import load_dataset

#notebook_login()

target_modules = ["feed_forward.value"]

URL_OF_HUGGINGFACE = "RWKV/rwkv-raven-1b5"
tokenizer = AutoTokenizer.from_pretrained("RWKV/rwkv-raven-1b5")
tokenizer.pad_token = tokenizer.eos_token

data_collator = DataCollatorWithPadding(tokenizer=tokenizer)
model = AutoModelForCausalLM.from_pretrained("RWKV/rwkv-raven-1b5")

for param in model.parameters():
    param.requires_grad = False # Freeze weights
    if param.ndim == 1:
        param.data = param.data.to(torch.float32)

#model.gradient_checkpointing_enable()
model.enable_input_require_grads()

dataset = load_dataset("json", data_files="./dataset.jsonl")

# Split the dataset into train and eval with 80-20 split
dataset = dataset['train'].train_test_split(test_size=0.2)

train_dataset = dataset['train']
eval_dataset = dataset['test']

# Tokenize and prepare our dataset
print((dataset["train"]))


training_args = TrainingArguments(
    f"RWKV-1b5-finetuned-overfit",
    evaluation_strategy = "epoch",
    num_train_epochs=8,
    warmup_steps=0,
    learning_rate=0.0005,
    logging_steps=1,
    weight_decay=0.01,
    push_to_hub=True,
    push_to_hub_model_id="RWKV-1b5-finetuned-overfit",
)

config = LoraConfig(
    r=8, lora_alpha=32, target_modules=target_modules, lora_dropout=0.1, bias="none", task_type="CAUSAL_LM"
)


lora_model = get_peft_model(model, config)
lora_model.print_trainable_parameters()



trainer = SFTTrainer(
    model=lora_model,
    train_dataset=train_dataset,
    eval_dataset=eval_dataset,
    dataset_text_field="text",
    args=training_args,
)

########## BEFORE TRAINING ##########
prompt = "Below is an instruction that describes a task, paired with an input that provides further context. Write a response that appropriately completes the request.\n# Instruction:\nYou are posting on social media platforms. A list of information signals is provided in JSON format. Please respond with only the id of the signal that is most relevant to the task formatted as a list.\n# Input:\n[{'id': 0, 'type': 'file', 'descriptor': 'File_Sorting_Script.py'}, {'id': 1, 'type': 'url', 'descriptor': 'https://www.linkedin.com'}, {'id': 2, 'type': 'function', 'descriptor': 'openai.Completion.create'}, {'id': 3, 'type': 'database', 'descriptor': 'footwear.db'}, {'id': 4, 'type': 'url', 'descriptor': 'https://www.skyscanner.com'}, {'id': 5, 'type': 'file', 'descriptor': 'restaurant_menu_data.txt'}, {'id': 6, 'type': 'url', 'descriptor': 'https://en.wikipedia.org/wiki/Web_development'}, {'id': 7, 'type': 'function', 'descriptor': 'math.sqrt'}, {'id': 8, 'type': 'url', 'descriptor': 'https://www.acuweather.com'}, {'id': 9, 'type': 'database', 'descriptor': 'user_info.db'}, {'id': 10, 'type': 'function', 'descriptor': 'sklearn.tree.DecisionTreeClassifier'}, {'id': 11, 'type': 'url', 'descriptor': 'https://www.chess.com'}, {'id': 12, 'type': 'database', 'descriptor': 'social_media_accounts.db'}, {'id': 13, 'type': 'file', 'descriptor': 'electronic_medical_record_template.xls'}, {'id': 14, 'type': 'function', 'descriptor': 'pandas.DataFrame'}]\n# Response: \n"
inputs = tokenizer.encode(prompt, return_tensors="pt")
outputs = model.generate(inputs, max_length=500, do_sample=True, temperature=0.7)
print(f"BEFORE TRAINING: {tokenizer.decode(outputs[0])}")


trainer.train()
# model = prepare_model_for_int8_training(model)
# lora_model.train()
lora_model = lora_model.merge_and_unload()

########## AFTER TRAINING ##########
prompt = "Below is an instruction that describes a task, paired with an input that provides further context. Write a response that appropriately completes the request.\n# Instruction:\nYou are posting on social media platforms. A list of information signals is provided in JSON format. Please respond with only the id of the signal that is most relevant to the task formatted as a list.\n# Input:\n[{'id': 0, 'type': 'file', 'descriptor': 'File_Sorting_Script.py'}, {'id': 1, 'type': 'url', 'descriptor': 'https://www.linkedin.com'}, {'id': 2, 'type': 'function', 'descriptor': 'openai.Completion.create'}, {'id': 3, 'type': 'database', 'descriptor': 'footwear.db'}, {'id': 4, 'type': 'url', 'descriptor': 'https://www.skyscanner.com'}, {'id': 5, 'type': 'file', 'descriptor': 'restaurant_menu_data.txt'}, {'id': 6, 'type': 'url', 'descriptor': 'https://en.wikipedia.org/wiki/Web_development'}, {'id': 7, 'type': 'function', 'descriptor': 'math.sqrt'}, {'id': 8, 'type': 'url', 'descriptor': 'https://www.acuweather.com'}, {'id': 9, 'type': 'database', 'descriptor': 'user_info.db'}, {'id': 10, 'type': 'function', 'descriptor': 'sklearn.tree.DecisionTreeClassifier'}, {'id': 11, 'type': 'url', 'descriptor': 'https://www.chess.com'}, {'id': 12, 'type': 'database', 'descriptor': 'social_media_accounts.db'}, {'id': 13, 'type': 'file', 'descriptor': 'electronic_medical_record_template.xls'}, {'id': 14, 'type': 'function', 'descriptor': 'pandas.DataFrame'}]\n# Response: \n"
inputs = tokenizer.encode(prompt, return_tensors="pt")
outputs = lora_model.generate(inputs, max_length=500, do_sample=True, temperature=0.7)
print(f"AFTER TRAINING: {tokenizer.decode(outputs[0])}")



lora_model.config.save_pretrained("./RWKV-1b5-finetuned-overfit")
trainer.save_model("./RWKV-1b5-finetuned-overfit")
prompt = "Once upon a time"

inputs = tokenizer.encode(prompt, return_tensors="pt")
outputs = lora_model.generate(inputs, max_length=500, do_sample=True, temperature=0.7)

print()
print(f"Can it write a story? {tokenizer.decode(outputs[0])}")
#trainer.push_to_hub("RWKV-1b5-finetuned-overfit")


# inputs = tokenizer("Hello, my dog is cute", return_tensors="pt")
# outputs = lora_model(**inputs, labels=inputs["input_ids"])
# loss, logits = outputs.loss, outputs.logits
# print(loss, logits)