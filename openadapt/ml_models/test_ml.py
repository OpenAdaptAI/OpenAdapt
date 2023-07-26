from openadapt.crud import get_action_events, get_latest_recording, get_window_events
from openadapt import utils 
from openadapt import models 
from copy import deepcopy 
from loguru import logger 


def grab_recording():
    recording = get_latest_recording()
    test_acts = get_action_events(recording)
    # you can have a window that has no action...what then 



    #logger.debug(f"{test_acts= }")


    # we need some consistent way of distilling action and window events such that
    # the information loss is minimal..ideally wanna get this right the first try and avoid
    # testing too much...

        #logger.debug(f"{i}th win {test_acts[i].window_event}\n")
    #logger.debug(f"{test_win=}")

    for i in range(len(test_acts)):
        logger.debug(f"{test_acts[i]=}\n")
        logger.debug(f"{sanitize(test_acts[i])[0]=}\n")
        logger.debug(f"{sanitize(test_acts[i])[1]=}\n")

        for key in sanitize(test_acts[i])[1]:
            logger.debug(f"{key=}")

        #logger.debug(f"{sanitize(test_acts[i])[1]['state']=}")

        for key in sanitize(test_acts[i])[1]['state']:
            logger.debug(f"{key=}")

        logger.debug(f"{sanitize(test_acts[i])[1]['state']['meta']=}")
        logger.debug(f"{sanitize(test_acts[i])[1]['state']['data']=}")

        break

    # create action window state pairings

def sanitize(reference_action):

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

if __name__ == "__main__":
    grab_recording()