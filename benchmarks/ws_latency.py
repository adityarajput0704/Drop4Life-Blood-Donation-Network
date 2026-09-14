import asyncio
import statistics
import time

import httpx
import websockets

WS_URL = "ws://127.0.0.1:8000/ws/benchmark"
HTTP_URL = "http://127.0.0.1:8000/benchmark/ws/broadcast"
COUNT = 100


async def main():
    async with websockets.connect(WS_URL) as ws:
        await asyncio.sleep(0.1)

        latencies = []

        async with httpx.AsyncClient() as client:
            for _ in range(COUNT):
                start = time.perf_counter()

                response = await client.post(HTTP_URL)
                expected = response.json()["benchmark_id"]

                while True:
                    message = await ws.recv()

                    if message and str(expected) in message:
                        break

                latencies.append((time.perf_counter() - start) * 1000)

    latencies.sort()

    print(f"Messages: {len(latencies)}")
    print(f"p50: {statistics.median(latencies):.2f} ms")
    print(f"p95: {latencies[int(COUNT * 0.95) - 1]:.2f} ms")
    print(f"p99: {latencies[int(COUNT * 0.99) - 1]:.2f} ms")
    print(f"min: {latencies[0]:.2f} ms")
    print(f"max: {latencies[-1]:.2f} ms")


asyncio.run(main())
