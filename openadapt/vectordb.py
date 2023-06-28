import chromadb
from chromadb.config import Settings


client = chromadb.Client(Settings(
    chroma_db_impl="duckdb+parquet",
    persist_directory="./chroma" # Optional, defaults to .chromadb/ in the current directory
))

try: Database = client.create_collection(name="SimilaritySearch")
except: Database = client.get_collection(name="SimilaritySearch")

def add_document(doc_text,id_counter):
    # If documents are formatted as sentences, split by sentences
    # If documents are not formatted as sentences, split into chunks of equal size
    doct_text_sentences = doc_text.split(".")
    ids = []
    for i in range(len(doct_text_sentences)):
        ids.append(str(id_counter))
        id_counter += 1
    Database.add(
        documents=doct_text_sentences,
        ids=ids
    )


def query_database(search_text, n_results=1):
    results = Database.query(
        query_texts=[search_text],
        n_results=1
    )
    return results["ids"][0]


if "__main__" == __name__:
    ID_COUNTER = int(Database.peek()["ids"][-1])
    #print(Database.peek())
    print(Database.count())
    #ID_COUNTER = 0
    print(ID_COUNTER)
    # add_document("The engineers name was Richard",ID_COUNTER)
    # ID_COUNTER += len("The engineers name was Richard".split("."))

    # add_document("Alex was an artist",ID_COUNTER)
    # ID_COUNTER += len("Alex was an artist".split("."))

    # add_document("Richard eats apples often",ID_COUNTER)
    # ID_COUNTER += len("Richard eats apples often".split("."))

    # add_document("Astronauts were amazed by Richard",ID_COUNTER)
    # ID_COUNTER += len("Astronauts were amazed by Richard".split("."))

    print(query_database("What was Richard?")) # Prints "0"
    print(query_database("What did Richard eat?")) # Prints "2"