# vision (module)



## 1 Introduction

The purpose of this document is to define the requirements and design of the vision system in the OpenAdapt process automation library.

## 2 Background

Motivation:

Want to incorporate vision into OpenAdapt and be able to ask the model questions about the screenshots, as currently we are just relying on WindowEvents

Literature review:

Many different vision models are popping up recently, an evaluation of some of these can be found [here](https://github.com/BradyFU/Awesome-Multimodal-Large-Language-Models/tree/Evaluation)

## 3 Goals

* Ultimately the goal is to create a ReplayStrategy with vision capabilities so that we can feed it the screenshots in a recording, rather than relying on WindowEvents

## 4 Design

### 4.1 System Overview

![](https://lh3.googleusercontent.com/kGMTfQQPQW0M3ITqAm79SD4\_Ds-71DCNk3oh5pqCfFmtcUs57t6q2dGfbK0a\_-lDE2w8FjhvSG\_g8NmnXMRKjjxnk7GDKwIGU3mDYCVSOkoRhEKxlOpJjk-15xH\_0UhT1TQiKclxjsiJiA1ICerp6A8)

[Original file](https://drive.google.com/file/d/1x8Kb2oxMzsO9MXskukZtr8if9oE9dMjk/view?usp=drive\_link)

### 4.2 Running the vision models

* As all of the vision models require a large amount of GPU memory, we started out by using the modal library to run them in a cloud container. All of the models can be run in modal following a similar structure:

1. Create an image: install all requirements, download the weights for the model (so they’re saved in the container image and don’t have to be redownloaded), and any other preliminary work needed to setup the model (usually just following the instructions on the GitHub repo). Non-huggingface models must be git cloned in the container.&#x20;
2. Create a modal class for the model:&#x20;

* Assign it a gpu and a timeout ex. @stub.cls(gpu="A100", timeout=18000)
* use the \_\_enter\_\_ method to set the class’ processor/model/device/any other attributes the class needs&#x20;
* Create a method with the modal @method decorator to generate completions given an image and prompt. For huggingface models, this is really simple - create the inputs to the model from the given image and question, generate the completion using the model’s generate method, then decoding the output. For non-huggingface models, this is a bit more complicated and every repo structures their model differently. Generally, these models usually have a demo file that uses Gradio, so you can follow the same idea without using Gradio.

3. @stub.local\_entrypoint(): open the image(s) using PIL and run the generate method defined above using generate.call to run it in the cloud. The model will be asked all of the questions in OpenAdapt/openadapt/vision/questions.txt for each of the images in OpenAdapt/openadapt/vision/images

\


Currently, we are manually testing each model and inputting the completions in [this spreadsheet](https://docs.google.com/spreadsheets/d/1etgr3LnM\_NrAMwMmFLUttmXmBrJ4Uia9-notBzyXk6c/edit?usp=sharing).

The first time any of the models are run, they will take a long time since all the models need to be downloaded. After that, the models will be saved in the container and will run much faster.

\


### 4.3 Creating and adding to a vision dataset

* Currently this process must be done manually.&#x20;
* To create a dataset and add to it, make a recording, then visualize it and find the window event timestamp and screenshot timestamp you would like to add to the dataset. Then, run \`python openadapt/scripts/tag\_dataset.py \<window\_event\_timestamp> \<screenshot\_timestamp> \[\<dataset\_id>]’.&#x20;
* If you want to create a new dataset and insert the 1st entry, leave dataset\_id blank.&#x20;
* If you want to add to an existing dataset, add the dataset\_id.&#x20;
* Currently, the dataset created [here ](https://drive.google.com/drive/folders/1g5cMMDa-rsVP2BV3QlgyS2GdDguHQ3EL?usp=sharing)is using dataset\_id=1.
* Once you are done and would like to save the dataset locally for finetuning, run ‘python openadapt/scripts/generate\_dataset.py \<dataset\_id>’ and the dataset will be saved at openadapt/ml/data/vision\_dataset as a JSON file containing the window states and a directory of images. The images are named after their id and the window states use this id to preserve the relationship.
* Note: Currently all the dataset images were taken on a Windows computer with 1920 x 1080 resolution.

## 5 Analysis

### 5.1 Performance



Out of the box performance of select multimodal LLMs found from [this evaluation](https://github.com/BradyFU/Awesome-Multimodal-Large-Language-Models/tree/Evaluation)

[https://docs.google.com/spreadsheets/d/1etgr3LnM\_NrAMwMmFLUttmXmBrJ4Uia9-notBzyXk6c/edit?usp=sharing](https://docs.google.com/spreadsheets/d/1etgr3LnM\_NrAMwMmFLUttmXmBrJ4Uia9-notBzyXk6c/edit?usp=sharing)&#x20;

## 6 Future Workhere

* Currently the next step is to finetune the best vision model using the vision dataset [here](https://drive.google.com/drive/folders/1g5cMMDa-rsVP2BV3QlgyS2GdDguHQ3EL?usp=sharing). Based on the testing I’ve done so far, we suspect the best model we will choose to be finetuned will be either MiniGPT-4, Otter, InstructBlip.&#x20;
* For the dataset, we would also like to come up with a way to generate similar data as what has been recorded, but with different parameters for e.g. windows theme settings, window sizes/positions, etc. We would like to create a script (vision/augment.py) to augment the data to automate the process of creating varying screenshots and window states. This would either be done by modifying the screenshots directly, or by replaying the recordings in a way that recreates the same screenshots, but with different windows themes, window sizes, etc.
