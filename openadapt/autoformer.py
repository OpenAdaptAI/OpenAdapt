from transformers import AutoformerForPrediction
import torch

from openadapt.crud import (
    get_latest_recording,
)
from openadapt.events import (
    get_events,
)

from torch.nn.utils.rnn import pad_sequence
from huggingface_hub import hf_hub_download

PROCESS_EVENTS = False


def event_to_number_timestamp(action_event):
    # negative number for clicks = groups of 2
    # return as list
    if action_event.name == "click":
        return [- action_event.mouse_x, - action_event.mouse_y], [action_event.timestamp,
                                                                action_event.timestamp]
    elif action_event.name == "press":
        # TODO: special chars? normal can use vk
        pass
    return [], []


def events_to_numbers(action_events):
    events_numbers = []
    timestamps = []
    for action_event in action_events:
        nums, timestamp = event_to_number_timestamp(action_event)
        events_numbers.extend(nums)
        timestamps.extend(timestamp)

    return events_numbers, timestamps


if __name__ == "__main__":
    # TODO: put everything in a function
    recording = get_latest_recording()
    action_events = get_events(recording, process=PROCESS_EVENTS)
    max_timestamp = action_events[-1].timestamp
    # TODO: maybe filter out release and then add back in later?

    # Prepare inputs for prediction
    events_as_numbers, timestamps = events_to_numbers(action_events)

    # Inference
    # Load the trained model for prediction
    model = AutoformerForPrediction.from_pretrained("kashif/autoformer-electricity-hourly")
    config = model.config
    config.lags_sequence = [*range(1, 47)]
    # config.scaling = False
    config.num_time_features = 1
    context_length = config.context_length + max(config.lags_sequence)
    prediction_length = config.prediction_length
    batch_size = len(events_as_numbers) // context_length

    # Convert lists to torch.FloatTensor
    # Calculate the number of elements for the last batch
    last_batch_elements = len(events_as_numbers) % context_length

    # Split the data into full batches and last batch
    full_batches = [torch.tensor(events_as_numbers[i:i + context_length]) for i in
                    range(0, len(events_as_numbers) - last_batch_elements, context_length)]
    last_batch = torch.tensor(events_as_numbers[-last_batch_elements:]) if last_batch_elements > 0 else None

    # Pad the last batch with zeros to match the sequence length
    if last_batch is not None:
        last_batch = torch.cat([last_batch, torch.zeros(context_length - last_batch_elements)])

    # Combine the full batches and the last batch into a single tensor
    combined_past_values_tensor = pad_sequence(full_batches + [last_batch], batch_first=True)

    # Print the combined tensor
    print("Combined data shape:", combined_past_values_tensor.shape)
    print(combined_past_values_tensor)

    # Create the mask tensor
    mask = (combined_past_values_tensor != 0).to(torch.bool)

    # Calculate the number of elements for the last batch
    last_batch_elements = len(timestamps) % context_length

    # Split the timestamps into full batches and last batch
    full_batches = [torch.tensor(timestamps[i:i + context_length]).unsqueeze(-1) for i in
                    range(0, len(timestamps) - last_batch_elements, context_length)]
    last_batch = torch.tensor(timestamps[-last_batch_elements:]).unsqueeze(
        -1) if last_batch_elements > 0 else None

    # Pad the last batch with zeros to match the sequence length
    if last_batch is not None:
        last_batch = torch.cat(
            [last_batch, torch.zeros(context_length - last_batch_elements, 1)], dim=0)

    # Combine the full batches and the last batch into a single tensor
    combined_timestamps_tensor = pad_sequence(full_batches + [last_batch], batch_first=True)
    shape = combined_timestamps_tensor.shape
    num_elements = combined_timestamps_tensor.numel()
    values = torch.arange(1, num_elements + 1)
    values = values.reshape(shape)
    combined_timestamps_tensor = values

    # Print the combined tensor
    print("Combined timestamps shape:", combined_timestamps_tensor.shape)

    # Increment the maximum timestamp by 1 for each prediction step
    future_timestamps = torch.arange(max_timestamp + 1, max_timestamp + 1 + prediction_length)

    # Create future_time_features tensor by repeating and reshaping the timestamps
    future_time_features = future_timestamps.unsqueeze(0).repeat(combined_timestamps_tensor.shape[0], 1).unsqueeze(-1)

    # Expand dimensions to match the desired shape
    future_time_features_tensor = future_time_features.expand(combined_timestamps_tensor.shape[0], prediction_length, 1)
    shape = future_time_features_tensor.shape
    values = torch.arange(num_elements + 1, num_elements + prediction_length + 1)
    values = values.reshape(shape)
    future_time_features_tensor = values

    print(future_time_features_tensor.shape)

    # Generate predictions
    # import ipdb; ipdb.set_trace()

    outputs = model.generate(
        past_values=combined_past_values_tensor,
        past_time_features=combined_timestamps_tensor,
        future_time_features=future_time_features_tensor,
        past_observed_mask=mask
    )

    mean_prediction = outputs.sequences.mean(dim=1)

    # Use the mean prediction for further analysis or tasks
    print(mean_prediction)
    print(mean_prediction.shape)

    # TODO: turn into ActionEvents
    # TODO: make sure numbers are ints
