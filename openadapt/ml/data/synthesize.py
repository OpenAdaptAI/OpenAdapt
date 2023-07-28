from openadapt.crud import (
    get_latest_recording,
    get_window_events
)

# TODO: temp
questions = ["What is the position of the button that closes the window? "
             "Is it on the right, left, top, or bottom side of the screen?"]


def synthesize():
    """
    TODO: fix
    - get latest recording
    - get all window events states
    - get associated screenshot
    - filter out unneeded info (meta/data)
    - split
    - create dataset
    """
    recording = get_latest_recording()
    window_events = get_window_events(recording)
    image_to_state = {}
    for event in window_events:
        state = event.state
        # take last action event's screenshot
        ss = event.action_events[-1].screenshot
        image_to_state[ss] = state


def get_event_screenshots():
    recording = get_latest_recording()
    window_events = get_window_events(recording)
    screenshots = []
    for event in window_events:
        ss = event.action_events[-1].screenshot
        screenshots.append(ss)
    return screenshots


def ask_models():
    import subprocess
    subprocess.run(["modal", "run", "openadapt/minigpt4.py"])


# TODO: generate questions to ask the model on the screenshots and put into a spreadsheet
def write_to_spreadsheet(answers, images):
    import openpyxl

    # Load the workbook
    workbook = openpyxl.Workbook()
    sheet = workbook.active

    # Write data to the sheet
    data = []
    for i in range(0, len(answers)):
        data.append([questions[0], answers[i]])
        # TODO: images

    for row in data:
        sheet.append(row)

    # Save the workbook
    workbook.save("example.xlsx")


if __name__ == "__main__":
    write_to_spreadsheet(["Hi"], [])
