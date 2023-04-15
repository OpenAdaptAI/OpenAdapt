#from puterbot. import get_latest_recording, get_screenshots
from puterbot.crud import get_latest_recording, get_screenshots
from puterbot.gui_process_automation import generate_input_event

def test_generate_input_event():
    # Retrieve instances of new_screenshot and recording from the database
    recording = get_latest_recording()
    # Get the first screenshot from the recording
    new_screenshot = get_screenshots(recording, True)[0] 
    

    generated_input_event = generate_input_event(new_screenshot, recording)

    # Perform any assertions or checks on the generated input event
    print(generated_input_event.mouse_pressed)
    print(generate_input_event.mouse_button_name)
    print(generate_input_event.mouse_x)
    print(generate_input_event.mouse_y)
    print(generate_input_event.mouse_dx)
    print(generate_input_event.mouse_dy)
    print(generate_input_event.key_name)
    print(generate_input_event.key_char)

if __name__ == "__main__":
    test_generate_input_event()