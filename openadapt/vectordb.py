import chromadb
from chromadb.config import Settings


class VectorDB:
    """
    A class for storing and querying data by vector similarity.
    """

    def __init__(self):
        self.client = chromadb.Client(
            Settings(
                chroma_db_impl="duckdb+parquet",
                persist_directory="./chroma",  # Optional, defaults to .chromadb/ in the current directory
            )
        )

        try:
            self.database = self.client.create_collection(name="SimilaritySearch",metadata={"hnsw:space": "cosine"})
        except:
            self.database = self.client.get_collection(name="SimilaritySearch",metadata={"hnsw:space": "cosine"})

    def add_individual_document(self, doc_text):
        """
        Adds a document to the database as a single entry.
        """
        db_count = self.database.count()
        if db_count:
            id = db_count
        else:
            id = 0
        self.database.add(documents=[doc_text], ids=[str(id)])

    def add_paragraph(self, paragraph_text):
        """
        Adds a paragraph to the database as multiple entries (split by sentences).
        """
        paragraph_text = paragraph_text.split(".")
        for sentence in paragraph_text:
            self.add_individual_document(sentence)

    def add_chunk_document(self, chunk_text, characters_per_chunk=100):
        """
        Adds a chunk of text to the database as multiple entries (split by chunks).
        """
        chunk_text = [
            chunk_text[i : i + characters_per_chunk]
            for i in range(0, len(chunk_text), characters_per_chunk)
        ]
        for chunk in chunk_text:
            self.add_individual_document(chunk)

    def query_database_id(self, search_text, n_results=1):
        """
        Queries the database for the most similar document to the search text.
        """
        results = self.database.query(query_texts=[search_text], n_results=n_results)
        return results["ids"][0]

    def query_database_text(self, search_text, n_results=1):
        """
        Queries the database for the most similar document to the search text.
        """
        results = self.database.query(query_texts=[search_text], n_results=n_results)
        return results["documents"][0]
    
    def query_database(self, search_text, n_results=1):
        """
        Queries the database for the most similar document to the search text.
        """
        results = self.database.query(query_texts=[search_text], n_results=n_results)
        return results

    def create_collection(self, name):
        """
        Creates a collection.
        """
        self.client.create_collection(name=name)

    def target_collection(self, name):
        """
        Targets a collection.
        """
        self.database = self.client.get_collection(name=name)

    def delete_collection(self, name):
        """
        Deletes a collection.
        """
        self.client.delete_collection(name=name)

    def reset_database(self):
        """
        Resets the database, clearing all collections.
        """
        self.client.reset()


if "__main__" == __name__:
    VDB = VectorDB()
    VDB.add_individual_document("The engineers name was Aria.")
    VDB.add_individual_document("Alex was an artist.")
    VDB.add_individual_document("Aria eats apples often.")
    VDB.add_individual_document("Astronauts were amazed by Aria.")

    print(VDB.query_database_id("What was Aria?"))  # Prints "0"
    print(VDB.query_database_id("What did Aria eat?"))  # Prints "2"

    VDB.delete_collection("SimilaritySearch")
    try:VDB.create_collection("ParagraphTest")
    except:pass
    VDB.target_collection("ParagraphTest")

    VDB.add_paragraph(
        "The engineers name was Aria. Alex was an artist. Aria eats apples often. Astronauts were amazed by Aria."
    )
    print(VDB.query_database_id("What was Aria?", n_results=1))  # Prints "0"
    print(VDB.query_database_id("He was a famous painter", n_results=1))  # Prints "1"

    print(
        VDB.query_database_text("Aria often amazed people.", n_results=1)[0]
    )  # Prints "Astronauts were amazed by Aria."

    print(
        VDB.query_database("Aria often amazed people.", n_results=VDB.database.count())
    )

    VDB.reset_database()
