from modal import (
    Image,
    Mount,
    Secret,
    NetworkFileSystem,
    Stub,
    asgi_app,
    method,
)


def download_model():
    import sys

    sys.path.append("/root/Otter")

    from otter.modeling_otter import OtterForConditionalGeneration

    OtterForConditionalGeneration.from_pretrained(
        "luodian/OTTER-9B-LA-InContext", device_map="auto"
    )


image = (
    Image.debian_slim(python_version="3.9")
    .apt_install("git", "gcc", "build-essential", "cmake")
    .pip_install(
        "accelerate>=0.19.0",
        "braceexpand>=0.1.7",
        "einops>=0.6.1",
        "einops_exts>=0.0.4",
        "fastapi>=0.95.2",
        "gradio>=3.33.1",
        "horovod>=0.27.0",
        "huggingface_hub>=0.13.3",
        "importlib_metadata>=6.6.0",
        "inflection>=0.5.1",
        "markdown2>=2.4.8",
        "more_itertools>=9.1.0",
        "nltk>=3.8.1",
        "numpy>=1.23.5",
        "open_clip_torch>=2.16.0",
        "opencv_python_headless>=4.5.5.64",
        "peft",
        "Pillow>=9.5.0",
        "pycocoevalcap>=1.",
        "pycocotools>=2.0.6",
        "Requests>=2.31.0",
        "scipy>=1.10.1",
        "timm>=0.9.2",
        "tqdm>=4.65.0",
        "transformers==4.29.0",
        "uvicorn>=0.22.0",
        "webdataset>=0.2.48",
        "xformers>=0.0.20",
        "natsort>=8.4.0",
    )
    .run_commands(
        "git clone https://github.com/Luodian/Otter.git /root/Otter",
    )
    .run_function(download_model)
)

stub = Stub(name="otter_llama7b", image=image)

if stub.is_inside():
    # TODO: remove unnecessary dependencies
    # TODO: black
    import mimetypes
    from typing import Union
    import requests
    import torch
    import transformers
    from PIL import Image
    import sys


@stub.cls(
    gpu="a100",
)
class Otter_Model:
    def __enter__(self):
        sys.path.append("/root/Otter")

        from otter.modeling_otter import OtterForConditionalGeneration
        import torch

        load_bit = "bf16"
        precision = {}
        if load_bit == "bf16":
            precision["torch_dtype"] = torch.bfloat16
        elif load_bit == "fp16":
            precision["torch_dtype"] = torch.float16
        elif load_bit == "fp32":
            precision["torch_dtype"] = torch.float32
        self.model = OtterForConditionalGeneration.from_pretrained(
            "luodian/OTTER-9B-LA-InContext", device_map="sequential", **precision
        )
        self.model.text_tokenizer.padding_side = "left"
        self.tokenizer = self.model.text_tokenizer
        self.image_processor = transformers.CLIPImageProcessor()
        self.model.eval()

    def get_formatted_prompt(self, prompt: str) -> str:
        return f"<image>User: {prompt} GPT:<answer>"

    @method()
    def get_response(self, image, prompt: str) -> str:
        input_data = image

        vision_x = (
            self.image_processor.preprocess([input_data], return_tensors="pt")[
                "pixel_values"
            ]
            .unsqueeze(1)
            .unsqueeze(0)
        )

        lang_x = self.model.text_tokenizer(
            [
                self.get_formatted_prompt(prompt),
            ],
            return_tensors="pt",
        )

        model_dtype = next(self.model.parameters()).dtype

        vision_x = vision_x.to(dtype=model_dtype)
        lang_x_input_ids = lang_x["input_ids"]
        lang_x_attention_mask = lang_x["attention_mask"]

        bad_words_id = self.model.text_tokenizer(
            ["User:", "GPT1:", "GFT:", "GPT:"], add_special_tokens=False
        ).input_ids
        generated_text = self.model.generate(
            vision_x=vision_x.to(self.model.device),
            lang_x=lang_x_input_ids.to(self.model.device),
            attention_mask=lang_x_attention_mask.to(self.model.device),
            max_new_tokens=512,
            num_beams=3,
            no_repeat_ngram_size=3,
            bad_words_ids=bad_words_id,
        )
        parsed_output = (
            self.model.text_tokenizer.decode(generated_text[0])
            .split("<answer>")[-1]
            .lstrip()
            .rstrip()
            .split("<|endofchunk|>")[0]
            .lstrip()
            .rstrip()
            .lstrip('"')
            .rstrip('"')
        )
        return parsed_output


@stub.local_entrypoint()
def main():
    otter = Otter_Model()
    from PIL import Image

    okcancel_image = Image.open(
        "C:/Users/Angel/Desktop/OpenAdapt/okcancel.jpg"
    ).convert("RGB")
    questions = [
        "How many buttons are there?",
        "What do the buttons say?",
        "What button would you click to leave?",
        "What is the position of the OK button?",
    ]

    for question in questions:
        print(f"\nPrompt: {question}")
        response = otter.get_response.call(okcancel_image, question)
        print(f"Response: {response}")
