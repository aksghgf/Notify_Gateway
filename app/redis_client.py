# app/redis_client.py
import redis.asyncio as redis
import os
from dotenv import load_dotenv

load_dotenv()

REDIS_URL = os.environ.get("REDIS_URL")

redis_client = redis.from_url(REDIS_URL, decode_responses=True)