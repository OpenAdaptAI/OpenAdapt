import chromadb

chroma_client = chromadb.Client()

collection = chroma_client.create_collection(name="my_collection")

# Using jsonl file
# For each line:
#   Extract and add each signal to the collection
#   Extract the trask description
#   Extract the desired response (if any) and use the number of results to determine the number of query results
#   Query the collection
#   Compare results to desired response

signals = [{'id': 0, 'type': 'file', 'descriptor': 'restaurant_menu_data.txt'},
           {'id': 1, 'type': 'url', 'descriptor': 'https://en.wikipedia.org/wiki/Web_development'},
           {'id': 2, 'type': 'function', 'descriptor': 'math.sqrt'},
           {'id': 3, 'type': 'url', 'descriptor': 'https://www.accuweather.com'},
           {'id': 4, 'type': 'database', 'descriptor': 'footwear.db'},
           {'id': 5, 'type': 'function', 'descriptor': 'openai.Completion.create'},
           {'id': 6, 'type': 'file', 'descriptor': 'anatomy_data.csv'}]

collection.add(
    documents=[str(signals[0]),str(signals[1]),str(signals[2]),str(signals[3]),str(signals[4]),str(signals[5]),str(signals[6])],
    ids=["signal 0","signal 1","signal 2","signal 3","signal 4","signal 5","signal 6"]
)

results = collection.query(
    query_texts=["Creating a website about shoes."],
    n_results=2
)

print(results["ids"])
