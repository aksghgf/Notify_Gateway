# NotifyGate

A rate-limited, LLM-classified real-time notification gateway. Built with FastAPI, Redis (Upstash), WebSockets, and Groq LLM.

---

## Architecture

```
POST /events
     │
     ▼
Rate Limiter: Redis Lua script, atomic token bucket (429 if rejected)
     │
     ▼
Classifier: Groq LLM, structured JSON output, 3s timeout + fallback
     │
     ▼
Redis PUBLISH on channel "channel:{user_id}"
     │
     ├──► Connected instance forwards to local WebSocket ─► delivered live
     │
     └──► No subscriber found ─ written to Redis List "pending:{user_id}"
                                   (24h TTL, retrievable via GET /notifications/:user_id/pending)
```

### Cross-instance delivery

The assignment's core constraint: a user connected to Instance A must receive an event ingested via Instance B.

- Each instance keeps a **local, in-memory** `user_id → WebSocket` map (`connection_manager.py`). Valid only within that process — never treated as the sole source of truth.
- On connect, each instance subscribes to a Redis Pub/Sub channel (`channel:{user_id}`) via a background task.
- On ingest, **any** instance publishes to that same channel, regardless of which instance received the HTTP request.
- Redis fans the message out to every subscribed instance; whichever one holds the live socket forwards it.
- `PUBLISH` returns the subscriber count. If `0`, the event is also written to a durable Redis List so it isn't lost.

Redis Pub/Sub is the cross-instance bridge; the Redis List is the durability backstop. No code change is needed to run this behind a load balancer with 2+ instances.

---

## Setup

**Prerequisites:** Python 3.10+, a free [Upstash Redis](https://upstash.com) instance, a free [Groq API key](https://console.groq.com)

```bash
git clone <your-repo-url>
cd notifygate
pip install -r requirements.txt
```

Copy `.env.example` → `.env` and fill in:
```
REDIS_URL=rediss://default:<password>@<your-endpoint>.upstash.io:6379
GROQ_API_KEY=gsk_xxxxxxxxxxxxxxxxxxxxx
```

Run it:
```bash
python -m uvicorn app.main:app --reload
```

Test it:
```bash
python -m pytest tests/ -v
```
Includes the required concurrency test — 20 simultaneous requests, asserts exactly 5 succeed.

---

## Trying It Out

**REST endpoints:** visit `http://127.0.0.1:8000/docs` (Swagger UI) — click "Try it out" on `/events`, `/health`, or `/notifications/{user_id}/pending`, no curl needed.

**WebSocket delivery:** Swagger can't test WebSockets, so:
```bash
python tests/test_ws_client.py
```
Run this in one terminal, then `POST /events` with a matching `user_id` from another — watch it arrive live.

---

## Written Section

### Redis outage (90s, mid-ingestion)

Both the rate limiter and the publish/pending-store write hit Redis directly with no error handling — an outage would surface as an unhandled exception, and every request would 500 during that window. No distinction between "rejected" and "system down."

**Minimal fix:** wrap the Redis calls in try/except and fail closed — return `503` rather than silently letting unlimited traffic through, since the limiter's whole job is protection. Log `redis_unavailable` distinctly from `rate_limit_rejected` so on-call can tell the two apart. A local in-memory fallback limiter would be more resilient, but adds complexity and reintroduces the cross-instance inconsistency the assignment is designed to avoid — fail-closed with clear logging is the more defensible tradeoff at this scope.

### LLM outage (10 minutes straight)

Each call is wrapped in `asyncio.wait_for(timeout=3.0)`, so failures surface fast rather than hanging — either Groq errors out directly, or the call is cancelled at 3s. Either way it's logged (`classification_failed` / `classification_timeout`, with the raw error) and falls back to `"normal"`.

**The event is never dropped** — it still gets published, delivered live if connected, or queued in the pending store if not. The only effect during the outage is misclassification: a genuinely `urgent` alert gets the same priority as routine noise. That's a real degradation, but a deliberate tradeoff — availability and delivery are preserved at the cost of prioritization accuracy. A delayed classification is recoverable; a dropped notification isn't.

### One ambiguity resolved: missing `user_id` on WebSocket connect

`POST /events` gets free validation via Pydantic (missing `user_id` → automatic `422`). The WebSocket endpoint has no such guardrail — the first message is read manually, so an empty/missing `user_id` would silently become a dictionary key, creating a dead "anonymous" channel and potentially overwriting another broken connection.

**Resolution:** explicitly check for `user_id` right after the handshake and close with a custom code (`4001`) and reason if missing — mirroring what Pydantic does for the REST side, and avoiding the silent-overwrite bug.

---

## With More Time

- **Rate limiter resilience** — try/except around Redis calls, fail-closed `503` instead of unhandled `500`s
- **Acknowledged delivery** — `GET /notifications/:user_id/pending` clears on read; a client that crashes before processing loses that data. A proper ack/nack flow (or grace period) would be safer
- **Local fallback rate limiting** during Redis outages, as a middle ground between fail-open and fail-closed
- **Load testing** the WebSocket/Pub-Sub fan-out under real concurrent connections, beyond the manual two-terminal verification done here
- **Request tracing** — a request ID threaded through rate-limit → classify → deliver logs. I hit exactly this gap once mid-build, when a stale reloaded server process produced misleading test results; a request ID would've made that obvious immediately