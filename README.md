# NotifyGate

A rate-limited, LLM-classified real-time notification gateway. Built with FastAPI, Redis (Upstash), WebSockets, and Groq LLM.

---

## Architecture Overview

NotifyGate accepts incoming events over HTTP, applies a per-user token-bucket rate limit, classifies each event using an LLM into `urgent` / `normal` / `promotional`, and delivers the classified event to the target user in real time over a WebSocket connection. If the user isn't currently connected, the event is stored durably and can be retrieved later.

```
POST /events
     |
     v
Rate Limiter (Redis Lua script, atomic token bucket)
     |  (429 if rejected)
     v
LLM Classifier (Groq, structured JSON output, 3s timeout + fallback)
     |
     v
Redis PUBLISH on channel "channel:{user_id}"
     |
     +--> Any instance subscribed to that channel forwards to its
     |    local WebSocket connection for that user (if connected)
     |
     +--> If no subscriber received it, event is also written to
          Redis List "pending:{user_id}" (24h TTL) as a durable fallback
```

### Cross-instance delivery design

This is the core design constraint from the assignment: the service must work correctly behind a load balancer with 2+ instances, where a user connected to Instance A must receive an event ingested via Instance B.

**How it works in this codebase:**

- Each FastAPI instance keeps a local, in-memory `ConnectionManager` (`app/connection_manager.py`) mapping `user_id -> WebSocket`. This map is only ever valid within that one process.
- When a client connects over `/ws` and identifies itself with a `user_id`, the instance starts a background task (`subscribe_user`) that subscribes to a Redis Pub/Sub channel named `channel:{user_id}`.
- When any instance ingests and classifies an event for that `user_id`, it calls `publish_event()`, which does a Redis `PUBLISH` on that same channel — regardless of which instance originally received the HTTP request.
- Redis Pub/Sub fans that message out to **every instance** currently subscribed to that channel. Whichever instance actually holds the live WebSocket connection for that user forwards the message to its local socket.
- `PUBLISH` returns the number of subscribers that received the message. If it's `0` (no instance has that user connected anywhere), the event is additionally written to a Redis List (`pending:{user_id}`) so it isn't lost. This list is retrievable via `GET /notifications/{user_id}/pending`, and is cleared once read.

This means the in-process dictionary is never the sole source of truth for delivery — Redis Pub/Sub is the cross-instance bridge, and the Redis List is the durability backstop for offline users. Running 2+ instances behind a load balancer requires no code change to this design.

---

## Setup & Running Locally

