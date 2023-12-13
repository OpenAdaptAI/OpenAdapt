---
description: This script executes the MiniGPT-4 model within a modal container.
---

# minigpt4.py

The user can interact with the [MiniGPT-4](https://github.com/Vision-CAIR/MiniGPT-4) model by asking the questions from openadapt/vision/questions.txt about the images in openadapt/vision/images.

To run this script, make sure you have created a modal token by running the following:

```
$ modal token new
```

Usage:

```
$ modal run openadapt/vision/minigpt4.py
```

Note: Since this script is being run in a container with large model weights, the first time it is run, it will take a long time, but subsequent runs should be relatively fast.
