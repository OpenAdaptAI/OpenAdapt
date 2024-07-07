"""Dummy Script for recording browser events using a WebSocket server.

And save them into a sqlite db.

Usage:

    $ python record.py

"""

import asyncio
import sqlite3
import websockets


def init_db() -> None:
    """A Function to initialize the database.

    Args:
        None

    Returns:
        None
    """
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


def insert_message(message: str) -> None:
    """Function to insert a message into the database.

    Args:
        message (str): The message to be inserted into the database.

    Returns:
        None
    """
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


async def handle_message(message: str) -> None:
    """Handle incoming messages and insert them into the database.

    Args:
        message (str): The message to be inserted into the database.

    Returns:
        None
    """
    print("Received message:", message)
    # insert_message(message)


async def handler(
    websocket: websockets.WebSocketServerProtocol, path: str, a: int, b: int
) -> None:
    """A web socket handler to handle incoming messages.

    Args:
        websocket: The websocket object.
        path: The path to the websocket.
        a: Additional argument a.
        b: Additional argument b.

    Returns:
        None
    """
    print(a)
    print(b)
    async for message in websocket:
        await handle_message(message)


async def main() -> None:
    """The main function to start the WebSocket server.

    Args:
        None

    Returns:
        None
    """
    # init_db()
    server = websockets.serve(
        lambda ws, path: handler(ws, path, a=1, b=2), "localhost", 8765
    )
    async with server:
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
