from openadapt import models
from openadapt.crud import get_action_events, get_window_events
import fire
import openai
from copy import deepcopy
import utils

class EventType:
    WINDOW = "WINDOW"
    ACTION = "ACTION"

def gpt_davinci_finetune(recording: models.Recording):

   
    # prompt is ref_win + ref_Act, 
    # completion is target_win + target_act

    for ref_win, ref_act, target_win, target_act in get_finetune_pairs(recording):
        pass

    # TODO: create JSON in the format {prompt: ref_win, ref_act completion: target_win, target_act} as per GPT finetune docs.



def get_finetune_pairs(recording: models.Recording):

    processed_acx = get_action_events(recording)
    for prev_action, next_action in zip(processed_acx, processed_acx[1:]):
        
        prev_window = prev_action[1]
        next_window = next_action[1]

        ref_win = prev_window
        ref_act = prev_action

        target_win = next_window
        target_act = next_action

        yield ref_win, ref_act, target_win, target_act


def sanitize_dicts(reference_action):
    reference_window = reference_action.window_event

    reference_window_dict = deepcopy({
        key: val
        for key, val in utils.row2dict(reference_window, follow=False).items()
        if val is not None
        and not key.endswith("timestamp")
        and not key.endswith("id")
        #and not isinstance(getattr(models.WindowEvent, key), property)
    })

    if reference_action.children:
        reference_action_dicts = [
            deepcopy({
                key: val
                for key, val in utils.row2dict(child, follow=False).items()
                if val is not None
                and not key.endswith("timestamp")
                and not key.endswith("id")
                and not isinstance(getattr(models.ActionEvent, key), property)
            })
            for child in reference_action.children
        ]
    else:
        reference_action_dicts = [
            deepcopy({
                key: val
                for key, val in utils.row2dict(reference_action, follow=False).items()
                if val is not None
                and not key.endswith("timestamp")
                and not key.endswith("id")
                #and not isinstance(getattr(models.ActionEvent, key), property)
            })
        ]
    
    return reference_action_dicts, reference_window_dict