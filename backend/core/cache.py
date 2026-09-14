import redis
import json
from typing import Optional, Any
from backend.config import get_settings

settings = get_settings()

# Single Redis client instance — reused across all requests
redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)


def get_cached(key: str) -> Optional[Any]:
    """
    Retrieve a value from Redis cache.
    Returns None if key doesn't exist or Redis is unavailable.
    """
    try:
        data = redis_client.get(key)
        if data:
            return json.loads(data)
        return None
    except Exception:
        # If Redis is down, fail silently — fall through to DB
        return None


def set_cached(key: str, value: Any, ttl_seconds: int = 60):
    """
    Store a value in Redis cache with a TTL (time to live).
    After TTL expires, Redis automatically deletes the key.
    """
    try:
        redis_client.setex(
            name=key,
            time=ttl_seconds,
            value=json.dumps(value, default=str)  # default=str handles datetime serialization
        )
    except Exception:
        # If Redis is down, fail silently — the DB result still gets returned
        pass


def invalidate_cache(pattern: str):
    """
    Delete all cache keys matching a pattern without blocking Redis.

    Uses SCAN instead of KEYS so Redis can continue serving
    other requests while the keyspace is being searched.
    """
    try:
        cursor = 0

        while True:
            cursor, keys = redis_client.scan(
                cursor=cursor,
                match=pattern,
                count=100,
            )

            if keys:
                redis_client.delete(*keys)

            if cursor == 0:
                break

    except Exception:
        pass