---
description: Script for saving the dataset entries with a given dataset_id locally.
---

# generate\_dataset.py

Usage:

```
$ python openadapt/scripts/generate_dataset.py <dataset_id>
```

Once you are done and would like to save the dataset locally for finetuning, run the above command and the dataset will be saved at openadapt/vision/vision\_dataset as a JSON file containing the window states and a directory of images. The images are named after their id and the window states use this id to preserve the relationship.
