"""This script executes the MiniGPT-4 model within a modal container and asks
the questions from openadapt/vision/questions.txt about the images in
openadapt/vision/images.

To run this script, make sure you have created a modal token by running
the following:

    $ modal token new

Usage:

    $ modal run openadapt/vision/minigpt4.py

"""

from modal import Image, Stub, method
from PIL import Image as PILImage

IMAGES_PATH = "openadapt/vision/images"
QUESTIONS_PATH = "openadapt/vision/questions.txt"

TIMEOUT = 18000
NUM_BEAMS = (1,)
TEMPERATURE = (1,)
MAX_NEW_TOKENS = (300,)
MAX_LENGTH = (2000,)


def download_model() -> None:
    """This function downloads the necessary Vicuna-13B model snapshot from Hugging Face
    Hub.
    """
    from huggingface_hub import snapshot_download

    snapshot_download("lmsys/vicuna-13b-delta-v0", local_dir="/root/weights/delta")


def apply_delta() -> None:
    """Use FastChat to apply the delta weights to create the final weights."""
    from fastchat.model.apply_delta import apply_delta

    apply_delta("Ejafa/llama_13B", "/root/weights/vicuna", "/root/weights/delta")


def download_checkpoint() -> None:
    """Download the pretrained checkpoint inside the container."""
    from huggingface_hub import snapshot_download

    snapshot_download("Vision-CAIR/MiniGPT-4", local_dir="/root/weights/checkpoint")
    # path is /root/weights/checkpoint/pretrained_minigpt4.pth


def change_paths() -> None:
    """Change the config paths in the MiniGPT-4 repository according to the
    setup instructions"""
    file1 = "/root/MiniGPT-4/eval_configs/minigpt4_eval.yaml"
    with open(file1, "r") as file:
        file_contents = file.read()

    modified_contents = file_contents.replace(
        "/path/to/vicuna/weights/", "/root/weights/vicuna"
    )

    with open(file1, "w") as file:
        file.write(modified_contents)

    file2 = "/root/MiniGPT-4/minigpt4/configs/models/minigpt4.yaml"
    with open(file2, "r") as file:
        file_contents = file.read()

    modified_contents = file_contents.replace(
        "/path/to/pretrained/ckpt/", "/root/weights/checkpoint/pretrained_minigpt4.pth"
    )

    with open(file2, "w") as file:
        file.write(modified_contents)


image = (
    Image.from_dockerhub(
        "pytorch/pytorch:2.0.0-cuda11.7-cudnn8-devel",
        setup_dockerfile_commands=[
            "RUN apt-get update",
            "RUN apt-get install -y python3-pip",
        ],
    )
    .run_commands("python -V")
    .apt_install(
        "git",
        "gcc",
        "build-essential",
        "cmake",
        "libsm6",
        "libxext6",
        "libxrender-dev",
        "libgl1-mesa-glx",
        "libglib2.0-0",
    )
    .pip_install(
        "accelerate==0.16.0",
        "aiohttp==3.8.4",
        "aiosignal==1.3.1",
        "async-timeout==4.0.2",
        "attrs==22.2.0",
        "bitsandbytes==0.37.0",
        "cchardet==2.1.7",
        "chardet==5.1.0",
        "contourpy==1.0.7",
        "cycler==0.11.0",
        "filelock==3.9.0",
        "fonttools==4.38.0",
        "frozenlist==1.3.3",
        "huggingface-hub==0.13.4",
        "importlib-resources==5.12.0",
        "jinja2 == 3.1.2",
        "kiwisolver==1.4.4",
        "matplotlib==3.7.0",
        "multidict==6.0.4",
        "openai==0.27.0",
        "packaging==23.0",
        "psutil==5.9.4",
        "pycocotools==2.0.6",
        "pyparsing==3.0.9",
        "python-dateutil==2.8.2",
        "pyyaml==6.0",
        "regex==2022.10.31",
        "tokenizers==0.13.2",
        "torch==2.0.0",
        "torchaudio",
        "torchvision",
        "tqdm==4.64.1",
        "transformers==4.28.0",
        "timm==0.6.13",
        "spacy==3.5.1",
        "webdataset==0.2.48",
        "scikit-learn==1.2.2",
        "scipy==1.10.1",
        "yarl==1.8.2",
        "zipp==3.14.0",
        "omegaconf==2.3.0",
        "opencv-python==4.7.0.72",
        "iopath==0.1.10",
        "decord==0.6.0",
        "tenacity==8.2.2",
        "peft",
        "pycocoevalcap",
        "sentence-transformers",
        "umap-learn",
        "notebook",
        "gradio==3.24.1",
        "gradio-client==0.0.8",
        "wandb",
    )
    .run_commands(
        "git clone https://github.com/Vision-CAIR/MiniGPT-4.git /root/MiniGPT-4",
    )
    .pip_install("fschat==0.1.10")
    .run_function(download_model)
    .run_function(apply_delta, memory=70000)
    .run_function(download_checkpoint)
    .run_commands(
        "sed -i 's@/path/to/pretrained/ckpt/@"
        "/root/weights/checkpoint/pretrained_minigpt4.pth@g' "
        "/root/MiniGPT-4/eval_configs/minigpt4_eval.yaml",
        "sed -i 's@/path/to/vicuna/weights/@/root/weights/vicuna/@g' "
        "/root/MiniGPT-4/minigpt4/configs/models/minigpt4.yaml",
        "sed -i 's@prompts/alignment.txt@/root/MiniGPT-4/prompts/alignment.txt@g' "
        "/root/MiniGPT-4/eval_configs/minigpt4_eval.yaml",
    )
)

