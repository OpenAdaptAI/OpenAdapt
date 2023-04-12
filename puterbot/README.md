## Setup

```
cd puterbot
python3.10 -m venv .venv
source .venv/bin/activate
pip install wheel
pip install -r requirements.txt
pip install -e .
alembic upgrade head
```

### Run tests
```
pytest
```

## Running

Record:
```
python puterbot/record.py
```

Visualize:
```
python puterbot/visualize.py
```

## Troubleshooting

Apple Silicon:

```
$ python puterbot/record.py
...
This process is not trusted! Input event monitoring will not be possible until it is added to accessibility clients.
```

Solution:
https://stackoverflow.com/a/69673312

```
Settings -> Security & Privacy
Click on the Privacy tab
Scroll and click on the Accessibility Row
Click the +
Navigate to /System/Applications/Utilities/ or wherever the Terminal.app is installed
Click okay.
```

## Submitting an Issue

Please submit any issues to https://github.com/MLDSAI/puterbot/issues with the
following information:

- Problem description (include any relevant console output and/or screenshots)
- Steps to reproduce (required in order for others to help you)

## Developing

### Generate migration (after editing a model)

```
alembic revision --autogenerate -m "<msg>"
```


## Building (Windows only)

install ms c++ build tools
https://visualstudio.microsoft.com/visual-cpp-build-tools/

install python 3.9.6
https://www.python.org/ftp/python/3.9.6/python-3.9.6-amd64.exe

trust pip hosts on proxy restricted windows machine:
```
pip config set global.trusted-host "pypi.org files.pythonhosted.org pypi.python.org" --trusted-host=pypi.python.org --trusted-host=pypi.org --trusted-host=files.pythonhosted.org
```

install tesseract
```
https://github.om/UB-Mannheim/tesseract/wiki/
```

add to path in the current shell
```
set PATH=%PATH%;%USERPROFILE%\AppData\Local\Tesseract-OCR
```

do it permanently
```
setx PATH "%PATH%;%USERPROFILE%\AppData\Local\Tesseract-OCR"
```

build
```
pyinstaller --noconfirm --add-data "resources\*.png;resources" audit.py
```

## Script dependencies

install easyocr
```
pip install --user easyocr -i https://pypi.python.org/simple/
```

install kerasocr
```
pip install --user keras-ocr -i https://pypi.python.org/simple
python -m pip install --user tensorflow -i https://pypi.python.org/simple --trusted-host=pypi.python.org --trusted-host=pypi.org --trusted-host=files.pythonhosted.org
```

install paddleocr
```
python -m pip install --user paddlepaddle
````
