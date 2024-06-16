import asyncio
import websockets
import sqlite3
import json


# Function to initialize the database
def init_db():
    conn = sqlite3.connect("chrome.db")
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS browser_event (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            message TEXT
        )
    """)
    conn.commit()
    conn.close()


# Function to insert a message into the database
def insert_message(message):
    conn = sqlite3.connect("chrome.db")
    c = conn.cursor()
    c.execute(
        """
        INSERT INTO browser_event (message)
        VALUES (?)
    """,
        (message,),
    )
    conn.commit()
    conn.close()


# Asynchronous function to handle incoming messages and insert them into the database
async def handle_message(message):
    print("Received message:", message)
    insert_message(message)


# WebSocket handler
async def handler(websocket, path):
    async for message in websocket:
        await handle_message(message)


# Main function to start the WebSocket server
async def main():
    init_db()
    async with websockets.serve(handler, "localhost", 8765):
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