stub = Stub(name="minigpt4", image=image)


@stub.cls(gpu="a10g", timeout=TIMEOUT)
class MiniGPT4Model:
    """This class facilitates interactions with the MiniGPT-4 model.
    Sample usage where pil_image is an open PIL Image and question is a string:
    >>> model = MiniGPT4Model()
    >>> model.generate.call(pil_image, question)
    """

    def __enter__(self) -> None:
        """
        Automatically initialize the MiniGPT-4 model and processor upon initializing
        the MiniGPT4Model class.
        """
        import sys

        sys.path.append("/root/MiniGPT-4")
        sys.path.append("/usr/local/lib/python3.9/site-packages")

        from minigpt4.common.config import Config
        from minigpt4.common.registry import registry
        from minigpt4.conversation.conversation import CONV_VISION, Chat

        print("Initializing Chat")
        args = self.parse_args()
        cfg = Config(args)

        model_config = cfg.model_cfg
        model_config.device_8bit = args.gpu_id
        model_cls = registry.get_model_class(model_config.arch)
        self.model = model_cls.from_config(model_config).to(
            "cuda:{}".format(args.gpu_id)
        )

        vis_processor_cfg = cfg.datasets_cfg.cc_sbu_align.vis_processor.train
        self.vis_processor = registry.get_processor_class(
            vis_processor_cfg.name
        ).from_config(vis_processor_cfg)
        self.chat = Chat(
            self.model, self.vis_processor, device="cuda:{}".format(args.gpu_id)
        )

        self.img_list = []
        self.chat_state = CONV_VISION.copy()
        print("Initialization Finished")

    def parse_args(self):
        import argparse

        parser = argparse.ArgumentParser(description="Demo")
        parser.add_argument(
            "--cfg-path", required=True, help="path to configuration file."
        )
        parser.add_argument(
            "--gpu-id", type=int, default=0, help="specify the gpu to load the model."
        )
        parser.add_argument(
            "--options",
            nargs="+",
            help=(
                "override some settings in the used config, the key-value pair "
                "in xxx=yyy format will be merged into config file (deprecate), "
                "change to --cfg-options instead."
            ),
        )
        args = parser.parse_args(
            ["--cfg-path=/root/MiniGPT-4/eval_configs/minigpt4_eval.yaml", "--gpu-id=0"]
        )
        return args

    @method()
    def generate(self, image: PILImage, question: str) -> str:
        """Generate a completion for the given question about the given image.
        Use .call to call this function in the modal container.

        Args:
            image (PILImage): an image to pass to the model
            question (str): a question about the image

        Returns:
            str: the completion generated by the model
        """
        self.chat.upload_img(image, self.chat_state, self.img_list)
        self.chat.ask(question, self.chat_state)
        return self.chat.answer(
            conv=self.chat_state,
            img_list=self.img_list,
            num_beams=NUM_BEAMS,
            temperature=TEMPERATURE,
            max_new_tokens=MAX_NEW_TOKENS,
            max_length=MAX_LENGTH,
        )[0]


@stub.local_entrypoint()
def main() -> None:
    """Entrypoint for the modal container."""

    import os

    from PIL import Image

    model = MiniGPT4Model()

    with open(QUESTIONS_PATH, "r") as file:
        lines = file.readlines()
        questions = [line.strip() for line in lines]

    images = os.listdir(IMAGES_PATH)
    for image in images:
        print(f"Current image: {image}")
        image_path = os.path.join(IMAGES_PATH, image)
        pil_image = Image.open(image_path).convert("RGB")

        for question in questions:
            print(f"Question: {question}")
            response = model.generate.call(
                pil_image,
                question,
            )
            print(response)
