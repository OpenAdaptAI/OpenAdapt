---
description: Script for tagging a screenshot and window event as a dataset entry.
---

# tag\_dataset.py

Usage:

```
$ python openadapt/scripts/tag_dataset.py <window_event_timestamp>
<screenshot_timestamp> [<dataset_id>]
```

To create a dataset and add to it, make a recording, then visualize it and find the window event timestamp and screenshot timestamp you would like to add to the dataset. Then, run the above command.

* If you want to create a new dataset and insert the 1st entry, leave dataset\_id blank.&#x20;
* If you want to add to an existing dataset, add the dataset\_id.
* Currently, the dataset created [here ](https://drive.google.com/drive/folders/1g5cMMDa-rsVP2BV3QlgyS2GdDguHQ3EL?usp=sharing)is using dataset\_id=1.
