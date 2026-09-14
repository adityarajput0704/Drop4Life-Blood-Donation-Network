import asyncio
import json
import time
import httpx
import websockets

WS_URL = "ws://127.0.0.1:8000/ws/benchmark"
HTTP_URL = "http://127.0.0.1:8000/benchmark/ws/broadcast"

CLIENTS = 1000


async def connect_client():
    return await websockets.connect(WS_URL)


async def wait_for_message(ws):
    await ws.recv()
    return time.perf_counter()


async def main():
    print(f"Connecting {CLIENTS} clients...")

    connections = await asyncio.gather(
        *(connect_client() for _ in range(CLIENTS))
    )

    print(f"Connected: {len(connections)}")

    # Give all connections time to settle.
    await asyncio.sleep(1)

    # Start waiting before broadcasting.
    receive_tasks = [
        asyncio.create_task(wait_for_message(ws))
        for ws in connections
    ]

    await asyncio.sleep(0.1)

    start = time.perf_counter()

    async with httpx.AsyncClient() as client:
        response = await client.post(HTTP_URL)
        response.raise_for_status()

    await asyncio.gather(*receive_tasks)

    end = time.perf_counter()

    total_ms = (end - start) * 1000

    print(f"Broadcast → all {CLIENTS} clients: {total_ms:.2f} ms")

    for ws in connections:
        await ws.close()


asyncio.run(main())