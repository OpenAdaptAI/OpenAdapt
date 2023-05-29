# import argparse
# import os
# import random
#
# import numpy as np
# import torch
# import torch.backends.cudnn as cudnn
# import gradio as gr

from minigpt4.common.config import Config
from minigpt4.common.dist_utils import get_rank
from minigpt4.common.registry import registry
from minigpt4.conversation.conversation import Chat, CONV_VISION

# imports modules for registration
from minigpt4.datasets.builders import *
from minigpt4.models import *
from minigpt4.processors import *
from minigpt4.runners import *
from minigpt4.tasks import *

import modal

# stub = modal.Stub("demo", image=modal.Image.from_dockerhub(
#     "bewithmeallmylife/mini-gpt4-runtime-cuda-10.2:1.0.0").pip_install('omegaconf', 'iopath').run_commands
# ("pip install torch==1.13.1+cu117 torchvision==0.14.1+cu117 torchaudio==0.13.1 --extra-index-url "
#  "https://download.pytorch.org/whl/cu117").pip_install("timm"))

# stub = modal.Stub("demo", image=modal.Image.from_dockerhub(
#     "bewithmeallmylife/mini-gpt4-runtime-cuda-10.2:1.0.0").run_commands
# ("conda init bash", "conda activate mini-gpt4", "conda install pytorch==1.12.1 torchvision==0.13.1 "
#                                                 "torchaudio==0.12.1 cudatoolkit=11.3 -c pytorch"))

# stub = modal.Stub("demo", image=modal.Image.from_dockerhub(
#     "bewithmeallmylife/mini-gpt4-runtime-cuda-10.2:1.0.0").conda().
#                   conda_update_from_environment("environment.yml").conda_install("git").
#                   run_commands('pip3 install "git+https://github.com/philferriere/cocoapi.git#egg='
#                                'pycocotools&subdirectory=PythonAPI"'))

# stub = modal.Stub("demo_please_work", image=modal.Image.from_dockerhub(
#     "bewithmeallmylife/mini-gpt4-runtime-cuda-10.2:1.0.0", force_build=True,
#     setup_dockerfile_commands=['RUN apt-get update \
#  && apt-get install -y sudo', "RUN adduser --disabled-password --gecos '' docker",
#                                'RUN adduser docker sudo',
#                                "RUN echo '%sudo ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers",
#                                'USER docker', 'RUN sudo apt-get update ']).
#                   pip_install("cython"))

# stub = modal.Stub("demo2_please_work", image=modal.Image.from_dockerhub(
#     "bewithmeallmylife/mini-gpt4-runtime-cuda-10.2:1.0.0",
#     setup_dockerfile_commands=['RUN echo "conda activate mini-gpt4" > ~/.bashrc',
#                                'SHELL ["/bin/bash", "-c"]',
#                                'ENTRYPOINT["D:\\OpenAdapt\\MiniGPTFour\\entrypoint.sh"]']))

# stub = modal.Stub("demo2_please_work", image=modal.Image.from_dockerfile("Dockerfile"), mounts=[
#     modal.Mount.from_local_dir("D:", remote_path="/projects")])

# stub = modal.Stub("demo2_please_work", mounts=[
#     modal.Mount.from_local_dir("D:", remote_path="/projects")], image=modal.Image.conda()
#                   .run_commands(
#     'conda env create -f /projects/OpenAdapt/MiniGPTFour/environment.yml',
#     'conda activate minigpt4'))

# stub = modal.Stub("demo2_please_work", mounts=[
#     modal.Mount.from_local_dir("D:", remote_path="/projects")], image=modal.Image.conda()
#                   .conda_update_from_environment("environment.yml")
#                   .run_commands('conda install -c conda-forge pycocotools'))

stub = modal.Stub("demo_modal", image=modal.Image.from_dockerhub(
    "bewithmeallmylife/mini-gpt4-runtime-cuda-10.2:1.0.0")
                  .conda_update_from_environment("environment.yml"))


if stub.is_inside():
    import numpy as np
    import torch
    import torch.backends.cudnn as cudnn
    import gradio as gr
    import argparse
    import random


@stub.function(gpu="A10G")
def parse_args():
    parser = argparse.ArgumentParser(description="Demo")
    parser.add_argument("--cfg-path", default="eval_configs/minigpt4_eval.yaml",
                        help="path to configuration file.")
    parser.add_argument("--gpu-id", type=int, default=0, help="specify the gpu to load the model.")
    parser.add_argument(
        "--options",
        nargs="+",
        help="override some settings in the used config, the key-value pair "
             "in xxx=yyy format will be merged into config file (deprecate), "
             "change to --cfg-options instead.",
    )
    args = parser.parse_args()
    return args


@stub.function(gpu="A10G")
def setup_seeds(config):
    seed = config.run_cfg.seed + get_rank()

    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)

    cudnn.benchmark = False
    cudnn.deterministic = True


