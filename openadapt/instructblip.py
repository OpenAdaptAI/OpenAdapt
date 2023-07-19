from modal import Image, Stub, method

BASE_MODEL = "Salesforce/instructblip-vicuna-13b"


def download_models():
    from transformers import InstructBlipProcessor, InstructBlipForConditionalGeneration

    InstructBlipProcessor.from_pretrained(BASE_MODEL)
    InstructBlipForConditionalGeneration.from_pretrained(BASE_MODEL)


image = (
    Image.debian_slim(python_version="3.10")
        .apt_install(
        "git"
    )
        .pip_install(
        "accelerate~=0.20.3",
        "transformers @git+https://github.com/huggingface/transformers",
        "torch",
        "Pillow"
    )
        .run_function(download_models)
)

stub = Stub(name="instructblip_vicuna_13b", image=image)


@stub.cls(gpu="A100", timeout=18000)
class InstructBlipModel:
    def __enter__(self):
        import torch
        from transformers import InstructBlipProcessor, InstructBlipForConditionalGeneration

        self.processor = InstructBlipProcessor.from_pretrained(BASE_MODEL)

        self.model = InstructBlipForConditionalGeneration.from_pretrained(BASE_MODEL,
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

        inputs = self.processor(images=image, text=question, return_tensors="pt").to(self.device,
                                                                                     torch.float16)
        out = self.model.generate(**inputs,
                                  do_sample=False,
                                  num_beams=5,
                                  max_length=256,
                                  min_length=1,
                                  top_p=0.9,
                                  repetition_penalty=1.5,
                                  length_penalty=1.0,
                                  temperature=1, )
        print(question)
        print(self.processor.batch_decode(out, skip_special_tokens=True)[0].strip())


@stub.local_entrypoint()
def main():
    from PIL import Image
    okcancel_image = Image.open("C:/Users/Angel/Desktop/OpenAdapt/leave.png").convert('RGB')
    questions = [
        "How many buttons are there?",
        "What do the buttons say?",
        "What button would you click to leave?",
        "What is the position of the OK button?",
    ]
    model = InstructBlipModel()
    for question in questions:
        model.generate.call(
            okcancel_image,
            question,
        )