### Prerequisites
- Python 3.10+
- A free Redis instance ([Upstash](https://upstash.com) recommended — 256MB free tier, no card required)
- A free [Groq API key](https://console.groq.com)

### Steps

1. **Clone the repo**
   ```bash
   git clone <your-repo-url>
   cd notifygate
   ```

2. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure environment variables**

   Copy `.env.example` to `.env` and fill in your own values:
   ```
   REDIS_URL=rediss://default:<password>@<your-endpoint>.upstash.io:6379
   GROQ_API_KEY=gsk_xxxxxxxxxxxxxxxxxxxxx
   ```

4. **Run the server**
   ```bash
   python -m uvicorn app.main:app --reload
   ```
   Server starts at `http://127.0.0.1:8000`.

5. **Run tests**
   ```bash
   python -m pytest tests/ -v
   ```
   This includes the required concurrency test for Part 1 (fires 20 simultaneous requests for the same `user_id` and asserts exactly 5 succeed).

---

## Testing the API

### Interactive docs (REST endpoints)

Once the server is running, visit **`http://127.0.0.1:8000/docs`** for FastAPI's auto-generated Swagger UI. You can expand `POST /events`, `GET /health`, and `GET /notifications/{user_id}/pending`, click "Try it out," and fire real requests directly from the browser — no curl/Postman needed.

### WebSocket delivery (manual)

Swagger UI doesn't support WebSocket testing, so a small client script is included:
```bash
python tests/test_ws_client.py
```
This connects, identifies as a test `user_id`, and prints any event pushed to it in real time. To see it in action, run this in one terminal, then `POST /events` with a matching `user_id` in another (via Swagger UI or curl).

### Example request (curl)
```bash
curl -X POST http://127.0.0.1:8000/events \
  -H "Content-Type: application/json" \
  -d '{"user_id": "abhishek123", "source": "monitoring", "message": "Server CPU usage at 98%"}'
```

---

## Written Section

### Redis outage: what breaks, and the minimal fix

If Redis goes down for 90 seconds while events are actively being ingested, two things break immediately: the rate limiter (`is_allowed()`) can no longer read/write token bucket state, and the Pub/Sub publish + pending-store write both fail. In the current implementation, both of these are `await`ed directly with no error handling around the Redis calls, so a Redis outage would raise an unhandled exception inside the `/events` handler, and FastAPI would return a `500 Internal Server Error` for every request during the outage. No events would be classified or delivered during that window, and the failure would be silent from the client's perspective beyond the 500 — there's no distinction between "rejected" and "system down."

The minimal change to degrade gracefully instead of hard-failing is to wrap the rate-limiter and Redis calls in a try/except, and decide a fail-open vs fail-closed policy explicitly. I'd fail-closed for the rate limiter specifically (return `503 Service Unavailable` rather than silently allowing unlimited traffic through, since the whole point of the limiter is protection) and log a `redis_unavailable` event distinctly from a `rate_limit_rejected` event so on-call engineers can tell the two apart in the logs. A more resilient version would add a short-lived local in-memory fallback limiter that activates only while Redis is unreachable, but that adds complexity (and a new source of the exact cross-instance inconsistency the assignment is designed to avoid) — for this scope, fail-closed with clear logging is the more defensible tradeoff.

### LLM outage: what happens end-to-end

If the Groq API returns errors for 10 minutes straight, the flow is: a request comes in, passes the rate limiter (Redis is unaffected by an LLM outage), and reaches `classify_event()`. The `asyncio.wait_for(..., timeout=3.0)` wrapper means each call fails fast rather than hanging — either the Groq call itself errors out (caught by the generic `except Exception` block) or it exceeds 3 seconds and is cancelled (caught by `except asyncio.TimeoutError`). Either way, the function logs the failure (`classification_failed` or `classification_timeout`, with the raw error included for the former) and returns the fallback classification `"normal"`.

Critically, the event is **not dropped** — it continues through the rest of the pipeline exactly as if it had been classified normally: it's published over Redis Pub/Sub, delivered in real time if the user is connected, or written to the pending store if not. The only user-visible effect during the 10-minute outage is that every event gets misclassified as `"normal"` regardless of its true urgency — an `urgent` alert would be delivered with the same priority as routine noise. This is a real degradation (a genuinely urgent notification could get lost in the noise), but it's a deliberate tradeoff: availability and delivery are preserved at the cost of prioritization accuracy, which I think is the right call for a notification system, since a delayed classification is recoverable but a dropped notification is not.

### One ambiguity I resolved

The spec doesn't say what should happen if `user_id` is missing or empty when a client connects to the WebSocket endpoint (as opposed to the `POST /events` body, where Pydantic validation handles it automatically by rejecting the request with a `422`). For the WebSocket, there's no built-in validation step — the first message is read manually with `receive_json()`, so an empty or missing `user_id` would otherwise be silently accepted and used as a dictionary key, effectively creating a broken "anonymous" channel that could never receive anything meaningful, and would silently overwrite any other connection that also failed to send a `user_id`.

I resolved this by explicitly checking for `user_id` right after the handshake and closing the connection immediately with a custom close code (`4001`, in the app-specific 4000–4999 range) and a descriptive reason if it's missing. This mirrors what Pydantic does automatically for the REST endpoint, keeps both entry points consistent in how they reject malformed identification, and avoids the silent-overwrite bug entirely.

---

## What I'd Do Differently With More Time

- **Rate limiter resilience:** as described above, add explicit try/except around Redis calls with a fail-closed `503` response instead of letting connection errors surface as unhandled `500`s.
- **Acknowledged delivery for pending notifications:** currently `GET /notifications/{user_id}/pending` clears the list immediately on read, which means a client that crashes after receiving the response but before processing it loses those notifications permanently. A proper ack/nack flow (or at least a short grace period before deletion) would be more robust.
- **Local fallback rate limiting** during Redis outages, as a middle ground between fail-open and fail-closed.
- **Load testing the WebSocket layer** with many concurrent connections to validate the Pub/Sub fan-out design actually holds up under real cross-instance load, rather than the manual two-terminal verification I relied on here.
- **Structured request tracing** (a request ID threaded through rate-limit → classify → deliver logs) to make debugging a single event's journey through the pipeline easier — I ran into exactly this kind of confusion once during development when a stale reloaded server process caused misleading test results, and a request ID would have made that immediately obvious from the logs.
