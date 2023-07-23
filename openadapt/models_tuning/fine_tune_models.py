from copy import deepcopy
from pprint import pformat
import math

from loguru import logger
import fire
import transformers as tf

from openadapt import crud, models, utils

LOG_LEVEL = "INFO"
MAX_SCREEN_SIZE = (1920, 1080)
MAX_INPUT_SIZE = 1024
MAX_TOKENS = 1024


@logger.catch
def tune_model(model_name: str = "gpt2"):
    utils.configure_logging(logger, LOG_LEVEL)

    tokenizer = tf.AutoTokenizer.from_pretrained(model_name)
    model = tf.AutoModelForCausalLM.from_pretrained(model_name)

    recording = crud.get_latest_recording()
    dataset = convert_recording_to_dataset(recording)

    prediction_dataset = _generate_prediction_dataset(model, tokenizer, dataset)

    ave_similarity_score = calculate_similarity_per_dataset(dataset, prediction_dataset)
    logger.info(f"Average similarity score is {ave_similarity_score=}")


def calculate_similarity_per_dataset(dataset, prediction):
    total_score = 0
    # loop through dataset and prediction pair wise
    for entry, prediction in zip(dataset[20::], prediction[20::]):
        reference_action_dicts = entry["reference_action_dicts"]
        prediction_action_dicts = prediction["active_action_dicts"]
        score_per_action = 0
        for ref_action, prediction_action in zip(
            reference_action_dicts, prediction_action_dicts
        ):
            score_per_action += _calculate_similarity_per_action(
                ref_action, prediction_action
            )
        average_score = score_per_action / len(reference_action_dicts)
        total_score += average_score
    average_score = total_score / len(dataset)
    return average_score


def convert_recording_to_dataset(recording):
    """
    Convert a recording to a dataset.
    """
    dataset = []
    for action_event in recording.processed_action_events:
        reference_window = action_event.window_event
        reference_window_dict = deepcopy(
            {
                key: val
                for key, val in utils.row2dict(reference_window, follow=False).items()
                if val is not None
                and not key.endswith("timestamp")
                and not key.endswith("id")
                # and not isinstance(getattr(models.WindowEvent, key), property)
            }
        )
        if action_event.children:
            reference_action_dicts = [
                deepcopy(
                    {
                        key: val
                        for key, val in utils.row2dict(child, follow=False).items()
                        if val is not None
                        and not key.endswith("timestamp")
                        and not key.endswith("id")
                        and not isinstance(getattr(models.ActionEvent, key), property)
                    }
                )
                for child in action_event.children
            ]
        else:
            reference_action_dicts = [
                deepcopy(
                    {
                        key: val
                        for key, val in utils.row2dict(
                            action_event, follow=False
                        ).items()
                        if val is not None
                        and not key.endswith("timestamp")
                        and not key.endswith("id")
                        # and not isinstance(getattr(models.ActionEvent, key), property)
                    }
                )
            ]
        active_window_dict = reference_window_dict

        prompt = {
            "reference_window_dict": reference_window_dict,
            "reference_action_dicts": reference_action_dicts,
            "active_window_dict": active_window_dict,
        }
        dataset.append(prompt)
    return dataset


def _euclidean_distance(point1, point2):
    if len(point1) != 2 or len(point2) != 2:
        raise ValueError("Both points must be 2D coordinates (x, y)")

    x1, y1 = point1
    x2, y2 = point2
    distance = math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)
    return distance


def _calculate_similarity_per_action(ref_action, prediction_action):
    # verify if the 2 actions contains the same keys
    is_same_action_type = ref_action.get("name") == prediction_action.get("name")
    if not is_same_action_type:
        return 0
    if ref_action.get("name") in ("press", "release"):
        is_same_key = ref_action.get("canonical_key_char") == prediction_action.get(
            "canonical_key_char"
        )
        if not is_same_key:
            return 0
        return 1
    else:
        # calculate euclidean distance between the mouse positions
        ref_mouse_pos = (ref_action.get("mouse_x"), ref_action.get("mouse_y"))
        prediction_mouse_pos = (
            prediction_action.get("mouse_x"),
            prediction_action.get("mouse_y"),
        )
        distance = _euclidean_distance(ref_mouse_pos, prediction_mouse_pos)
        # normalize the distance between 0 and 1
        normalized_distance = distance / MAX_SCREEN_SIZE[0]
        # the smaller the distance, the better the score
        return 1 - normalized_distance


def _generate_prediction_dataset(model, tokenizer, dataset):
    """
    Generate a prediction dataset based on the given dataset.
    """
    prediction_dataset = []
    count = 0
    for entry in dataset:
        if count > 10:
            return prediction_dataset
        reference_window_dict = entry["reference_window_dict"]
        reference_action_dicts = entry["reference_action_dicts"]
        active_window_dict = entry["active_window_dict"]
        reference_window_dict["state"].pop("data")

        prompt = (
            f"{reference_window_dict=}\n{reference_action_dicts=}\n{active_window_dict=}\nProvide"
            " valid Python3 code containing the action dicts by completing the"
            " following, and nothing else:\nactive_action_dicts="
        )
        if len(prompt) > MAX_INPUT_SIZE:
            logger.warning(f"Truncating from {len(prompt) =} to {MAX_INPUT_SIZE=}")
            prompt = prompt[-MAX_INPUT_SIZE:]
            logger.warning(f"Truncated {len(prompt)=}")
        input_tokens = tokenizer(prompt, return_tensors="pt")
        pad_token_id = tokenizer.eos_token_id
        attention_mask = input_tokens["attention_mask"]
        output_tokens = model.generate(
            input_ids=input_tokens["input_ids"],
            attention_mask=attention_mask,
            pad_token_id=pad_token_id,
            max_length=1000,
            num_return_sequences=1,
        )
        N = input_tokens["input_ids"].shape[-1]
        completion = tokenizer.decode(
            output_tokens[:, N:][0],
            clean_up_tokenization_spaces=True,
        )
        active_action_dicts = get_action_dict_from_completion(completion)
        logger.debug(f"active_action_dicts=\n{pformat(active_action_dicts)}")
        prediction_dataset.append({"active_action_dicts": active_action_dicts})
        print(active_action_dicts)
        count += 1

    return prediction_dataset


def get_action_dict_from_completion(completion):
    try:
        action = eval(completion)
    except Exception as exc:
        logger.warning(f"{exc=}")
    else:
        return action


# entry point
def start():
    fire.Fire(tune_model)


if __name__ == "__main__":
    fire.Fire(tune_model)
