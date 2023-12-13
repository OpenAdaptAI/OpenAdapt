---
description: This script executes the BLIP2 model within a modal container.
---

# blip2.py

The user can interact with the [BLIP2 ](https://huggingface.co/docs/transformers/main/model\_doc/blip-2)model by asking the questions from openadapt/vision/questions.txt about the images in openadapt/vision/images.&#x20;

To run this script, make sure you have created a modal token by running the following:

```
$ modal token new
```

Usage:

```
$ modal run openadapt/vision/blip2.py
```

Note: Since this script is being run in a container with large model weights, the first time it is run, it will take a long time, but subsequent runs should be relatively fast.
