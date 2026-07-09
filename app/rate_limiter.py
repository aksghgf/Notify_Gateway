# app/rate_limiter.py
import time
from app.redis_client import redis_client

with open("app/token_bucket.lua", "r") as f:
    TOKEN_BUCKET_SCRIPT = f.read()

async def is_allowed(user_id: str) -> bool:
    key = f"rate_limit:{user_id}"
    now = time.time()
    
    result = await redis_client.eval(
        TOKEN_BUCKET_SCRIPT,
        1,
        key,
        5,
        2,
        now
    )
    return result == 1