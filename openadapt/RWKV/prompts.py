import random
import json

Tasks = {
    0: "making a website about cats",
    1: "playing chess",
    2: "making a website about shoes",
    3: "filling out medical forms",
    4: "parsing and analyzing data from spreadsheets",
    5: "emailing an agent about house requirements",
    6: "filling out a job application",
    7: "signing into and powering on a virtual machine",
    8: "advertising a new restaurant",
    9: "booking a flight",
    10: "posting on social media platforms",
    11: "editing photos for an online portfolio",
    12: "updating a LinkedIn profile",
    13: "posting to a book blog",
    14: "joining a team meeting",
    15: "updating store inventory records",
    16: "performing a virus scan",
    17: "updating lab reports",
    18: "sending an update to the team",
    19: "deleting old emails",
    20: "making a credit card payment",
    21: "testing a new software integration",
    22: "testing a new software deployment", 
    23: "starting a ROS instance",   
    24: "organizing a folder of files",
}

ShortSignals = [
    {
        "id": 0,
        "type": "file",
        "descriptor": "restaurant_menu_data.txt",
        "relevant_task_ids": [8],
    },
    {
        "id": 1,
        "type": "url",
        "descriptor": "https://en.wikipedia.org/wiki/Web_development",
        "relevant_task_ids": [0, 2],
    },
    {"id": 2, "type": "function", "descriptor": "math.sqrt", "relevant_task_ids": []},
    {
        "id": 3,
        "type": "url",
        "descriptor": "https://www.acuweather.com",
        "relevant_task_ids": [],
    },  # 3 is very loosely relevant
    {
        "id": 4,
        "type": "database",
        "descriptor": "footwear.db",
        "relevant_task_ids": [2, 3],
    },
    {
        "id": 5,
        "type": "function",
        "descriptor": "openai.Completion.create",
        "relevant_task_ids": [],
    },
    {
        "id": 6,
        "type": "file",
        "descriptor": "electronic_medical_record_template.xls",
        "relevant_task_ids": [3, 4],
    },  # 4 is loosely relevant
    {
        "id": 7,
        "type": "url",
        "descriptor": "https://www.chess.com",
        "relevant_task_ids": [1],
    },
    {
        "id": 8,
        "type": "database",
        "descriptor": "user_info.db",
        "relevant_task_ids": [3, 6, 7, 14, 20],
    },
    {
        "id": 9,
        "type": "function",
        "descriptor": "pandas.DataFrame",
        "relevant_task_ids": [4],
    },
    {
        "id": 10,
        "type": "file",
        "descriptor": "File_Sorting_Script.py",
        "relevant_task_ids": [24],
    },
    {
        "id": 11,
        "type": "function",
        "descriptor": "sklearn.tree.DecisionTreeClassifier",
        "relevant_task_ids": [],
    },
    {
        "id": 12,
        "type": "url",
        "descriptor": "https://www.skyscanner.com",
        "relevant_task_ids": [9],
    },
    {
        "id": 13,
        "type": "database",
        "descriptor": "social_media_accounts.db",
        "relevant_task_ids": [10, 11],
    },
    {
        "id": 14,
        "type": "url",
        "descriptor": "https://www.linkedin.com",
        "relevant_task_ids": [6, 10, 12],
    },
    {
        "id": 15,
        "type": "database",
        "descriptor": "book_summaries.db",
        "relevant_task_ids": [15],
    },
    {
        "id": 16,
        "type": "url",
        "descriptor": "https://www.zoom.us",
        "relevant_task_ids": [14, 18],
    },
    {
        "id": 17,
        "type": "file",
        "descriptor": "clothing_size_inventory.csv",
        "relevant_task_ids": [15],
    },
    {
        "id": 18,
        "type": "function",
        "descriptor": "openai.Engine.list",
        "relevant_task_ids": [],
    },
    {
        "id": 19,
        "type": "function",
        "descriptor": "antivirus.start_security_scan",
        "relevant_task_ids": [16],
    },
    {
        "id": 20,
        "type": "file",
        "descriptor": "genetherapy_data.csv",
        "relevant_task_ids": [17],
    },
    {
        "id": 21,
        "type": "function",
        "descriptor": "emailLibrary.sendEmail",
        "relevant_task_ids": [5, 18],
    },
    {
        "id": 22,
        "type": "url",
        "descriptor": "https://www.gmail.com",
        "relevant_task_ids": [5, 18, 19],
    },
    {
        "id": 23,
        "type": "file",
        "descriptor": "sales_report.xls",
        "relevant_task_ids": [4, 15],
    }, # 4 is loosely relevant
    {
        "id": 24,
        "type": "function",
        "descriptor": "runIntegrationTest",
        "relevant_task_ids": [21],
    },
    {
        "id": 25,
        "type": "function",
        "descriptor": "runDeploymentTest",
        "relevant_task_ids": [22],
    },
    {   
        "id": 26,
        "type": "file",
        "descriptor": "test_results.txt",
        "relevant_task_ids": [17, 21, 22],
    },
    {
        "id": 27,
        "type": "function",
        "descriptor": "roscore",
        "relevant_task_ids": [23],
    },
]


