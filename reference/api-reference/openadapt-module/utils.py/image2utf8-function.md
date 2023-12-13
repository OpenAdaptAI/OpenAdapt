---
description: The function converts an image to a UTF-8 encoded base64 string.
---

# image2utf8 (function)

## Syntax

```python
def image2utf8(image: PIL.Image) -> str:
```

## Parameters

`image`

The `image` parameter is expected to be a PIL (Python Imaging Library) image object. It will be converted to RGB format and then encoded as a base64 string with a JPEG format prefix. The resulting string will be returned as UTF-8 encoded text.

## Return value

A string representation of the input image in UTF-8 format, encoded in base64. The string includes a prefix indicating that the image is in JPEG format.