# ========================================
#             Model Initialization
# ========================================
@stub.function(gpu="A10G")
def model_initialization():
    print('Initializing Chat')
    # parse_args will use default values
    args = parse_args.call()
    cfg = Config(args)

    model_config = cfg.model_cfg
    model_config.device_8bit = args.gpu_id
    model_cls = registry.get_model_class(model_config.arch)
    model = model_cls.from_config(model_config).to('cuda:{}'.format(args.gpu_id))

    vis_processor_cfg = cfg.datasets_cfg.cc_sbu_align.vis_processor.train
    vis_processor = registry.get_processor_class(vis_processor_cfg.name).from_config(
        vis_processor_cfg)
    chat = Chat(model, vis_processor, device='cuda:{}'.format(args.gpu_id))
    print('Initialization Finished')
    return chat


# ========================================
#             Gradio Setting
# ========================================

@stub.function(gpu="A10G")
def gradio_reset(chat_state, img_list):
    if chat_state is not None:
        chat_state.messages = []
    if img_list is not None:
        img_list = []
    return None, gr.update(value=None, interactive=True), gr.update(
        placeholder='Please upload your image first', interactive=False), gr.update(
        value="Upload & Start Chat", interactive=True), chat_state, img_list


@stub.function(gpu="A10G")
def upload_img(gr_img, text_input, chat_state):
    if gr_img is None:
        return None, None, gr.update(interactive=True), chat_state, None
    chat_state = CONV_VISION.copy()
    img_list = []
    llm_message = chat.upload_img(gr_img, chat_state, img_list)
    return gr.update(interactive=False), gr.update(interactive=True,
                                                   placeholder='Type and press Enter'), gr.update(
        value="Start Chatting", interactive=False), chat_state, img_list


@stub.function(gpu="A10G")
def gradio_ask(user_message, chatbot, chat_state):
    if len(user_message) == 0:
        return gr.update(interactive=True,
                         placeholder='Input should not be empty!'), chatbot, chat_state
    chat.ask(user_message, chat_state)
    chatbot = chatbot + [[user_message, None]]
    return '', chatbot, chat_state


@stub.function(gpu="A10G")
def gradio_answer(chatbot, chat_state, img_list, num_beams, temperature):
    llm_message = chat.answer(conv=chat_state,
                              img_list=img_list,
                              num_beams=num_beams,
                              temperature=temperature,
                              max_new_tokens=300,
                              max_length=2000)[0]
    chatbot[-1][1] = llm_message
    return chatbot, chat_state, img_list



@stub.local_entrypoint()
def main():
    chat = model_initialization.call()

    title = """<h1 align="center">Demo of MiniGPT-4</h1>"""
    description = """<h3>This is the demo of MiniGPT-4. Upload your images and start chatting!</h3>"""
    article = """<p><a href='https://minigpt-4.github.io'><img src='https://img.shields.io/badge/Project-Page-Green'></a></p><p><a href='https://github.com/Vision-CAIR/MiniGPT-4'><img src='https://img.shields.io/badge/Github-Code-blue'></a></p><p><a href='https://raw.githubusercontent.com/Vision-CAIR/MiniGPT-4/main/MiniGPT_4.pdf'><img src='https://img.shields.io/badge/Paper-PDF-red'></a></p>
    """

    # TODO show examples below

    with gr.Blocks() as demo:
        gr.Markdown(title)
        gr.Markdown(description)
        gr.Markdown(article)

        with gr.Row():
            with gr.Column(scale=0.5):
                image = gr.Image(type="pil")
                upload_button = gr.Button(value="Upload & Start Chat", interactive=True,
                                          variant="primary")
                clear = gr.Button("Restart")

                num_beams = gr.Slider(
                    minimum=1,
                    maximum=10,
                    value=1,
                    step=1,
                    interactive=True,
                    label="beam search numbers)",
                )

                temperature = gr.Slider(
                    minimum=0.1,
                    maximum=2.0,
                    value=1.0,
                    step=0.1,
                    interactive=True,
                    label="Temperature",
                )

            with gr.Column():
                chat_state = gr.State()
                img_list = gr.State()
                chatbot = gr.Chatbot(label='MiniGPT-4')
                text_input = gr.Textbox(label='User', placeholder='Please upload your image first',
                                        interactive=False)

        upload_button.click(upload_img.call, [image, text_input, chat_state],
                            [image, text_input, upload_button, chat_state, img_list])

        text_input.submit(gradio_ask.call, [text_input, chatbot, chat_state],
                          [text_input, chatbot, chat_state]).then(
            gradio_answer.call, [chatbot, chat_state, img_list, num_beams, temperature],
            [chatbot, chat_state, img_list]
        )
        clear.click(gradio_reset.call, [chat_state, img_list],
                    [chatbot, image, text_input, upload_button, chat_state, img_list], queue=False)

    demo.launch(share=True, enable_queue=True)
