from modal import Image, Stub, method

BASE_MODEL = "Salesforce/blip2-flan-t5-xxl"


def download_models():
    from transformers import Blip2Processor, Blip2ForConditionalGeneration

    Blip2Processor.from_pretrained(BASE_MODEL)
    Blip2ForConditionalGeneration.from_pretrained(BASE_MODEL)


image = (
    Image.debian_slim(python_version="3.10")
        .pip_install(
        "accelerate~=0.18.0",
        "transformers~=4.28.1",
        "torch~=2.0.0",
        "Pillow"
    )
        .run_function(download_models)
)

stub = Stub(name="blip2_flan_t5_xxl", image=image)


@stub.cls(gpu="A100", timeout=18000)
class Blip2Model:
    def __enter__(self):
        import torch
        from transformers import Blip2Processor, Blip2ForConditionalGeneration

        self.processor = Blip2Processor.from_pretrained(BASE_MODEL)

        self.model = Blip2ForConditionalGeneration.from_pretrained(BASE_MODEL,
                                                                   torch_dtype=torch.float16,
                                                                   device_map="auto")
        self.device = "cuda"

    @method()
    def generate(
            self,
            image,
            question,
    ):
        import torch

        inputs = self.processor(image, question, return_tensors="pt").to(self.device, torch.float16)
        out = self.model.generate(**inputs)
        print(question)
        print(self.processor.decode(out[0], skip_special_tokens=True))

@stub.local_entrypoint()
def main():
    from PIL import Image
    okcancel_image = Image.open("C:/Users/Angel/Desktop/OpenAdapt/desktop.png").convert('RGB')
    questions = [
        "Describe the picture",
        "What icons are there on the desktop?",
        "What button would you click to make a search on the Internet?",
        "What button would you click to text a friend?",
        "What is the position of the Safari button?",
    ]
    model = Blip2Model()
    for question in questions:
        model.generate.call(
            okcancel_image,
            question,
        )
