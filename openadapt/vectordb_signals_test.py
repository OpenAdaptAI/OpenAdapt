import chromadb
import json
import numpy as np

def find_minima(x):
    # get the sorted indices
    sorted_indices = np.argsort(x)

    # sort the values using these indices
    x_sorted = np.array(x)[sorted_indices]

    # find the differences between adjacent pairs
    diffs = np.diff(x_sorted)

    # find the median of differences
    median_diff = np.median(diffs)

    # find all indices where the difference is significantly larger than the median
    indices = np.where(diffs > 4*median_diff)[0]

    # if there's no significant difference found, return an empty list
    if len(indices) == 0 and diffs[0] <= 4*median_diff:
        return []
    
    # use these indices to get the original indices of the minima
    minima_indices = sorted_indices[indices]
    
    return minima_indices.tolist()

def find_minima_std(x, num_std=2):
    mean = np.mean(x)
    std = np.std(x)
    minima_indices = [i for i, xi in enumerate(x) if xi < mean - num_std * std]
    return minima_indices

def find_minima_percentile(x, percentile=10):
    threshold = np.percentile(x, percentile)
    minima_indices = [i for i, xi in enumerate(x) if xi < threshold]
    return minima_indices

def find_minima_absolute(x, value=0.7):
    minima_indices = [i for i, xi in enumerate(x) if xi < value]
    return minima_indices



# Using jsonl file
# For each line:
#   Extract and add each signal to the collection
#   Extract the trask description
#   Extract the desired response (if any) and use the number of results to determine the number of query results
#   Query the collection
#   Compare results to desired response

with open("./dataset.jsonl") as data_file:
    total_score = 0
    total_alternative_score = 0
    find_minima_score = 0
    find_minima_std_score = 0
    find_minima_percentile_score = 0
    find_minima_absolute_score = 0
    num_lines = 0
    for line in data_file:

        chroma_client = chromadb.Client()

        collection = chroma_client.create_collection(name="SignalTest",metadata={"hnsw:space": "cosine"})

        num_lines += 1
        data = json.loads(line)

        # Use Python splitlines to create a list of lines
        text = data['text']

        task_description = text[text.index('Instruction:') + 21:text.index('. A list')]

        input_signals = text[text.index('['):text.index(']') + 1]
        input_signals = json.loads(input_signals.replace("'", '"'))

        response = json.loads(text[text.index('Response: ') + 11:len(text)])

        # print(f"task_description: {task_description}")
        # print(f"Input Signals: {input_signals}")
        # print(f"Response: {response}")
        
        for i, signal in enumerate(input_signals):
            collection.add(
                documents=[str(signal)],
                ids=[f"{i}"]
            )

        if len(response) != 0:
            results = collection.query(
                query_texts=[task_description],
                n_results=len(response)
            )
    

        alternative_results_test = collection.query(
            query_texts=[task_description],
            n_results=collection.count()
        )

        alternative_results = find_minima(alternative_results_test['distances'][0])
        alternative_results_std = find_minima_std(alternative_results_test['distances'][0])
        alternative_results_percentile = find_minima_percentile(alternative_results_test['distances'][0])
        alternative_results_absolute = find_minima_absolute(alternative_results_test['distances'][0])
        alternative_result_ids = []
        # for result in alternative_results:
        #     alternative_result_ids.append(alternative_results_test['ids'][0][result])

        for result in alternative_results_std:
            alternative_result_ids.append(alternative_results_test['ids'][0][result])

        # evaluate results, ignoring order #
        score_sum = 1
        for id in response:
            if str(id) not in results['ids'][0]:
                score_sum -= 1/len(response)

        alternative_score_sum = 1
        for id in response:
            if str(id) not in str(alternative_result_ids):
                alternative_score_sum -= 1/len(response)

        if len(alternative_results) == len(response):
            find_minima_score += 1
        if len(alternative_results_std) == len(response):
            find_minima_std_score += 1
        if len(alternative_results_percentile) == len(response):
            find_minima_percentile_score += 1
        if len(alternative_results_absolute) == len(response):
            find_minima_absolute_score += 1
        
        

        total_score += score_sum
        total_alternative_score += alternative_score_sum

        #print("Desired Response: ", response, "Query Results: ", results['ids'][0])
        print("Total Score: ", total_score, "Line: ", num_lines)
        print("Accuracy: ", total_score / num_lines)

        #print("Desired Response: ", response, "Alternative Query Results: ", alternative_result_ids)
        print("Alternative Total Score: ", total_alternative_score, "Line: ", num_lines)
        print("Alternative Accuracy: ", total_alternative_score / num_lines)    

        print("Accuracy of Find Minima: ", find_minima_score / num_lines)
        print("Accuracy of Find Minima Std: ", find_minima_std_score / num_lines)
        print("Accuracy of Find Minima Percentile: ", find_minima_percentile_score / num_lines)
        print("Accuracy of Find Minima Absolute: ", find_minima_absolute_score / num_lines)
        chroma_client.reset()

    total_score /= num_lines
    print(f"Total Score: {total_score}")
