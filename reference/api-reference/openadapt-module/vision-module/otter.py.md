---
description: This script executes the Otter model within a modal container.
---

# otter.py

The user can interact with the [Otter ](https://github.com/Luodian/Otter)model by asking the questions from openadapt/vision/questions.txt about the images in openadapt/vision/images.

There are two available checkpoints, 9B and MPT7B. To change which one is being used, modify the download\_model function to use either BASE\_MODEL\_9B or BASE\_MODEL\_MPT7B. To run this script, make sure you have created a modal token by running the following:

```
$ modal token new
```

Usage:

```
$ modal run openadapt/vision/otter.py
```

Note: Since this script is being run in a container with large model weights, the first time it is run, it will take a long time, but subsequent runs should be relatively fast.
