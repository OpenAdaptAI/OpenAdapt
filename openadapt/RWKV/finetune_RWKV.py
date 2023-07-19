from transformers import AutoTokenizer, RwkvForCausalLM, AutoModelForCausalLM, Trainer, TrainingArguments
from peft import LoraConfig, PeftModel, get_peft_model, prepare_model_for_int8_training
#from huggingface_hub import notebook_login
import torch
from datasets import load_dataset

#notebook_login()

target_modules = ["feed_forward.value"]

URL_OF_HUGGINGFACE = "RWKV/rwkv-raven-1b5"
tokenizer = AutoTokenizer.from_pretrained("RWKV/rwkv-raven-1b5")
model = AutoModelForCausalLM.from_pretrained("RWKV/rwkv-raven-1b5")

for param in model.parameters():
    param.requires_grad = False # Freeze weights
    if param.ndim == 1:
        param.data = param.data.to(torch.float32)

#model.gradient_checkpointing_enable()
model.enable_input_require_grads()

dataset = load_dataset("json", data_files="./labelled_dataset.jsonl")

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
)

config = LoraConfig(
    r=8, lora_alpha=32, target_modules=target_modules, lora_dropout=0.1, bias="none", task_type="CAUSAL_LM"
)


lora_model = get_peft_model(model, config)
lora_model.print_trainable_parameters()

trainer = Trainer(
    model=lora_model,
    train_dataset=dataset["train"]["text"],
    args=training_args,
)

trainer.train()
# model = prepare_model_for_int8_training(model)
# lora_model.train()


# inputs = tokenizer("Hello, my dog is cute", return_tensors="pt")
# outputs = lora_model(**inputs, labels=inputs["input_ids"])
# loss, logits = outputs.loss, outputs.logits
# print(loss, logits)