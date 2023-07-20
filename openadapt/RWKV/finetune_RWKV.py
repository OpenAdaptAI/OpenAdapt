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
    warmup_steps=100,
    learning_rate=0.001,
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

trainer.train()
# model = prepare_model_for_int8_training(model)
# lora_model.train()
trainer.push_to_hub("RWKV-1b5-finetuned-overfit")


# inputs = tokenizer("Hello, my dog is cute", return_tensors="pt")
# outputs = lora_model(**inputs, labels=inputs["input_ids"])
# loss, logits = outputs.loss, outputs.logits
# print(loss, logits)