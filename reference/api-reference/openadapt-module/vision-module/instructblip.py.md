---
description: This script executes the InstructBLIP model within a modal container.
---

# instructblip.py

The user can interact with the [InstructBLIP ](https://huggingface.co/docs/transformers/main/model\_doc/instructblip)model by asking the questions from openadapt/vision/questions.txt about the images in openadapt/vision/images.

To run this script, make sure you have created a modal token by running the following:

```
$ modal token new
```

Usage:

```
$ modal run openadapt/vision/instructblip.py
```

Note: Since this script is being run in a container with large model weights, the first time it is run, it will take a long time, but subsequent runs should be relatively fast.
