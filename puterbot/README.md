## Setup

```
git clone https://github.com/MLDSAI/puterbot.git
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
