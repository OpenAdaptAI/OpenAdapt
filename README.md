# PuterBot: GUI Process Automation with Transformers

Welcome to PuterBot: GUI Process Automation with Transformers! We are working with a dataset of user input events, screenshots, and window events. Our task is to generate the appropriate InputEvent(s) based on the previously recorded InputEvents and associated Screenshots, such that the task in the recording is accomplished, while accounting for differences in screen resolution, window size, application behavior, etc.

## Problem Statement

Given a new Screenshot, we want to generate the appropriate InputEvent(s) based on the previously recorded InputEvents, where each Screenshot is taken immediately before its associated InputEvent. We need to account for differences in screen resolution, window size, application behavior, etc. InputEvents contain raw mouse and keyboard data which have been aggregated to remove unnecessary events.

## Dataset

The dataset consists of the following entities: 
1. `Recording`: Contains information about the screen dimensions, platform, and other metadata. 
2. `InputEvent`: Represents a user input event such as a mouse click or key press. Each InputEvent has an associated Screenshot taken immediately before the event. 
3. `Screenshot`: Contains the PNG data of a screenshot taken during the recording. 
4. `WindowEvent`: Represents a window event such as a change in window title, position, or size.

You can assume that you have access to the following functions: 
- `get_recording()`: Gets the latest recording. 
- `get_events(recording)`: Returns a list of `InputEvent` objects for the given recording.

## Contributing 

1. Fork this repository and clone it to your local machine. 
2. Get puterbot up and running by following the instructions under [Setup and Run puterbot](#setup-and-run-puterbot) below.
3. Implement a Python function `generate_input_event(new_screenshot, recording)`, where:
- `new_screenshot`: A `Screenshot` object representing the new screenshot. 
- `recording`: A `Recording` whose `.screenshots` property is a list of `InputEvent` objects from a previous recording, with each InputEvent having an associated Screenshot.
- This function should return a new `InputEvent` object that can be used to replay the recording, taking into account differences in screen resolution, window size, and/or application behavior.
4. Write unit tests for your implementation.

See https://github.com/MLDSAI/puterbot/issues for ideas on where to start.

Instead of Segment Anything, you can also try converting screenshots to text via ASCII art, e.g. https://github.com/LeandroBarone/python-ascii_magic.

### Evaluation Criteria

Your submission will be evaluated based on the following criteria: 

1. **Functionality** : Your implementation should correctly generate the new `InputEvent` objects that can be replayed in order to accomplish the task in the original recording.

2. **Code Quality** : Your code should be well-structured, clean, and easy to understand. 

3. **Scalability** : Your solution should be efficient and scale well with large datasets. 

4. **Testing** : Your tests should cover various edge cases and scenarios to ensure the correctness of your implementation.

### Submission

1. Commit your changes to your forked repository.

2. Create a pull request to the original repository with your changes.

3. In your pull request, include a brief summary of your approach, any assumptions you made, and how you integrated external libraries.

4. *Bonus*: interacting with ChatGPT and/or other language transformer models in order to generate code and/or evaluate design decisions is encouraged. If you choose to do so, please include the full transcript.

# Setup and Run puterbot

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

## Developing

### Generate migration (after editing a model)

```
alembic revision --autogenerate -m "<msg>"
```

# Submitting an Issue

Please submit any issues to https://github.com/MLDSAI/puterbot/issues with the
following information:

- Problem description (please include any relevant console output and/or screenshots)
- Steps to reproduce (please help others to help you!)
