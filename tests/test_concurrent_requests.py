import asyncio
import httpx

async def send_request(client, i):
    response = await client.post(
        "http://127.0.0.1:8000/events",
        json={"user_id": "ratelimit_test4", "source": "test", "message": f"parallel {i}"}
    )
    print(f"Request {i}: status={response.status_code}")
    return response.status_code

async def main():
    async with httpx.AsyncClient() as client:
        tasks = [send_request(client, i) for i in range(6)]
        results = await asyncio.gather(*tasks)
        success = results.count(200)
        rejected = results.count(429)
        print(f"\nSummary: {success} succeeded, {rejected} rate-limited")

asyncio.run(main())