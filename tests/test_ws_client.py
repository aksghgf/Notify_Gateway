import asyncio
import websockets
import json

async def connect_as_user():
    uri = "ws://127.0.0.1:8000/ws"
    async with websockets.connect(uri) as websocket:
        # First message: identify ourselves
        await websocket.send(json.dumps({"user_id": "abhishek123"}))
        print("Connected and identified as abhishek123. Waiting for messages...")
        
        # Keep listening for incoming events
        async for message in websocket:
            print("Received:", message)

asyncio.run(connect_as_user())