def get_relevant_signal_ids(task_id):
    return [
        signal["id"]
        for signal in ShortSignals
        if task_id in signal["relevant_task_ids"]
    ]


def get_signal_data_without_task_ids(signal_id):
    return {
        k: v for k, v in ShortSignals[signal_id].items() if k != "relevant_task_ids"
    }


def get_prompt(task_id, signal_ids, rearranged_indices=None):
    task = Tasks[task_id]
    test_signals = []

    for signal_id in signal_ids:
        signal = get_signal_data_without_task_ids(signal_id)
        test_signals.append(signal)

    if rearranged_indices is not None:
        for signal in test_signals:
            signal["id"] = rearranged_indices[signal["id"]]

    prompt = f"""Below is an instruction that describes a task, paired with an input that provides further context. Write a response that appropriately completes the request.
# Instruction:
You are {task}. A list of information signals is provided in JSON format. Please respond with only the ids of the signals that are most relevant to the task formatted as a list.
# Input:
{str(test_signals)}
# Response: 
"""
    return prompt

def back_heavy_randint(max_val):
    """
    Returns a random integer between 0 and max_val, with a bias towards higher numbers.
    
    The bias is achieved by squaring a random float between 0 and 1, then multiplying it by max_val.
    """
    rand_float = random.random()
    squared_float = rand_float ** 0.9
    return round(squared_float * max_val)

def evaluate(for_dataset=False):
    # 1. Pick a random task_id and a random number of random signals
    # 2. Generate a prompt
    # 3. Run the model
    # 4. Get the relevant signals
    # 5. Check if the model's output is correct

    task_id = random.randint(0, len(Tasks) - 1)
    signal_count = back_heavy_randint(15)
    # signal_count = len(ShortSignals)

    # Select signal_count random signals (avoid duplicates)
    # Have signal indexes start at 0 despite pre-existing signal ids
    # Store the signal ids in a list to determine the desired response
    signal_ids = []
    id_swaps = {}
    for i in range(signal_count):
        signal_id = random.randint(0, len(ShortSignals) - 1)
        while signal_id in signal_ids:
            signal_id = random.randint(0, len(ShortSignals) - 1)
        signal_ids.append(signal_id)

        id_swaps[signal_id] = i

    prompt = get_prompt(task_id, signal_ids, rearranged_indices=id_swaps)

    full_relevant_signals = get_relevant_signal_ids(task_id)
    # Remove any signals that are not in the prompt
    relevant_signals = [
        signal_id for signal_id in full_relevant_signals if signal_id in signal_ids
    ]
    # Swap the signal ids to match the prompt
    relevant_signals = [id_swaps[signal_id] for signal_id in relevant_signals]
    relevant_signals.sort()
    
    if not for_dataset:
        print("\n", prompt)
        print("Desired Output: ", relevant_signals)

    if for_dataset:
        return prompt, relevant_signals


def generate_dataset(num_samples=1000):
    # Run evaluate() several times and save the results to a file
    # Each line of jsonl file should be a dictionary as follows: {"text": "{prompt} {target_response}"}
    # Create jsonl file
    # Run evaluate() 1000x times and save the result to the file
    with open("dataset.jsonl", "w") as file:
        for i in range(num_samples):
            value = evaluate(for_dataset=True)
            if i != num_samples-1:
                file.write(json.dumps({"text": value[0] + str(value[1])}) + "\n")
            else:
                file.write(json.dumps({"text": value[0] + str(value[1])}))

    print("\n###### Dataset Generated Successfully ######")

def generate_labelled_dataset():
    # Run evaluate() several times and save the results to a file
    # Each line of jsonl file should be a dictionary as follows: {"prompt": "{prompt}, "output" {target_output}"}
    # Create jsonl file
    # Run evaluate() 1000x times and save the result to the file
    with open("labelled_dataset.jsonl", "w") as file:
        for i in range(500):
            value = evaluate(for_dataset=True)
            if i != 499:
                file.write(json.dumps({"prompt": value[0], "completion": str(value[1])}) + "\n")
            else:
                file.write(json.dumps({"prompt": value[0], "completion": str(value[1])}))


    print("\n###### Labelled Dataset Generated Successfully ######")


if __name__ == "__main__":
    print("Tasks:")
    for task_id, task in Tasks.items():
        print(f"{task_id}: {task}")
    print("\nSignals:")
    for signal in ShortSignals:
        print(f"{signal['id']}: {signal['descriptor']} ({signal['type']})")
    print()
    for task_id in Tasks.keys():
        relevant_signals = get_relevant_signal_ids(task_id)
        print(
            f"Task {task_id} has {len(relevant_signals)} relevant signals: {relevant_signals}"
        )

    evaluate()

    print("\n###### Generating Dataset ######")
    generate_dataset(5000)
    # generate_labelled_dataset()