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
    from huggingface_hub import snapshot_download

    snapshot_download("TheBloke/llama-13b", local_dir="/root/weights/13B")

image = (
    Image.from_dockerhub("pytorch/pytorch:1.13.0-cuda11.6-cudnn8-devel",
                         setup_dockerfile_commands=[
                             "RUN apt-get update",
                             "RUN apt-get install -y python3.8 python3-pip",
                         ],
                         )
        .apt_install("git", "gcc", "build-essential")
        .pip_install(
        "fairscale",
        "fire",
        "sentencepiece",
        "transformers == 4.30.0",
        "timm",
        "tensorboard",
        "ftfy",
        "gradio",
        "bitsandbytes == 0.39.0",
        "jinja2 == 3.1.2"
    )
        .run_commands(
        "git clone https://github.com/luogen1996/LaVIN.git /root/LaVIN",
    )
        .run_function(download_model
                      )
)

stub = Stub(image=image)

if stub.is_inside():
    import os
    import argparse
    import random

    import numpy as np
    import torch
    import torch.backends.cudnn as cudnn
    import gradio as gr
    from PIL import Image
    from torchvision.transforms import transforms
    from timm.data.constants import IMAGENET_DEFAULT_MEAN, IMAGENET_DEFAULT_STD
    from fairscale.nn.model_parallel.initialize import initialize_model_parallel
    from typing import Tuple


@stub.cls(gpu="a10g",
          mounts=[Mount.from_local_dir("C:/Users/Angel/Desktop/OpenAdapt/checkpoints",
                                       remote_path="/root/weights/checkpoints")]
)
class LaVIN_Model:
    def __enter__(self):
        print('Initializing Chat')

        import os
        os.chdir("/root/weights")
        for path in os.listdir():
            print(path)
        import sys
        sys.path.append("/root/LaVIN")

        from util.misc import get_rank
        from conversation.conversation import Chat, CONV_VISION
        from eval import load

        # def setup_model_parallel() -> Tuple[int, int]:
        #     local_rank = int(os.environ.get("LOCAL_RANK", -1))
        #     world_size = int(os.environ.get("WORLD_SIZE", -1))
        #
        #     torch.distributed.init_process_group("nccl")
        #     initialize_model_parallel(world_size)
        #     torch.cuda.set_device(local_rank)
        #
        #     # seed must be the same in all processes
        #     torch.manual_seed(1)
        #     return local_rank, world_size
        #
        # local_rank, world_size = setup_model_parallel()
        self.lavin = load(
            ckpt_dir="/root/weights/13B",
            llm_model="13B",
            adapter_path="/root/weights/checkpoints/llama13B-15-eph-conv.pth",
            max_seq_len=512,
            max_batch_size=4,
            adapter_type='attn',
            adapter_dim=8,
            adapter_scale=1,
            hidden_proj=128,
            visual_adapter_type='router',
            temperature=5.,
            tokenizer_path='',
            local_rank=0,
            world_size=1,
            use_vicuna=False
        )
        self.vis_processor = transforms.Compose(
            [transforms.Resize((224, 224), interpolation=Image.BICUBIC), transforms.ToTensor(),
             transforms.Normalize(IMAGENET_DEFAULT_MEAN, IMAGENET_DEFAULT_STD)])
        # self.chat = Chat(self.lavin, self.vis_processor, device=torch.device('cuda'))
        # TODO: chat unneeded
        self.device = "cuda"

        print('Initialization Finished')

    # @method()
    # def upload_img(self, gr_img, chat_state, img_list):
    #     return self.chat.upload_img(gr_img, chat_state, img_list)
    #
    # @method()
    # def ask(self, user_message, chat_state):
    #     self.chat.ask(user_message, chat_state)
    #
    # @method()
    # def answer(self, chat_state, img_list, num_beams, temperature):
    #     return self.chat.answer(conv=chat_state,
    #                             img_list=img_list,
    #                             num_beams=num_beams,
    #                             temperature=temperature,
    #                             max_new_tokens=300,
    #                             max_length=2000)

    @method()
    def generate(self, prompt, raw_image):
        # upload image and ask
        # image is Image.Image
        img = self.vis_processor(raw_image).unsqueeze(0).to(self.device)

        outputs = self.lavin.generate(
            prompts=[prompt],
            images=img,
            indicators=[1],
            max_gen_len=2000,
            n_feats=6,
            temperature=0.1,
            top_p=0.75,
        )
        output_text = outputs[0].split('Response:')[-1].strip()
        print(prompt)
        print(output_text)


@stub.local_entrypoint()
def main():
    model = LaVIN_Model()
    from PIL import Image
    okcancel_image = Image.open("C:/Users/Angel/Desktop/OpenAdapt/okcancel.jpg").convert('RGB')
    questions = [
        "How many buttons are there?",
        "What do the buttons say?",
        "What button would you click to leave?",
        "What is the position of the OK button?",
    ]
    for question in questions:
        model.generate.call(
            question,
            okcancel_image,
        )
