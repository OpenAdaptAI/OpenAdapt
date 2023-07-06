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
    Database.add(
        documents=[doc_text],
        ids=[str(id_counter)]
    )


def query_database(search_text, n_results=1):
    results = Database.query(
        query_texts=[search_text],
        n_results=1
    )
    return results["ids"][0]


if "__main__" == __name__:
    ID_COUNTER = 0
    if (Database.peek()["ids"] != []):
        ID_COUNTER = int(Database.count())
    else:
        add_document("The engineers name was Richard.",ID_COUNTER)
        ID_COUNTER += 1

        add_document("Alex was an artist.",ID_COUNTER)
        ID_COUNTER += 1

        add_document("Richard eats apples often.",ID_COUNTER)
        ID_COUNTER += 1

        add_document("Astronauts were amazed by Richard.",ID_COUNTER)
        ID_COUNTER += 1

    print(query_database("What was Richard?")) # Prints "0"
    print(query_database("What did Richard eat?")) # Prints "2"