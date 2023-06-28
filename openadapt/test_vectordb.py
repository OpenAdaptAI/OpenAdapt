import chromadb

chroma_client = chromadb.Client()

collection = chroma_client.create_collection(name="my_collection")

# Demo Test
collection.add(
    documents=["This is a document", "This is another document"],
    metadatas=[{"source": "my_source"}, {"source": "my_source"}],
    ids=["id1", "id2"]
)

results = collection.query(
    query_texts=["This is a query document"],
    n_results=2
)

print(results["ids"])

# More Complicated Test

collection.add(
    documents=["The engineers name was Richard", "Alex was an artist","Richard eats apples often","Astronauts were amazed by Richard"],
    ids=["id3","id4","id5","id6"]
)

results = collection.query(
    query_texts=["What was Richard?"],
    n_results=1
)

print(results["ids"])
print(collection.get(results["ids"][0])["documents"])