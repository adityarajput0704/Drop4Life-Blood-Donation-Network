# backend/core/websocket_manager.py

import asyncio
import json
import logging
from typing import Dict, List

import redis.asyncio as aioredis
from fastapi import WebSocket

from backend.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()


class ConnectionManager:
    def __init__(self):
        # Local connections handled by this FastAPI worker
        self.rooms: Dict[str, List[WebSocket]] = {}

        # Redis Pub/Sub
        self.redis = aioredis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
        )

        self.pubsub = self.redis.pubsub()
        self.listener_task = None

    async def start(self):
        """Start listening for broadcasts from all workers."""
        await self.pubsub.subscribe("drop4life:websocket")

        self.listener_task = asyncio.create_task(
            self._listen_for_messages()
        )

        logger.info("WebSocket Redis Pub/Sub listener started")

    async def stop(self):
        """Stop Redis listener and close Redis connections."""
        if self.listener_task:
            self.listener_task.cancel()

            try:
                await self.listener_task
            except asyncio.CancelledError:
                pass

            self.listener_task = None

        await self.pubsub.unsubscribe("drop4life:websocket")
        await self.pubsub.close()
        await self.redis.close()

        logger.info("WebSocket Redis Pub/Sub listener stopped")

    async def _listen_for_messages(self):
        """Receive broadcasts published by any FastAPI worker."""

        try:
            async for message in self.pubsub.listen():

                if message["type"] != "message":
                    continue

                try:
                    data = json.loads(message["data"])

                    room = data["room"]
                    payload = data["message"]

                    if room == "__all__":
                        for local_room in list(self.rooms.keys()):
                            await self._broadcast_local(local_room, payload)
                    else:
                        await self._broadcast_local(room, payload)                  

                except Exception:
                    logger.exception(
                        "Failed to process Redis WebSocket message"
                    )

        except asyncio.CancelledError:
            pass

        except Exception:
            logger.exception(
                "Redis WebSocket listener crashed"
            )

    async def _broadcast_local(
        self,
        room: str,
        message: dict,
    ):
        """Broadcast to WebSocket clients connected to this worker."""

        if room not in self.rooms:
            return

        dead_connections = []

        for websocket in list(self.rooms[room]):
            try:
                await websocket.send_json(message)

            except Exception:
                dead_connections.append(websocket)

        for websocket in dead_connections:
            self.disconnect(websocket, room)

    async def connect(
        self,
        websocket: WebSocket,
        room: str,
    ):
        """Accept and register a WebSocket connection."""

        await websocket.accept()

        if room not in self.rooms:
            self.rooms[room] = []

        self.rooms[room].append(websocket)

        logger.info(
            "WebSocket connected: room=%s connections=%d",
            room,
            len(self.rooms[room]),
        )

    def disconnect(
        self,
        websocket: WebSocket,
        room: str,
    ):
        """Remove a WebSocket connection."""

        if room not in self.rooms:
            return

        if websocket in self.rooms[room]:
            self.rooms[room].remove(websocket)

        if not self.rooms[room]:
            del self.rooms[room]

    async def broadcast_to_room(
        self,
        room: str,
        message: dict,
    ):
        """
        Publish a room broadcast to Redis.

        Every FastAPI worker receives this message and
        delivers it to its own local clients.
        """

        payload = json.dumps(
            {
                "room": room,
                "message": message,
            }
        )

        await self.redis.publish(
            "drop4life:websocket",
            payload,
        )

    async def broadcast_to_all(
        self,
        message: dict,
    ):
        """
        Broadcast a message to every WebSocket connection
        across every FastAPI worker.
        """

        payload = json.dumps(
            {
                "room": "__all__",
                "message": message,
            }
        )

        await self.redis.publish(
            "drop4life:websocket",
            payload,
        )   

manager = ConnectionManager()