import asyncio
import json
import structlog
from app.redis_client import redis_client
from app.connection_manager import manager

logger = structlog.get_logger()

async def subscribe_user(user_id: str):
    """Background task: listens to this user's Redis channel for the lifetime of the connection"""
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(f"channel:{user_id}")
    
    try:
        async for message in pubsub.listen():
            if message["type"] == "message":
                data = json.loads(message["data"])
                delivered = await manager.send_to_user(user_id, data)
                logger.info(
                    "delivery_attempt",
                    user_id=user_id,
                    delivered=delivered,
                    source="pubsub"
                )
    except asyncio.CancelledError:
        # Task is cancelled when the connection closes; clean up subscription
        await pubsub.unsubscribe(f"channel:{user_id}")
        raise


async def publish_event(user_id: str, event_data: dict):
    """Called whenever a newly classified event arrives, from any instance"""
    channel = f"channel:{user_id}"
    subscribers_count = await redis_client.publish(channel, json.dumps(event_data))
    return subscribers_count > 0

async def store_pending_notification(user_id: str, event_data: dict):
    key = f"pending:{user_id}"
    await redis_client.rpush(key, json.dumps(event_data))
    await redis_client.expire(key, 86400)  # 24 hours TTL, cleanup ke liye


async def get_pending_notifications(user_id: str) -> list:
    key = f"pending:{user_id}"
    items = await redis_client.lrange(key, 0, -1)
    return [json.loads(item) for item in items]


async def clear_pending_notifications(user_id: str):
    key = f"pending:{user_id}"
    await redis_client.delete(key)