import chromadb
from chromadb.config import Settings


client = chromadb.Client(Settings(
    chroma_db_impl="duckdb+parquet",
    persist_directory="/path/to/persist/directory" # Optional, defaults to .chromadb/ in the current directory
))


Database = client.create_collection(name="SimSearch")
