import chromadb

chroma_client = chromadb.Client()

collection = chroma_client.create_collection(name="my_collection")

# # Demo Test
# collection.add(
#     documents=["This is a document", "This is another document"],
#     metadatas=[{"source": "my_source"}, {"source": "my_source"}],
#     ids=["id1", "id2"]
# )

# results = collection.query(
#     query_texts=["This is a query document"],
#     n_results=2
# )

# print(results["ids"])

# # More Complicated Test

# collection.add(
#     documents=["The engineers name was Richard", "Alex was an artist","Richard eats apples often","Astronauts were amazed by Richard"],
#     ids=["id3","id4","id5","id6"]
# )

# results = collection.query(
#     query_texts=["What was Richard?"],
#     n_results=1
# )

# print(results["ids"])
# print(collection.get(results["ids"][0])["documents"])

# Signal Test

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
