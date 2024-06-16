import asyncio
import websockets

async def print_message(message):
    print("Received message:", message)

async def handler(websocket, path):
    async for message in websocket:
        await print_message(message)

async def main():
    async with websockets.serve(handler, "localhost", 8765):
        await asyncio.Future()  # run forever

if __name__ == "__main__":
    asyncio.run(main())
