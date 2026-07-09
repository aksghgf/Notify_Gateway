import asyncio
import pytest
from app.rate_limiter import is_allowed
from app.redis_client import redis_client

@pytest.mark.asyncio
async def test_exactly_5_requests_allowed_under_concurrency():
    user_id = "test_user_concurrent"
    
    # Cleanup: clear older state for this user_id before test
    await redis_client.delete(f"rate_limit:{user_id}")
    
    # Fire 20 request for same user id
    tasks = [is_allowed(user_id) for _ in range(20)]
    results = await asyncio.gather(*tasks)
    
    allowed_count = sum(results)  # True ko 1 count karta hai
    
    assert allowed_count == 5, f"Expected exactly 5 allowed, got {allowed_count}"
    
    # Cleanup after test
    await redis_client.delete(f"rate_limit:{user_id}")