from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from app.rate_limiter import is_allowed
from app.classifier import classify_event
from app.logging_config import setup_logging
import asyncio
from app.connection_manager import manager
from app.pubsub_manager import subscribe_user, publish_event, store_pending_notification, get_pending_notifications, clear_pending_notifications
import structlog

logger = structlog.get_logger()


setup_logging()
app = FastAPI()

class EventRequest(BaseModel):
    user_id: str
    source: str
    message: str

@app.post("/events")
async def create_event(event: EventRequest):
    allowed = await is_allowed(event.user_id)
    
    if not allowed:
        logger.warning(
            "rate_limit_rejected",
            user_id=event.user_id,
            source=event.source,
        )
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded for user_id: {event.user_id}"
        )
    
    classification = await classify_event(event.user_id, event.source, event.message)
    
    event_payload = {
        "user_id": event.user_id,
        "source": event.source,
        "message": event.message,
        "classification": classification
    }
    
    delivered = await publish_event(event.user_id, event_payload)
    
    if not delivered:
        await store_pending_notification(event.user_id, event_payload)
        logger.info(
            "delivery_fallback_stored",
            user_id=event.user_id,
            reason="no_active_subscriber",
        )
    else:
        logger.info(
            "delivery_success",
            user_id=event.user_id,
            channel="pubsub",
        )
    
    return {
        "status": "accepted",
        "delivered": delivered,
        **event_payload
    }

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    # First message from client must be their user_id
    await websocket.accept()
    data = await websocket.receive_json()
    user_id = data.get("user_id")
    
    if not user_id:
        await websocket.close(code=4001, reason="user_id required")
        return
    
    manager.active_connections[user_id] = websocket
    
    # Start background task that listens to this user's Redis channel
    subscribe_task = asyncio.create_task(subscribe_user(user_id))
    
    try:
        while True:
            # Keep the connection alive; we don't expect more messages from client,
            # but this lets us detect disconnects
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(user_id)
        subscribe_task.cancel()

@app.get("/notifications/{user_id}/pending")
async def get_pending(user_id: str):
    notifications = await get_pending_notifications(user_id)
    await clear_pending_notifications(user_id)
    return {
        "user_id": user_id,
        "count": len(notifications),
        "notifications": notifications
    }

@app.get("/health")
async def health_check():
    return {"status": "healthy"}
