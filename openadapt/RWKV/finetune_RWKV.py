from transformers import AutoTokenizer, RwkvForCausalLM, AutoModelForCausalLM, Trainer, TrainingArguments, DataCollatorWithPadding
from peft import LoraConfig, PeftModel, get_peft_model, prepare_model_for_int8_training
from trl import SFTTrainer
#from huggingface_hub import notebook_login
import torch
import modal
from datasets import load_dataset

#notebook_login()

stub = modal.Stub("finetune-RWKV")

torch_image = modal.Image.debian_slim().pip_install("transformers", "peft", "trl", "torch", "datasets")
torch_image = torch_image.apt_install("git")
torch_image = torch_image.apt_install("git-lfs")

@stub.function(timeout=18000, image = torch_image, mounts=[modal.Mount.from_local_dir("./openadapt/RWKV", remote_path="/root/data")])
def finetune():
    target_modules = ["feed_forward.value"]

    URL_OF_HUGGINGFACE = "RWKV/rwkv-raven-7b"
    tokenizer = AutoTokenizer.from_pretrained(URL_OF_HUGGINGFACE)
    tokenizer.pad_token = tokenizer.eos_token

    data_collator = DataCollatorWithPadding(tokenizer=tokenizer)
    model = AutoModelForCausalLM.from_pretrained(URL_OF_HUGGINGFACE)

    for param in model.parameters():
        param.requires_grad = False # Freeze weights
        if param.ndim == 1:
            param.data = param.data.to(torch.float32)

    #model.gradient_checkpointing_enable()
    model.enable_input_require_grads()

    dataset = load_dataset("json", data_files="./data/dataset.jsonl")

    # Split the dataset into train and eval with 80-20 split
    dataset = dataset['train'].train_test_split(test_size=0.2)

    train_dataset = dataset['train']
    eval_dataset = dataset['test']

    # Tokenize and prepare our dataset
    # print((dataset["train"]))


    training_args = TrainingArguments(
        f"RWKV-7b-finetuned",
        evaluation_strategy = "epoch",
        num_train_epochs=2,
        warmup_steps=0,
        learning_rate=0.001,
        logging_steps=1,
        weight_decay=0.01,
        push_to_hub=True,
        #push_to_hub_model_id="RWKV-1b5-finetuned-overfit",
        hub_model_id="avidoavid/RWKV-7b-finetuned",
        hub_token="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"
    )

    config = LoraConfig(
        r=8, lora_alpha=32, target_modules=target_modules, lora_dropout=0.1, bias="none", task_type="CAUSAL_LM"
    )


    lora_model = get_peft_model(model, config)
    lora_model.print_trainable_parameters()


    trainer = SFTTrainer(
        model=lora_model,
        train_dataset=train_dataset,
        tokenizer=tokenizer,
        eval_dataset=eval_dataset,
        dataset_text_field="text",
        args=training_args,
    )


    trainer.train()

    # Encode the prompt and run it through the model
    prompt = "Once upon a time"
    inputs = tokenizer.encode(prompt, return_tensors="pt")
    batch = tokenizer(prompt, return_tensors="pt")

    output_tokens = model.generate(**batch, max_length=500)

    # Decode the output and print it
    print(f"AFTER TRAINING:")
    print(tokenizer.decode(output_tokens[0], skip_special_tokens=True))


    trainer.push_to_hub("RWKV-7b-finetuned")



@stub.local_entrypoint()
def main():
    finetune.call()