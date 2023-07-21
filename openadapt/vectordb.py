import chromadb
from chromadb.config import Settings

CLIENT = chromadb.Client(Settings(
    chroma_db_impl="duckdb+parquet",
    persist_directory="./chroma" # Optional, defaults to .chromadb/ in the current directory
))

try: DATABASE = CLIENT.create_collection(name="SimilaritySearch")
except: DATABASE = CLIENT.get_collection(name="SimilaritySearch")

def add_individual_document(doc_text):
    """
    Adds a document to the database as a single entry.
    """
    db_count = DATABASE.count()
    if db_count:
        id = db_count
    else:
        id = 0
    DATABASE.add(
        documents=[doc_text],
        ids=[str(id)]
    )


def add_paragraph(paragraph_text):
    """
    Adds a paragraph to the database as multiple entries (split by sentences).
    """
    paragraph_text = paragraph_text.split(".")
    for sentence in paragraph_text:
        id = DATABASE.count()
        DATABASE.add(
            documents=[sentence],
            ids=[str(id)]
        )


def query_database(search_text, n_results=1):
    """
    Queries the database for the most similar document to the search text.
    """
    results = DATABASE.query(
        query_texts=[search_text],
        n_results=n_results
    )
    return results["ids"][0]

def create_collection(name):
    """
    Creates a collection.
    """
    CLIENT.create_collection(name=name)

def target_collection(name):
    """
    Targets a collection.
    """
    DATABASE = CLIENT.get_collection(name=name)

def delete_collection(name):
    """
    Deletes a collection.
    """
    CLIENT.delete_collection(name=name)

def reset_database():
    """
    Resets the database, clearing all collections.
    """
    CLIENT.reset()





if "__main__" == __name__:
    add_individual_document("The engineers name was Richard.")
    add_individual_document("Alex was an artist.") 
    add_individual_document("Richard eats apples often.") 
    add_individual_document("Astronauts were amazed by Richard.")

    print(query_database("What was Richard?")) # Prints "0"
    print(query_database("What did Richard eat?")) # Prints "2"

    delete_collection("SimilaritySearch")
    create_collection("ParagraphTest")
    target_collection("ParagraphTest")

    add_paragraph("The engineers name was Richard. Alex was an artist. Richard eats apples often. Astronauts were amazed by Richard.")
    print(query_database("What was Richard?",n_results=1)) # Prints "0"

    reset_database()