import chromadb
import json

chroma_client = chromadb.Client()

collection = chroma_client.create_collection(name="my_collection",metadata={"hnsw:space": "cosine"})

# Using jsonl file
# For each line:
#   Extract and add each signal to the collection
#   Extract the trask description
#   Extract the desired response (if any) and use the number of results to determine the number of query results
#   Query the collection
#   Compare results to desired response

with open("./dataset.jsonl") as data_file:
    total_score = 0
    num_lines = 0
    for line in data_file:
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

        # evaluate results, ignoring order #
        score_sum = 1

        for id in response:
            if str(id) not in results['ids'][0]:
                score_sum -= 1/len(response)

        total_score += score_sum
        print("Total Score: ", total_score, "Line: ", num_lines)
        print("Accuracy: ", total_score / num_lines)

    total_score /= num_lines
    print(f"Total Score: {total_score}")
