from openadapt import models
from openadapt.crud import get_action_events, get_window_events
import fire
import openai

class EventType:
    WINDOW = "WINDOW"
    ACTION = "ACTION"

def main(recording: models.Recording):
    # sanitize, convert to json, feed to model
    act_dicts = get_action_events(recording)
    win_dicts = get_window_events(recording)
    
    sanitized_act_dict = sanitize_dicts("ACTION", act_dicts)
    sanitized_win_dict = sanitize_dicts("WINDOW", win_dicts)
    pass

def sanitize_dicts(event_type: EventType, event):
    pass

if __name__ == "__main__":
    fire.Fire(main)