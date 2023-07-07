import random

Tasks = {
    0: "making a website about cats",
    1: "playing chess",
    2: "making a website about shoes",
    3: "filling out medical forms",
    4: "parse and analyze data from spreadsheets",
    5: "emailing an agent about house requirements",
    6: "filling out a job application",
    7: "signing into and powering on a virtual machine",
    8: "advertising a new restaurant",
    9: "booking a flight",
    10: "posting on social media platforms",
    11: "editing photos for an online portfolio",
    12: "updating a LinkedIn profile"
}

ShortSignals = [
    {"id": 0, "type": "file", "descriptor": "restaurant_menu_data.txt", "relevant_task_ids": [8]},
    {"id": 1, "type": "url", "descriptor": "https://en.wikipedia.org/wiki/Web_development", "relevant_task_ids": [0, 2]},
    {"id": 2, "type": "function", "descriptor": "math.sqrt", "relevant_task_ids": []},
    {"id": 3, "type": "url", "descriptor": "https://www.acuweather.com", "relevant_task_ids": []},  # 3 is very loosely relevant
    {"id": 4, "type": "database", "descriptor": "footwear.db", "relevant_task_ids": [2]},
    {"id": 5, "type": "function", "descriptor": "openai.Completion.create", "relevant_task_ids": []},
    {"id": 6, "type": "file", "descriptor": "electronic_medical_record_template.xls", "relevant_task_ids": [3]}, # 4 is very loosely relevant
    {"id": 7, "type": "url", "descriptor": "https://www.chess.com", "relevant_task_ids": [1]},
    {"id": 8, "type": "database", "descriptor": "user_info.db", "relevant_task_ids": [3, 6, 7]},
    {"id": 9, "type": "function", "descriptor": "pandas.DataFrame", "relevant_task_ids": [4]},
    {"id": 10, "type": "file", "descriptor": "File_Sorting_Script.py", "relevant_task_ids": []},
    {"id": 11, "type": "function", "descriptor": "sklearn.tree.DecisionTreeClassifier", "relevant_task_ids": []},
    {"id": 12, "type": "url", "descriptor": "https://www.skyscanner.com", "relevant_task_ids": [9]},
    {"id": 13, "type": "database", "descriptor": "social_media_accounts.db", "relevant_task_ids": [10,11]},
    {"id": 14, "type": "url", "descriptor": "https://www.linkedin.com", "relevant_task_ids": [6,10,11,12]},
]

def get_relevant_signal_ids(task_id):
    return [signal["id"] for signal in ShortSignals if task_id in signal["relevant_task_ids"]]

def get_signal_data_without_task_ids(signal_id):
    return {k: v for k, v in ShortSignals[signal_id].items() if k != "relevant_task_ids"}

def get_prompt(task_id, signal_ids):
    task = Tasks[task_id]
    test_signals = []
    for signal_id in signal_ids:
        signal = get_signal_data_without_task_ids(signal_id)
        test_signals.append(signal)
    
    prompt = f"""Below is an instruction that describes a task, paired with an input that provides further context. Write a response that appropriately completes the request.
# Instruction:
You are {task}. A list of information signals is provided in JSON format. Please respond with only the id of the signal that is most relevant to the task formatted as a list.
# Input:
{str(test_signals)}
# Response: 
"""
    return prompt

def evaluate():
    # 1. Pick a random task_id and a random number of random signals
    # 2. Generate a prompt
    # 3. Run the model
    # 4. Get the relevant signals
    # 5. Check if the model's output is correct

    task_id = random.randint(0, len(Tasks)-1)
    signal_count = random.randint(3, 7)

    # Select signal_count random signals (avoid duplicates)
    signal_ids = []
    for i in range(signal_count):
        signal_id = random.randint(0, len(ShortSignals)-1)
        while signal_id in signal_ids:
            signal_id = random.randint(0, len(ShortSignals)-1)
        signal_ids.append(signal_id)

    prompt = get_prompt(task_id, signal_ids)
    print("\n")
    print(prompt)

    full_relevant_signals = get_relevant_signal_ids(task_id)
    # Remove any signals that are not in the prompt
    relevant_signals = [signal_id for signal_id in full_relevant_signals if signal_id in signal_ids]

    #RUN MODEL
    print("Desired Output: ", relevant_signals)

    return

if __name__ == '__main__':
    print("Tasks:")
    for task_id, task in Tasks.items():
        print(f"{task_id}: {task}")
    print("Signals:")
    for signal in ShortSignals:
        print(f"{signal['id']}: {signal['descriptor']} ({signal['type']})")
    for task_id in Tasks.keys():
        relevant_signals = get_relevant_signal_ids(task_id)
        print(f"Task {task_id} has {len(relevant_signals)} relevant signals: {relevant_signals}")


    # # Test prompt generation:
    # print()
    # signal_ids = [0,2,4,6,8]
    # task_id = 3
    # prompt = get_prompt(task_id, signal_ids)
    # print(prompt)
    # print(f"\n **MOST RELEVANT SIGNALS**: {get_relevant_signal_ids(task_id)}")
    
    # # Test prompt generation:
    # print()
    # signal_ids = [0,1,2,3,4,5]
    # task_id = 0
    # prompt = get_prompt(task_id, signal_ids)
    # print(prompt)
    # print(f"\n **MOST RELEVANT SIGNALS**: {get_relevant_signal_ids(task_id)}")
    
    evaluate()